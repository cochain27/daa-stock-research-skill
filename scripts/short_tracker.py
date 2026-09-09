# -*- coding: utf-8 -*-
"""短线激进跟踪模块（2026-09-05 双策略分立）
- log_picks(): 短线票推荐落账（data/推荐台账_短线激进.csv）
- update_track(): 收盘更新短线票状态（3天持股上限）
- analyze_track_pool(): 短线跟踪池分析（含每日情绪分重评淘汰）
- roll_backtest(): 结算待回测短线票（第3天开盘价卖出）
- stats(): 短线策略累计统计
- virtual_nav(): 短线策略虚拟净值
"""
import csv
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd
from fetch_data import get_realtime_quotes, get_stock_hist
from config import (
    TRACK_SHORT_PATH, SHORT_BACKTEST_PATH,
    SHORT_STOP_LOSS, SHORT_TAKE_PROFIT_1, SHORT_TAKE_PROFIT_2,
    SHORT_HOLD_DAYS_MAX, SHORT_TIME_STOP_DAYS, VIRTUAL_ENABLED, ALLOW_CODE_PREFIX,
)
from tracker_common import (
    _read_rows, _write_rows, _vtrade_read, _vtrade_write,
    record_settlement, tech_break, virtual_nav_by_tag,
    check_virtual_settled,
)

OPEN_STATUS = ("持有中", "止盈1减半")
STRATEGY_TAG = "短线"
SHORT_SL = SHORT_STOP_LOSS
SHORT_TP1 = SHORT_TAKE_PROFIT_1
SHORT_TP2 = SHORT_TAKE_PROFIT_2
SHORT_MAX = SHORT_HOLD_DAYS_MAX


def _short_ledger_fields():
    return ["日期", "代码", "名称", "自研评分", "策略标签", "验证评分", "基准价", "买区", "止损",
            "止盈1", "止盈2", "虚拟成本", "虚拟建仓日", "虚拟建仓状态", "状态", "最高收益%",
            "最后更新", "备注", "情绪分", "连板数", "炸板次数", "候选来源",
            "开盘买入价", "开盘卖出价", "回测盈亏%"]


def _backtest_fields():
    return ["买入日", "代码", "名称", "买入价(开盘价)", "卖出日", "卖出价(第三天开盘价)",
            "持有天数", "盈亏%", "盈亏额", "状态", "备注"]


def check_and_init():
    """确保短线台账和回测台账存在"""
    sp = Path(TRACK_SHORT_PATH)
    if not sp.exists():
        with open(sp, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=_short_ledger_fields())
            w.writeheader()
    bp = Path(SHORT_BACKTEST_PATH)
    if not bp.exists():
        with open(bp, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=_backtest_fields())
            w.writeheader()


def _get_open_price(code, date_str):
    """获取指定日期的开盘价；若当日停牌/无数据，顺延至下一交易日"""
    df = get_stock_hist(code, days=60)
    if df is None or df.empty:
        return None, None
    df = df.sort_values("日期").reset_index(drop=True)
    df["日期_dt"] = pd.to_datetime(df["日期"])
    target = pd.to_datetime(date_str)
    sub = df[df["日期_dt"] >= target]
    if sub.empty:
        return None, None
    row = sub.iloc[0]
    return row["开盘"], row["日期"]


def _next_trading_day(date_str, offset):
    """从 date_str 起第 offset 个交易日的日期字符串"""
    df = get_stock_hist("000001", days=120)
    if df is None or df.empty:
        return None
    df = df.sort_values("日期").reset_index(drop=True)
    df["日期_dt"] = pd.to_datetime(df["日期"])
    target = pd.to_datetime(date_str)
    sub = df[df["日期_dt"] > target]
    if len(sub) < offset:
        return None
    return sub.iloc[offset - 1]["日期"]


def log_picks(picks, market_env=None):
    """短线票推荐落账；推荐即虚拟成交；同步写入回测台账（待结算状态）"""
    check_and_init()
    today = datetime.now().strftime("%Y-%m-%d")
    rows = _read_rows(TRACK_SHORT_PATH)
    existing = {(r["日期"], r["代码"]) for r in rows}
    added = 0

    # 尝试获取今日开盘价
    today_open = None
    if picks:
        sample_code = picks[0].get("symbol", "")
        if sample_code:
            op, _ = _get_open_price(sample_code, today)
            today_open = op

    for p in picks:
        code = str(p.get("symbol", ""))
        if (today, code) in existing or not code:
            continue
        if not code.startswith(ALLOW_CODE_PREFIX):
            continue
        b = p.get("buy", {})
        base = b.get("基准价") or p.get("现价") or ""
        cv = p.get("交叉验证") or {}

        # 尝试获取今日开盘价（若今日K线已生成）
        open_price_today, _ = _get_open_price(code, today)
        if open_price_today is None:
            open_price_today = float(base) if base else None  # 降级为推荐价

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
            "情绪分": str(p.get("情绪分", "")),
            "连板数": str(p.get("连板数", "")),
            "炸板次数": str(p.get("炸板次数", "")),
            "候选来源": p.get("候选来源", ""),
            "开盘买入价": f"{open_price_today:.2f}" if open_price_today else "",
            "开盘卖出价": "",
            "回测盈亏%": "",
        })
        added += 1

        # 同步写入回测台账
        _append_backtest({
            "买入日": today,
            "代码": code,
            "名称": p.get("名称", ""),
            "买入价(开盘价)": f"{open_price_today:.2f}" if open_price_today else f"{float(base):.2f}" if base else "",
            "卖出日": "",
            "卖出价(第三天开盘价)": "",
            "持有天数": "",
            "盈亏%": "",
            "盈亏额": "",
            "状态": "待结算",
            "备注": "",
        })

    if added:
        _write_rows(TRACK_SHORT_PATH, rows, _short_ledger_fields())
    return added


def roll_backtest():
    """收盘/次日早盘：结算所有"待结算"回测记录，获取T+2开盘价，计算3天回测盈亏"""
    check_and_init()
    today = datetime.now().strftime("%Y-%m-%d")
    rows = _read_backtest()
    updated = 0
    for r in rows:
        if r["状态"] != "待结算":
            continue
        buy_day = r["买入日"]
        sell_day = _next_trading_day(buy_day, 3)
        if sell_day is None:
            continue
        sell_open, actual_sell_date = _get_open_price(r["代码"], sell_day)
        if sell_open is None:
            continue
        try:
            buy_price = float(r["买入价(开盘价)"])
        except (ValueError, TypeError):
            buy_price = None
        if buy_price is None or buy_price <= 0:
            continue
        ret_pct = (sell_open / buy_price - 1) * 100
        hold_days = (datetime.strptime(actual_sell_date, "%Y-%m-%d") -
                     datetime.strptime(buy_day, "%Y-%m-%d")).days
        pnl = 10000 * ret_pct / 100
        r["卖出日"] = actual_sell_date
        r["卖出价(第三天开盘价)"] = f"{sell_open:.2f}"
        r["持有天数"] = str(hold_days)
        r["盈亏%"] = f"{ret_pct:.2f}"
        r["盈亏额"] = f"{pnl:.0f}"
        r["状态"] = "已结算"
        updated += 1

    if updated:
        _write_backtest(rows)
    return updated, f"结算{updated}笔3日回测"


def update_track():
    """收盘更新短线跟踪票状态（含每日情绪分重评淘汰）"""
    check_and_init()
    today = datetime.now().strftime("%Y-%m-%d")
    rows = _read_rows(TRACK_SHORT_PATH)
    open_rows = [r for r in rows if r["状态"] in OPEN_STATUS]
    if not open_rows:
        return 0, "无未结清短线票"
    quotes = get_realtime_quotes([r["代码"] for r in open_rows])
    updated = 0
    vtrade = _vtrade_read()

    for r in open_rows:
        q = quotes.get(r["代码"])
        if not q:
            continue
        price = q["现价"]
        if q.get("最高", -1) == 0 or price <= 0:
            continue  # 停牌
        try:
            base = float(r["基准价"])
            sl = float(r["止损"]) if r["止损"] else base * (1 + SHORT_SL)
        except (ValueError, TypeError):
            continue
        ret = (price / base - 1) * 100
        peak = max(float(r["最高收益%"] or 0), ret)
        hi = max(float(r["最高收益%"] or 0), ret)
        r["最高收益%"] = str(round(hi, 2))
        r["最后更新"] = today

        tp1_target = base * (1 + SHORT_TP1)
        tp2_target = base * (1 + SHORT_TP2)
        days = (datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(r["日期"], "%Y-%m-%d")).days

        # 技术面破坏检测（第2天起）
        tech_broken, tech_reason = tech_break(r, price, mode="short") if days >= 1 else (False, "")

        # 止损
        if price <= sl:
            r["状态"] = "止损出局"
            r["备注"] = f"触发{int(abs(SHORT_SL)*100)}%止损(峰值{hi:+.1f}%)"
            record_settlement(r, vtrade, price, "止损出局")
        # 技术面破坏（含移动止盈：盈利>3%跌破MA10清仓）
        elif tech_broken:
            r["状态"] = "技术离场"
            r["备注"] = f"技术面破坏:{tech_reason}(收益{ret:+.1f}%)"
            record_settlement(r, vtrade, price, "技术面破坏离场")
        # 止盈1（减半后剩余仓位走移动止损，不再 +8% 一次性清仓——2026-09-09 改造）
        elif price >= tp1_target:
            if r["状态"] != "止盈1减半":
                r["状态"] = "止盈1减半"
                r["备注"] = f"达+{SHORT_TP1*100:.0f}%减半仓，剩余仓位移动止损(MA10)"
            else:
                r["备注"] = f"减半后持有中(峰值{hi:+.1f}%)"
        # 时间止损（2026-09-09：持有≥3天仍浮亏，等待成本高于快刀）
        elif SHORT_TIME_STOP_DAYS > 0 and days >= SHORT_TIME_STOP_DAYS and ret < 0:
            r["状态"] = "时间止损"
            r["备注"] = f"满{SHORT_TIME_STOP_DAYS}天仍浮亏{ret:+.1f}%，时间止损"
            record_settlement(r, vtrade, price, "时间止损")
        # 3天硬上限（短线持股不超过1周，按回测逻辑3天了结）
        elif days >= SHORT_MAX:
            r["状态"] = "到期离场"
            r["备注"] = f"短线满{SHORT_MAX}天硬上限,收益{ret:+.1f}%"
            record_settlement(r, vtrade, price, "短线到期离场")
        updated += 1

    _write_rows(TRACK_SHORT_PATH, rows, _short_ledger_fields())
    _vtrade_write(vtrade)
    return updated, f"更新{updated}条"


def load_open_picks():
    check_and_init()
    rows = _read_rows(TRACK_SHORT_PATH)
    return [r for r in rows if r["状态"] in OPEN_STATUS]


def analyze_track_pool():
    """短线跟踪池分析（含每日情绪分重评，低于及格线剔除）"""
    check_and_init()
    today = datetime.now().strftime("%Y-%m-%d")
    rows = load_open_picks()
    if not rows:
        return [], {"总只数": 0, "总盈亏": 0, "平均盈亏": 0, "破止损": 0,
                    "建议": "短线跟踪池为空", "虚拟净值": None}

    quotes = get_realtime_quotes([r["代码"] for r in rows])
    analyzed = []
    stop_count = 0
    total_pnl = 0.0

    for r in rows:
        code = r["代码"]
        q = quotes.get(code, {})
        price = q.get("现价", 0)
        name = r["名称"]

        try:
            base = float(r["基准价"])
            sl = float(r["止损"]) if r["止损"] else base * (1 + SHORT_SL)
        except (ValueError, TypeError):
            continue

        days = (datetime.strptime(r["最后更新"], "%Y-%m-%d") - datetime.strptime(r["日期"], "%Y-%m-%d")).days

        if q.get("最高", -1) == 0:
            analyzed.append({
                "名称": name, "代码": code, "推荐日": r["日期"], "策略标签": STRATEGY_TAG,
                "持有天数": days, "基准价": base, "现价": price,
                "当日涨跌%": 0, "累计盈亏%": 0, "最高收益%": 0,
                "状态": "停牌中",
                "建议": "⛔ 停牌中，暂停跟踪", "标记": "⛔",
            })
            continue

        if price <= 0:
            continue

        ret = (price / base - 1) * 100
        peak = max(float(r["最高收益%"] or 0), ret)
        total_pnl += ret

        tech_broken, tech_reason = tech_break(r, price, mode="short") if days >= 1 else (False, "")
        target_tp1 = round(base * (1 + SHORT_TP1), 2)
        target_tp2 = round(base * (1 + SHORT_TP2), 2)

        advice = ""
        flag = "✅"
        if price <= sl:
            advice = f"⛔ 已破{int(abs(SHORT_SL)*100)}%止损 {sl:.2f}，无条件移除"
            flag = "⛔"
            stop_count += 1
        elif tech_broken:
            advice = f"⛔ 技术面破坏：{tech_reason}，剔除跟踪池"
            flag = "⛔"
            stop_count += 1
        elif price >= target_tp2:
            advice = f"💰 达止盈2 {target_tp2:.2f}（+{SHORT_TP2*100:.0f}%），清仓移除"
            flag = "💰"
        elif price >= target_tp1:
            advice = f"💰 达止盈1 {target_tp1:.2f}（+{SHORT_TP1*100:.0f}%），建议减半仓"
            flag = "💰"
        elif days >= SHORT_MAX:
            advice = f"⛔ 短线满{SHORT_MAX}天，强制离场（收益 {ret:+.1f}%）"
            flag = "⛔"
        elif ret >= 2:
            advice = f"✅ 盈利 +{ret:.1f}%，止损上移至成本线；目标 +{SHORT_TP1*100:.0f}%/+{SHORT_TP2*100:.0f}%"
            flag = "✅"
        elif ret < 0:
            advice = f"⚠️ 浮亏 {ret:.1f}%，止损位 {sl:.2f} 必须挂上"
            flag = "⚠️"
        else:
            advice = f"✅ 微利 {ret:.1f}%，持有"
            flag = "✅"

        analyzed.append({
            "名称": name, "代码": code, "推荐日": r["日期"], "策略标签": STRATEGY_TAG,
            "持有天数": days, "基准价": base, "现价": price,
            "当日涨跌%": q.get("涨跌幅"), "累计盈亏%": round(ret, 2),
            "最高收益%": round(peak, 2), "状态": r["状态"],
            "止损价": sl, "止盈1": target_tp1, "止盈2": target_tp2,
            "更新止损": sl, "更新止盈1": target_tp1, "更新止盈2": target_tp2,
            "建议": advice, "标记": flag,
            "情绪分": r.get("情绪分", ""),
            "连板数": r.get("连板数", ""),
            "候选来源": r.get("候选来源", ""),
        })

    overview = {
        "总只数": len(analyzed),
        "总盈亏": round(total_pnl, 1),
        "平均盈亏": round(total_pnl / len(analyzed), 1) if analyzed else 0,
        "破止损": stop_count,
        "建议": "请优先处理破止损/到期/止盈票" if stop_count else "短线跟踪池整体正常",
        "虚拟净值": virtual_nav_by_tag(TRACK_SHORT_PATH, STRATEGY_TAG) if VIRTUAL_ENABLED else None,
    }
    return analyzed, overview


def stats():
    """短线策略累计统计 + 回测汇总"""
    check_and_init()
    rows = _read_rows(TRACK_SHORT_PATH)
    bk_rows = _read_backtest()
    nav = virtual_nav_by_tag(TRACK_SHORT_PATH, STRATEGY_TAG) if VIRTUAL_ENABLED else None

    if not rows:
        return {"策略": "短线激进", "累计推荐": 0, "持有中": 0, "已结清": 0,
                "胜率": "-", "平均峰值": "-", "虚拟净值": nav,
                "回测笔数": 0, "回测胜率": "-", "回测平均盈亏": "-"}

    closed = [r for r in rows if r["状态"] in
              ("止盈2清仓", "止盈1清仓", "止盈清仓", "止损出局", "技术离场", "到期离场", "时间止损")]

    def _wr(records):
        if not records:
            return "-"
        win = [r for r in records if r["状态"] in ("止盈2清仓", "止盈1清仓", "止盈清仓") or
               (r["状态"] in ("到期离场", "技术离场", "时间止损") and
                "+" in r.get("备注", "").split("收益")[-1])]
        return f"{len(win)/len(records)*100:.0f}%"

    hi_list = [float(r["最高收益%"] or 0) for r in rows if r["最高收益%"]]

    # 回测统计
    settled_bk = [r for r in bk_rows if r["状态"] == "已结算"]
    bk_win = [r for r in settled_bk if float(r["盈亏%"]) > 0] if settled_bk else []
    bk_ret_list = [float(r["盈亏%"]) for r in settled_bk] if settled_bk else []
    bk_stats = {
        "回测笔数": len(settled_bk),
        "回测胜率": f"{len(bk_win)/len(settled_bk)*100:.0f}%" if settled_bk else "-",
        "回测平均盈亏": f"{sum(bk_ret_list)/len(bk_ret_list):+.2f}%" if bk_ret_list else "-",
    }

    return {
        "策略": "短线激进",
        "累计推荐": len(rows),
        "持有中": len([r for r in rows if r["状态"] in OPEN_STATUS]),
        "已结清": len(closed),
        "胜率": _wr(closed),
        "平均峰值": f"{sum(hi_list)/len(hi_list):+.1f}%" if hi_list else "-",
        "虚拟净值": nav,
        **bk_stats,
    }


# ===== 回测台账读写 =====

def _read_backtest():
    if not Path(SHORT_BACKTEST_PATH).exists():
        return []
    with open(SHORT_BACKTEST_PATH, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _write_backtest(rows):
    with open(SHORT_BACKTEST_PATH, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_backtest_fields())
        w.writeheader()
        w.writerows(rows)


def _append_backtest(record):
    rows = _read_backtest()
    rows.append(record)
    _write_backtest(rows)
