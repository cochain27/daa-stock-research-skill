# -*- coding: utf-8 -*-
"""午间复盘（12:00）：上午盘面 + 持仓体检 + 今日推荐股上午表现"""
import json
from datetime import datetime
from pathlib import Path
import pandas as pd
from market_analysis import calc_market_temperature, decide_position
from fetch_data import get_market_snapshot, get_industry_boards, get_realtime_quotes
from portfolio import analyze_holdings
from config import PUSH_DIR

BASE = Path(__file__).resolve().parent.parent


def run_noon():
    today = datetime.now().strftime("%Y-%m-%d")
    now = datetime.now().strftime("%H:%M")
    temp, details = calc_market_temperature()
    pos = decide_position(temp)
    snap = get_market_snapshot()
    industry, _ = get_industry_boards()
    hold_rows, overview = analyze_holdings()

    lines = [f"# 午间复盘 {today}（{now}）\n"]
    lines.append(f"**上午市场温度：{temp:.0f}/100 → {pos[0]}（{pos[1].split('：')[0]}）**\n")
    lines.append("| 维度 | 分值 | 说明 |")
    lines.append("|------|------|------|")
    for k, v in details.items():
        lines.append(f"| {k} | {v['分']:.0f} | {v['说明']} |")
    lines.append("")

    if industry is not None and not industry.empty:
        ind = industry.copy()
        ind["涨跌幅"] = pd.to_numeric(ind.get("涨跌幅"), errors="coerce")
        top = ind.sort_values("涨跌幅", ascending=False).head(5)
        lines.append("**上午领涨板块**：" + "、".join(top["板块名称"].astype(str).tolist()))
        lines.append("")

    # 今日推荐股上午表现
    picks_path = BASE / "data" / "today_picks.json"
    if picks_path.exists():
        picks = json.loads(picks_path.read_text(encoding="utf-8"))
        quotes = get_realtime_quotes([p["symbol"] for p in picks])
        if quotes:
            lines.append("## 今日推荐股上午表现\n")
            lines.append("| 股票 | 现价 | 当日% | 建议买区 | 状态 |")
            lines.append("|------|------|-------|----------|------|")
            for p in picks:
                q = quotes.get(str(p["symbol"]))
                if not q:
                    continue
                b = p.get("buy", {})
                rng = b.get("建议买价区间", "-")
                status = "观察"
                try:
                    lo, hi = [float(x) for x in rng.split("-")]
                    if lo <= q["现价"] <= hi:
                        status = "🎯 在买区内"
                    elif q["现价"] > hi:
                        status = "已超买区上沿"
                    else:
                        status = "低于买区"
                except Exception:
                    pass
                lines.append(f"| {p['名称']}({p['symbol']}) | {q['现价']} | {q['涨跌幅']:+.2f}% | {rng} | {status} |")
            lines.append("")

    if hold_rows:
        lines.append("## 持仓上午体检\n")
        lines.append("| 持仓 | 现价 | 浮动盈亏% | 操作建议 |")
        lines.append("|------|------|-----------|----------|")
        for r in hold_rows:
            lines.append(f"| {r['名称']}×{r['股数']} | {r['现价'] or '-'} | {r['盈亏%']:+.2f}% | {r['建议']} |")
        if overview:
            lines.append(f"\n账户：总资产{overview['总资产']:,.0f} 仓位{overview['仓位%']}% 浮盈{overview['总浮动盈亏']:+,.0f}元")

    lines.append("\n> 下午关注：止损条件单有效性、推荐股是否回踩买区。研究参考，不构成投资建议。")
    out = "\n".join(lines)
    path = PUSH_DIR / f"午间复盘_{today}.md"
    path.write_text(out, encoding="utf-8")
    print(out[:600])
    print(f"\n[午间复盘已保存] {path}")
    try:
        from notify import push_report
        ok, ch, msg = push_report(f"午间复盘 {today}", out)
        print(f"[微信推送] {'成功' if ok else '跳过/失败: '+msg}")
    except Exception:
        pass


if __name__ == "__main__":
    run_noon()
