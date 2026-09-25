# -*- coding: utf-8 -*-
"""收盘复盘（15:10）：全天市场 + 推荐股表现追踪 + 双策略跟踪池收盘体检 + 双虚拟净值
2026-09-03：去真实持仓，改虚拟盘口径（我推荐=我买了）
2026-09-05：双策略分立 —— 右侧趋势 + 短线激进 分池展示，短线3日回测结算
2026-09-09：低位观察池连续追踪 —— 从 watch_history.csv 识别近 N 天持续出现的票，
            检查现价 vs 止损、连续出现天数，发出强提醒并可一键入趋势池。
"""
import json, csv, re, os
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd
from market_analysis import calc_market_temperature, safe_market_temperature, decide_position, judge_market_env
from fetch_data import get_market_snapshot, get_industry_boards, get_realtime_quotes, get_index_daily
from trend_tracker import analyze_track_pool as trend_analyze, update_track as trend_update, stats as trend_stats
from short_tracker import analyze_track_pool as short_analyze, update_track as short_update, stats as short_stats, roll_backtest
import config
from config import REVIEW_DIR, PUSH_DIR, VIRTUAL_ENABLED

BASE = Path(__file__).resolve().parent.parent


WATCH_FIELDS = ["日期", "代码", "名称", "现价", "60日位置", "量比", "5日涨幅",
                "行业", "买点区间", "止损价", "蓄势路径", "状态"]

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
                                stock_heat,
                                load_heat)



# 低位启动观察池连续追踪的启动信号参数
# 2026-09-16 触发式改造：观察池不再"埋伏等启动"，而是盯放量突破触发线，
#   只有触发才提示介入（未触发不买入）；T+5 个交易日内未触发 → 时间止损移出。
WATCH_TRIGGER_LB = 1.5       # 启动信号：量比 ≥1.5（2026-09-23 2.0→1.5，与 config 一致）
WATCH_TRIGGER_CHG_MIN = 3.0  # 启动信号：当日涨幅 ≥3%（温和放量启动确认）
WATCH_TIME_STOP_DAYS = 5     # 时间止损：入池 T+5 个交易日内未触发 → 移出观察


def _archive_watch(watch, today):
    """低位启动观察池落盘留痕。

    复盘 md 只有一份（次日重跑即覆盖），且 04_每日复盘/ 被 .gitignore 排除，
    历史观察池无处可查 —— 这里按日期累加进 data/watch_history.csv，同日重跑去重。
    """
    import csv, shutil
    path = BASE / "data" / "watch_history.csv"

    # 当日待写入的记录（去重：同日期+同代码只留一条）
    new_rows = []
    seen = set()
    for w in watch:
        rec = {"日期": today, **{k: w.get(k, "") for k in WATCH_FIELDS[1:]}}
        rec["状态"] = rec.get("状态") or "观察中"   # 新入库默认观察中
        key = (today, rec.get("代码", ""))
        if key not in seen:
            seen.add(key)
            new_rows.append(rec)
    if not new_rows:
        return path

    # 2026-09-23 加固：历史常态 3-5 只/日；单日≥10 只大概率是桩数据/异常批量灌入
    # （09-21 深夜版面重构曾无声灌入 22 行假数据污染 watch_history+笔记本+复盘）。
    # 观察池入池条件本就宽松（低量比也可入池），普涨日数量偏多属正常，故只告警不阻断。
    if len(new_rows) >= 10:
        print(f"⚠️ [低位观察池] 今日入池 {len(new_rows)} 只（历史常态 3-5 只/日），"
              f"请人工核实数据来源是否为真实筛选（历史备份: data/watch_history_backup/）")

    exists = path.exists()

    # append-only：只读“是否已有今日记录”做幂等判断，绝不读/写历史行
    has_today = False
    if exists:
        with path.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("日期") == today:
                    has_today = True
                    break

    # 防丢保护：追加前备份（异常时可追溯）
    try:
        bak_dir = BASE / "data" / "watch_history_backup"
        bak_dir.mkdir(exist_ok=True)
        shutil.copy2(path, bak_dir / f"watch_history_{today}.csv") if exists else None
        baks = sorted(bak_dir.glob("watch_history_*.csv"))
        for old in baks[:-20]:
            old.unlink(missing_ok=True)
    except Exception:
        pass

    # 同日已写过则跳过（幂等），否则以追加模式落盘，历史行零接触
    if has_today:
        return path

    with path.open("a", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=WATCH_FIELDS)
        if not exists or path.stat().st_size == 0:
            wr.writeheader()
        wr.writerows(new_rows)
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

    _expired_codes = []   # 2026-09-19：本次判定的 T+5 超时票，函数末尾统一写"已失效"

    # 读取近 N 天记录（自然日窗口，含周末/节假日）
    cutoff = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    all_rows = []
    with path.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("日期", "") >= cutoff:
                all_rows.append(r)

    if not all_rows:
        return []

    # ===== 交易日历：上证指数K线日期（真实交易日，含9-10/9-11等未归档日）=====
    # 2026-09-11 修复：此前从 watch_history 收集日期推导交易日，但 csv 只记录"入选日"、
    #   9-10/9-11 未归档 → 天数被低估（中国电影9-11报3天、实际跨6交易日）。
    # 上证指数每个交易日必有K线 → 是最可靠的交易日序列。
    today_s = today.strftime("%Y-%m-%d") if hasattr(today, "strftime") else str(today)
    _trade_days = []
    try:
        from fetch_data import get_stock_hist as _gh
        _idx = _gh("000001", days=max(40, lookback_days * 2))
        _trade_days = [str(d)[:10] for d in _idx["日期"].dt.strftime("%Y-%m-%d").tolist()]
    except Exception:
        _trade_days = []
    if not _trade_days:
        # 兜底：watch_history 去重日期 + 今日（缺失中间交易日仍会低估，但至少含首日/今日）
        _trade_days = set()
        with path.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                _trade_days.add(r.get("日期", ""))
        _trade_days.add(today_s)
        _trade_days = sorted(d for d in _trade_days if d >= cutoff)

    # 按代码分组：记录首次出现日期、出现次数、最新一条的信息
    from collections import defaultdict
    by_code = defaultdict(list)
    for r in all_rows:
        by_code[r["代码"]].append(r)

    # 2026-09-12 修复：回溯体检标记"已失效"的存量票不再进入跟踪。
    # 判定口径：该 code 最新一条记录状态为"已失效" → 整组剔除（历史观察中行保留审计）。
    invalid_codes = set()
    for code, rs in by_code.items():
        rs_sorted = sorted(rs, key=lambda r: r["日期"])
        if rs_sorted[-1].get("状态", "") == "已失效":
            invalid_codes.add(code)
    for code in invalid_codes:
        del by_code[code]

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
        # 天数 = 首次入选日到"今日"的交易日跨度（含首日与今日）
        # 修复 2026-09-11：此前 days_count=len(rows) 只在入选日+1，
        #   未入选日（如9-09断档）不累计 → 中国电影9-10报4天(实际跨5交易日)、
        #   9-11报3天(实际跨6交易日) 倒挂。改用交易日跨度：
        #   days = 交易日序列中 first_date..today 的个数（若今日无记录，
        #   以 last_hist_date 兜底，保证≥入选次数）。
        last_hist_date = rows[-1]["日期"]
        _d = [d for d in _trade_days if first_date <= d <= today_s]
        days_count = len(_d)
        if days_count < len(rows):   # 兜底：交易日序列不完整时至少等于入选次数
            days_count = len(rows)
        days_count = max(1, days_count)
        try:
            chg_since = (cur_price / first_price - 1) * 100 if cur_price and first_price else None
        except Exception:
            chg_since = None

        # 状态判定
        # 2026-09-11 修复"首现"语义：此前只看 days_count==1 → 光电股份9-02已入选、
        #   今天涨停再入选仍标🆕首现(1天)，自相矛盾。正确口径：
        #   - "首现" = 今天首次进入观察池（rows[-1]是今天 且 今天之前无记录）
        #   - 连续N天 = 从首次入选日到今天一直在跟踪（无论中间是否每天入选）
        #   注意：历史入选过但近10日没再出现（today 无记录）→ 不算"今天新筛出"，
        #   但仍在追踪池内 → 显示"连续N天"（N=距今天跨度），不显示"首现"。
        #
        # 2026-09-16 触发式改造：观察池不再"埋伏等启动"，改为启动信号触发判定。
        #   触发线 = 首现日收盘价 × (1+3%)，量比 ≥ 2.0 且当日涨幅 ≥ 3% 才提示介入；
        #   未触发只挂观察，绝不提示买入；入池 T+5 交易日未触发 → 自动移出（时间止损）。
        #   止损锚定已改为买区上沿×(1-8%)（low_pos_entry），区间内买入至少留 8% 洗盘空间。
        #   阈值引用 config 触发参数族（观察池用"温和"档：涨幅3%而非选股侧10%，
        #   因为观察池是候选池，只需确认"开始放量启动"即可提示，正式触发以选股侧为准）。
        _tg_line_pct = getattr(config, "LOW_POS_ENTRY_WATCH_TRIGGER_PCT", 0.03)
        _tg_lb = getattr(config, "LOW_POS_ENTRY_WATCH_TRIGGER_LB", 1.5)
        _tg_chg = getattr(config, "LOW_POS_ENTRY_WATCH_TRIGGER_CHG", 3.0)
        _tg_timeout = getattr(config, "LOW_POS_ENTRY_WATCH_TIMEOUT_DAYS", 5)
        try:
            trigger_line = round(first_price * (1 + _tg_line_pct), 2) if first_price else None
        except Exception:
            trigger_line = None
        if last_hist_date == today_s and len(rows) == 1:
            status = f"🆕首现（{first_date}），观察中"
            status_tag = "🆕首现"
            action = "观察"
        else:
            tags = []
            acts = []
            # 启动信号触发判定（优先于止损/时间止损）：现价≥触发线 且 量比≥_tg_lb 且 涨幅≥_tg_chg
            # 2026-09-16 修复：涨停（涨幅≥9.5%）直判启动，不依赖量比——涨停本身就是最强启动确认，
            #   且涨停日量比常被封单扭曲（一字/缩量板量比反而低）。此前时间止损分支抢跑，
            #   导致露笑今日涨停仍被标"⏰移出"，误伤已启动票。
            trigger_hit = False
            try:
                lb = float(q.get("量比") or 0)
                pct = float(q.get("涨幅") or q.get("涨跌幅") or 0)
                if cur_price and pct >= 9.5:
                    trigger_hit = True
                elif trigger_line and cur_price and cur_price >= trigger_line and lb >= _tg_lb and pct >= _tg_chg:
                    trigger_hit = True
            except Exception:
                pass
            if trigger_hit:
                status = f"🚀启动信号触发（{trigger_line}，量比{lb:.1f}x/涨{pct:.1f}%）"
                status_tag = "🚀触发启动"
                acts.append("放量突破可介入")
                action = "放量突破可介入"
            else:
                # 止损接近判断：现价距止损 ≤2%（优先于时间止损——破位止损比超时更紧急）
                _hit_stop = False
                if stop_loss and cur_price and cur_price > 0:
                    dist = (cur_price - stop_loss) / cur_price * 100
                    if dist <= 0:
                        status = f"⚠️已触及止损 {stop_loss}！立即检查"
                        status_tag = "🚨触及止损"
                        acts.append("止损触发")
                        _hit_stop = True
                    elif dist <= 2:
                        tags.append(f"⚠️距止损{dist:.1f}%")
                        acts.append("减仓/止损")
                    else:
                        tags.append(f"距止损{dist:.1f}%")
                # 时间止损：入池 T+_tg_timeout 交易日未触发启动信号 → 移出观察池
                #   （仅未触发启动 且 未触及止损 时判定；已破位票走止损路径，不再重复标移出）
                if not _hit_stop and days_count > _tg_timeout:
                    status = f"⏰T+{days_count}未启动，移出观察池"
                    status_tag = "⏰移出"
                    acts.append("⏰时间止损移出")
                    _expired_codes.append(code)   # 2026-09-19 修复：超时票落盘"已失效"，避免次日重复追踪
                else:
                    # 买点区间判断（仅提示回踩机会，不再单独"可买入"）
                    if buy_zone and buy_zone != "-":
                        try:
                            lo = float(buy_zone.split("-")[0])
                            hi = float(buy_zone.split("-")[1])
                            if cur_price and lo <= cur_price <= hi:
                                tags.append("✅买点区间")
                        except Exception:
                            pass
                    # 已大涨（相对首次出现 >15%）
                    if chg_since and chg_since > 15:
                        tags.append(f"🔥已+{chg_since:.0f}%")
                        acts.append("不追高")

                    if _hit_stop:
                        pass  # 状态已在止损分支赋值
                    elif tags:
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

    # 2026-09-19 修复：T+5 超时票落盘"已失效"，下次自动剔除（此前只展示"⏰移出"标签，
    # 从不下沉到 watch_history.csv → 超时票每日被重复追踪，永留池中）。
    if _expired_codes:
        try:
            import shutil as _sh
            bak_dir = BASE / "data" / "watch_history_backup"
            os.makedirs(bak_dir, exist_ok=True)
            _sh.copy2(path, bak_dir / f"watch_history_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
            _rd = []
            with path.open(encoding="utf-8") as _f:
                _rd = list(csv.DictReader(_f))
            fields = list(_rd[0].keys()) if _rd else WATCH_FIELDS
            _exp = set(_expired_codes)
            for _r in _rd:
                if _r.get("代码") in _exp:
                    _r["状态"] = "已失效"
            with path.open("w", newline="", encoding="utf-8") as _f:
                _w = csv.DictWriter(_f, fieldnames=fields)
                _w.writeheader()
                _w.writerows(_rd)
            print(f"[低位追踪] 已标记 {len(_exp)} 只 T+5 超时票为已失效")
        except Exception as e:
            print(f"[低位追踪] 标记失效失败 {e}")

    return results


def _nav_line(nav):
    if not nav:
        return ""
    return f"**虚拟净值：{nav['净值']:.0f}（{nav['累计收益率']:+.2f}%）**｜ 已实现 {nav['已实现盈亏']:+.0f}｜ 浮动 {nav['浮动盈亏']:+.0f}｜ 已了结{nav['已了结笔数']}笔｜ 持仓{nav['持仓笔数']}笔"


def run_close():
    today = datetime.now().strftime("%Y-%m-%d")
    now = datetime.now().strftime("%H:%M")
    # ===== 2026-09-16 三报合一：收盘复盘前先以 --no-push 模式触发选股登记 =====
    # 晨报/午间复盘取消推送后，选股逻辑（趋势/短线/低位观察池）统一下沉到收盘复盘：
    #   1) 先跑 daily_report --no-push → 更新 today_picks.json（今日推荐股）、
    #      登记趋势/短线台账、扫描低位观察池（写 watch_history）；
    #   2) 再生成收盘复盘 → 自然包含"今日推荐表现追踪 + 低位观察池 + 明日关注"，
    #      晨报的市场温度/板块/风险日历等有价值内容已由复盘承载，微信每日只推 1 次。
    # 失败不影响复盘主流程（选股异常仅告警，复盘仍按已有 today_picks 生成）。
    try:
        import subprocess, sys as _sys
        _sel_log = BASE / "data" / "selection_trigger.log"
        _ret = subprocess.run(
            [_sys.executable, "daily_report.py", "--no-push"],
            cwd=str(BASE / "scripts"), timeout=1800,
            capture_output=True, text=True)
        _sel_log.write_text(_ret.stdout[-3000:] + _ret.stderr[-1000:], encoding="utf-8")
        if _ret.returncode == 0:
            print("[选股触发] daily_report --no-push 完成（台账/today_picks 已更新）")
        else:
            print(f"[选股触发] ⚠️ daily_report 退出码 {_ret.returncode}，复盘按存量数据生成（详见 data/selection_trigger.log）")
    except Exception as e:
        print(f"[选股触发] ⚠️ 异常（{e}），复盘按存量数据生成")

    # 2026-09-10 修复：温度计任一步失败不得阻断收盘复盘整体流程（曾因全A快照失败
    # 在第一步崩溃 → 复盘文件不生成 + 微信推送静默丢失）。失败时用中性温度兜底，
    # 复盘正文标注"数据降级"，推送照常必达。
    temp, details, _temp_ok = safe_market_temperature()
    pos = decide_position(temp)
    industry, _ = get_industry_boards()

    # 市场环境判定
    idx_df = get_index_daily("sh000001")
    market_env, env_desc, env_reason = None, None, ""
    try:
        market_env, env_desc, env_reason = judge_market_env(idx_df, temp, None)
    except Exception as e:
        print(f"[环境判定] 失败降级: {e}")
        env_reason = f"环境数据降级({str(e)[:40]})"

    lines = [f"# 收盘复盘 {today}（{now}）\n"]
    env_s = f"【{market_env}（{env_desc}）】" if market_env else ""
    warm = []  # 2026-09-21：初动池结果提前初始化，供末尾策略笔记本复用（扫描异常时不至于 NameError）
    lines.append(f"**{env_s}收盘市场温度：{temp:.0f}/100 → {pos[0]}（{pos[1].split('：')[0]}）**")
    lines.append(f"**环境判定：{env_reason}**\n")
    if not _temp_ok:
        lines.append("> ⚠️ 本日温度计数据源异常，以上为中性兜底值；若为盘中请稍后重跑。\n")
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
    # 2026-09-21 版面重构：本节降级为「推荐跟踪池」首个子段「今日新推荐」
    lines.append("## 推荐跟踪池收盘体检\n")
    picks_path = BASE / "data" / "today_picks.json"
    track = []
    if picks_path.exists():
        picks = json.loads(picks_path.read_text(encoding="utf-8"))
        if picks:
            quotes = get_realtime_quotes([p["symbol"] for p in picks])
            # 2026-09-21 版面重构：原独立「## 今日推荐股收盘表现」降级为
            #   「推荐跟踪池」下的首个子段「今日新推荐」，贴合用户版面结构（3 推荐跟踪池）
            lines.append("### 🆕 今日新推荐（收盘表现）\n")
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

    # 双净值展示（仅渲染非空池，避免空池出现 "****" 占位）
    trend_nav = trend_overview.get("虚拟净值") or {}
    short_nav = short_overview.get("虚拟净值") or {}
    nav_parts = []
    if trend_nav:
        nav_parts.append(f"**趋势净值 {trend_nav.get('净值', 0):.0f}（{trend_nav.get('累计收益率', 0):+.2f}%）**")
    if short_nav:
        nav_parts.append(f"**短线净值 {short_nav.get('净值', 0):.0f}（{short_nav.get('累计收益率', 0):+.2f}%）**")
    nav_s = ("｜ " + "｜ ".join(nav_parts)) if nav_parts else ""

    def _tag_advice(prefix, msg):
        # 追踪器消息已自带策略前缀（如"趋势跟踪池为空"），避免重复拼接"趋势趋势"
        msg = msg or "-"
        return msg if msg.startswith(prefix) else f"{prefix}{msg}"

    trend_advice = _tag_advice("趋势", trend_overview.get("建议", "-"))
    short_advice = _tag_advice("短线", short_overview.get("建议", "-"))
    total = trend_overview.get("总只数", 0) + short_overview.get("总只数", 0)
    total_pnl = trend_overview.get("总盈亏", 0) + short_overview.get("总盈亏", 0)
    avg_pnl = round(total_pnl / total, 1) if total else 0
    lines.append(f"> 当前跟踪 **{total} 只**，总盈亏 {total_pnl:+.1f}%（平均 {avg_pnl:+.1f}%）{nav_s}｜ 建议：{trend_advice}；{short_advice}\n")

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
            lines.append(f"| {r['名称']} | {r['推荐日']} | {r['现价']} | {r['累计盈亏%']:+.1f}% | {r['最高收益%']:+.1f}% | {sl} | {tp1} | {tp2} | {r['建议']} |")
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
            lines.append(f"| {r['名称']} | {r['推荐日']} | {r['现价']} | {r['累计盈亏%']:+.1f}% | {r['最高收益%']:+.1f}% | {sl} | {tp1} | {tp2} | {r['建议']} |")
        lines.append("")
    else:
        lines.append("> 短线跟踪池为空。\n")

    # 第3正式策略：温和放量初动池（2026-09-20 升级为正式策略，V2 破MA5离场已固化）
    # 2026-09-21 版面重构：初动池从「低位观察段」移至「推荐跟踪池」第3子池（用户版面要求），
    #   且独立于趋势/短线（两者为空时初动池仍须渲染）
    # K线更新由独立步骤负责（warm_start_entry.py --update），此处只读缓存选股避免重复拉取
    lines.append("### 🔥 温和放量初动池（第3策略）\n")
    try:
        from warm_start_entry import pick_warm_start as _pick_warm
        warm = _pick_warm(top=600, update=False, quiet=True)
        if warm:
            lines.append("**▎今日新信号**（信号日收盘确认，T+1 开盘介入，无固定止盈最长 T+5）\n")
            # 2026-09-23 晚版面调整（用户拍板）：删「前10振幅」「量能比」列（蓄势证据列，筛选口径不变见脚注），加「行业热度」列
            lines.append("| 股票 | 行业热度 | 现价 | 量比 | 5日涨幅 | 60日位置 | 成交额 | 硬止损 |")
            lines.append("|------|----------|------|------|---------|----------|--------|--------|")
            try:
                from industry_heat_tool import prefetch_industries as _pf
                _pf([w["代码"] for w in warm[:8]])
            except Exception:
                pass
            for w in warm[:8]:
                try:
                    heat_s, _ = stock_heat(w["代码"], today)
                except Exception:
                    heat_s = "—"
                lines.append(f"| {w['名称']} | {heat_s} | {w['现价']:.2f} | {w['量比']:.2f}x | {w['5日涨幅%']:+.1f}% | {w['60日位置']:.0%} | {w['成交额亿']:.1f}亿 | {w['止损价']:.2f} |")
            lines.append("")
        else:
            lines.append("*今日无满足最优档画像的初动票（缩量调整日量比普遍<1.3，属正常），可关注明日更新。*\n")
    except Exception as e:
        print(f"[初动池] 失败 {e}")
        lines.append(f"- 筛选异常：{e}\n")
    # 持有中跟踪（跨日，09-22 新增）：修复「持有中票不在版面」缺口；天数=T+n（距信号日交易日数）
    try:
        from strategy_notebook import warm_tracking_rows
        trows = warm_tracking_rows(today)
        if trows:
            lines.append("**▎持有中跟踪**（-8%硬止损 · 破MA5次日离场 · 最长T+5）\n")
            # 2026-09-23 晚版面调整：加「最高浮盈」（T+1以来最高价相对信号日收盘，让利润奔跑的回吐可视化）与「硬止损」列
            lines.append("| 股票 | 行业热度 | 信号日 | 天数 | 现价 | 距信号 | 最高浮盈 | 破<br/>MA5 | 硬止损 | 建议 |")
            lines.append("|------|----------|--------|------|------|--------|----------|-------|--------|------|")
            for r in trows:
                last_s = f"{r['现价']:.2f}" if r["现价"] is not None else "—"
                dist_s = f"{r['距信号%']:+.1f}%" if r["距信号%"] is not None else "—"
                peak_s = f"{r['最高浮盈%']:+.1f}%" if r.get("最高浮盈%") is not None else "—"
                days_s = f"T+{r['天数']}" if r["天数"] is not None else "—"
                below_s = ("是" if r["破MA5"] else "否") if r["破MA5"] is not None else "—"
                try:
                    stop_s = f"{float(r['止损']):.2f}"
                except Exception:
                    stop_s = str(r.get("止损") or "—")
                try:
                    heat_s, _ = stock_heat(r["代码"], today)
                except Exception:
                    heat_s = "—"
                lines.append(f"| {r['名称']} | {heat_s} | {r['信号日'][5:]} | {days_s} | {last_s} | {dist_s} | {peak_s} | {below_s} | {stop_s} | {r['建议']} |")
            lines.append("")
    except Exception as e:
        print(f"[初动跟踪] 失败 {e}")
    # 2026-09-23 晚版面调整（用户拍板）：交易规则+回测口径脚注移出推送（操作信息已由表格列覆盖，口径入台账）
    lines.append("")

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
        from low_pos_entry import pick_low_pos_entry, filter_active_pool
        track_codes = [r["代码"] for r in track_rows] if track_rows else []
        # 策略与日报同步：pick_low_pos_entry 双路径候选
        # 2026-09-21 修低位池膨胀：filter_active_pool 剔除已在池（观察中）老票，
        #   此前只排除短线/趋势跟踪池（track_rows），宁德/兆易/东财/烽火等
        #   在册票每日重刷「今日候选」并入池记录，池子持续膨胀。
        candidates = filter_active_pool(pick_low_pos_entry(top=600, quiet=True))
        # 排除已在跟踪池的票
        # 2026-09-18 改：去掉 [:3] 截断，与晨报显示全量保持一致（池子一致性）
        watch = [w for w in candidates if w.get("代码") not in track_codes]
        lines.append("## 低位启动观察\n")
        if mainline:
            lines.append("**🔥 持续主线行业（近5日板块涨幅居前≥3日）：** " + "、".join(f"{n}（{rd}/{td}日）" for n, rd, td in mainline))
            lines.append("")
        if watch:
            try:
                _archive_watch(watch, today)
            except Exception as e:
                print(f"[低位观察] 归档失败 {e}")
            lines.append("### 今日低位埋伏候选（策略同步日报）\n")
            # 行业去重展示（同 pick_low_pos_entry 内置去重逻辑）
            shown = []
            seen_ind = set()
            for w in watch:
                ind = w.get("行业", "未知")
                if ind in seen_ind:
                    continue
                seen_ind.add(ind)
                shown.append(w)
            if shown:
                lines.append("| 股票 | 行业 | 现价 | 60日位置 | 量比 | 5日涨幅 | 振幅 | 买点区间 | 止损 | 蓄势路径 |")
                lines.append("|------|------|------|----------|------|---------|------|----------|------|----------|")
                for w in shown:
                    lines.append(f"| {w['名称']} | {w.get('行业','未知')} | {w['现价']:.2f} | {w['60日位置']:.0%} | {w['量比']:.2f}x | {w['5日涨幅%']:+.1f}% | {w['20日振幅%']:.0f}% | {w['买点区间']} | {w['止损价']:.2f} | {w.get('蓄势路径','标准蓄势')} |")
                lines.append("")
            lines.append("> ⚠️ 研究观察池，**非买入推荐**，仅供盘后研究参考。")
            lines.append("")
        else:
            lines.append("今日未筛出符合条件的低位启动股（可能整体处于高位或数据缺失），可关注明日更新。")
            lines.append("")

        # ===== 明日关注（自动填写）=====
        # 2026-09-23 用户定版：移到「连续追踪」之前（今日候选 → 明日关注 → 连续追踪）
        tomorrow_watch = []
        # 来自今日新筛出的低位池（取前2只作为次日重点）
        if watch:
            for w in watch[:2]:
                tomorrow_watch.append({
                    "代码": w.get("代码"), "名称": w.get("名称"),
                    "现价": w.get("现价"), "止损价": w.get("止损价"),
                    "买点区间": w.get("买点区间"), "信号": "今日新筛"
                })
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
        lines.append("\n### ▎明日关注\n")
        if tomorrow_watch:
            # 去重（同名保留第一条）
            seen = set()
            deduped = []
            for w in tomorrow_watch:
                key = w.get("代码") or w.get("code")
                if key not in seen:
                    seen.add(key)
                    deduped.append(w)

            def _fmt(v):
                if v is None or v == "" or v == "?":
                    return "—"
                try:
                    return f"{float(v):.2f}"
                except (ValueError, TypeError):
                    return str(v)

            lines.append("| 股票 | 现价 | 买点区间 | 止损 | 信号 |")
            lines.append("|------|------|----------|------|------|")
            for w in deduped[:4]:
                name = w.get("名称") or w.get("name", "?")
                code = w.get("代码") or w.get("code", "?")
                lines.append(f"| {name}({code}) | {_fmt(w.get('现价'))} | {w.get('买点区间') or '—'} | {_fmt(w.get('止损价'))} | {w.get('信号') or '—'} |")
            lines.append("")
        else:
            lines.append("- 今日无重点盯防对象；新筛候选见上方表格，等待触发信号再关注。")
        lines.append("")

        # 连续追踪段（2026-09-16 改：只显示「非当日新入池」的在池票，
        #   当日新入池只在「今日低位埋伏候选」表出现一次，消除两表重复）
        # 2026-09-23 版面调整：连续追踪移至「明日关注」之后
        tracked_old = [t for t in tracked if t["first_date"] != today]
        if tracked_old:
            lines.append("### ▎连续追踪（近10天持续关注，含存量在池票）\n")
            lines.append("| # | 股票 | 行业热度 | 首现日 | 天数 | 现价 | 买点区间 | 止损 | 状态 |")
            lines.append("|---|------|----------|--------|------|------|----------|------|-------|")
            for i, t in enumerate(tracked_old, 1):
                cur = f"{t['cur_price']:.2f}" if t["cur_price"] else "—"
                sl = f"{t['stop_loss']:.2f}" if t["stop_loss"] else "—"
                chg = f"{t['chg_since_first']:+.1f}%" if t["chg_since_first"] is not None else "—"
                bz = t.get("buy_zone") or "—"
                heat_tag, rank_days, heat_days, _ = _industry_heat_status(t.get("industry", ""), today)
                heat_s = f"{heat_tag}" + (f"{rank_days}/{heat_days}日" if rank_days else "")
                # 2026-09-18 改：状态与操作合并为一列（去掉冗余重复），股票列只显示名称不含代码
                act = t.get("action") or ""
                if act and act != "观察":
                    status_merged = f"{t['status_tag']}·{act}"
                else:
                    status_merged = t["status_tag"]
                # 危险行标红
                danger = t["status_tag"] in ("🚨触及止损",) or (t["stop_loss"] and t["cur_price"] and (t["cur_price"] - t["stop_loss"]) / t["cur_price"] <= 0.02)
                flag = "🚨" if danger else ("⚠️" if t["status_tag"].startswith("📈连续") else "🆕")
                lines.append(f"| {flag}{i} | {t['name']} | {heat_s} | {t['first_date']} | **{t['days_count']}** | {cur} {chg} | {bz} | {sl} | {status_merged} |")
            lines.append("")
            # 需人工决策：不再逐条复述上表，仅给出数量提示，避免与连续追踪表格重复
            urgent = [t for t in tracked_old if t["days_count"] >= 3 or t["status_tag"] == "🚨触及止损" or (t["stop_loss"] and t["cur_price"] and (t["cur_price"] - t["stop_loss"]) / t["cur_price"] <= 0.02)]
            if urgent:
                lines.append(f"> ⚠️ 其中 **{len(urgent)} 只**已连续≥3天或临近止损，见上表 🚨/⚠️ 标红行，需人工优先决策。")
                lines.append("")
        else:
            lines.append("*暂无历史追踪记录（今日首次出现或数据不可得）。*")
            lines.append("")

    except Exception as e:
        lines.append(f"\n## 低位启动观察\n\n- 筛选异常：{e}\n")
        print(f"[低位观察] 失败 {e}")

    # ===== 双策略台账累计表现 + 双虚拟净值 + 短线回测（置于推送末尾）=====
    # （2026-09-23 明日关注块已移入上方低位启动观察段，位于连续追踪之前）
    try:
        tst = trend_stats()
        sst = short_stats()

        def _nav_cell(st, nav, is_short):
            if nav:
                net = f"{nav['净值']:.0f}"
                ret = f"{nav['累计收益率']:+.2f}%"
                realized = f"{nav['已实现盈亏']:+.0f}"
                floating = f"{nav['浮动盈亏']:+.0f}"
                closed_holding = f"{nav['已了结笔数']}/{nav['持仓笔数']}"
            else:
                net = ret = realized = floating = closed_holding = "-"
            if is_short:
                if st.get('回测笔数', 0):
                    bk = f"{st.get('回测笔数')}笔 胜率{st.get('回测胜率','-')} 均{st.get('回测平均盈亏','-')}"
                else:
                    bk = "待开闸"
            else:
                bk = "-"
            return (st["策略"], st["累计推荐"],
                    st["胜率"], st["平均峰值"], net, ret, realized, floating,
                    closed_holding, bk)

        lines.append("## 推荐台账累计表现 + 虚拟净值\n")
        lines.append("| 策略 | 累计<br/>推荐 | 胜率 | 平均峰值 | 净值 | 累计<br/>收益率 | 已实现 | 浮动 | 已了结/<br/>持仓 | 回测 |")
        lines.append("|------|---------|------|---------|------|-----------|--------|------|------------|------|")
        lines.append("| " + " | ".join(str(x) for x in _nav_cell(tst, tst.get("虚拟净值"), False)) + " |")
        lines.append("| " + " | ".join(str(x) for x in _nav_cell(sst, sst.get("虚拟净值"), True)) + " |")
        lines.append("")
        lines.append("> 台账文件：`data/推荐台账_右侧趋势.csv` + `data/推荐台账_短线激进.csv`\n")
    except Exception as e:
        lines.append(f"- 台账统计异常：{e}\n")
        print(f"[台账] 统计失败 {e}")

    # ===== 低位启动观察池累计表现（2026-09-16 新增，2026-09-24 移除）=====
    # 展示精简：统计类表格（累计入池/观察中/已移出）与入池口径注释不再进推送，
    # 口径信息完整保留在台账与 watch_pool_stats.py 周复盘脚本中。
    lines.append("\n## 策略体检（自动生成 + 每日复盘）\n")
    # 2026-09-16 第3项落地：把空的"策略反思"升级为自动生成的三池体检 + 改进建议。
    # 输入：今日温度/市场环境、三池触发事件（tracked 低位观察、track_rows 双池）、
    #       台账累计胜率。输出：逐策略体检结论 + 明确的改进建议，供周复盘汇总。
    _sug = []
    _tag_t = trend_overview.get("总只数", 0)
    _tag_s = short_overview.get("总只数", 0)
    _win_t = trend_overview.get("胜率", "-")
    _win_s = short_overview.get("胜率", "-")
    # ① 温度/环境体检
    if temp >= 65:
        _sug.append(f"温度 {temp:.0f}/100（强势档），仓位可上沿，趋势策略可正常开仓；追高票注意回撤。")
    elif temp >= 45:
        _sug.append(f"温度 {temp:.0f}/100（中性档），保持防守仓位，等待放量或情绪拐点再加仓。")
    else:
        _sug.append(f"温度 {temp:.0f}/100（弱势档），以观望/轻仓为主，短线开闸需等涨停家数回升。")
    # ② 三池体检
    _trig = []
    for t in (tracked or []):
        if t["status_tag"] == "🚀触发启动":
            _trig.append(f"{t['name']}启动信号触发（放量突破，可介入）")
        elif t["status_tag"] == "⏰移出":
            _trig.append(f"{t['name']}T+5未启动已移出（时间止损）")
        elif t["status_tag"] == "🚨触及止损":
            _trig.append(f"{t['name']}触及止损，人工确认")
    _trig.extend(r.get("建议", "") for r in (track_rows or []) if "止盈" in str(r.get("建议", "")) or "止损" in str(r.get("建议", "")))
    _trig = [x for x in _trig if x]
    if _trig:
        _sug.append("三池触发：" + "；".join(_trig[:5]) + (f"（共{len(_trig)}条）" if len(_trig) > 5 else "") + "。")
    # ③ 台账体检
    if _tag_t or _tag_s:
        _sug.append(f"台账现状：趋势累计{_tag_t}只/胜率{_win_t}，短线累计{_tag_s}只/胜率{_win_s}。"
                    + ("趋势胜率<40%建议收窄涨幅过滤或延长持有。" if isinstance(_win_t, float) and _win_t < 40 else "")
                    + ("短线胜率<45%建议降低开闸温度或提高情绪过滤。" if isinstance(_win_s, float) and _win_s < 45 else ""))
    # ④ 低位观察池体检（触发式改造后的持仓周期验证）
    if tracked:
        _moved = sum(1 for t in tracked if t["status_tag"] == "⏰移出")
        _trig_n = sum(1 for t in tracked if t["status_tag"] == "🚀触发启动")
        if _moved or _trig_n:
            _sug.append(f"低位观察池：今日 {_trig_n} 只触发启动 / {_moved} 只时间止损移出；触发率若不升，考虑收紧入池筛选（量比/成交额/位置）。")
    for s in _sug:
        lines.append(f"- {s}")
    lines.append("- 做对的事：")
    lines.append("- 做错的事：")
    lines.append("- 待人工复核：以上触发项 + 明日关注表，未确认不操作。")
    lines.append("\n> 研究参考，不构成投资建议。—— Rachael")

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
    except Exception as e:
        print(f"[微信推送] 异常(内容已保存本地): {e}")

    # 策略笔记本自动更新（每策略一本，每日推荐流水+跟踪状态）
    # 2026-09-21：新增策略笔记本体系。warm 复用本次已扫描结果（避免重复拉 K 线）。
    try:
        from strategy_notebook import update_all
        update_all(today, warm=warm)
    except Exception as e:
        print(f"[笔记本] 更新失败 {e}")


if __name__ == "__main__":
    run_close()
