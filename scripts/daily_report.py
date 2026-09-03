# -*- coding: utf-8 -*-
"""日报生成：完整日报 + 精简版推送文本"""
from datetime import datetime
import pandas as pd
from market_analysis import calc_market_temperature, decide_position, single_stock_position
from stock_screener import top_industry_boards, pick_top_stocks, value_screen, pick_quality
from fetch_data import get_market_snapshot, get_industry_boards, get_realtime_quotes
from portfolio import analyze_holdings
from tracker import analyze_track_pool, log_picks
from skill_bridge import get_sector_rps, get_stock_score, get_value_decision
from config import PUSH_DIR, REVIEW_DIR


def generate_full_report(temp, details, pos_advice, boards, picks, hold_rows=None, overview=None, rps_rows=None, track_rows=None, track_overview=None):
    """生成完整日报 markdown"""
    today = datetime.now().strftime("%Y-%m-%d")
    lines = []
    lines.append(f"# 大A每日投研日报 {today}\n")
    lines.append(f"> 生成时间：{datetime.now().strftime('%H:%M')} ｜ 风格：右侧波段7-10天（满10天可展期，≤2只，最长30天）\n")

    # 市场温度
    lines.append("## 一、市场环境\n")
    lines.append(f"**市场温度：{temp:.0f}/100**")
    lines.append(f"**仓位建议：{pos_advice[0]}**")
    lines.append(f"**操作指引：{pos_advice[1]}**\n")
    lines.append("| 维度 | 分值 | 说明 |")
    lines.append("|------|------|------|")
    for k, v in details.items():
        lines.append(f"| {k} | {v['分']:.0f} | {v['说明']} |")
    lines.append("")

    # 板块
    lines.append("## 二、强势板块\n")
    boards_preopen = boards is not None and not boards.empty and \
        pd.to_numeric(boards.get("涨跌幅"), errors="coerce").fillna(0).abs().sum() == 0
    if boards is not None and not boards.empty:
        if boards_preopen:
            lines.append("> 盘前模式：集合竞价阶段板块无涨跌数据，下表仅供参考，开盘后以盘中为准\n")
        lines.append("| 板块 | 涨跌幅% | 换手率% | 领涨股 |")
        lines.append("|------|--------|---------|--------|")
        for _, b in boards.head(6).iterrows():
            name = b.get("板块名称", "")
            chg = b.get("涨跌幅", "")
            hs = b.get("换手率", "")
            lead = b.get("领涨股票", "")
            chg = f"{chg:.2f}" if pd.notna(chg) else "-"
            hs = f"{hs:.2f}" if pd.notna(hs) else "-"
            lead = str(lead) if pd.notna(lead) else "-"
            lines.append(f"| {name} | {chg} | {hs} | {lead} |")
    else:
        lines.append("板块数据获取失败")
    lines.append("")

    # RPS中期相对强度（stock-researcher 交叉维度）
    if rps_rows:
        lines.append("### 中期相对强度（RPS，申万口径，周级别）\n")
        lines.append("| 排名 | 板块 | RPS | 百分位 | 20日 | 60日 | 趋势 |")
        lines.append("|------|------|-----|--------|------|------|------|")
        for i, r in enumerate(rps_rows[:8], 1):
            lines.append(f"| {i} | {r['板块']} | {r['RPS']:+.1f} | {r['百分位']}% | {r['20d']:+.1f}% | {r['60d']:+.1f}% | {r['趋势']} |")
        lines.append("> 当日涨幅榜看脉冲，RPS榜看中期主线——两者共振的板块优先级最高")
        lines.append("")

    # 推荐个股
    lines.append(f"## 三、推荐个股（{len(picks)}只）\n")
    if not picks:
        lines.append("> ⚠️ **今日无合格推荐**：涨停剔除+行业去重后无达标标的，建议空仓观望或等待回调\n")
    else:
        # 质量评估提示
        healthy, qreason = pick_quality(picks, min_score=60)
        if not healthy:
            lines.append(f"> ⚠️ **质量提示**：{qreason}\n")
    for i, p in enumerate(picks, 1):
        price = p.get("现价")
        chg = p.get("涨跌幅")
        ind = p.get("行业")
        lines.append(f"### {i}. {p['名称']}（{p['symbol']}）")
        ind_s = f" ｜ 行业：{ind}" if ind else ""
        zt_s = ""
        if p.get("近涨停"):
            zt_s = f" ｜ ⚠️近涨停/连板{p.get('连板数', 0)}"
        # 可展期标签（独立维度，不计入主评分；满10天展期评估的底气分）
        ext_s = ""
        if p.get("可展期分") is not None:
            tag = "🟢 可展期" if p["可展期分"] >= 12 else "🟡 纯波段"
            ext_s = f" ｜ {tag} {p['可展期分']}/20"
        lines.append(f"- 现价：{price if price is not None else '-'} ｜ 当日涨跌：{chg if chg is not None else '-'}%{ind_s}{zt_s}{ext_s} ｜ **评分：{p['total']:.0f}/100**")
        lines.append(f"- 价值面：{p['value']}/30（{p.get('说明','')}）｜ 技术面：{p['tech']['分']}/30 ｜ 资金面：{p['capital']}/25 ｜ 题材：{p['theme']}/15")
        lines.append(f"- 技术说明：{p['tech']['说明']}")
        if p.get("可展期分") and p.get("可展期分") >= 12 and p.get("可展期理由"):
            lines.append(f"- 🟢 可展期依据：{'、'.join(p['可展期理由'])}（满10天未达波段目标时，若趋势+量能 intact 可申请展期，最长30天；展期后止盈升至 +10%/+15%）")
        cv = p.get("交叉验证")
        if cv and cv.get("score") is not None:
            conf = f"{cv['confidence']*100:.0f}%" if isinstance(cv.get("confidence"), (int, float)) else "-"
            lines.append(f"- 🔁 交叉验证（stock-researcher）：{cv['score']:.0f}/100 {cv.get('signal','')} 置信{conf}")
        # 价值投资交叉验证（6大模块：护城河/财务/DCF/管理层/行业/因子）
        vd = p.get("价值投资")
        if vd and vd.get("verdict"):
            vscore = vd.get("score")
            vv = vd.get("verdict")
            vpart = " / ".join(f"{k}:{v:.0f}" for k, v in (vd.get("breakdown") or {}).items() if v is not None)
            lines.append(f"- 💎 价值投资交叉验证：**{vv}**（{vscore}/100）{'｜数据不足' if vd.get('insufficient') else ''}"
                         + (f" ｜ {vpart}" if vpart else ""))
        if p.get("近涨停"):
            lines.append("- ⚠️ **不可追高**：已近涨停/连板，买不进或次日溢价风险高，请等回踩企稳或次日竞价再评估")
        if "当日已大涨" in str(p["tech"].get("说明", "")):
            lines.append("- ⚠️ **追高风险提示**：该股当日已大涨，不宜追高，等待回踩企稳或次日竞价再评估")
        if p.get("buy"):
            b = p["buy"]
            lines.append("- 条件单建议：")
            lines.append(f"  - 突破买入（右侧优先）：≥ {b.get('突破买点','-')}")
            lines.append(f"  - 回踩买入（备用）：≤ {b.get('回踩买点','-')}")
            lines.append(f"  - 建议买价区间：{b.get('建议买价区间','-')}")
            lines.append(f"  - 止损价：{b.get('止损价','-')}（-6%）")
            lines.append(f"  - 止盈1：{b.get('止盈1','-')}（+6%减半仓）")
            lines.append(f"  - 止盈2：{b.get('止盈2','-')}（+10%清仓）")
            if p.get("可展期分") is not None and p["可展期分"] >= 12:
                lines.append(f"  - 展期后备选：满10天趋势+量能 intact 可展期 → 目标升至 +10%/+15%，最长30天")
        lines.append("")
    # 组合分散提示
    inds = [p.get("行业") for p in picks if p.get("行业")]
    if inds:
        dup = {x for x in inds if inds.count(x) > 1}
        if dup:
            lines.append(f"> ⚠️ 组合提示：推荐中出现同行业扎堆（{'、'.join(dup)}），已按行业去重规则控制。若仍有重复，注意板块联动回撤风险")
        else:
            lines.append(f"> ✅ 组合分散：推荐覆盖 {len(set(inds))} 个不同行业（{'、'.join(dict.fromkeys(inds))}），板块联动风险较低")

    # 推荐跟踪池（连续跟踪分析）
    lines.append("## 四、推荐跟踪池（连续跟踪）\n")
    if track_rows:
        ext_n = track_overview.get('展期数', 0)
        ext_s = f"｜ 🟢展期中 {ext_n} 只（≤2只，最长30天）" if ext_n else ""
        lines.append(f"> 当前跟踪 **{track_overview.get('总只数', 0)} 只**，总盈亏 {track_overview.get('总盈亏', 0):+.1f}%（平均 {track_overview.get('平均盈亏', 0):+.1f}%）{ext_s}｜ {track_overview.get('建议', '')}\n")
        lines.append("| 名称 | 推荐日 | 天数 | 现价 | 累计% | 最高% | 状态 | 操作建议 |")
        lines.append("|------|--------|------|------|-------|-------|------|----------|")
        for r in track_rows:
            st = "展期中" if r.get("展期") else "波段"
            lines.append(f"| {r['名称']}({r['代码']}) | {r['推荐日']} | {r['持有天数']} | {r['现价']} | {r['累计盈亏%']:+.1f}% | {r['最高收益%']:+.1f}% | {st} | {r['建议']} |")
        lines.append("")
        lines.append("**目标价更新（基于成本价，跟踪用）**：")
        for r in track_rows:
            tag = "🟢展期" if r.get("展期") else "波段"
            lines.append(f"- {r['名称']}（{tag}）：更新止损 {r['更新止损']} ｜ 更新止盈1 {r['更新止盈1']} ｜ 更新止盈2 {r['更新止盈2']}")
        # 与今日仓位建议的对比提示
        try:
            sug_max = int(''.join(filter(str.isdigit, pos_advice[0].split('-')[-1])) or 0)
        except Exception:
            sug_max = 0
        if sug_max and track_overview.get('总只数', 0) >= 2 and track_overview.get('平均盈亏', 0) < -3:
            lines.append(f"\n> ⚠️ 跟踪池平均浮亏 {track_overview['平均盈亏']:+.1f}%，且今日建议仓位 {pos_advice[0]}，优先处理亏损票、暂缓开新仓")
        lines.append("")
    else:
        lines.append("> 当前无未结清推荐跟踪池。今日新推荐将自动入池，收盘后启动连续跟踪。\n")

    lines.append("## 五、风控提醒")
    lines.append("- 止损 -6% 无条件执行；盈利>3% 后上移成本线，跌破 MA10 移动止盈离场")
    lines.append("- 波段止盈 +6% 减半 / +10% 清仓；满10日触发展期评估（趋势+量能 intact 且展期≤2只可展期），展期后目标升至 +10%/+15%，最长30天强制离场")
    lines.append("- 横盘5日无进展建议移除；免责声明：本报告为研究参考，不构成投资建议\n")
    return "\n".join(lines)


def generate_brief(temp, pos_advice, boards, picks, hold_rows=None, overview=None, track_rows=None, track_overview=None):
    """精简版推送文本"""
    today = datetime.now().strftime("%m-%d")
    regime = pos_advice[1].split('：')[0] if '：' in pos_advice[1] else pos_advice[1]
    lines = [f"【大A投研 {today}】"]
    lines.append(f"温度 {temp:.0f}/100 → 仓位 {pos_advice[0]}（{regime}）")
    if boards is not None and not boards.empty:
        top3 = "、".join(boards["板块名称"].astype(str).head(3).tolist())
        lines.append(f"强势板块：{top3}")
    lines.append(f"推荐{len(picks)}只：")
    for i, p in enumerate(picks, 1):
        b = p.get("buy", {})
        price = p.get("现价")
        cv = p.get("交叉验证") or {}
        cv_score = f" 验证{cv['score']:.0f}" if cv and cv.get('score') is not None else ""
        ext_s = ""
        if p.get("可展期分") is not None:
            ext_s = " 🟢" if p["可展期分"] >= 12 else " 🟡"
        lines.append(f"{i}. {p['名称']}({p['symbol']}) 评分{p['total']:.0f}{cv_score}{ext_s} "
                     f"突破≥{b.get('突破买点','-')} 回踩≤{b.get('回踩买点','-')} "
                     f"止损{b.get('止损价','-')} 止盈{b.get('止盈1','-')}/{b.get('止盈2','-')}")
    if track_rows:
        lines.append("跟踪池：")
        for r in track_rows:
            tag = "🟢展期" if r.get("展期") else ""
            lines.append(f"- {r['名称']} 持有{r['持有天数']}天 累计{r['累计盈亏%']:+.1f}% 最高{r['最高收益%']:+.1f}% {tag} → {r['建议']}")
        if track_overview:
            lines.append(f"汇总：{track_overview['总只数']}只 平均{track_overview['平均盈亏']:+.1f}%｜{track_overview['建议']}")
    return "\n".join(lines)


def run_daily():
    """主流程：分析→选股→【发报前刷新最新行情】→生成"""
    temp, details = calc_market_temperature()
    pos_advice = decide_position(temp)
    snapshot = get_market_snapshot()
    industry, _src = get_industry_boards()
    boards = top_industry_boards(industry)
    picks = pick_top_stocks(snapshot, boards=boards, n=2)

    # ===== stock-researcher 技能增强：RPS中期强度 + 入选股交叉验证 =====
    rps_rows = get_sector_rps()
    for p in picks:
        try:
            p["交叉验证"] = get_stock_score(p["symbol"])
        except Exception:
            p["交叉验证"] = None
        # 价值投资交叉验证（6大模块，较重，单只约5-10s）
        try:
            p["价值投资"] = get_value_decision(p["symbol"])
        except Exception:
            p["价值投资"] = None

    # ===== 发报前最后一步：强制刷新最新实时行情（推荐股现价/涨跌幅） =====
    refresh_codes = [p["symbol"] for p in picks if p.get("symbol")]
    if refresh_codes:
        rt = get_realtime_quotes(refresh_codes)
        for p in picks:
            q = rt.get(str(p["symbol"]))
            if q:
                p["现价"] = q["现价"]
                p["涨跌幅"] = q["涨跌幅"]
                # 买点/止损/止盈基准同步刷新为最新价
                b = p.get("buy") or {}
                if b.get("基准价"):
                    base = q["现价"]
                    b["突破买点"] = round(base * 1.02, 2)
                    b["回踩买点"] = round(min(base, base * 0.97), 2)
                    b["建议买价区间"] = f"{round(base * 0.97, 2)}-{round(base * 1.02, 2)}"
                    b["止损价"] = round(base * 0.94, 2)
                    b["止盈1"] = round(base * 1.06, 2)
                    b["止盈2"] = round(base * 1.10, 2)

    # 持仓体检（实时价）— 保留读取能力，但不写入日报正文
    hold_rows, overview = analyze_holdings()

    # 推荐跟踪池：读取台账中未结清推荐，做连续跟踪分析
    track_rows, track_overview = analyze_track_pool()

    # 落盘今日推荐股，供盘中监控脚本使用
    import json as _json
    _picks_path = PUSH_DIR.parent / "data" / "today_picks.json"
    try:
        _picks_path.write_text(_json.dumps(
            [{"symbol": p["symbol"], "名称": p["名称"], "buy": p.get("buy", {})} for p in picks],
            ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass

    # 推荐台账落账（复盘追踪用）
    try:
        print(f"[台账] 新登记 {log_picks(picks)} 条")
    except Exception as e:
        print(f"[台账] 登记失败 {e}")

    full = generate_full_report(temp, details, pos_advice, boards, picks, hold_rows, overview, rps_rows, track_rows, track_overview)
    brief = generate_brief(temp, pos_advice, boards, picks, hold_rows, overview, track_rows, track_overview)

    today = datetime.now().strftime("%Y-%m-%d")
    path = PUSH_DIR / f"推送_{today}.md"
    path.write_text(full, encoding="utf-8")
    print(brief)
    print(f"\n[完整日报已保存] {path}")
    # 微信推送完整日报
    try:
        from notify import push_report
        ok, ch, msg = push_report(f"大A投研晨报 {today}", full)
        print(f"[微信推送] {'成功' if ok else '跳过/失败: '+msg}")
    except Exception:
        pass
    return full, brief


if __name__ == "__main__":
    run_daily()
