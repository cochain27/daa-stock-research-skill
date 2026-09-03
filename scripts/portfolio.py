# -*- coding: utf-8 -*-
"""持仓分析模块：读取持仓 → 拉实时价 → 生成操作建议"""
import json
from pathlib import Path
from fetch_data import get_realtime_quotes

HOLDINGS_PATH = Path(__file__).resolve().parent.parent / "data" / "holdings.json"

# 风控参数（与核心策略一致，2026-09-02 校准为 1-2 周波段）
STOP_LOSS = -0.06      # 止损 -6%
TP1 = 0.06             # 止盈1 +6% 减半
TP2 = 0.10             # 止盈2 +10% 清仓
MOVE_UP = 0.03         # 盈利>3% 上移止损至成本线
SINGLE_MAX = 0.30      # 单票上限 30%


def load_holdings():
    if not HOLDINGS_PATH.exists():
        return {"持仓": [], "可用资金": 0.0}
    return json.loads(HOLDINGS_PATH.read_text(encoding="utf-8"))


def analyze_holdings():
    """返回 (rows, 总览)。rows: 每只票的实时盈亏+操作建议"""
    data = load_holdings()
    holds = data.get("持仓", [])
    cash = float(data.get("可用资金", 0))
    quotes = get_realtime_quotes([h["代码"] for h in holds]) if holds else {}

    rows = []
    total_mv = 0.0
    total_cost = 0.0
    for h in holds:
        code = str(h["代码"])
        q = quotes.get(code, {})
        price = q.get("现价")
        chg = q.get("涨跌幅")
        cost = float(h["成本价"])
        qty = int(h["股数"])
        mv = price * qty if price else cost * qty
        cost_sum = cost * qty
        pnl = (price - cost) * qty if price else 0
        pnl_pct = (price / cost - 1) * 100 if price else 0
        total_mv += mv
        total_cost += cost_sum

        # 操作建议（1-2周波段规则：止损-6% / 止盈+6%/+10% / 盈利>3%上移成本线）
        stop_price = round(cost * (1 + STOP_LOSS), 2)
        tp1_price = round(cost * (1 + TP1), 2)
        tp2_price = round(cost * (1 + TP2), 2)
        if pnl_pct <= -6:
            advice = f"⛔ 已破止损线（-6%），无条件离场，参考价 {stop_price}"
        elif pnl_pct < 0:
            advice = f"⚠️ 浮亏中，持有观察；条件单止损 {stop_price}（-6%）必须挂上"
        elif pnl_pct < MOVE_UP * 100:
            advice = f"✅ 持有；若回破成本价 {cost:.2f} 减半，止损上移至成本线"
        elif pnl_pct < TP1 * 100:
            advice = f"✅ 持有，盈利>3%后跌破MA10移动止盈"
        elif pnl_pct < TP2 * 100:
            advice = f"💰 达止盈1（+6%），减半仓锁定利润 {tp1_price}，余仓看 {tp2_price}"
        else:
            advice = f"💰 达止盈2（+10%），清仓离场，参考价 {tp2_price}"

        rows.append({
            "名称": h["名称"], "代码": code, "股数": qty, "成本": cost,
            "现价": price, "当日涨跌%": chg, "浮动盈亏": round(pnl, 0),
            "盈亏%": round(pnl_pct, 2), "建议": advice,
        })

    total_asset = total_mv + cash
    overview = {
        "总市值": round(total_mv, 2), "总成本": round(total_cost, 2),
        "总浮动盈亏": round(total_mv - total_cost, 2),
        "可用资金": cash, "总资产": round(total_asset, 2),
        "仓位%": round(total_mv / total_asset * 100, 1) if total_asset else 0,
    }
    return rows, overview
