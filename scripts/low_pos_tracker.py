# -*- coding: utf-8 -*-
"""低位触发跟踪模块（2026-10-20 用户拍板新增第 3 台账）
- register_fired(): 低位观察池票触发启动 → 「推荐即虚拟成交」入账（幂等）
- update_track():   收盘体检：+15% 减半 / +25% 清仓 / 破 MA5 离场 / -8% 止损 / 最长 T+30 自动离场
- stats():          低位触发策略累计统计 + 虚拟净值（计入累计表现与虚拟盘）
- _invalidate_watch(): 结算后同步把 watch_history 对应票标「已失效」，从低位追踪池移除
"""
import csv
from datetime import datetime
from pathlib import Path

import os as _os
# 2026-10-10 补跑支持：DAA_REPORT_DATE 指定报告日期（台账/体检日期随动）
TODAY = _os.environ.get("DAA_REPORT_DATE") or datetime.now().strftime("%Y-%m-%d")

import pandas as pd
from fetch_data import get_realtime_quotes, get_stock_hist
from config import (
    TRACK_LOWPOS_PATH, LOWPOS_TAG, VIRTUAL_ENABLED, ALLOW_CODE_PREFIX,
    LOW_POS_ENTRY_FIRED_TP_HALF, LOW_POS_ENTRY_FIRED_TP_FULL,
    LOW_POS_ENTRY_FIRED_TRAIL_MA, LOW_POS_ENTRY_FIRED_MAX_HOLD,
    LOW_POS_ENTRY_FIRED_STOP,
)
from tracker_common import (
    _read_rows, _write_rows, _vtrade_read, _vtrade_write,
    record_settlement, record_partial, virtual_nav_by_tag, trade_day_span,
    _remain_ratio, VIRTUAL_REMAIN_FIELD,
)

OPEN_STATUS = ("持有中", "止盈1减半")
STRATEGY_TAG = LOWPOS_TAG
MAX_HOLD = LOW_POS_ENTRY_FIRED_MAX_HOLD          # T+30（交易日）
STOP = LOW_POS_ENTRY_FIRED_STOP                  # -8%
TP_HALF = LOW_POS_ENTRY_FIRED_TP_HALF            # +15%
TP_FULL = LOW_POS_ENTRY_FIRED_TP_FULL            # +25%
TRAIL_MA = LOW_POS_ENTRY_FIRED_TRAIL_MA          # MA5

# watch_history 路径（与 close_review 共用，唯一写入口在各自模块内读-改-写最新一条）
WATCH_HISTORY = Path(__file__).resolve().parent.parent / "data" / "watch_history.csv"


def _ledger_fields():
    """低位触发台账字段（复用趋势台账结构 + 触发专用列）"""
    return ["日期", "代码", "名称", "自研评分", "策略标签", "验证评分", "基准价", "买区", "止损",
            "止盈1", "止盈2", "虚拟成本", "虚拟建仓日", "虚拟建仓状态", "状态", "最高收益%",
            "最后更新", "备注", "触发价", "触发日", "入池日", "虚拟剩余比例"]


def check_and_init():
    p = Path(TRACK_LOWPOS_PATH)
    if not p.exists():
        with open(p, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=_ledger_fields())
            w.writeheader()


def _open_rows():
    """返回全量台账行，调用方自行按 OPEN_STATUS 过滤。
    ⚠️ 必须返回全量列表引用：update_track 对行内状态/备注的修改需通过同一
    rows 列表写回，若返回切片会导致结算状态丢失、虚拟交易重复记账（2026-10-20 冒烟发现）。"""
    check_and_init()
    return _read_rows(TRACK_LOWPOS_PATH)


def _fired_date_from_watch(code):
    """反推该票首次「已触发」落盘日（watch_history 最早 已触发=1 的行）。
    存量已触发票（如中国电影 09-08 触发）回补台账时，触发日必须用真实触发日，
    否则 T+30 从入账日算起，超过 T+30 的存量票永远触发不了自动离场。
    2026-10-23：优先读「触发日」列（close_review 首触发固化写入），无该列时
    回退 earliest 已触发行日期。
    """
    if not WATCH_HISTORY.exists():
        return None
    try:
        with open(WATCH_HISTORY, encoding="utf-8") as f:
            rd = list(csv.DictReader(f))
        for r in rd:
            if r.get("代码") == code and str(r.get("已触发")) == "1":
                return r.get("触发日") or r.get("日期") or None
    except Exception:
        return None
    return None


def register_fired(fired_rows, today=None):
    """低位观察池票触发启动 → 「推荐即虚拟成交」入账（幂等）。
    fired_rows: list of dict，需含 code/name/cur_price/触发价/买点区间/止损价/first_date
    已在池（持有中/止盈1减半）的票不重复登记；已结算票重新触发则新开一段。
    触发日：优先 caller 传入 first_fired_date，否则 watch_history 反推，最后回退 today。
    """
    today = today or TODAY
    check_and_init()
    if not fired_rows:
        return 0
    rows = _read_rows(TRACK_LOWPOS_PATH)
    open_codes = {r["代码"] for r in rows if r["状态"] in OPEN_STATUS}
    added = 0
    for fr in fired_rows:
        code = str(fr.get("code", ""))
        if not code or not code.startswith(ALLOW_CODE_PREFIX):
            continue
        if code in open_codes:
            continue
        trigger_price = fr.get("触发价") or fr.get("cur_price")
        if not trigger_price:
            continue
        try:
            tp = float(trigger_price)
        except (TypeError, ValueError):
            continue
        fired_day = (fr.get("first_fired_date") or _fired_date_from_watch(code)
                     or fr.get("first_date") or today)
        base = tp
        stop = fr.get("止损价") or round(tp * (1 + abs(STOP)), 2)
        rows.append({
            "日期": today, "代码": code, "名称": fr.get("name", ""),
            "自研评分": "", "策略标签": STRATEGY_TAG, "验证评分": "",
            "基准价": f"{base:.2f}", "买区": fr.get("buy_zone", ""), "止损": f"{stop:.2f}",
            "止盈1": f"{base * (1 + TP_HALF):.2f}", "止盈2": f"{base * (1 + TP_FULL):.2f}",
            # 2026-10-23 修复：虚拟建仓日/建仓状态时间必须用**触发日**，不能写入账日 today
            #   （此前 600977 触发 10-08、入账 10-20 → 建仓日错 12 天，T+30 到期日与持有天数全错）
            "虚拟成本": f"{tp:.2f}", "虚拟建仓日": fired_day, "虚拟建仓状态": "已建仓",
            "状态": "持有中", "最高收益%": "0", "最后更新": today, "备注": "低位触发启动入账",
            "触发价": f"{tp:.2f}", "触发日": fired_day,
            "入池日": fr.get("first_date", ""),
            # 2026-10-23：分批止盈记账用——新建仓默认全仓 1.0，减半后降到 0.5
            VIRTUAL_REMAIN_FIELD: "1",
        })
        open_codes.add(code)
        added += 1
    if added:
        _write_rows(TRACK_LOWPOS_PATH, rows, _ledger_fields())
    return added


def _count_trade_days(start_date, end_date):
    """数 start_date..end_date 的交易日个数（含两端）。
    2026-10-23：改用 tracker_common.trade_day_span（读本地指数K线，零网络请求），
    原实现每次调 get_stock_hist('000001') 会拉网络，一轮体检几十次调用极慢。"""
    return trade_day_span(start_date, end_date)


def _ma_today(code, window):
    """今日收盘 MAwindow（用 get_stock_hist 本地K线）"""
    df = get_stock_hist(code, days=60)
    if df is None or df.empty or len(df) < window:
        return None
    df = df.sort_values("日期").reset_index(drop=True)
    ma = df["收盘"].rolling(window).mean().iloc[-1]
    return float(ma) if pd.notna(ma) else None


def _invalidate_watch(codes):
    """把 watch_history 最新一条记录状态标「已失效」（低位追踪池剔除用）"""
    if not codes or not WATCH_HISTORY.exists():
        return 0
    try:
        with open(WATCH_HISTORY, encoding="utf-8") as f:
            rd = list(csv.DictReader(f))
        fields = list(rd[0].keys()) if rd else None
        if not rd or not fields:
            return 0
        latest_idx = {}
        for i, r in enumerate(rd):
            if r.get("代码"):
                latest_idx[r["代码"]] = i
        n = 0
        for c in codes:
            i = latest_idx.get(c)
            if i is not None:
                rd[i]["状态"] = "已失效"
                n += 1
        with open(WATCH_HISTORY, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(rd)
        return n
    except Exception:
        return 0


def update_track(today=None):
    """收盘体检低位触发持仓，自动离场结算；返回 (更新条数, 消息)"""
    today = today or TODAY
    rows = _open_rows()          # 全量列表（引用，修改会写回）
    open_rows = [r for r in rows if r["状态"] in OPEN_STATUS]
    if not open_rows:
        return 0, "无低位触发持仓"
    quotes = get_realtime_quotes([r["代码"] for r in open_rows])
    vtrade = _vtrade_read()
    settled_codes = []
    updated = 0

    for r in open_rows:
        q = quotes.get(r["代码"])
        if not q:
            continue
        price = q["现价"]
        if q.get("最高", -1) == 0 or price <= 0:
            continue  # 停牌
        try:
            base = float(r["虚拟成本"])
        except (TypeError, ValueError):
            continue
        ret = (price / base - 1) * 100
        peak = max(float(r["最高收益%"] or 0), ret)
        r["最高收益%"] = str(round(peak, 2))
        r["最后更新"] = today

        # 止损（相对触发价 -8%）
        stop = base * (1 + STOP)
        # 交易日持有天数
        fired_day = r.get("触发日") or r.get("虚拟建仓日") or r["日期"]
        days = _count_trade_days(fired_day, today)

        # 破 MA5（移动止盈，TRAIL_MA=5）
        ma5 = _ma_today(r["代码"], TRAIL_MA) if TRAIL_MA else None
        broke_ma5 = bool(ma5 and price < ma5)

        # 判定顺序：止损 → T+30 到期自动离场 → 破MA5 → 止盈2清仓 → 止盈1减半
        # 2026-10-20 修正：T+30 是硬性清仓，必须优先于止盈1减半，否则到期当天浮盈
        # +15%~+25% 只卖一半、止盈1减半余仓永不结算（用户要求「超过T+30自动离场」）。
        if price <= stop:
            r["状态"] = "止损出局"
            r["备注"] = f"触发{int(abs(STOP)*100)}%止损（触发价{base:.2f}，峰值{peak:+.1f}%）"
            record_settlement(r, vtrade, price, "止损出局")
            settled_codes.append(r["代码"])
        elif days >= MAX_HOLD:
            r["状态"] = "到期离场"
            r["备注"] = f"满T+{MAX_HOLD}交易日自动离场（触发日{fired_day}，收益{ret:+.1f}%）"
            record_settlement(r, vtrade, price, "低位到期离场")
            settled_codes.append(r["代码"])
        elif broke_ma5:
            r["状态"] = "技术离场"
            r["备注"] = f"破MA{TRAIL_MA} {ma5:.2f}（现价{price:.2f}，收益{ret:+.1f}%），移动止盈离场"
            record_settlement(r, vtrade, price, "破MA5离场")
            settled_codes.append(r["代码"])
        elif ret >= TP_FULL * 100:
            r["状态"] = "止盈清仓"
            r["备注"] = f"达+{TP_FULL*100:.0f}%清仓（触发价{base:.2f}，收益{ret:+.1f}%）"
            record_settlement(r, vtrade, price, "低位止盈清仓")
            settled_codes.append(r["代码"])
        elif ret >= TP_HALF * 100:
            if r["状态"] != "止盈1减半":
                r["状态"] = "止盈1减半"
                r["备注"] = f"达+{TP_HALF*100:.0f}%减半仓，余仓看破MA{TRAIL_MA}"
                # 2026-10-23 修复：减半必须落虚拟交易（原先只改状态，半仓收益不进净值）
                record_partial(r, vtrade, price, f"低位止盈1减半(+{TP_HALF*100:.0f}%)", 0.5)
            else:
                r["备注"] = f"减半后持有中（峰值{peak:+.1f}%）"
        updated += 1

    _write_rows(TRACK_LOWPOS_PATH, rows, _ledger_fields())
    _vtrade_write(vtrade)
    inv = _invalidate_watch(settled_codes) if settled_codes else 0
    return updated, f"更新{updated}条，结算{len(settled_codes)}笔（watch_history失效{inv}条）"


def stats():
    """低位触发策略累计统计 + 虚拟净值"""
    check_and_init()
    rows = _read_rows(TRACK_LOWPOS_PATH)
    nav = virtual_nav_by_tag(TRACK_LOWPOS_PATH, STRATEGY_TAG) if VIRTUAL_ENABLED else None
    if not rows:
        return {"策略": "低位触发", "累计推荐": 0, "持有中": 0, "已结清": 0,
                "胜率": "-", "平均峰值": "-", "虚拟净值": nav}
    closed = [r for r in rows if r["状态"] in
              ("止盈清仓", "止损出局", "技术离场", "到期离场")]
    win = [r for r in closed if
           ("+" in r.get("备注", "") and "收益" in r.get("备注", "")) or
           r["状态"] == "止盈清仓"]
    hi_list = [float(r["最高收益%"] or 0) for r in rows if r["最高收益%"]]
    return {
        "策略": "低位触发",
        "累计推荐": len(rows),
        "持有中": len([r for r in rows if r["状态"] in OPEN_STATUS]),
        "已结清": len(closed),
        "胜率": f"{len(win)/len(closed)*100:.0f}%" if closed else "-",
        "平均峰值": f"{sum(hi_list)/len(hi_list):+.1f}%" if hi_list else "-",
        "虚拟净值": nav,
    }


def analyze_track_pool():
    """低位触发持仓跟踪池分析（供收盘复盘展示，返回 overview 含虚拟净值）"""
    check_and_init()
    # 2026-10-23 修复：_open_rows() 返回全量台账（含已结清），必须按 OPEN_STATUS 过滤，
    # 否则已离场票会被当成「✅持有」展示，并把已了结收益重复计入池内总盈亏。
    rows = [r for r in _open_rows() if r["状态"] in OPEN_STATUS]
    if not rows:
        return [], {"总只数": 0, "总盈亏": 0, "平均盈亏": 0, "破止损": 0,
                    "建议": "低位触发跟踪池为空（无持有中标的）", "虚拟净值": None}
    quotes = get_realtime_quotes([r["代码"] for r in rows])
    analyzed = []
    stop_count = 0
    total_pnl = 0.0
    for r in rows:
        code = r["代码"]
        q = quotes.get(code, {})
        price = q.get("现价", 0)
        if q.get("最高", -1) == 0:
            analyzed.append({"名称": r["名称"], "代码": code, "推荐日": r["触发日"], "策略标签": STRATEGY_TAG,
                             "持有天数": 0, "基准价": r["虚拟成本"], "现价": price,
                             "当日涨跌%": 0, "累计盈亏%": 0, "最高收益%": 0,
                             "状态": "停牌中", "建议": "⛔ 停牌中，暂停跟踪", "标记": "⛔"})
            continue
        if price <= 0:
            continue
        try:
            base = float(r["虚拟成本"])
        except (TypeError, ValueError):
            continue
        ret = (price / base - 1) * 100
        peak = max(float(r["最高收益%"] or 0), ret)
        # 2026-10-23：池内总盈亏按**剩余仓位**加权，减半票不再按全仓计入（否则虚增）
        total_pnl += ret * _remain_ratio(r)
        stop = base * (1 + STOP)
        ma5 = _ma_today(code, TRAIL_MA) if TRAIL_MA else None
        broke_ma5 = bool(ma5 and price < ma5)
        fired_day = r.get("触发日") or r["日期"]
        days = _count_trade_days(fired_day, TODAY)
        if price <= stop:
            advice = f"⛔ 已破-8%止损 {stop:.2f}，无条件移除"; flag = "⛔"; stop_count += 1
        elif days >= MAX_HOLD:
            advice = f"⛔ 满T+{MAX_HOLD}自动离场（收益{ret:+.1f}%）"; flag = "⛔"; stop_count += 1
        elif broke_ma5:
            advice = f"⛔ 收盘破MA5 {ma5:.2f}，移动止盈离场"; flag = "⛔"; stop_count += 1
        elif price >= base * (1 + TP_FULL):
            advice = f"💰 达+{TP_FULL*100:.0f}%清仓 {base*(1+TP_FULL):.2f}"; flag = "💰"
        elif price >= base * (1 + TP_HALF):
            advice = f"💰 达+{TP_HALF*100:.0f}%减半 {base*(1+TP_HALF):.2f}，余仓看破MA{TRAIL_MA}"; flag = "💰"
        elif ret < 0:
            advice = f"⚠️ 浮亏 {ret:.1f}%，止损 {stop:.2f} 挂上"; flag = "⚠️"
        else:
            advice = f"✅ 持有（浮盈 {ret:.1f}%）"; flag = "✅"
        analyzed.append({
            "名称": r["名称"], "代码": code, "推荐日": r["触发日"], "策略标签": STRATEGY_TAG,
            "持有天数": days, "基准价": base, "现价": price,
            "当日涨跌%": q.get("涨跌幅"), "累计盈亏%": round(ret, 2),
            "最高收益%": round(peak, 2), "状态": r["状态"],
            "止损价": round(stop, 2), "止盈1": round(base * (1 + TP_HALF), 2),
            "止盈2": round(base * (1 + TP_FULL), 2),
            "建议": advice, "标记": flag,
        })
    overview = {
        "总只数": len(analyzed),
        "总盈亏": round(total_pnl, 1),
        "平均盈亏": round(total_pnl / len(analyzed), 1) if analyzed else 0,
        "破止损": stop_count,
        "建议": "请优先处理破止损/到期/止盈票" if stop_count else "低位触发跟踪池整体正常",
        "虚拟净值": virtual_nav_by_tag(TRACK_LOWPOS_PATH, STRATEGY_TAG) if VIRTUAL_ENABLED else None,
    }
    return analyzed, overview
