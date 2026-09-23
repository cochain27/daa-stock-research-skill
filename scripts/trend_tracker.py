# -*- coding: utf-8 -*-
"""右侧趋势跟踪模块（2026-09-05 双策略分立）
- log_picks(): 趋势票推荐落账（data/推荐台账_右侧趋势.csv）
- update_track(): 收盘更新趋势票状态
- analyze_track_pool(): 趋势跟踪池分析
- stats(): 趋势策略累计统计
- virtual_nav(): 趋势策略虚拟净值
"""
import csv
from datetime import datetime
from pathlib import Path
import pandas as pd
from fetch_data import get_realtime_quotes
from config import (
    TRACK_TREND_PATH, TREND_STOP_LOSS,
    EXTEND_PEAK_DRAWBACK,
    TREND_HOLD_DAYS, TREND_EXTEND_MAX_DAYS, TREND_EXTEND_MAX_COUNT,
    VIRTUAL_ENABLED, VIRTUAL_BASE, ALLOW_CODE_PREFIX,
)
from tracker_common import (
    _read_rows, _write_rows, _vtrade_read, _vtrade_write,
    record_settlement, tech_break, extend_decision, virtual_nav_by_tag,
    check_virtual_settled,
)

OPEN_STATUS = ("持有中", "止盈1减半", "展期中")  # "止盈1减半"保留以兼容历史台账，变体B不再新产生该状态
STRATEGY_TAG = "趋势"
SL = TREND_STOP_LOSS                # -6% 硬止损（变体B：保留）
MAX_DAYS = TREND_HOLD_DAYS[1]       # 28 天到期/展期触发（变体B，原误用 HOLD_DAYS_MAX=10）
EXT_MAX = TREND_EXTEND_MAX_DAYS     # 60 天展期硬上限

# 趋势策略展期配置（变体B：满 28 天趋势完好可展期至 60 天，展期后无固定止盈，继续 MA10 跟踪）
TREND_EXTEND_CFG = {
    "HOLD_DAYS_MAX": TREND_HOLD_DAYS[1],
    "EXTEND_MAX_DAYS": TREND_EXTEND_MAX_DAYS,
    "EXTEND_MAX_COUNT": TREND_EXTEND_MAX_COUNT,
    "EXTEND_PEAK_DRAWBACK": EXTEND_PEAK_DRAWBACK,
}


def _ledger_fields():
    return ["日期", "代码", "名称", "自研评分", "策略标签", "验证评分", "基准价", "买区", "止损",
            "止盈1", "止盈2", "虚拟成本", "虚拟建仓日", "虚拟建仓状态", "状态", "最高收益%",
            "最后更新", "备注", "横盘天数", "突破类型", "趋势得分",
            "移动止损", "持仓上限天", "持仓展期天", "历史来源"]


def check_and_init():
    """确保趋势台账存在且字段完整"""
    p = Path(TRACK_TREND_PATH)
    if not p.exists():
        with open(p, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=_ledger_fields())
            w.writeheader()


def log_picks(picks, market_env=None):
    """趋势票推荐落账（同日同票不重复）；推荐即虚拟成交"""
    check_and_init()
    today = datetime.now().strftime("%Y-%m-%d")
    rows = _read_rows(TRACK_TREND_PATH)
    existing = {(r["日期"], r["代码"]) for r in rows}
    added = 0
    for p in picks:
        code = str(p.get("symbol", ""))
        if (today, code) in existing or not code:
            continue
        if not code.startswith(ALLOW_CODE_PREFIX):
            continue
        b = p.get("buy", {})
        base = b.get("基准价") or p.get("现价") or ""
        cv = p.get("交叉验证") or {}
        rows.append({
            "日期": today, "代码": code, "名称": p.get("名称", ""),
            "自研评分": f"{p.get('total', 0):.0f}",
            "策略标签": STRATEGY_TAG,
            "验证评分": f"{cv.get('score') or ''}",
            "基准价": base, "买区": b.get("建议买价区间", ""), "止损": b.get("止损价", ""),
            "止盈1": b.get("止盈1", ""), "止盈2": b.get("止盈2", ""),
            "虚拟成本": f"{float(base):.2f}" if base else "",
            "虚拟建仓日": today, "虚拟建仓状态": "已建仓",
            "状态": "持有中", "最高收益%": "0", "最后更新": today, "备注": "",
            "横盘天数": str(p.get("横盘天数", "")),
            "突破类型": p.get("突破类型", ""),
            "趋势得分": str(p.get("趋势得分", "")),
        })
        added += 1
    if added:
        _write_rows(TRACK_TREND_PATH, rows, _ledger_fields())
    return added


def update_track():
    """收盘更新趋势跟踪票状态：止盈/止损/到期/展期/技术面"""
    check_and_init()
    today = datetime.now().strftime("%Y-%m-%d")
    rows = _read_rows(TRACK_TREND_PATH)
    open_rows = [r for r in rows if r["状态"] in OPEN_STATUS]
    if not open_rows:
        return 0, "无未结清趋势票"
    quotes = get_realtime_quotes([r["代码"] for r in open_rows])
    ext_count = len([r for r in open_rows
                      if r["状态"] == "展期中" or "展期" in (r.get("备注") or "")])
    updated = 0
    vtrade = _vtrade_read()

    for r in open_rows:
        q = quotes.get(r["代码"])
        if not q:
            continue
        price = q["现价"]
        if q.get("最高", -1) == 0 or price <= 0:
            continue  # 停牌
        is_extended = r["状态"] == "展期中" or "展期" in (r.get("备注") or "")

        try:
            base = float(r["基准价"])
            sl = float(r["止损"]) if r["止损"] else base * (1 + SL)
        except (ValueError, TypeError):
            continue
        ret = (price / base - 1) * 100
        peak = max(float(r["最高收益%"] or 0), ret)
        hi = max(float(r["最高收益%"] or 0), ret)
        r["最高收益%"] = str(round(hi, 2))
        r["最后更新"] = today

        days = (datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(r["日期"], "%Y-%m-%d")).days

        # 技术面破坏检测（第2天起）：变体B = 破MA20结构止损 + 破MA10移动止盈（无盈利门槛）
        tech_broken, tech_reason = tech_break(r, price, mode="trend") if days >= 1 else (False, "")

        # 变体B出场优先级：①-6%止损 ②技术面(MA20/MA10) ③展期硬上限 ④满28天展期评估
        if price <= sl:
            r["状态"] = "止损出局"
            r["备注"] = f"触发{int(abs(SL)*100)}%止损(峰值{hi:+.1f}%)"
            record_settlement(r, vtrade, price, "止损出局")
        # 技术面破坏
        elif tech_broken:
            r["状态"] = "技术离场"
            r["备注"] = f"技术面破坏:{tech_reason}(收益{ret:+.1f}%)"
            record_settlement(r, vtrade, price, "技术面破坏离场")
        # 展期硬上限
        elif is_extended and days >= EXT_MAX:
            r["状态"] = "到期离场"
            r["备注"] = f"满{EXT_MAX}天展期硬上限,收益{ret:+.1f}%"
            record_settlement(r, vtrade, price, "展期到期离场")
        # 满目标天数 → 展期评估
        elif days >= MAX_DAYS and not is_extended:
            can_ext, reason = extend_decision(r, price, ret, peak, ext_count, TREND_EXTEND_CFG)
            if can_ext:
                r["状态"] = "展期中"
                r["备注"] = f"展期:{reason}"
                ext_count += 1
            else:
                r["状态"] = "到期离场"
                r["备注"] = f"满{MAX_DAYS}天,{reason},收益{ret:+.1f}%"
                record_settlement(r, vtrade, price, "到期离场")
        updated += 1

    _write_rows(TRACK_TREND_PATH, rows, _ledger_fields())
    _vtrade_write(vtrade)
    return updated, f"更新{updated}条"


def load_open_picks():
    check_and_init()
    rows = _read_rows(TRACK_TREND_PATH)
    return [r for r in rows if r["状态"] in OPEN_STATUS]


def analyze_track_pool():
    """趋势跟踪池分析"""
    check_and_init()
    today = datetime.now().strftime("%Y-%m-%d")
    rows = load_open_picks()
    if not rows:
        return [], {"总只数": 0, "总盈亏": 0, "平均盈亏": 0, "破止损": 0,
                    "展期数": 0, "建议": "趋势跟踪池为空", "虚拟净值": None}

    quotes = get_realtime_quotes([r["代码"] for r in rows])
    ext_count = len([r for r in rows if r["状态"] == "展期中" or "展期" in (r.get("备注") or "")])
    analyzed = []
    stop_count = 0
    ext_num = 0
    total_pnl = 0.0

    for r in rows:
        code = r["代码"]
        q = quotes.get(code, {})
        price = q.get("现价", 0)
        name = r["名称"]
        is_extended = r["状态"] == "展期中" or "展期" in (r.get("备注") or "")

        try:
            base = float(r["基准价"])
            sl = float(r["止损"]) if r["止损"] else base * (1 + SL)
        except (ValueError, TypeError):
            continue

        days = (datetime.strptime(r["最后更新"], "%Y-%m-%d") - datetime.strptime(r["日期"], "%Y-%m-%d")).days

        if q.get("最高", -1) == 0:
            analyzed.append({
                "名称": name, "代码": code, "推荐日": r["日期"], "策略标签": STRATEGY_TAG,
                "持有天数": days, "基准价": base, "现价": price,
                "当日涨跌%": 0, "累计盈亏%": 0, "最高收益%": 0,
                "状态": "停牌中", "展期": False,
                "止损价": 0, "止盈1": 0, "止盈2": 0,
                "建议": "⛔ 停牌中，暂停跟踪操作建议", "标记": "⛔",
            })
            continue

        if price <= 0:
            continue

        ret = (price / base - 1) * 100
        peak = max(float(r["最高收益%"] or 0), ret)
        total_pnl += ret

        tech_broken, tech_reason = tech_break(r, price, mode="trend") if days >= 1 else (False, "")

        advice = ""
        flag = "✅"
        if price <= sl:
            advice = f"⛔ 已破{int(abs(SL)*100)}%止损 {sl:.2f}，无条件移除"
            flag = "⛔"
            stop_count += 1
        elif tech_broken:
            advice = f"⛔ 技术面破坏：{tech_reason}，剔除跟踪池"
            flag = "⛔"
            stop_count += 1
        elif is_extended and days >= EXT_MAX:
            advice = f"⛔ 满 {EXT_MAX} 天展期硬上限，强制离场（收益 {ret:+.1f}%）"
            flag = "⛔"
        elif days >= MAX_DAYS and not is_extended:
            can_ext, reason = extend_decision(r, price, ret, peak, ext_count, TREND_EXTEND_CFG)
            if can_ext:
                advice = f"📅 满 {MAX_DAYS} 天，{reason}；展期后继续 MA10 跟踪，最长 {EXT_MAX} 天"
                flag = "🟢"
            else:
                advice = f"📅 满 {MAX_DAYS} 天，{reason}，建议到期离场移除"
                flag = "📅"
        elif ret >= 0:
            advice = f"✅ 持有 {ret:+.1f}%，破 MA10 移动止盈离场、破 MA20 结构止损（变体B让利润奔跑）"
            flag = "✅"
        else:
            advice = f"⚠️ 浮亏 {ret:.1f}%，止损位 {sl:.2f} 必须挂上；破 MA10/MA20 离场"
            flag = "⚠️"

        analyzed.append({
            "名称": name, "代码": code, "推荐日": r["日期"], "策略标签": STRATEGY_TAG,
            "持有天数": days, "基准价": base, "现价": price,
            "当日涨跌%": q.get("涨跌幅"), "累计盈亏%": round(ret, 2),
            "最高收益%": round(peak, 2), "状态": r["状态"], "展期": is_extended,
            "止损价": sl, "止盈1": "", "止盈2": "",
            "更新止损": sl, "更新止盈1": "", "更新止盈2": "",
            "建议": advice, "标记": flag,
            "横盘天数": r.get("横盘天数", ""),
            "突破类型": r.get("突破类型", ""),
            "趋势得分": r.get("趋势得分", ""),
        })
        if is_extended:
            ext_num += 1

    overview = {
        "总只数": len(analyzed),
        "总盈亏": round(total_pnl, 1),
        "平均盈亏": round(total_pnl / len(analyzed), 1) if analyzed else 0,
        "破止损": stop_count,
        "展期数": ext_num,
        "建议": "请优先处理破止损/到期/止盈票" if (stop_count or any(r["建议"].startswith(x) for x in ["💰", "📅", "⛔"] for r in analyzed)) else "趋势跟踪池整体正常",
        "虚拟净值": virtual_nav_by_tag(TRACK_TREND_PATH, STRATEGY_TAG) if VIRTUAL_ENABLED else None,
    }
    return analyzed, overview


def stats():
    """趋势策略累计统计"""
    check_and_init()
    rows = _read_rows(TRACK_TREND_PATH)
    nav = virtual_nav_by_tag(TRACK_TREND_PATH, STRATEGY_TAG) if VIRTUAL_ENABLED else None
    if not rows:
        return {"策略": "右侧趋势", "累计推荐": 0, "持有中": 0, "已结清": 0,
                "胜率": "-", "平均峰值": "-", "虚拟净值": nav}

    closed = [r for r in rows if r["状态"] in
              ("止盈2清仓", "止盈1清仓", "止盈清仓", "止损出局", "技术离场", "到期离场")]

    def _wr(records):
        if not records:
            return "-"
        win = [r for r in records if r["状态"] in ("止盈2清仓", "止盈1清仓", "止盈清仓") or
               (r["状态"] == "到期离场" and "+" in r.get("备注", "").split("收益")[-1]) or
               (r["状态"] == "技术离场" and "+" in r.get("备注", "").split("收益")[-1])]
        return f"{len(win)/len(records)*100:.0f}%"

    hi_list = [float(r["最高收益%"] or 0) for r in rows if r["最高收益%"]]
    return {
        "策略": "右侧趋势",
        "累计推荐": len(rows),
        "持有中": len([r for r in rows if r["状态"] in OPEN_STATUS]),
        "已结清": len(closed),
        "胜率": _wr(closed),
        "平均峰值": f"{sum(hi_list)/len(hi_list):+.1f}%" if hi_list else "-",
        "虚拟净值": nav,
    }
