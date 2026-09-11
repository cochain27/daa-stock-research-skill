# -*- coding: utf-8 -*-
"""午间复盘（12:00）：上午盘面 + 今日推荐股（双策略分池） + 跟踪池盘中预警（分池） + 双虚拟净值
2026-09-03：去真实持仓，改虚拟盘口径（我推荐=我买了）
2026-09-05：双策略分立 —— 右侧趋势 + 短线激进 分池展示
"""
import json
from datetime import datetime
from pathlib import Path
import pandas as pd
from market_analysis import calc_market_temperature, safe_market_temperature, decide_position, judge_market_env
from fetch_data import get_market_snapshot, get_industry_boards, get_realtime_quotes, get_index_daily
from trend_tracker import analyze_track_pool as trend_analyze
from short_tracker import analyze_track_pool as short_analyze
from config import PUSH_DIR, VIRTUAL_ENABLED

BASE = Path(__file__).resolve().parent.parent


def _nav_line(nav):
    """虚拟净值单行文案"""
    if not nav:
        return ""
    return f"**虚拟净值：{nav['净值']:.0f}（{nav['累计收益率']:+.2f}%）**｜ 已了结{nav['已了结笔数']}笔｜ 持仓{nav['持仓笔数']}笔"


def _tag_icon(tag, extended=False):
    if extended:
        return "🟢展期"
    return "⚡短线" if tag == "短线" else "📈趋势"


def run_noon():
    today = datetime.now().strftime("%Y-%m-%d")
    now = datetime.now().strftime("%H:%M")
    temp, details, _temp_ok = safe_market_temperature()
    pos = decide_position(temp)
    snap = get_market_snapshot()
    industry, _ = get_industry_boards()

    # 市场环境判定
    idx_df = get_index_daily("sh000001")
    market_env, env_desc, _ = judge_market_env(idx_df, temp, snap)

    lines = [f"# 午间复盘 {today}（{now}）\n"]
    env_s = f"【{market_env}（{env_desc}）】" if market_env else ""
    lines.append(f"**{env_s}上午市场温度：{temp:.0f}/100 → {pos[0]}（{pos[1].split('：')[0]}）**\n")
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

    # ===== 今日推荐股上午表现（双策略分池） =====
    picks_path = BASE / "data" / "today_picks.json"
    if picks_path.exists():
        picks = json.loads(picks_path.read_text(encoding="utf-8"))
        if picks:
            quotes = get_realtime_quotes([p["symbol"] for p in picks])
            if quotes:
                lines.append("## 今日推荐股上午表现\n")
                # 双策略分池
                trend_picks = [p for p in picks if (p.get("strategy_tag") or "趋势") == "趋势"]
                short_picks = [p for p in picks if (p.get("strategy_tag") or "") == "短线"]
                lines.append("| 股票 | 策略 | 现价 | 当日% | 建议买区 | 状态 |")
                lines.append("|------|------|------|-------|----------|------|")
                for group_tag, group in (("趋势", trend_picks), ("短线", short_picks)):
                    if not group:
                        continue
                    for p in group:
                        q = quotes.get(str(p["symbol"]))
                        if not q:
                            continue
                        b = p.get("buy", {})
                        rng = b.get("建议买价区间", "-")
                        tag_s = "⚡短线" if group_tag == "短线" else "📈趋势"
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
                        lines.append(f"| {p['名称']}({p['symbol']}) | {tag_s} | {q['现价']} | {q['涨跌幅']:+.2f}% | {rng} | {status} |")
                lines.append("")

    # ===== 跟踪池盘中预警（双策略分池） =====
    trend_rows, trend_overview = trend_analyze()
    short_rows, short_overview = short_analyze()
    lines.append("## 跟踪池盘中预警\n")
    if not trend_rows and not short_rows:
        lines.append("> 当前无未结清跟踪池（今日新推荐将在收盘虚拟建仓后入池）。\n")

    # --- 右侧趋势池 ---
    lines.append("### 📈 右侧趋势池\n")
    if trend_rows:
        nav = trend_overview.get("虚拟净值")
        nav_s = f"｜ {_nav_line(nav)}" if nav else ""
        lines.append(f"> 当前跟踪 **{trend_overview.get('总只数', 0)} 只**，总盈亏 {trend_overview.get('总盈亏', 0):+.1f}%（平均 {trend_overview.get('平均盈亏', 0):+.1f}%）{nav_s}｜ {trend_overview.get('建议', '')}\n")
        lines.append("| 名称 | 推荐日 | 现价 | 累计% | 止损 | 止盈1 | 止损距离 | 预警 |")
        lines.append("|------|--------|------|-------|------|-------|---------|------|")
        for r in trend_rows:
            sl = r.get("更新止损") or r.get("止损价") or "-"
            tp1 = r.get("更新止盈1") or r.get("止盈1") or "-"
            try: tp1 = f"{float(tp1):.2f}"
            except Exception: pass
            try: sl = f"{float(sl):.2f}"
            except Exception: pass
            try:
                sl_dist = (r["现价"] - float(sl)) / r["现价"] * 100
            except Exception:
                sl_dist = None
            warn = ""
            if r.get("建议", "").startswith("⛔"):
                warn = "⛔ 触发离场信号"
            elif sl_dist is not None and sl_dist < 2:
                warn = "🚨 距止损<2%"
            elif r.get("建议", "").startswith("💰"):
                warn = "💰 即将触止盈"
            sl_s = f"{sl_dist:.1f}%" if sl_dist is not None else "-"
            lines.append(f"| {r['名称']}({r['代码']}) | {r['推荐日']} | {r['现价']} | {r['累计盈亏%']:+.1f}% | {sl} | {tp1} | {sl_s} | {warn or '正常'} |")
        lines.append("")
    else:
        lines.append("> 趋势跟踪池为空。\n")

    # --- 短线激进池 ---
    lines.append("### ⚡ 短线激进池\n")
    if short_rows:
        nav = short_overview.get("虚拟净值")
        nav_s = f"｜ {_nav_line(nav)}" if nav else ""
        lines.append(f"> 当前跟踪 **{short_overview.get('总只数', 0)} 只**，总盈亏 {short_overview.get('总盈亏', 0):+.1f}%（平均 {short_overview.get('平均盈亏', 0):+.1f}%）{nav_s}｜ {short_overview.get('建议', '')}\n")
        lines.append("| 名称 | 推荐日 | 现价 | 累计% | 止损 | 止盈1 | 止损距离 | 预警 |")
        lines.append("|------|--------|------|-------|------|-------|---------|------|")
        for r in short_rows:
            sl = r.get("更新止损") or r.get("止损价") or "-"
            tp1 = r.get("更新止盈1") or r.get("止盈1") or "-"
            try: tp1 = f"{float(tp1):.2f}"
            except Exception: pass
            try: sl = f"{float(sl):.2f}"
            except Exception: pass
            try:
                sl_dist = (r["现价"] - float(sl)) / r["现价"] * 100
            except Exception:
                sl_dist = None
            warn = ""
            if r.get("建议", "").startswith("⛔"):
                warn = "⛔ 触发离场信号"
            elif sl_dist is not None and sl_dist < 2:
                warn = "🚨 距止损<2%"
            elif r.get("建议", "").startswith("💰"):
                warn = "💰 即将触止盈"
            sl_s = f"{sl_dist:.1f}%" if sl_dist is not None else "-"
            lines.append(f"| {r['名称']}({r['代码']}) | {r['推荐日']} | {r['现价']} | {r['累计盈亏%']:+.1f}% | {sl} | {tp1} | {sl_s} | {warn or '正常'} |")
        lines.append("")
    else:
        lines.append("> 短线跟踪池为空。\n")

    lines.append("\n> 下午关注：止损条件单有效性、推荐股是否回踩买区、跟踪池距止损/止盈距离。研究参考，不构成投资建议。")
    out = "\n".join(lines)
    path = PUSH_DIR / f"午间复盘_{today}.md"
    path.write_text(out, encoding="utf-8")
    print(out[:800])
    print(f"\n[午间复盘已保存] {path}")
    try:
        from notify import push_report
        ok, ch, msg = push_report(f"午间复盘 {today}", out)
        print(f"[微信推送] {'成功' if ok else '跳过/失败: '+msg}")
    except Exception:
        pass


if __name__ == "__main__":
    run_noon()
