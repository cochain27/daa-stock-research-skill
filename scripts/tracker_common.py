# -*- coding: utf-8 -*-
"""公共工具函数（tracker_common.py）
供 trend_tracker.py 和 short_tracker.py 共同调用
不要在此文件内引用 config.STOP_LOSS 等策略专属参数
"""
import csv, re
from datetime import datetime
import os as _os

# 2026-10-10 补跑支持：DAA_REPORT_DATE 指定报告日期（台账/体检日期随动）
TODAY = _os.environ.get("DAA_REPORT_DATE") or datetime.now().strftime("%Y-%m-%d")
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

_INDEX_KLINE = DATA_DIR / "index_klines" / "sh000001.csv"
_TRADE_DAYS_CACHE = None


def _trade_day_set():
    """本地交易日集合。

    数据源优先级（2026-10-23 修正）：
      ① `trade_calendar`（法定休市公告口径，**波动性最低、覆盖全年**）
      ② 指数K线 `data/index_klines/sh000001.csv`（⚠ 该文件由研究脚本按需生成，
         长期不更新——实测 2026-10-23 时最新只到 09-18，若当主源会让 9-18 后的
         所有跨度退化成自然日，等于修复白做，故仅作交叉校验/兜底）
    结果缓存，避免每次调用重复计算。
    """
    global _TRADE_DAYS_CACHE
    if _TRADE_DAYS_CACHE is not None:
        return _TRADE_DAYS_CACHE
    days = set()
    try:
        from trade_calendar import holiday_set, HOLIDAY_RANGES
        from datetime import date, timedelta
        d = date(min(HOLIDAY_RANGES) - 1, 1, 1) if HOLIDAY_RANGES else date(2019, 1, 1)
        end = date.today() + timedelta(days=40)
        while d <= end:
            if d not in holiday_set(d.year):
                days.add(d.strftime("%Y-%m-%d"))
            d += timedelta(days=1)
    except Exception:
        days = set()
    if not days:
        try:
            if _INDEX_KLINE.exists():
                with open(_INDEX_KLINE, encoding="utf-8-sig") as f:
                    for r in csv.DictReader(f):
                        dd = (r.get("date") or r.get("日期") or "").strip()[:10]
                        if dd:
                            days.add(dd)
        except Exception:
            days = set()
    _TRADE_DAYS_CACHE = days
    return days


def trade_day_span(start_date, end_date):
    """start→end 的**交易日跨度**（含两端同一天计 1）。

    2026-10-23 修复：全项目「持有 N 天」阈值原用自然日差（datetime.days），
    周五建仓→周一即被算作 3 天，时间止损/硬上限/技术面「第N天」全部提前 1~N 个交易日触发。
    口径与 `low_pos_tracker._count_trade_days` 一致（含两端）。
    退化：无交易日历时回退自然日差（宁可宽松，不可漏判）。
    """
    if not start_date or not end_date:
        return 0
    s, e = str(start_date)[:10], str(end_date)[:10]
    if e < s:
        s, e = e, s
    days = _trade_day_set()
    if days:
        n = len([d for d in days if s <= d <= e])
        if n:
            return n
    try:
        return (datetime.strptime(e, "%Y-%m-%d") - datetime.strptime(s, "%Y-%m-%d")).days + 1
    except Exception:
        return 0


def trade_days_held(start_date, end_date):
    """已持有**交易日数**（不含建仓日当天）。

    与 short/trend 原自然日口径严格对齐的替换：
      原式 (end - start).days → 10-09(五)买、10-12(一)算 = 3 天（错，实为 1 个交易日）
      新式 trade_days_held → 10-09→10-12 = 1；10-13→10-16 = 3（与 10-16 踩坑日记的
      「灵康药业才是真正 3 交易日」一致）
    仅用于「持有 N 天后触发」类阈值；T+30「含触发日」类阈值请直接用 trade_day_span。
    """
    return max(trade_day_span(start_date, end_date) - 1, 0)


def _is_extended(r):
    """判断某条台账记录是否已处于展期状态"""
    return r["状态"] == "展期中" or ("展期" in (r.get("备注") or ""))


def check_virtual_settled(log_path):
    """存量迁移：新规则"推荐即虚拟成交"——旧遗留"待确认"记录按推荐价一次性迁移为已建仓"""
    if not VIRTUAL_ENABLED:
        return 0, "虚拟盘未启用"

    today = TODAY
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
            # 2026-10-23：浮动盈亏按**剩余仓位**计提，减半后不再按全仓虚增浮盈
            pnl = VIRTUAL_TRADE_AMOUNT * _remain_ratio(r) * ret_pct / 100
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

# 2026-10-23 新增：分批止盈记账
#   原实现只有「一次性了结」，+15% 减半只在台账状态上打个标记、不落虚拟交易，
#   导致半仓那部分收益**既不计已实现、也没锁进净值**（海正药业 +16.92% 减半后
#   净值里仍是全仓浮动，等于分批止盈完全没生效）。
#   现在引入「虚拟剩余比例」列：减半时按 VIRTUAL_TRADE_AMOUNT×比例 结算一笔，
#   余仓按剩余比例计浮动；彻底清仓时剩余比例归零，避免重复记账。
VIRTUAL_REMAIN_FIELD = "虚拟剩余比例"


def _remain_ratio(r):
    """当前剩余仓位比例（0~1），缺列/脏数据按 1.0（全仓）处理。"""
    try:
        v = float(r.get(VIRTUAL_REMAIN_FIELD) or 1.0)
    except (TypeError, ValueError):
        return 1.0
    return min(max(v, 0.0), 1.0)


def _append_vtrade(vtrade_list, r, price, reason, ratio, vcost):
    """按比例写一条虚拟交易记录。ratio 为本次了结的仓位占比（0~1）。"""
    amount = VIRTUAL_TRADE_AMOUNT * ratio
    ret_pct = (price / vcost - 1) * 100
    pnl = amount * ret_pct / 100
    rec_day = r.get("虚拟建仓日") or r.get("触发日") or r["日期"]
    days = trade_day_span(rec_day, r["最后更新"])
    vtrade_list.append({
        "虚拟建仓日": rec_day,
        "代码": r["代码"],
        "名称": r["名称"],
        "虚拟成本": f"{vcost:.2f}",
        "虚拟了结日": r["最后更新"],
        "了结方式": reason,
        "了结价": f"{price:.2f}",
        "浮动盈亏%": f"{ret_pct:.2f}",
        "已实现盈亏": f"{pnl:.0f}",
        "持有天数": str(days),
        "策略标签": r.get("策略标签", ""),
        "最高收益%": r.get("最高收益%", ""),
    })


def record_partial(r, vtrade_list, price, reason, part=0.5):
    """分批止盈：了结当前剩余仓位的 `part`（默认一半），余仓继续持有。
    幂等：同一比例不会重复结算（剩余比例已下降则跳过）。
    返回实际了结的仓位占比（0 表示未记账）。
    """
    if r.get("虚拟建仓状态") != "已建仓":
        return 0.0
    try:
        vcost = float(r["虚拟成本"])
    except (ValueError, TypeError):
        return 0.0
    remain = _remain_ratio(r)
    if remain <= 0.001:
        return 0.0
    sold = round(remain * part, 4)
    if sold <= 0:
        return 0.0
    _append_vtrade(vtrade_list, r, price, f"{reason}（{sold:.0%}仓）", sold, vcost)
    r[VIRTUAL_REMAIN_FIELD] = f"{max(remain - sold, 0):.4f}"
    return sold


def record_settlement(r, vtrade_list, settle_price, reason):
    """当虚拟建仓票了结时，同步记录到虚拟交易台账（只结算剩余仓位）。"""
    if r.get("虚拟建仓状态") != "已建仓":
        return
    try:
        vcost = float(r["虚拟成本"])
    except (ValueError, TypeError):
        return
    remain = _remain_ratio(r)
    if remain > 0.001:
        _append_vtrade(vtrade_list, r, settle_price, reason, remain, vcost)
    r[VIRTUAL_REMAIN_FIELD] = "0"
    r["虚拟建仓状态"] = "已了结"


# ===== 通用技术面破坏检测 =====

def tech_break(r, price, mode="trend"):
    """技术面破坏检测
    mode="trend": 波段/趋势票 → 破MA20结构止损（兜底，最优先）；破MA10移动止盈（变体B，无盈利门槛）
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
            # 移动止盈（2026-09-09：止盈1减半后剩余仓位让利润奔跑，跌破MA10清仓）
            if ret > 3 and price < ma10:
                return True, f"盈利{ret:+.1f}%已减半，跌破MA10 {ma10:.2f}，移动止盈清仓"
        else:
            # 变体B（2026-09-14 落地）：破MA20结构止损兜底（最优先），破MA10即移动止盈（无盈利门槛）
            # 与回测 _simulate_let_profit(move_thresh=None) 顺序一致：结构止损(MA20)先于移动止盈(MA10)
            if price < ma20:
                return True, f"跌破MA20 {ma20:.2f}（现价{price:.2f}），趋势结构破坏离场"
            if price < ma10:
                return True, f"破MA10 {ma10:.2f}（现价{price:.2f}），移动止盈离场"
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
