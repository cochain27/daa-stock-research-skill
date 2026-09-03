# -*- coding: utf-8 -*-
"""推荐台账与多日表现追踪
- log_picks(): 晨报推荐落账（data/推荐台账.csv）
- update_track(): 收盘时更新未结清推荐的状态（止盈/止损/到期/展期）与最高收益
- stats(): 累计胜率统计（供复盘引用）
"""
import csv
from datetime import datetime
from pathlib import Path
import pandas as pd
from fetch_data import get_realtime_quotes, get_stock_hist
from config import (STOP_LOSS, TAKE_PROFIT_1, TAKE_PROFIT_2,
                    HOLD_DAYS_MAX, HOLD_DAYS_QUIT,
                    EXTEND_MAX_DAYS, EXTEND_MAX_COUNT, EXTEND_TP1, EXTEND_TP2)

BASE = Path(__file__).resolve().parent.parent
LOG_PATH = BASE / "data" / "推荐台账.csv"
FIELDS = ["日期", "代码", "名称", "自研评分", "验证评分", "基准价", "买区", "止损", "止盈1", "止盈2",
          "状态", "最高收益%", "最后更新", "备注"]

# 策略参数（统一从 config 读取，避免多处不一致）
TP1, TP2, SL = TAKE_PROFIT_1, TAKE_PROFIT_2, STOP_LOSS
MAX_DAYS = HOLD_DAYS_MAX      # 满10天触发展期评估，非机械离场
QUIT_DAYS = HOLD_DAYS_QUIT    # 横盘 5 天无进展离场
EXTEND_PEAK_DRAWBACK = 6      # 展期条件：从最高点回撤不超过6%

# 未结清状态（含展期中）
OPEN_STATUS = ("持有中", "止盈1减半", "展期中")


def _read_rows():
    if not LOG_PATH.exists():
        return []
    with open(LOG_PATH, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _is_extended(r):
    """判断某条台账记录是否已处于展期状态（兼容旧数据：状态=展期中 或 备注含'展期'）"""
    return r["状态"] == "展期中" or ("展期" in (r.get("备注") or ""))


def _extend_decision(r, price, ret, peak_ret, ext_count=0):
    """满 MAX_DAYS 时评估是否展期（趋势延续则继续跟踪）
    约束（2026-09-03 小火炉拍板）：同时展期≤EXTEND_MAX_COUNT只，单票最长EXTEND_MAX_DAYS天
    返回 (can_extend: bool, reason: str)
    """
    # 基本条件：未触发止损、当前盈利、从峰值回撤可控
    if ret <= 0:
        return False, f"满{MAX_DAYS}天收益{ret:+.1f}%未盈利，不展期"
    if peak_ret - ret > EXTEND_PEAK_DRAWBACK:
        return False, f"满{MAX_DAYS}天但峰值回撤{peak_ret - ret:.1f}%>{EXTEND_PEAK_DRAWBACK}%，不展期"
    # 名额限制：展期是少数（≤2只），不是常态
    if ext_count >= EXTEND_MAX_COUNT:
        return False, f"展期名额已满({ext_count}/{EXTEND_MAX_COUNT}只)，不展期"

    # 趋势条件：当前价 > MA10 > MA20
    try:
        df = get_stock_hist(r["代码"], days=40)
        if df is None or df.empty or len(df) < 25:
            return False, "K线数据不足，不展期"
        df = df.sort_values("日期").reset_index(drop=True)
        df["MA10"] = df["收盘"].rolling(10).mean()
        df["MA20"] = df["收盘"].rolling(20).mean()
        ma10 = df["MA10"].iloc[-1]
        ma20 = df["MA20"].iloc[-1]
        if pd.isna(ma10) or pd.isna(ma20):
            return False, "均线缺失，不展期"
        if not (price > ma10 > ma20):
            return False, f"趋势破坏(价{price:.2f}<=MA10{ma10:.2f}或MA10<=MA20{ma20:.2f})，不展期"

        # 量能条件：近5日平均成交额 >= 推荐日前5日平均成交额的60%
        rec_date = datetime.strptime(r["日期"], "%Y-%m-%d")
        df["日期"] = pd.to_datetime(df["日期"])
        recent5 = df[df["日期"] > rec_date].tail(5)["成交额"].mean()
        before5 = df[df["日期"] <= rec_date].tail(5)["成交额"].mean()
        if pd.notna(recent5) and pd.notna(before5) and before5 > 0:
            if recent5 < before5 * 0.6:
                return False, f"量能萎缩至{recent5/before5*100:.0f}%（<60%），不展期"
    except Exception as e:
        return False, f"展期评估异常：{e}，不展期"

    return True, f"趋势+量能 intact，建议展期（收益{ret:+.1f}%）"


def _write_rows(rows):
    with open(LOG_PATH, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def log_picks(picks):
    """晨报推荐落账（同日同票不重复）"""
    today = datetime.now().strftime("%Y-%m-%d")
    rows = _read_rows()
    existing = {(r["日期"], r["代码"]) for r in rows}
    added = 0
    for p in picks:
        code = str(p.get("symbol", ""))
        if (today, code) in existing or not code:
            continue
        # 过滤：无权限(科创板68/北交所)、代码非白名单前缀 —— 防止无效候选落账
        if not code.startswith(("60", "00", "30")):
            continue
        b = p.get("buy", {})
        base = b.get("基准价") or p.get("现价") or ""
        cv = p.get("交叉验证") or {}
        rows.append({
            "日期": today, "代码": code, "名称": p.get("名称", ""),
            "自研评分": f"{p.get('total', 0):.0f}", "验证评分": f"{cv.get('score') or ''}",
            "基准价": base, "买区": b.get("建议买价区间", ""), "止损": b.get("止损价", ""),
            "止盈1": b.get("止盈1", ""), "止盈2": b.get("止盈2", ""),
            "状态": "持有中", "最高收益%": "0", "最后更新": today, "备注": "",
        })
        added += 1
    if added:
        _write_rows(rows)
    return added


def update_track():
    """收盘更新未结清推荐：触止盈/止损/到期/展期 → 结清或延续；否则刷新最高收益"""
    today = datetime.now().strftime("%Y-%m-%d")
    rows = _read_rows()
    open_rows = [r for r in rows if r["状态"] in OPEN_STATUS]
    if not open_rows:
        return 0, "无未结清推荐"
    quotes = get_realtime_quotes([r["代码"] for r in open_rows])
    # 当前已展期票数（名额限制依据；评估中的新票若未展期不计入）
    ext_count = len([r for r in open_rows if _is_extended(r)])
    updated = 0
    for r in open_rows:
        q = quotes.get(r["代码"])
        if not q:
            continue
        price = q["现价"]
        if price <= 0:
            continue  # 停牌/无行情，跳过不更新（避免写入无意义数据）
        try:
            base = float(r["基准价"])
            sl = float(r["止损"]) if r["止损"] else base * (1 + SL)
        except (ValueError, TypeError):
            continue
        ret = (price / base - 1) * 100
        # 盘中最高价作为峰值（更贴近真实可兑现收益）；现价兜底
        peak = max(q.get("最高") or price, price)
        peak_ret = (peak / base - 1) * 100
        hi = max(float(r["最高收益%"] or 0), round(peak_ret, 2))
        r["最高收益%"] = str(hi)
        r["最后更新"] = today

        # 已展期判定：展期票止盈档位升级为 +10%/+15%（到了就走），非展期为 +6%/+10%
        extended = _is_extended(r)
        tp1r = EXTEND_TP1 if extended else TP1
        tp2r = EXTEND_TP2 if extended else TP2
        tp1_target = base * (1 + tp1r)
        tp2_target = base * (1 + tp2r)

        days = (datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(r["日期"], "%Y-%m-%d")).days
        if price <= sl:
            r["状态"], r["备注"] = "止损出局", f"触发-{abs(SL)*100:.0f}%止损(峰值{hi:+.1f}%)"
        elif price >= tp2_target:
            tag = "展期" if extended else ""
            r["状态"], r["备注"] = "止盈2清仓", f"{tag}达+{tp2r*100:.0f}%目标(峰值{hi:+.1f}%)"
        elif price >= tp1_target:
            if r["状态"] != "止盈1减半":
                r["状态"], r["备注"] = "止盈1减半", f"{tag}达+{tp1r*100:.0f}%减半仓，余仓看+{tp2r*100:.0f}%"
            else:
                r["备注"] = f"减半后持有中(峰值{hi:+.1f}%)"
        elif days >= EXTEND_MAX_DAYS:
            # 展期硬上限：超 1 个月强制离场（不再续）
            r["状态"], r["备注"] = "到期离场", f"满{EXTEND_MAX_DAYS}天硬上限,收益{ret:+.1f}%"
        elif days >= MAX_DAYS:
            if extended:
                # 已展期中：继续跟踪直至 30 天硬上限或触发止盈止损
                r["状态"], r["备注"] = "展期中", f"展期跟踪中(峰值{hi:+.1f}%,收益{ret:+.1f}%)"
            else:
                can_ext, reason = _extend_decision(r, price, ret, peak_ret, ext_count)
                if can_ext:
                    r["状态"], r["备注"] = "展期中", f"展期:{reason}"
                else:
                    r["状态"], r["备注"] = "到期离场", f"满{MAX_DAYS}天,{reason},收益{ret:+.1f}%"
        updated += 1
    _write_rows(rows)
    return updated, f"更新{updated}条"


def load_open_picks():
    """读取当前未结清的推荐记录（状态为持有中/止盈1减半/展期中）"""
    rows = _read_rows()
    return [r for r in rows if r["状态"] in OPEN_STATUS]


def analyze_track_pool():
    """对未结清推荐做连续跟踪分析，返回 (rows, overview)
    rows: 每只跟踪票的当前状态、累计盈亏、最高收益、持有天数、操作建议、更新目标价
    overview: 跟踪池汇总（总只数/平均盈亏/展期只数等）
    """
    today = datetime.now().strftime("%Y-%m-%d")
    rows = load_open_picks()
    if not rows:
        return [], {"总只数": 0, "总盈亏": 0, "平均盈亏": 0, "破止损": 0, "展期数": 0, "建议": "跟踪池为空"}

    codes = [r["代码"] for r in rows]
    quotes = get_realtime_quotes(codes)
    ext_count = len([r for r in rows if _is_extended(r)])
    analyzed = []
    stop_count = 0
    ext_num = 0
    total_pnl = 0.0

    for r in rows:
        code = r["代码"]
        q = quotes.get(code, {})
        price = q.get("现价", 0)
        name = r["名称"]
        try:
            base = float(r["基准价"])
            sl = float(r["止损"]) if r["止损"] else base * (1 + SL)
        except (ValueError, TypeError):
            continue

        if price <= 0:
            continue  # 停牌/无行情

        ret = (price / base - 1) * 100
        peak = max(float(r["最高收益%"] or 0), ret)
        days = (datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(r["日期"], "%Y-%m-%d")).days
        total_pnl += ret

        # 展期判定：展期票止盈档位 +10%/+15%，目标价基于成本价；非展期 +6%/+10%
        extended = _is_extended(r)
        if extended:
            ext_num += 1
            tp1r, tp2r = EXTEND_TP1, EXTEND_TP2
            # 展期票止损建议上移保本（盈利>3% 后以成本线为心理底线）
            ref_sl = base if ret > 3 else sl
            target_tp1 = base * (1 + tp1r)
            target_tp2 = base * (1 + tp2r)
        else:
            tp1r, tp2r = TP1, TP2
            ref_sl = sl
            target_tp1 = base * (1 + tp1r)
            target_tp2 = base * (1 + tp2r)

        # 操作建议（优先级：止损 > 止盈2 > 止盈1减半 > 30天上限 > 展期评估 > 横盘 > 移动止盈 > 持有）
        advice = ""
        flag = "✅"
        if price <= sl:
            advice = f"⛔ 已破止损 {sl:.2f}（-{abs(SL)*100:.0f}%），无条件移除跟踪池"
            flag = "⛔"
            stop_count += 1
        elif price >= target_tp2:
            advice = f"💰 达{'展期' if extended else ''}止盈2 {target_tp2:.2f}（+{tp2r*100:.0f}%），清仓移除"
            flag = "💰"
        elif price >= target_tp1:
            if r["状态"] == "止盈1减半":
                advice = f"💰 已减半，余仓看 {target_tp2:.2f}（+{tp2r*100:.0f}%）；跌破成本 {base:.2f} 清仓"
            else:
                advice = f"💰 达{'展期' if extended else ''}止盈1 {target_tp1:.2f}（+{tp1r*100:.0f}%），建议减半仓，余仓看 +{tp2r*100:.0f}%"
            flag = "💰"
        elif days >= EXTEND_MAX_DAYS:
            advice = f"⛔ 满 {EXTEND_MAX_DAYS} 天展期硬上限，强制离场（收益 {ret:+.1f}%）"
            flag = "⛔"
        elif days >= MAX_DAYS:
            if extended:
                advice = f"🟢 展期中：目标 +{tp1r*100:.0f}%/{tp2r*100:.0f}%（{target_tp1:.2f}/{target_tp2:.2f}）到了就走；破 MA20/硬上限30天 离场"
                flag = "🟢"
            else:
                can_ext, reason = _extend_decision(r, price, ret, peak, ext_count)
                if can_ext:
                    advice = f"📅 满 {MAX_DAYS} 天，{reason}；展期后目标升至 +{EXTEND_TP1*100:.0f}%/+{EXTEND_TP2*100:.0f}%（{target_tp1:.2f}/{target_tp2:.2f}）"
                    flag = "🟢"
                else:
                    advice = f"📅 满 {MAX_DAYS} 天，{reason}，建议到期离场移除"
                    flag = "📅"
        elif days >= QUIT_DAYS and ret < 3:
            advice = f"⚠️ 持有 {days} 天仍无进展（{ret:+.1f}%），横盘离场，不再占用跟踪池"
            flag = "⚠️"
        elif ret >= 3:
            advice = f"✅ 盈利 +{ret:.1f}%（>3%），移动止盈上移至成本线 {base:.2f}，跌破离场；目标 +{tp1r*100:.0f}%/+{tp2r*100:.0f}%（{target_tp1:.2f}/{target_tp2:.2f}）"
            flag = "✅"
        elif ret < 0:
            advice = f"⚠️ 浮亏 {ret:.1f}%，持有观察；止损位 {sl:.2f} 必须挂上"
            flag = "⚠️"
        else:
            advice = f"✅ 微利 {ret:.1f}%，持有；回踩 {base*0.97:.2f} 附近可补，破成本减半"
            flag = "✅"

        analyzed.append({
            "名称": name, "代码": code, "推荐日": r["日期"],
            "持有天数": days, "基准价": base, "现价": price,
            "当日涨跌%": q.get("涨跌幅"), "累计盈亏%": round(ret, 2),
            "最高收益%": round(peak, 2), "状态": r["状态"], "展期": extended,
            "止损价": sl, "止盈1": target_tp1, "止盈2": target_tp2,
            "更新止损": round(ref_sl, 2), "更新止盈1": round(target_tp1, 2), "更新止盈2": round(target_tp2, 2),
            "建议": advice, "标记": flag,
        })

    overview = {
        "总只数": len(analyzed),
        "总盈亏": round(total_pnl, 1),
        "平均盈亏": round(total_pnl / len(analyzed), 1) if analyzed else 0,
        "破止损": stop_count,
        "展期数": ext_num,
        "建议": "请优先处理破止损/到期/止盈票" if (stop_count or any(r["建议"].startswith("💰") or r["建议"].startswith("📅") or r["建议"].startswith("⛔") for r in analyzed)) else "跟踪池整体正常",
    }
    return analyzed, overview


def stats():
    """累计统计：总推荐/结清/胜率/平均峰值"""
    rows = _read_rows()
    if not rows:
        return "台账为空"
    closed = [r for r in rows if r["状态"] in ("止盈2清仓", "止盈1清仓", "止盈清仓", "止损出局", "到期离场")]
    # 胜率：结清且为盈利单（止盈类 / 到期且正收益 / 备注含"+"收益）
    win = [r for r in closed if r["状态"] in ("止盈2清仓", "止盈1清仓", "止盈清仓") or
           (r["状态"] == "到期离场" and "+" in r["备注"].split("收益")[-1])]
    hi_list = [float(r["最高收益%"] or 0) for r in rows if r["最高收益%"]]
    return {
        "累计推荐": len(rows),
        "持有中": len([r for r in rows if r["状态"] in OPEN_STATUS]),
        "已结清": len(closed),
        "胜率": f"{len(win)/len(closed)*100:.0f}%" if closed else "-",
        "平均峰值": f"{sum(hi_list)/len(hi_list):+.1f}%" if hi_list else "-",
    }
