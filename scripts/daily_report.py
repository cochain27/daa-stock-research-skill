# -*- coding: utf-8 -*-
"""日报生成：完整日报 + 精简版推送文本"""
from datetime import datetime
import pandas as pd
from market_analysis import calc_market_temperature, decide_position, single_stock_position, judge_market_env
from stock_screener import (top_industry_boards, pick_top_stocks, value_screen, pick_quality,
                            pick_shortline_stocks, pick_trend_stocks, bottom_fishing_picks,
                            _is_trend, _is_short)
from fetch_data import get_market_snapshot, get_industry_boards, get_realtime_quotes
from trend_tracker import log_picks as trend_log, analyze_track_pool as trend_analyze
from short_tracker import log_picks as short_log, analyze_track_pool as short_analyze, roll_backtest
from skill_bridge import get_sector_rps, get_stock_score, get_value_decision
from config import (PUSH_DIR, REVIEW_DIR,
                    STOP_LOSS, TAKE_PROFIT_1, TAKE_PROFIT_2,
                    SHORT_STOP_LOSS, SHORT_TAKE_PROFIT_1, SHORT_TAKE_PROFIT_2,
                    TREND_ENABLED, TREND_MAX_PICKS, BOTTOM_FISHING_TEMP_MAX)


def generate_full_report(temp, details, pos_advice, boards, picks, rps_rows=None, track_rows=None, track_overview=None,
                         market_env=None, env_desc=None, env_reason=None):
    """生成完整日报 markdown"""
    today = datetime.now().strftime("%Y-%m-%d")
    lines = []
    env_tag = f"【{market_env}】" if market_env else ""
    style_note = "右侧趋势2-4周（满14天可展期，≤2只，最长60天）｜短线激进3天（快进快出，-4%止损，+5%/+8%止盈）"
    lines.append(f"# 大A每日投研日报 {today}\n")
    lines.append(f"> 生成时间：{datetime.now().strftime('%H:%M')} ｜ {env_tag} 风格：{style_note}\n")

    # 市场温度 + 环境判定
    lines.append("## 一、市场环境\n")
    lines.append(f"**市场温度：{temp:.0f}/100**")
    if market_env:
        lines.append(f"**环境判定：{market_env}（{env_desc}）**")
        lines.append(f"**判定依据：{env_reason}**")
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

    # 推荐个股（双策略：短线激进 + 右侧趋势）
    short_list = [p for p in picks if _is_short(p)]
    trend_list = [p for p in picks if _is_trend(p)]
    band_list = [p for p in picks if p.get("strategy_tag") not in ("短线", "趋势")]  # 旧波段兼容
    lines.append(f"## 三、推荐个股（{len(picks)}只）\n")
    if not picks:
        lines.append("> ⚠️ **今日无合格推荐**：涨停剔除+行业去重后无达标标的，建议空仓观望或等待回调\n")
    else:
        # 质量评估提示（波段通道用 pick_quality；短线通道独立判断）
        band_healthy, band_q = pick_quality(band_list, min_score=60)
        if band_list and not band_healthy:
            lines.append(f"> ⚠️ **波段通道质量提示**：{band_q}")
        short_low = [p for p in short_list if p["total"] < 60]
        if short_list and len(short_low) == len(short_list):
            lines.append(f"> ⚠️ **短线通道质量提示**：全部情绪分<60，短线情绪不足，谨慎参与")
        if lines[-1].startswith("> ⚠️"):
            lines.append("")
    for i, p in enumerate(picks, 1):
        price = p.get("现价")
        chg = p.get("涨跌幅")
        ind = p.get("行业")
        tag = p.get("strategy_tag", "波段")
        tag_icon = "⚡短线" if tag == "短线" else ("📈趋势" if tag == "趋势" else "📈波段")
        lines.append(f"### {i}. {p['名称']}（{p['symbol']}） {tag_icon}")
        ind_s = f" ｜ 行业：{ind}" if ind else ""
        zt_s = ""
        if p.get("近涨停"):
            zt_s = f" ｜ ⚠️近涨停/连板{p.get('连板数', 0)}"
        # 可展期标签（波段票专属，短线票不显示）
        ext_s = ""
        if tag == "波段" and p.get("可展期分") is not None:
            ext_s = f" ｜ {'🟢 可展期' if p['可展期分'] >= 12 else '🟡 纯波段'} {p['可展期分']}/20"
        if tag == "短线" and p.get("连板数") is not None:
            ext_s = f" ｜ 连板{p.get('连板数', 0)} 炸{p.get('炸板次数', 0)}次"
        lines.append(f"- 现价：{price if price is not None else '-'} ｜ 当日涨跌：{chg if chg is not None else '-'}%{ind_s}{zt_s}{ext_s} ｜ **评分：{p['total']:.0f}/100**")
        if tag == "短线":
            # 短线通道：情绪评分维度
            tech_note = p.get("tech", {}).get("说明", "")
            lines.append(f"- 情绪评分：{p['total']:.0f}/100（{tech_note}）")
            src = p.get("来源", "")
            if src:
                lines.append(f"- 候选来源：{src}（炸板池/涨停梯队/冲高强势票，只推未封死可成交的票）")
        else:
            lines.append(f"- 价值面：{p['value']}/30（{p.get('说明','')}）｜ 技术面：{p['tech']['分']}/30 ｜ 资金面：{p['capital']}/25 ｜ 题材：{p['theme']}/15")
            lines.append(f"- 技术说明：{p['tech']['说明']}")
        if tag == "波段" and p.get("可展期分") and p["可展期分"] >= 12 and p.get("可展期理由"):
            lines.append(f"- 🟢 可展期依据：{'、'.join(p['可展期理由'])}（满10天未达波段目标时，若趋势+量能 intact 可申请展期，最长30天；展期后止盈升至 +10%/+15%）")
        cv = p.get("交叉验证")
        if cv and cv.get("score") is not None:
            conf = f"{cv['confidence']*100:.0f}%" if isinstance(cv.get("confidence"), (int, float)) else "-"
            lines.append(f"- 🔁 交叉验证（stock-researcher）：{cv['score']:.0f}/100 {cv.get('signal','')} 置信{conf}")
        vd = p.get("价值投资")
        if vd and vd.get("verdict"):
            vscore = vd.get("score")
            vv = vd.get("verdict")
            vpart = " / ".join(f"{k}:{v:.0f}" for k, v in (vd.get("breakdown") or {}).items() if v is not None)
            lines.append(f"- 💎 价值投资交叉验证：**{vv}**（{vscore}/100）{'｜数据不足' if vd.get('insufficient') else ''}" + (f" ｜ {vpart}" if vpart else ""))
        if p.get("近涨停"):
            lines.append("- ⚠️ **不可追高**：已近涨停/连板，买不进或次日溢价风险高，请等回踩企稳或次日竞价再评估")
        if "当日已大涨" in str(p.get("tech", {}).get("说明", "")):
            lines.append("- ⚠️ **追高风险提示**：该股当日已大涨，不宜追高，等待回踩企稳或次日竞价再评估")
        if p.get("buy"):
            b = p["buy"]
            # 差异化止损止盈文案
            if tag == "短线":
                sl_note = "-4%止损（短线紧止损）"
                tp_note = "+5%减半仓 / +8%清仓"
                period_note = "目标3-5天，极限5天强制离场"
            else:
                sl_note = "-6%止损"
                tp_note = "+6%减半仓 / +10%清仓"
                period_note = "满10天触发展期评估（趋势+量能 intact 可展期≤2只，最长30天）"
            lines.append("- 条件单建议：")
            lines.append(f"  - 突破买入（右侧优先）：≥ {b.get('突破买点','-')}")
            lines.append(f"  - 回踩买入（备用）：≤ {b.get('回踩买点','-')}")
            lines.append(f"  - 建议买价区间：{b.get('建议买价区间','-')}（**推荐即虚拟成交：按推荐时实时价入账**）")
            lines.append(f"  - 止损：{b.get('止损价','-')}（{sl_note}）")
            lines.append(f"  - 止盈：{b.get('止盈1','-')}（{tp_note}）")
            lines.append(f"  - 周期：{period_note}")
            if tag == "波段" and p.get("可展期分") is not None and p["可展期分"] >= 12:
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

    # 推荐跟踪池（连续跟踪分析）+ 虚拟净值
    lines.append("## 四、推荐跟踪池（连续跟踪）\n")
    if track_rows:
        ext_n = track_overview.get('展期数', 0)
        ext_s = f"｜ 🟢展期中 {ext_n} 只（≤2只，最长30天）" if ext_n else ""
        # 虚拟净值：双策略分开展示
        trend_nav = track_overview.get("趋势", {}).get("虚拟净值") or {}
        short_nav = track_overview.get("短线", {}).get("虚拟净值") or {}
        trend_nav_s = f"趋势净值 {trend_nav.get('净值', 0):.0f}（{trend_nav.get('累计收益率', 0):+.2f}%）" if trend_nav else ""
        short_nav_s = f"短线净值 {short_nav.get('净值', 0):.0f}（{short_nav.get('累计收益率', 0):+.2f}%）" if short_nav else ""
        nav_s = f"｜ **{trend_nav_s}**" + (f"｜ **{short_nav_s}**" if short_nav_s else "")
        lines.append(f"> 当前跟踪 **{track_overview.get('总只数', 0)} 只**，总盈亏 {track_overview.get('总盈亏', 0):+.1f}%（平均 {track_overview.get('平均盈亏', 0):+.1f}%）{ext_s}{nav_s}｜ {track_overview.get('建议', '')}\n")
        # 表格含止损/止盈字段（简洁展示）
        lines.append("| 名称 | 策略 | 推荐日 | 现价 | 累计% | 最高% | 止损 | 止盈1 | 止盈2 | 操作建议 |")
        lines.append("|------|------|--------|------|-------|-------|------|-------|-------|----------|")
        for r in track_rows:
            tag = "展期" if r.get("展期") else (r.get("策略标签") or "趋势")
            sl = r.get("更新止损") or r.get("止损价") or "-"
            tp1 = r.get("更新止盈1") or r.get("止盈1") or "-"
            tp2 = r.get("更新止盈2") or r.get("止盈2") or "-"
            # 数字格式美化
            try: tp1 = f"{float(tp1):.2f}"
            except: pass
            try: tp2 = f"{float(tp2):.2f}"
            except: pass
            try: sl = f"{float(sl):.2f}"
            except: pass
            lines.append(f"| {r['名称']}({r['代码']}) | {tag} | {r['推荐日']} | {r['现价']} | {r['累计盈亏%']:+.1f}% | {r['最高收益%']:+.1f}% | {sl} | {tp1} | {tp2} | {r['建议']} |")
        # 仓位预警
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
    lines.append("- **右侧趋势票**：止损 -6%；盈利>3% 上移成本线，跌破 MA10 移动止盈离场；+6% 减半 / +10% 清仓；满14日触发展期评估（趋势完好且展期≤2只可展期），展期后目标升至 +10%/+15%，最长60天强制离场")
    lines.append("- **短线激进票**：止损 -4%；盈利>2% 上移成本线；+5% 减半 / +8% 清仓；目标3天，极限5天强制离场；每日重评情绪分，低于门槛剔除")
    lines.append("- 横盘5日无进展建议移除；免责声明：本报告为研究参考，不构成投资建议")
    return "\n".join(lines)


def generate_brief(temp, pos_advice, boards, picks, track_rows=None, track_overview=None, market_env=None):
    """精简版推送文本"""
    today = datetime.now().strftime("%m-%d")
    regime = pos_advice[1].split('：')[0] if '：' in pos_advice[1] else pos_advice[1]
    env_s = f"[{market_env}] " if market_env else ""
    lines = [f"【大A投研 {today}】"]
    lines.append(f"{env_s}温度 {temp:.0f}/100 → 仓位 {pos_advice[0]}（{regime}）")
    if boards is not None and not boards.empty:
        top3 = "、".join(boards["板块名称"].astype(str).head(3).tolist())
        lines.append(f"强势板块：{top3}")
    lines.append(f"推荐{len(picks)}只：")
    for i, p in enumerate(picks, 1):
        b = p.get("buy", {})
        tag = p.get("strategy_tag", "波段")
        tag_s = "⚡短线" if tag == "短线" else "📈波段"
        price = p.get("现价")
        cv = p.get("交叉验证") or {}
        cv_score = f" 验{cv['score']:.0f}" if cv and cv.get('score') is not None else ""
        ext_s = ""
        if tag == "波段" and p.get("可展期分") is not None:
            ext_s = " 🟢" if p["可展期分"] >= 12 else " 🟡"
        lines.append(f"{i}. {p['名称']}({p['symbol']}) {tag_s} 评分{p['total']:.0f}{cv_score}{ext_s} "
                     f"突破≥{b.get('突破买点','-')} 回踩≤{b.get('回踩买点','-')} "
                     f"止损{b.get('止损价','-')} 止盈{b.get('止盈1','-')}/{b.get('止盈2','-')}")
    if track_rows:
        lines.append("跟踪池：")
        for r in track_rows:
            tag = "展期" if r.get("展期") else (r.get("策略标签") or "波段")
            tag_icon = "⚡" if tag == "短线" else ("🟢" if r.get("展期") else "📈")
            lines.append(f"- {r['名称']} {tag_icon}{tag} 持有{r['持有天数']}天 累计{r['累计盈亏%']:+.1f}% 最高{r['最高收益%']:+.1f}% → {r['建议']}")
        if track_overview:
            trend_nav = track_overview.get("趋势", {}).get("虚拟净值")
            short_nav = track_overview.get("短线", {}).get("虚拟净值")
            nav_parts = []
            if trend_nav:
                nav_parts.append(f"趋势净值{trend_nav.get('净值', 0):.0f}({trend_nav.get('累计收益率', 0):+.2f}%)")
            if short_nav:
                nav_parts.append(f"短线净值{short_nav.get('净值', 0):.0f}({short_nav.get('累计收益率', 0):+.2f}%)")
            nav_s = ("  " + " / ".join(nav_parts)) if nav_parts else ""
            lines.append(f"汇总：{track_overview['总只数']}只 平均{track_overview['平均盈亏']:+.1f}%{nav_s}｜{track_overview['建议']}")
    return "\n".join(lines)


def run_daily():
    """主流程：分析→双通道选股（短线激进+右侧波段）→生成日报→虚拟盘建仓校验"""
    temp, details = calc_market_temperature()
    pos_advice = decide_position(temp)
    snapshot = get_market_snapshot()
    industry, _src = get_industry_boards()
    boards = top_industry_boards(industry)

    # 市场环境判定（强势市→波段为主 / 弱势市→短线为主）
    from market_analysis import judge_market_env, shortline_gate
    from fetch_data import get_index_daily
    idx_df = get_index_daily("sh000001")
    market_env, env_desc, env_reason = judge_market_env(idx_df, temp, snapshot)

    # ===== 双策略选股（2026-09-05 架构：短线激进 + 右侧趋势） =====
    # ① 右侧趋势通道：横盘放量突破，2-4周持仓
    trend_picks = []
    if TREND_ENABLED:
        try:
            trend_picks = pick_trend_stocks(snapshot, boards=boards, n=TREND_MAX_PICKS)
            for p in trend_picks:
                p["strategy_tag"] = "趋势"
            print(f"[右侧趋势] 推荐 {len(trend_picks)} 只: {', '.join(p['名称'] for p in trend_picks)}")
        except Exception as e:
            print(f"[右侧趋势] 出票异常 {e}")
    trend_codes = {p["symbol"] for p in trend_picks}

    # ② 短线激进通道：情绪驱动 or 冰点抄底
    short_picks = []
    gate_open, gate_reason = shortline_gate()
    if gate_open:
        try:
            short_picks = pick_shortline_stocks(snapshot, n=2, exclude_codes=trend_codes)
            for p in short_picks:
                p["strategy_tag"] = "短线"
        except Exception as e:
            print(f"[短线通道] 出票异常 {e}")
    else:
        print(f"[短线通道] 休战：{gate_reason}")
        # 冰点抄底：温度≤35时，即使短线休战也尝试冰点抄底
        if temp <= BOTTOM_FISHING_TEMP_MAX:
            try:
                short_picks = bottom_fishing_picks(snapshot, temp=temp, exclude_codes=trend_codes, n=2)
                for p in short_picks:
                    p["strategy_tag"] = "短线"
                if short_picks:
                    print(f"[冰点抄底] 入选 {len(short_picks)} 只: {', '.join(p['名称'] for p in short_picks)}")
            except Exception as e:
                print(f"[冰点抄底] 出票异常 {e}")
    picks = trend_picks + short_picks

    # ===== stock-researcher 技能增强：RPS中期强度 + 入选股交叉验证 =====
    rps_rows = get_sector_rps()
    for p in picks:
        try:
            p["交叉验证"] = get_stock_score(p["symbol"])
        except Exception:
            p["交叉验证"] = None
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
                b = p.get("buy") or {}
                if b.get("基准价"):
                    base = q["现价"]
                    is_short = p.get("strategy_tag") == "短线"
                    sl_r = SHORT_STOP_LOSS if is_short else STOP_LOSS
                    tp1_r = SHORT_TAKE_PROFIT_1 if is_short else TAKE_PROFIT_1
                    tp2_r = SHORT_TAKE_PROFIT_2 if is_short else TAKE_PROFIT_2
                    b["突破买点"] = round(base * 1.02, 2)
                    b["回踩买点"] = round(min(base, base * 0.97), 2)
                    b["建议买价区间"] = f"{round(base * 0.97, 2)}-{round(base * 1.02, 2)}"
                    b["止损价"] = round(base * (1 + sl_r), 2)
                    b["止盈1"] = round(base * (1 + tp1_r), 2)
                    b["止盈2"] = round(base * (1 + tp2_r), 2)

    # 双策略分别落账（趋势入趋势台账，短线入短线台账；推荐即虚拟成交）
    try:
        trend_logged = trend_log(trend_picks, market_env) if trend_picks else 0
        short_logged = short_log(short_picks, market_env) if short_picks else 0
        print(f"[台账] 趋势新登记 {trend_logged} 条，短线新登记 {short_logged} 条")
    except Exception as e:
        print(f"[台账] 登记失败 {e}")

    # 落盘今日推荐股，供盘中监控脚本使用
    import json as _json
    _picks_path = PUSH_DIR.parent / "data" / "today_picks.json"
    try:
        _picks_path.write_text(_json.dumps(
            [{"symbol": p["symbol"], "名称": p["名称"], "buy": p.get("buy", {}),
              "strategy_tag": p.get("strategy_tag", "波段")} for p in picks],
            ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass

    # 双策略跟踪池分别分析
    trend_rows, trend_overview = trend_analyze()
    short_rows, short_overview = short_analyze()
    # 构建兼容层（同时满足 generate_brief 和 generate_full_report 的字段访问）
    total = trend_overview.get("总只数", 0) + short_overview.get("总只数", 0)
    total_pnl = (trend_overview.get("总盈亏", 0) + short_overview.get("总盈亏", 0))
    avg_pnl = round(total_pnl / total, 1) if total else 0
    track_rows = trend_rows + short_rows
    track_overview = {
        # 兼容 generate_brief / generate_full_report 直接访问字段
        "总只数": total,
        "总盈亏": round(total_pnl, 1),
        "平均盈亏": avg_pnl,
        "建议": f"趋势{trend_overview.get('建议', '-')}；短线{short_overview.get('建议', '-')}",
        # 嵌套结构供 generate_full_report 双净值展示
        "趋势": trend_overview,
        "短线": short_overview,
    }

    full = generate_full_report(temp, details, pos_advice, boards, picks, rps_rows, track_rows, track_overview,
                                 market_env=market_env, env_desc=env_desc, env_reason=env_reason)
    brief = generate_brief(temp, pos_advice, boards, picks, track_rows, track_overview,
                           market_env=market_env)

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
