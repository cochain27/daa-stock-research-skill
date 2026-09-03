# -*- coding: utf-8 -*-
"""收盘复盘（15:10）：全天市场 + 推荐股表现追踪 + 持仓收盘体检 → 存档04_每日复盘
复盘结果用于策略优化：记录推荐股当日命中情况。
"""
import json
from datetime import datetime
from pathlib import Path
import pandas as pd
from market_analysis import calc_market_temperature, decide_position
from fetch_data import get_market_snapshot, get_industry_boards, get_realtime_quotes, get_index_daily
from tracker import analyze_track_pool, update_track, stats
from portfolio import analyze_holdings
from config import REVIEW_DIR, PUSH_DIR

BASE = Path(__file__).resolve().parent.parent


def run_close():
    today = datetime.now().strftime("%Y-%m-%d")
    now = datetime.now().strftime("%H:%M")
    temp, details = calc_market_temperature()
    pos = decide_position(temp)
    industry, _ = get_industry_boards()
    hold_rows, overview = analyze_holdings()
    track_rows, track_overview = analyze_track_pool()

    lines = [f"# 收盘复盘 {today}（{now}）\n"]
    lines.append(f"**收盘市场温度：{temp:.0f}/100 → {pos[0]}（{pos[1].split('：')[0]}）**\n")
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

    # ===== 今日推荐股表现追踪（策略有效性记录） =====
    picks_path = BASE / "data" / "today_picks.json"
    track = []
    if picks_path.exists():
        picks = json.loads(picks_path.read_text(encoding="utf-8"))
        quotes = get_realtime_quotes([p["symbol"] for p in picks])
        lines.append("## 今日推荐股收盘表现（策略追踪）\n")
        lines.append("| 股票 | 收盘价 | 当日% | 买区 | 收盘状态 | 命中? |")
        lines.append("|------|--------|-------|------|----------|-------|")
        for p in picks:
            q = quotes.get(str(p["symbol"]))
            if not q:
                continue
            b = p.get("buy", {})
            rng = b.get("建议买价区间", "-")
            status, hit = "-", "-"
            try:
                lo, hi = [float(x) for x in rng.split("-")]
                stop = b.get("止损价")
                if stop and q["现价"] <= stop:
                    status, hit = "跌破止损", "❌"
                elif lo <= q["现价"] <= hi:
                    status, hit = "收于买区内", "✅ 可介入"
                elif q["现价"] > hi:
                    status, hit = "超买区上沿", "⚠️ 未给机会"
                else:
                    status, hit = "低于买区", "⏳ 继续观察"
            except Exception:
                pass
            lines.append(f"| {p['名称']}({p['symbol']}) | {q['现价']} | {q['涨跌幅']:+.2f}% | {rng} | {status} | {hit} |")
            track.append({"代码": p["symbol"], "名称": p["名称"], "收盘": q["现价"],
                          "当日%": q["涨跌幅"], "状态": status, "命中": hit})
        lines.append("")

    # ===== 推荐跟踪池收盘体检 =====
    if track_rows:
        lines.append("## 推荐跟踪池收盘体检\n")
        ext_n = track_overview.get('展期数', 0)
        ext_s = f"｜ 🟢展期中 {ext_n} 只（≤2只，最长30天）" if ext_n else ""
        lines.append(f"> 当前跟踪 **{track_overview.get('总只数', 0)} 只**，总盈亏 {track_overview.get('总盈亏', 0):+.1f}%（平均 {track_overview.get('平均盈亏', 0):+.1f}%）{ext_s}｜ {track_overview.get('建议', '')}\n")
        lines.append("| 名称 | 收盘价 | 天数 | 累计% | 最高% | 状态 | 收盘操作建议 |")
        lines.append("|------|--------|------|-------|-------|------|--------------|")
        for r in track_rows:
            st = "展期中" if r.get("展期") else "波段"
            lines.append(f"| {r['名称']}({r['代码']}) | {r['现价']} | {r['持有天数']} | {r['累计盈亏%']:+.1f}% | {r['最高收益%']:+.1f}% | {st} | {r['建议']} |")
        lines.append("")
        lines.append("**目标价更新（基于成本价，跟踪用）**：")
        for r in track_rows:
            tag = "🟢展期" if r.get("展期") else "波段"
            lines.append(f"- {r['名称']}（{tag}）：更新止损 {r['更新止损']} ｜ 更新止盈1 {r['更新止盈1']} ｜ 更新止盈2 {r['更新止盈2']}")
        lines.append("")
        lines.append("> 注：持有满10天的票，收盘台账会触发展期评估（趋势+量能 intact 且展期≤2只可展期）；已展期票目标升至 +10%/+15%，最长30天硬上限强制离场。")
        lines.append("")
    else:
        lines.append("## 推荐跟踪池收盘体检\n")
        lines.append("当前无未结清推荐跟踪池。\n")

    # ===== 台账追踪更新 + 累计统计 =====
    try:
        n, msg = update_track()
        st = stats()
        lines.append("## 推荐台账累计表现\n")
        if isinstance(st, dict):
            lines.append(f"- 累计推荐 {st['累计推荐']} 只 ｜ 持有中 {st['持有中']} ｜ 已结清 {st['已结清']} ｜ **胜率 {st['胜率']}** ｜ 平均峰值 {st['平均峰值']}")
        lines.append(f"- 台账文件：`data/推荐台账.csv`（{msg}）")
        lines.append("")
        print(f"[台账] {msg}")
    except Exception as e:
        print(f"[台账] 更新失败 {e}")

    # ===== 本周低位启动观察（低位+趋势启动初现，供本周跟踪） =====
    try:
        from stock_screener import low_pos_watch
        # 排除已在跟踪池中的代码，避免重复推荐
        track_codes = [r["代码"] for r in track_rows] if track_rows else []
        watch = low_pos_watch(top_n=3, exclude_codes=track_codes)
        lines.append("## 本周低位启动观察（跟踪池）\n")
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
        lines.append(f"\n## 本周低位启动观察（跟踪池）\n\n- 筛选异常：{e}\n")
        print(f"[低位观察] 失败 {e}")

    # ===== 明日关注 + 复盘留白 =====
    lines.append("\n## 明日关注\n")
    lines.append("- （大发填：基于今日盘面与RPS主线的预判）")
    lines.append("\n## 策略反思（每日必填）\n")
    lines.append("- 今日推荐命中率：")
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
    print(out[:800])
    print(f"\n[收盘复盘已保存] {path}")
    try:
        from notify import push_report
        ok, ch, msg = push_report(f"收盘复盘 {today}", out)
        print(f"[微信推送] {'成功' if ok else '跳过/失败: '+msg}")
    except Exception:
        pass


if __name__ == "__main__":
    run_close()
