# -*- coding: utf-8 -*-
"""收盘复盘（15:10）：全天市场 + 推荐股表现追踪 + 双策略跟踪池收盘体检 + 双虚拟净值
2026-09-03：去真实持仓，改虚拟盘口径（我推荐=我买了）
2026-09-05：双策略分立 —— 右侧趋势 + 短线激进 分池展示，短线3日回测结算
"""
import json
from datetime import datetime
from pathlib import Path
import pandas as pd
from market_analysis import calc_market_temperature, decide_position, judge_market_env
from fetch_data import get_market_snapshot, get_industry_boards, get_realtime_quotes, get_index_daily
from trend_tracker import analyze_track_pool as trend_analyze, update_track as trend_update, stats as trend_stats
from short_tracker import analyze_track_pool as short_analyze, update_track as short_update, stats as short_stats, roll_backtest
from config import REVIEW_DIR, PUSH_DIR, VIRTUAL_ENABLED

BASE = Path(__file__).resolve().parent.parent


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

    if industry is not None and not industry.empty:
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

    # ===== 本周低位启动观察（趋势侧补充） =====
    try:
        from stock_screener import low_pos_watch
        track_codes = [r["代码"] for r in track_rows] if track_rows else []
        watch = low_pos_watch(top_n=3, exclude_codes=track_codes)
        lines.append("## 本周低位启动观察（趋势侧跟踪池补充）\n")
        if watch:
            lines.append("| 股票 | 现价 | 60日位置 | 启动信号 | 5日涨幅 | 买点区间 | 止损 |")
            lines.append("|------|------|----------|----------|---------|----------|------|")
            for w in watch:
                lines.append(f"| {w['名称']}({w['代码']}) | {w['现价']} | {w['60日位置']:.0%} | {w['信号']} | +{w['5日涨幅']:.1f}% | {w['买点区间']} | {w['止损价']} |")
            lines.append("")
            lines.append("关注逻辑：" + "；".join(f"{w['名称']}: {w['关注逻辑']}" for w in watch))
            lines.append("> 说明：低位启动观察池基于收盘数据筛选，供本周跟踪（不追高，回踩买点或放量突破再介入）。")
        else:
            lines.append("今日未筛出符合条件的低位启动股（可能整体处于高位或数据缺失），可关注明日更新。")
        lines.append("")
    except Exception as e:
        lines.append(f"\n## 本周低位启动观察（趋势侧跟踪池补充）\n\n- 筛选异常：{e}\n")
        print(f"[低位观察] 失败 {e}")

    # ===== 明日关注 + 复盘留白 =====
    lines.append("\n## 明日关注\n")
    lines.append("- （大发填：基于今日盘面与RPS主线的预判）")
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
