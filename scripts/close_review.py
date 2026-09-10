# -*- coding: utf-8 -*-
"""收盘复盘（15:10）：全天市场 + 推荐股表现追踪 + 双策略跟踪池收盘体检 + 双虚拟净值
2026-09-03：去真实持仓，改虚拟盘口径（我推荐=我买了）
2026-09-05：双策略分立 —— 右侧趋势 + 短线激进 分池展示，短线3日回测结算
2026-09-09：低位观察池连续追踪 —— 从 watch_history.csv 识别近 N 天持续出现的票，
            检查现价 vs 止损、连续出现天数，发出强提醒并可一键入趋势池。
"""
import json, csv, re
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd
from market_analysis import calc_market_temperature, decide_position, judge_market_env
from fetch_data import get_market_snapshot, get_industry_boards, get_realtime_quotes, get_index_daily
from trend_tracker import analyze_track_pool as trend_analyze, update_track as trend_update, stats as trend_stats
from short_tracker import analyze_track_pool as short_analyze, update_track as short_update, stats as short_stats, roll_backtest
from config import REVIEW_DIR, PUSH_DIR, VIRTUAL_ENABLED

BASE = Path(__file__).resolve().parent.parent


WATCH_FIELDS = ["日期", "代码", "名称", "现价", "60日位置", "信号", "5日涨幅",
                "行业", "买点区间", "止损价", "关注逻辑"]

INDUSTRY_HEAT_FILE = BASE / "data" / "industry_heat.csv"

# 东财个股行业名 → 新浪板块名（模糊匹配表）
# 个股"行业"字段来自东财个股信息，板块热度来自新浪/东财行业板块，名称体系不同
INDUSTRY_MAP = {
    "计算机": ["电子信息", "软件", "IT"],
    "电子": ["电子器件", "电子信息", "半导体"],
    "电力": ["电力行业"],
    "通信": ["通信", "电子信息"],
    "有色金属": ["有色金属"],
    "军工": ["飞机制造", "船舶制造", "航天", "国防"],
    "化工": ["化工行业", "化纤"],
    "石油石化": ["石油行业"],
    "传媒": ["传媒娱乐"],
    "煤炭": ["煤炭行业"],
    "新能源": ["发电设备", "新能源"],
    "券商": ["金融行业"],
    "银行": ["金融行业"],
    "保险": ["金融行业"],
    "汽车": ["汽车制造"],
    "机械": ["机械行业", "仪器仪表"],
    "钢铁": ["钢铁行业"],
    "医药": ["生物制药", "医疗器械"],
    "食品饮料": ["食品行业", "酿酒行业"],
    "家电": ["家电行业", "电器行业"],
    "建筑": ["建筑建材", "水泥行业"],
    "房地产": ["房地产", "开发区"],
    "纺织服装": ["纺织行业", "服装鞋类"],
    "交通运输": ["交通运输", "公路桥梁"],
    "农业": ["农林牧渔"],
    "环保": ["环保行业"],
    "公用事业": ["供水供气", "电力行业"],
}


def _match_board_names(industry_name, all_boards):
    """个股行业名 → 新浪板块名列表（映射表优先，含中文模糊匹配兜底）"""
    if not industry_name or industry_name in ("其他", "其它", "-", ""):
        return []
    # 精确映射
    if industry_name in INDUSTRY_MAP:
        hits = [b for b in all_boards for kw in INDUSTRY_MAP[industry_name] if kw in b]
        if hits:
            return hits
    # 模糊兜底：板块名包含行业名
    hits = [b for b in all_boards if industry_name in b]
    return hits


def _record_industry_heat(industry_df, today):
    """每日板块涨幅落盘 → data/industry_heat.csv
    列：日期, 板块, 涨跌幅（按日期+板块唯一，同日重跑覆盖）
    用途：行业主线持续性判定（连续N日涨幅居前 = 主线）
    """
    import csv as _csv
    if industry_df is None or industry_df.empty:
        return
    path = INDUSTRY_HEAT_FILE
    rows = []
    if path.exists():
        with path.open(encoding="utf-8") as f:
            rows = [r for r in _csv.DictReader(f) if r.get("日期") != today]
    df = industry_df.copy()
    df["涨跌幅"] = pd.to_numeric(df.get("涨跌幅"), errors="coerce")
    for _, r in df.dropna(subset=["涨跌幅"]).iterrows():
        rows.append({"日期": today, "板块": str(r.get("板块名称", "")).strip(),
                     "涨跌幅": f"{float(r['涨跌幅']):.2f}"})
    rows.sort(key=lambda x: (x.get("日期", ""), x.get("板块", "")))
    with path.open("w", newline="", encoding="utf-8") as f:
        wr = _csv.DictWriter(f, fieldnames=["日期", "板块", "涨跌幅"])
        wr.writeheader()
        wr.writerows(rows)
    return path


def _refresh_heat_today(today):
    """刷新最近交易日同花顺90板块热度 → data/industry_heat.csv（增量合并）。

    数据源与 backfill_industry_heat 完全一致（板块指数K线→涨跌幅），保证
    industry_heat.csv 是单一命名体系（THS二级行业），避免新浪/东财板块名混写
    导致排行失真。today 可传实际交易日；若 today 无K线数据（凌晨/盘中跑，
    或传了未来日期），自动落到「最后一个有数据的交易日」（≤today）。
    返回当日 df(板块名称,涨跌幅)；失败或无数据返回 None。
    """
    import csv as _csv
    try:
        import akshare as ak
        from backfill_industry_heat import get_board_daily
    except Exception as e:
        print(f"[板块热度] 导入失败 {e}")
        return None
    try:
        boards = ak.stock_board_industry_name_ths()
        start = (pd.Timestamp(today) - pd.Timedelta(days=10)).strftime("%Y%m%d")
        end = (pd.Timestamp(today) + pd.Timedelta(days=2)).strftime("%Y%m%d")
        got = {}          # day -> {板块: 涨跌幅}
        for _, b in boards.iterrows():
            name = str(b["name"]).strip()
            try:
                daily = get_board_daily(name, b["code"], start=start, end=end)
            except Exception:
                continue
            if not daily:
                continue
            valid = [x for x in daily if x[0] <= today]
            day, pct = valid[-1] if valid else daily[-1]
            got.setdefault(day, {})[name] = pct
        if not got:
            print("[板块热度] 无任何板块K线数据")
            return None
        day = max(got)
        rows = []
        if INDUSTRY_HEAT_FILE.exists():
            with INDUSTRY_HEAT_FILE.open(encoding="utf-8") as f:
                rows = [r for r in _csv.DictReader(f) if r.get("日期") != day]
        for name, pct in got[day].items():
            rows.append({"日期": day, "板块": name, "涨跌幅": f"{pct:.2f}"})
        rows.sort(key=lambda x: (x["日期"], x["板块"]))
        with INDUSTRY_HEAT_FILE.open("w", newline="", encoding="utf-8") as f:
            w = _csv.DictWriter(f, fieldnames=["日期", "板块", "涨跌幅"])
            w.writeheader()
            w.writerows(rows)
        print(f"[板块热度] THS板块 {day} 刷新 {len(got[day])}/90 条")
        return pd.DataFrame([{"板块名称": n, "涨跌幅": p} for n, p in got[day].items()])
    except Exception as e:
        print(f"[板块热度] 刷新失败 {e}")
        return None


# ===== 行业热度工具（自 industry_heat_tool 复用，供复盘主线判定）=====
# 原 _IND_ROMAN/_IND_SYN/_norm_ind/_match_heat_board/_industry_heat_status
# 2026-09-10 抽出为 industry_heat_tool.py 纯函数模块，供 close_review 与
# stock_screener（低位池/趋势池排序加分）共用，避免反向 import 成环。
from industry_heat_tool import (_norm_ind, _match_heat_board,
                                industry_heat_status as _industry_heat_status,
                                load_heat)



def _archive_watch(watch, today):
    """低位启动观察池落盘留痕。

    复盘 md 只有一份（次日重跑即覆盖），且 04_每日复盘/ 被 .gitignore 排除，
    历史观察池无处可查 —— 这里按日期累加进 data/watch_history.csv，同日重跑去重。
    """
    import csv
    path = BASE / "data" / "watch_history.csv"
    rows = []
    if path.exists():
        with path.open(encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r.get("日期") != today]
    for w in watch:
        rows.append({"日期": today, **{k: w.get(k, "") for k in WATCH_FIELDS[1:]}})
    rows.sort(key=lambda r: (r.get("日期", ""), r.get("代码", "")))
    with path.open("w", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=WATCH_FIELDS)
        wr.writeheader()
        wr.writerows(rows)
    return path


def _track_watch_pool(today, lookback_days=10):
    """低位观察池连续追踪。

    从 watch_history.csv 取出近 lookback_days 个交易日内出现过的票，
    今日重新拉取现价，与止损/买点区间比较，输出追踪结果列表。
    每条返回 dict：
        code, name, first_date, days_count, cur_price, stop_loss,
        buy_zone, chg_since_first, status, status_tag, action
    status_tag: "🆕首现" / "📈连续N天" / "⚠️止损接近" / "✅买点区间" / "🔥已大涨"
    action: 推荐操作（买入/观察/忽略）
    """
    path = BASE / "data" / "watch_history.csv"
    if not path.exists():
        return []

    # 读取近 N 天记录
    cutoff = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    all_rows = []
    with path.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("日期", "") >= cutoff:
                all_rows.append(r)

    if not all_rows:
        return []

    # 按代码分组：记录首次出现日期、出现次数、最新一条的信息
    from collections import defaultdict
    by_code = defaultdict(list)
    for r in all_rows:
        by_code[r["代码"]].append(r)

    # 批量拉今日现价
    codes = list(by_code.keys())
    quotes = {}
    try:
        quotes_raw = get_realtime_quotes(codes)
        for c, q in quotes_raw.items():
            quotes[c] = q
    except Exception:
        pass

    results = []
    for code, rows in by_code.items():
        rows.sort(key=lambda r: r["日期"])
        latest = rows[-1]
        name = latest.get("名称", code)

        # 今日现价
        q = quotes.get(code, {})
        cur_price = q.get("现价") or q.get("price")
        if not cur_price:
            try:
                from fetch_data import get_realtime_quotes as _rq
                cur_price = _rq([code]).get(code, {}).get("现价")
            except Exception:
                cur_price = None

        stop_loss_raw = latest.get("止损价", "")
        try:
            stop_loss = float(stop_loss_raw) if stop_loss_raw else None
        except (ValueError, TypeError):
            stop_loss = None

        buy_zone = latest.get("买点区间", "-")
        first_price_raw = rows[0].get("现价", "")
        try:
            first_price = float(first_price_raw)
        except (ValueError, TypeError):
            first_price = None

        first_date = rows[0]["日期"]
        days_count = len(rows)
        try:
            chg_since = (cur_price / first_price - 1) * 100 if cur_price and first_price else None
        except Exception:
            chg_since = None

        # 状态判定
        if days_count == 1:
            status = f"🆕首现（{first_date}），观察中"
            status_tag = "🆕首现"
            action = "观察"
        else:
            tags = []
            acts = []
            # 止损接近判断：现价距止损 ≤2%
            if stop_loss and cur_price and cur_price > 0:
                dist = (cur_price - stop_loss) / cur_price * 100
                if dist <= 0:
                    status = f"⚠️已触及止损 {stop_loss}！立即检查"
                    status_tag = "🚨触及止损"
                    acts.append("止损触发")
                elif dist <= 2:
                    tags.append(f"⚠️距止损{dist:.1f}%")
                    acts.append("减仓/止损")
                else:
                    tags.append(f"距止损{dist:.1f}%")
            # 买点区间判断
            if buy_zone and buy_zone != "-":
                try:
                    lo = float(buy_zone.split("-")[0])
                    hi = float(buy_zone.split("-")[1])
                    if cur_price and lo <= cur_price <= hi:
                        tags.append("✅买点区间")
                        acts.append("可买入")
                except Exception:
                    pass
            # 已大涨（相对首次出现 >15%）
            if chg_since and chg_since > 15:
                tags.append(f"🔥已+{chg_since:.0f}%")
                acts.append("不追高")

            if tags:
                status = f"📈连续{days_count}天关注：{', '.join(tags)}"
                status_tag = f"📈连续{days_count}天"
            else:
                status = f"📈连续{days_count}天关注中"
                status_tag = f"📈连续{days_count}天"

            action = "；".join(acts) if acts else "观察"

        results.append({
            "code": code,
            "name": name,
            "first_date": first_date,
            "days_count": days_count,
            "cur_price": cur_price,
            "stop_loss": stop_loss,
            "buy_zone": buy_zone,
            "chg_since_first": chg_since,
            "status": status,
            "status_tag": status_tag,
            "action": action,
            "industry": latest.get("行业", ""),
        })

    # 按出现天数降序 → 距止损升序（越危险越靠前）
    results.sort(key=lambda r: (-r["days_count"],
                                 r["stop_loss"] / r["cur_price"] if r["stop_loss"] and r["cur_price"] else 999))
    return results


def _nav_line(nav):
    if not nav:
        return ""
    return f"**虚拟净值：{nav['净值']:.0f}（{nav['累计收益率']:+.2f}%）**｜ 已实现 {nav['已实现盈亏']:+.0f}｜ 浮动 {nav['浮动盈亏']:+.0f}｜ 已了结{nav['已了结笔数']}笔｜ 持仓{nav['持仓笔数']}笔"


def run_close():
    today = datetime.now().strftime("%Y-%m-%d")
    now = datetime.now().strftime("%H:%M")
    temp, details = calc_market_temperature()
    pos = decide_position(temp)
    industry, _ = get_industry_boards()

    # 市场环境判定
    idx_df = get_index_daily("sh000001")
    market_env, env_desc, env_reason = judge_market_env(idx_df, temp, None)

    lines = [f"# 收盘复盘 {today}（{now}）\n"]
    env_s = f"【{market_env}（{env_desc}）】" if market_env else ""
    lines.append(f"**{env_s}收盘市场温度：{temp:.0f}/100 → {pos[0]}（{pos[1].split('：')[0]}）**")
    lines.append(f"**环境判定：{env_reason}**\n")
    lines.append("| 维度 | 分值 | 说明 |")
    lines.append("|------|------|------|")
    for k, v in details.items():
        lines.append(f"| {k} | {v['分']:.0f} | {v['说明']} |")
    lines.append("")

    # 板块热度（同花顺90板块，与 industry_heat.csv 同源；领涨/领跌一并用它展示）
    heat_today = None
    try:
        heat_today = _refresh_heat_today(today)
    except Exception as e:
        print(f"[板块热度] 刷新异常 {e}")
    if heat_today is not None and not heat_today.empty:
        ind = heat_today.copy()
        ind["涨跌幅"] = pd.to_numeric(ind.get("涨跌幅"), errors="coerce").fillna(0)
        top = ind.sort_values("涨跌幅", ascending=False).head(5)
        bot = ind.sort_values("涨跌幅").head(3)
        lines.append("**领涨板块**：" + "、".join(top["板块名称"].astype(str).tolist()))
        lines.append("**领跌板块**：" + "、".join(bot["板块名称"].astype(str).tolist()))
        lines.append("")
    elif industry is not None and not industry.empty:
        # THS 失败时用新浪行业兜底展示（不落盘，避免命名混写）
        ind = industry.copy()
        ind["涨跌幅"] = pd.to_numeric(ind.get("涨跌幅"), errors="coerce")
        top = ind.sort_values("涨跌幅", ascending=False).head(5)
        bot = ind.sort_values("涨跌幅").head(3)
        lines.append("**领涨板块**：" + "、".join(top["板块名称"].astype(str).tolist()))
        lines.append("**领跌板块**：" + "、".join(bot["板块名称"].astype(str).tolist()))
        lines.append("")

    # ===== 今日推荐股收盘表现追踪（双策略分池） =====
    picks_path = BASE / "data" / "today_picks.json"
    track = []
    if picks_path.exists():
        picks = json.loads(picks_path.read_text(encoding="utf-8"))
        if picks:
            quotes = get_realtime_quotes([p["symbol"] for p in picks])
            lines.append("## 今日推荐股收盘表现（策略追踪）\n")
            lines.append("| 股票 | 策略 | 收盘价 | 当日% | 买区 | 收盘状态 | 命中? |")
            lines.append("|------|------|--------|-------|------|----------|-------|")
            for p in picks:
                q = quotes.get(str(p["symbol"]))
                if not q:
                    continue
                b = p.get("buy", {})
                rng = b.get("建议买价区间", "-")
                tag = p.get("strategy_tag", "趋势")
                tag_s = "⚡短线" if tag == "短线" else "📈趋势"
                status, hit = "-", "-"
                try:
                    lo, hi = [float(x) for x in rng.split("-")]
                    stop = b.get("止损价")
                    if stop and q["现价"] <= float(stop):
                        status, hit = "跌破止损", "❌"
                    elif lo <= q["现价"] <= hi:
                        status, hit = "收于买区内", "✅ 可介入"
                    elif q["现价"] > hi:
                        status, hit = "超买区上沿", "⚠️ 未给机会"
                    else:
                        status, hit = "低于买区", "⏳ 继续观察"
                except Exception:
                    pass
                lines.append(f"| {p['名称']}({p['symbol']}) | {tag_s} | {q['现价']} | {q['涨跌幅']:+.2f}% | {rng} | {status} | {hit} |")
                track.append({"代码": p["symbol"], "名称": p["名称"], "收盘": q["现价"],
                              "当日%": q["涨跌幅"], "状态": status, "命中": hit,
                              "策略标签": tag})
            lines.append("")

    # ===== 双策略跟踪池收盘体检（先更新状态再分析） =====
    try:
        tn, tmsg = trend_update()
        sn, smsg = short_update()
        print(f"[台账] 趋势{tn}条更新：{tmsg}；短线{sn}条更新：{smsg}")
    except Exception as e:
        print(f"[台账] 更新失败 {e}")
    # 短线3日回测滚动结算
    try:
        bk_n, bk_msg = roll_backtest()
        print(f"[短线回测] 滚动结算 {bk_n} 笔：{bk_msg}")
    except Exception as e:
        print(f"[短线回测] 结算失败 {e}")

    trend_rows, trend_overview = trend_analyze()
    short_rows, short_overview = short_analyze()
    track_rows = trend_rows + short_rows

    lines.append("## 推荐跟踪池收盘体检\n")
    if track_rows:
        # 双净值展示
        trend_nav = trend_overview.get("虚拟净值") or {}
        short_nav = short_overview.get("虚拟净值") or {}
        trend_nav_s = f"趋势净值 {trend_nav.get('净值', 0):.0f}（{trend_nav.get('累计收益率', 0):+.2f}%）" if trend_nav else ""
        short_nav_s = f"短线净值 {short_nav.get('净值', 0):.0f}（{short_nav.get('累计收益率', 0):+.2f}%）" if short_nav else ""
        nav_s = f"｜ **{trend_nav_s}**" + (f"｜ **{short_nav_s}**" if short_nav_s else "")
        total = trend_overview.get("总只数", 0) + short_overview.get("总只数", 0)
        total_pnl = trend_overview.get("总盈亏", 0) + short_overview.get("总盈亏", 0)
        avg_pnl = round(total_pnl / total, 1) if total else 0
        lines.append(f"> 当前跟踪 **{total} 只**，总盈亏 {total_pnl:+.1f}%（平均 {avg_pnl:+.1f}%）{nav_s}｜ 建议：趋势{trend_overview.get('建议','-')}；短线{short_overview.get('建议','-')}\n")

        # 右侧趋势池
        lines.append("### 📈 右侧趋势池\n")
        if trend_rows:
            lines.append("| 名称 | 推荐日 | 收盘价 | 累计% | 最高% | 止损 | 止盈1 | 止盈2 | 收盘操作建议 |")
            lines.append("|------|--------|--------|-------|-------|------|-------|-------|--------------|")
            for r in trend_rows:
                sl = r.get("更新止损") or r.get("止损价") or "-"
                tp1 = r.get("更新止盈1") or r.get("止盈1") or "-"
                tp2 = r.get("更新止盈2") or r.get("止盈2") or "-"
                for x in (sl, tp1, tp2):
                    try:
                        _ = f"{float(x):.2f}"
                    except Exception:
                        pass
                lines.append(f"| {r['名称']}({r['代码']}) | {r['推荐日']} | {r['现价']} | {r['累计盈亏%']:+.1f}% | {r['最高收益%']:+.1f}% | {sl} | {tp1} | {tp2} | {r['建议']} |")
            lines.append("")
        else:
            lines.append("> 趋势跟踪池为空。\n")

        # 短线激进池
        lines.append("### ⚡ 短线激进池\n")
        if short_rows:
            lines.append("| 名称 | 推荐日 | 收盘价 | 累计% | 最高% | 止损 | 止盈1 | 止盈2 | 收盘操作建议 |")
            lines.append("|------|--------|--------|-------|-------|------|-------|-------|--------------|")
            for r in short_rows:
                sl = r.get("更新止损") or r.get("止损价") or "-"
                tp1 = r.get("更新止盈1") or r.get("止盈1") or "-"
                tp2 = r.get("更新止盈2") or r.get("止盈2") or "-"
                for x in (sl, tp1, tp2):
                    try:
                        _ = f"{float(x):.2f}"
                    except Exception:
                        pass
                lines.append(f"| {r['名称']}({r['代码']}) | {r['推荐日']} | {r['现价']} | {r['累计盈亏%']:+.1f}% | {r['最高收益%']:+.1f}% | {sl} | {tp1} | {tp2} | {r['建议']} |")
            lines.append("")
        else:
            lines.append("> 短线跟踪池为空。\n")
    else:
        lines.append("当前无未结清推荐跟踪池。\n")

    # ===== 双策略台账累计表现 + 双虚拟净值 + 短线回测 =====
    try:
        tst = trend_stats()
        sst = short_stats()
        lines.append("## 推荐台账累计表现 + 虚拟净值\n")
        # 趋势策略
        tnav = tst.get("虚拟净值")
        tnav_s = f"\n  - **趋势虚拟净值：{tnav['净值']:.0f}（{tnav['累计收益率']:+.2f}%）**｜ 已实现 {tnav['已实现盈亏']:+.0f}｜ 浮动 {tnav['浮动盈亏']:+.0f}｜ 已了结{tnav['已了结笔数']}笔｜ 持仓{tnav['持仓笔数']}笔" if tnav else ""
        lines.append(f"- **右侧趋势**：累计推荐 {tst['累计推荐']} 只 ｜ 持有中 {tst['持有中']} ｜ 已结清 {tst['已结清']} ｜ **胜率 {tst['胜率']}** ｜ 平均峰值 {tst['平均峰值']}{tnav_s}")
        # 短线策略
        snav = sst.get("虚拟净值")
        snav_s = f"\n  - **短线虚拟净值：{snav['净值']:.0f}（{snav['累计收益率']:+.2f}%）**｜ 已实现 {snav['已实现盈亏']:+.0f}｜ 浮动 {snav['浮动盈亏']:+.0f}｜ 已了结{snav['已了结笔数']}笔｜ 持仓{snav['持仓笔数']}笔" if snav else ""
        bk_s = f" ｜ 回测 {sst.get('回测笔数',0)} 笔 胜率 {sst.get('回测胜率','-')} 均盈亏 {sst.get('回测平均盈亏','-')}" if sst.get('回测笔数', 0) else " ｜ 回测待开闸（等短线情绪开闸日）"
        lines.append(f"- **短线激进**：累计推荐 {sst['累计推荐']} 只 ｜ 持有中 {sst['持有中']} ｜ 已结清 {sst['已结清']} ｜ **胜率 {sst['胜率']}** ｜ 平均峰值 {sst['平均峰值']}{bk_s}{snav_s}")
        lines.append(f"- 台账文件：`data/推荐台账_右侧趋势.csv` + `data/推荐台账_短线激进.csv`")
        lines.append("")
    except Exception as e:
        lines.append(f"- 台账统计异常：{e}\n")
        print(f"[台账] 统计失败 {e}")

    # ===== 本周低位启动观察（趋势侧补充）=====
    # 新增：连续追踪（近10天内出现过的票）
    tracked = []
    watch = []
    try:
        tracked = _track_watch_pool(today, lookback_days=10)
    except Exception as e:
        print(f"[低位追踪] 失败 {e}")

    # 主线行业热度概览（确认行业是否长期主线）
    mainline = []
    try:
        import csv as _csv
        heat_path = INDUSTRY_HEAT_FILE
        if heat_path.exists():
            rows = list(_csv.DictReader(heat_path.open(encoding="utf-8")))
            days = sorted(set(r["日期"] for r in rows))[-5:]
            mainline = []
            for ind_name in sorted(set(r["板块"] for r in rows)):
                tag, rd, td, _ = _industry_heat_status(ind_name, today)
                if tag == "🔥主线":
                    mainline.append((ind_name, rd, td))
            mainline.sort(key=lambda x: -x[1])
    except Exception as e:
        print(f"[主线概览] 失败 {e}")

    try:
        from stock_screener import low_pos_watch
        track_codes = [r["代码"] for r in track_rows] if track_rows else []
        watch = low_pos_watch(top_n=3, exclude_codes=track_codes)
        lines.append("## 本周低位启动观察（趋势侧跟踪池补充）\n")
        if mainline:
            lines.append("**🔥 持续主线行业（近5日板块涨幅居前≥3日）：** " + "、".join(f"{n}（{rd}/{td}日）" for n, rd, td in mainline))
            lines.append("")
        if watch:
            try:
                _archive_watch(watch, today)
            except Exception as e:
                print(f"[低位观察] 归档失败 {e}")
            lines.append("### 今日新筛出（待观察）\n")
            lines.append("| 股票 | 行业热度 | 现价 | 60日位置 | 启动信号 | 5日涨幅 | 买点区间 | 止损 |")
            lines.append("|------|----------|------|----------|----------|---------|----------|------|")
            for w in watch:
                ind_name = w.get("行业", "")
                heat_tag, rank_days, heat_days, last_rank = _industry_heat_status(ind_name, today)
                heat_s = f"{heat_tag}" + (f"{rank_days}/{heat_days}日" if rank_days else "")
                lines.append(f"| {w['名称']}({w['代码']}) | {heat_s} | {w['现价']} | {w['60日位置']:.0%} | {w['信号']} | +{w['5日涨幅']:.1f}% | {w['买点区间']} | {w['止损价']} |")
            lines.append("")
            lines.append("关注逻辑：" + "；".join(f"{w['名称']}: {w['关注逻辑']}" for w in watch))
            lines.append("> 说明：基于收盘数据筛选，供本周跟踪（不追高，回踩买点或放量突破再介入）。")
            lines.append("")
        else:
            lines.append("今日未筛出符合条件的低位启动股（可能整体处于高位或数据缺失），可关注明日更新。")
            lines.append("")

        # 连续追踪段（本次新增）
        if tracked:
            lines.append("### 连续追踪（近10天持续关注）\n")
            lines.append("| # | 股票 | 行业热度 | 首现日 | 天数 | 现价 | 止损 | 状态 | 推荐操作 |")
            lines.append("|---|------|----------|--------|------|------|------|-------|---------|")
            for i, t in enumerate(tracked, 1):
                cur = f"{t['cur_price']:.2f}" if t["cur_price"] else "—"
                sl = f"{t['stop_loss']:.2f}" if t["stop_loss"] else "—"
                chg = f"{t['chg_since_first']:+.1f}%" if t["chg_since_first"] is not None else "—"
                heat_tag, rank_days, heat_days, _ = _industry_heat_status(t.get("industry", ""), today)
                heat_s = f"{heat_tag}" + (f"{rank_days}/{heat_days}日" if rank_days else "")
                # 危险行标红
                danger = t["status_tag"] in ("🚨触及止损",) or (t["stop_loss"] and t["cur_price"] and (t["cur_price"] - t["stop_loss"]) / t["cur_price"] <= 0.02)
                flag = "🚨" if danger else ("⚠️" if t["status_tag"].startswith("📈连续") else "🆕")
                lines.append(f"| {flag}{i} | {t['name']}({t['code']}) | {heat_s} | {t['first_date']} | **{t['days_count']}** | {cur} {chg} | {sl} | {t['status_tag']} | {t['action']} |")
            lines.append("")
            # 人工决策提醒（连续≥3天 或 止损危险）
            urgent = [t for t in tracked if t["days_count"] >= 3 or t["status_tag"] == "🚨触及止损" or (t["stop_loss"] and t["cur_price"] and (t["cur_price"] - t["stop_loss"]) / t["cur_price"] <= 0.02)]
            if urgent:
                lines.append("**⚠️ 需人工决策（连续3天+ 或 止损临近）：**")
                for t in urgent:
                    lines.append(f"- **{t['name']}({t['code']})**：{t['status']}，建议：{t['action']}。买入参考区间 {t['buy_zone']}，止损 {t['stop_loss']}。")
                lines.append("")
        else:
            lines.append("*暂无历史追踪记录（今日首次出现或数据不可得）。*")
            lines.append("")

    except Exception as e:
        lines.append(f"\n## 本周低位启动观察（趋势侧跟踪池补充）\n\n- 筛选异常：{e}\n")
        print(f"[低位观察] 失败 {e}")

    # ===== 明日关注（自动填写）=====
    tomorrow_watch = []
    # 来自今日新筛出的低位池（取前2只作为次日重点）
    if watch:
        tomorrow_watch.extend(watch[:2])
    # 来自连续追踪中买点区间内的票
    if tracked:
        for t in tracked:
            if t["cur_price"] and t["buy_zone"] and t["buy_zone"] != "-":
                try:
                    lo = float(t["buy_zone"].split("-")[0])
                    hi = float(t["buy_zone"].split("-")[1])
                    if lo <= t["cur_price"] <= hi:
                        tomorrow_watch.append({
                            "代码": t["code"], "名称": t["name"],
                            "现价": t["cur_price"], "止损价": t["stop_loss"],
                            "买点区间": t["buy_zone"], "信号": t["status_tag"]
                        })
                except Exception:
                    pass
    lines.append("\n## 明日关注\n")
    if tomorrow_watch:
        # 去重（同名保留第一条）
        seen = set()
        deduped = []
        for w in tomorrow_watch:
            key = w.get("代码") or w.get("code")
            if key not in seen:
                seen.add(key)
                deduped.append(w)
        for w in deduped[:4]:
            name = w.get("名称") or w.get("name", "?")
            code = w.get("代码") or w.get("code", "?")
            price = w.get("现价", "?")
            sl = w.get("止损价", "?")
            bz = w.get("买点区间", "?")
            sig = w.get("信号", "")
            lines.append(f"- **{name}({code})** 现价 {price}｜买点 {bz}｜止损 {sl}｜{sig}")
    else:
        lines.append("- 趋势池空，关注明日新筛出；市场震荡，短线激进等待情绪拐点信号（涨停家数回升）再入场。")
    lines.append("")
    lines.append("\n## 策略反思（每日必填）\n")
    lines.append("- 今日推荐命中率：")
    lines.append("- 右侧趋势/短线激进策略表现对比：")
    lines.append("- 做对的事：")
    lines.append("- 做错的事：")
    lines.append("- 策略优化点：")
    lines.append("\n> 研究参考，不构成投资建议。")

    out = "\n".join(lines)
    path = REVIEW_DIR / f"复盘_{today}.md"
    path.write_text(out, encoding="utf-8")
    # 追踪数据落盘（供策略优化统计）
    if track:
        tpath = BASE / "data" / f"picks_track_{today}.json"
        tpath.write_text(json.dumps(track, ensure_ascii=False, indent=1), encoding="utf-8")
    print(out[:900])
    print(f"\n[收盘复盘已保存] {path}")
    try:
        from notify import push_report
        ok, ch, msg = push_report(f"收盘复盘 {today}", out)
        print(f"[微信推送] {'成功' if ok else '跳过/失败: '+msg}")
    except Exception:
        pass


if __name__ == "__main__":
    run_close()
