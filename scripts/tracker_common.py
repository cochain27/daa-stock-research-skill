# -*- coding: utf-8 -*-
"""公共工具函数（tracker_common.py）
供 trend_tracker.py 和 short_tracker.py 共同调用
不要在此文件内引用 config.STOP_LOSS 等策略专属参数
"""
import csv, re
from datetime import datetime
from pathlib import Path
import pandas as pd
from fetch_data import get_realtime_quotes, get_stock_hist
from config import (VIRTUAL_ENABLED, VIRTUAL_BASE, VIRTUAL_TRADE_AMOUNT,
                    ALLOW_CODE_PREFIX, DATA_DIR)

BASE = Path(__file__).resolve().parent
VTRADE_PATH = DATA_DIR / "虚拟交易.csv"
VTRADE_FIELDS = ["虚拟建仓日", "代码", "名称", "虚拟成本", "虚拟了结日", "了结方式", "了结价",
                 "浮动盈亏%", "已实现盈亏", "持有天数", "策略标签", "最高收益%"]
OPEN_STATUS = ("持有中", "止盈1减半", "展期中")


# ===== 通用工具 =====

def _is_extended(r):
    """判断某条台账记录是否已处于展期状态"""
    return r["状态"] == "展期中" or ("展期" in (r.get("备注") or ""))


def check_virtual_settled(log_path):
    """存量迁移：新规则"推荐即虚拟成交"——旧遗留"待确认"记录按推荐价一次性迁移为已建仓"""
    if not VIRTUAL_ENABLED:
        return 0, "虚拟盘未启用"

    today = datetime.now().strftime("%Y-%m-%d")
    if not Path(log_path).exists():
        return 0, f"台账不存在: {log_path}"

    rows = _read_rows(log_path)
    pending = [r for r in rows
               if r.get("虚拟建仓状态", "") in ("", "待确认", "待确认(1次)")
               and r["状态"] in OPEN_STATUS]
    if not pending:
        return 0, "无待迁移虚拟建仓"

    settled = 0
    for r in pending:
        base = r.get("基准价", "")
        try:
            vcost = float(base)
        except (TypeError, ValueError):
            r["虚拟建仓状态"] = "放弃(无基准价)"
            continue
        r["虚拟成本"] = f"{vcost:.2f}"
        r["虚拟建仓日"] = r["日期"]
        r["虚拟建仓状态"] = "已建仓"
        settled += 1

    _write_rows(log_path, rows, _ledger_fields(log_path))
    return settled, f"存量迁移已建仓{settled}笔"


# ===== 通用净值计算 =====

def virtual_nav_by_tag(log_path, tag):
    """按策略标签计算某策略的虚拟净值
    净值 = VIRTUAL_BASE + Σ已实现盈亏(本标签) + Σ浮动盈亏(本标签持仓)
    """
    rows = _read_rows(log_path)
    vtrade = _vtrade_read()

    realized = 0.0
    floating = 0.0
    open_count = 0
    closed_count = 0

    # 已了结交易：从 vtrade 表按标签筛选
    for vt in vtrade:
        if vt.get("虚拟了结日") and vt.get("策略标签") == tag:
            try:
                realized += float(vt.get("已实现盈亏", 0))
                closed_count += 1
            except (ValueError, TypeError):
                pass

    # 未了结持仓：从台账按标签+状态筛选
    open_rows = [r for r in rows
                 if r.get("虚拟建仓状态") == "已建仓"
                 and r["状态"] in OPEN_STATUS
                 and r.get("策略标签") == tag]
    if open_rows:
        quotes = get_realtime_quotes([r["代码"] for r in open_rows])
        for r in open_rows:
            code = r["代码"]
            q = quotes.get(code, {})
            price = q.get("现价", 0)
            if price <= 0:
                continue
            try:
                vcost = float(r["虚拟成本"])
            except (ValueError, TypeError):
                continue
            ret_pct = (price / vcost - 1) * 100
            pnl = VIRTUAL_TRADE_AMOUNT * ret_pct / 100
            floating += pnl
            open_count += 1

    nav = VIRTUAL_BASE + realized + floating
    total_ret = (nav / VIRTUAL_BASE - 1) * 100
    return {
        "净值": round(nav, 0),
        "累计收益": round(nav - VIRTUAL_BASE, 0),
        "累计收益率": round(total_ret, 2),
        "已实现盈亏": round(realized, 0),
        "浮动盈亏": round(floating, 0),
        "已了结笔数": closed_count,
        "持仓笔数": open_count,
        "基准": VIRTUAL_BASE,
    }


# ===== 通用虚拟了结写入 =====

def record_settlement(r, vtrade_list, settle_price, reason):
    """当虚拟建仓票了结时，同步记录到虚拟交易台账"""
    if r.get("虚拟建仓状态") != "已建仓":
        return
    try:
        vcost = float(r["虚拟成本"])
        rec_day = r.get("虚拟建仓日") or r["日期"]
        days = (datetime.strptime(r["最后更新"], "%Y-%m-%d") -
                datetime.strptime(rec_day, "%Y-%m-%d")).days
        ret_pct = (settle_price / vcost - 1) * 100
        pnl = VIRTUAL_TRADE_AMOUNT * ret_pct / 100
        vtrade_list.append({
            "虚拟建仓日": rec_day,
            "代码": r["代码"],
            "名称": r["名称"],
            "虚拟成本": f"{vcost:.2f}",
            "虚拟了结日": r["最后更新"],
            "了结方式": reason,
            "了结价": f"{settle_price:.2f}",
            "浮动盈亏%": f"{ret_pct:.2f}",
            "已实现盈亏": f"{pnl:.0f}",
            "持有天数": str(days),
            "策略标签": r.get("策略标签", ""),
            "最高收益%": r["最高收益%"],
        })
        r["虚拟建仓状态"] = "已了结"
    except (ValueError, TypeError):
        pass


# ===== 通用技术面破坏检测 =====

def tech_break(r, price, mode="trend"):
    """技术面破坏检测
    mode="trend": 波段/趋势票 → MA10<MA20 且 破MA10；盈利>3% 破MA10
    mode="short":  短线票     → 跌破MA5
    第2天起(days>=1)才判，避免误杀开盘波动
    返回 (is_break: bool, reason: str)
    """
    try:
        df = get_stock_hist(r["代码"], days=40)
        if df is None or df.empty or len(df) < 25:
            return False, ""
        df = df.sort_values("日期").reset_index(drop=True)
        df["MA5"] = df["收盘"].rolling(5).mean()
        df["MA10"] = df["收盘"].rolling(10).mean()
        df["MA20"] = df["收盘"].rolling(20).mean()
        ma5 = df["MA5"].iloc[-1]
        ma10 = df["MA10"].iloc[-1]
        ma20 = df["MA20"].iloc[-1]
        if pd.isna(ma5) or pd.isna(ma10) or pd.isna(ma20):
            return False, ""
        try:
            base = float(r["基准价"])
            ret = (price / base - 1) * 100
        except (ValueError, TypeError):
            ret = 0

        if mode == "short":
            if price < ma5:
                return True, f"跌破MA5 {ma5:.2f}（现价{price:.2f}），短线生命线失守"
        else:
            # 趋势走坏：MA10 下穿 MA20 且 现价跌破 MA10
            if ma10 < ma20 and price < ma10:
                return True, f"MA10({ma10:.2f})<MA20({ma20:.2f})且破MA10（现价{price:.2f}），趋势走坏"
            # 盈利>3% 跌破MA10 → 移动止盈离场
            if ret > 3 and price < ma10:
                return True, f"盈利{ret:+.1f}%已上移成本线，跌破MA10 {ma10:.2f}，移动止盈离场"
    except Exception:
        pass
    return False, ""


# ===== 通用展期评估 =====

def extend_decision(r, price, ret, peak_ret, ext_count, cfg):
    """满持仓上限时评估是否展期（仅右侧趋势用）
    cfg: dict，含 HOLD_DAYS_MAX / EXTEND_MAX_DAYS / EXTEND_MAX_COUNT / EXTEND_PEAK_DRAWBACK
    返回 (can_extend: bool, reason: str)
    """
    max_days = cfg.get("HOLD_DAYS_MAX", 14)
    ext_max = cfg.get("EXTEND_MAX_DAYS", 60)
    ext_max_cnt = cfg.get("EXTEND_MAX_COUNT", 2)
    drawback = cfg.get("EXTEND_PEAK_DRAWBACK", 6)

    if ret <= 0:
        return False, f"满{max_days}天收益{ret:+.1f}%未盈利，不展期"
    if peak_ret - ret > drawback:
        return False, f"满{max_days}天但峰值回撤{peak_ret - ret:.1f}%>{drawback}%，不展期"
    if ext_count >= ext_max_cnt:
        return False, f"展期名额已满({ext_count}/{ext_max_cnt}只)，不展期"

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
        rec_date = datetime.strptime(r["日期"], "%Y-%m-%d")
        df["日期_dt"] = pd.to_datetime(df["日期"])
        recent5 = df[df["日期_dt"] > rec_date].tail(5)["成交额"].mean()
        before5 = df[df["日期_dt"] <= rec_date].tail(5)["成交额"].mean()
        if pd.notna(recent5) and pd.notna(before5) and before5 > 0:
            if recent5 < before5 * 0.6:
                return False, f"量能萎缩至{recent5/before5*100:.0f}%（<60%），不展期"
    except Exception as e:
        return False, f"展期评估异常：{e}，不展期"

    return True, f"趋势+量能 intact，建议展期（收益{ret:+.1f}%）"


# ===== CSV 基础读写 =====

def _ledger_fields(path):
    """读取台账首行字段（兼容旧台账）"""
    try:
        with open(path, encoding="utf-8-sig") as f:
            return next(csv.DictReader(f)).keys()
    except (StopIteration, FileNotFoundError):
        return None


def _read_rows(path):
    if not Path(path).exists():
        return []
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _write_rows(path, rows, fields=None):
    """写回台账。动态合并行内出现的所有字段，避免 DictWriter 因多余字段抛错破坏台账。
    若显式传入 fields，则以 fields 为表头顺序，并自动追加行内出现的额外字段。
    若未传入 fields，则从首行字段推断；首行为空时以行内字段并集为准。
    """
    if fields is None:
        fields = _ledger_fields(path)
    ext = []
    for r in rows:
        for k in r:
            if k not in fields and k not in ext:
                ext.append(k)
    fields = list(fields) + ext
    if not fields:
        return
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _vtrade_read():
    if not VTRADE_PATH.exists():
        return []
    with open(VTRADE_PATH, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _vtrade_write(rows):
    with open(VTRADE_PATH, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=VTRADE_FIELDS)
        w.writeheader()
        w.writerows(rows)
