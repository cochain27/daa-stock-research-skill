# -*- coding: utf-8 -*-
"""双策略历史回放回测引擎（右侧趋势 + 短线激进）。

为什么不用现成回测框架：我们的策略是「每天从全市场横截面筛 2-3 只 + 止损止盈/持有期结算」，
不是单标的时序金叉死叉。Backtrader 之类要重写选股逻辑，这里直接复刻 config.py 里的规则。

防未来函数（核心纪律）：
  - 第 T 日的选股，**只用 T-1 收盘及之前**的数据；
  - 成交价用 **T 日开盘价**（T-1 收盘决策后最早可成交的价）；
  - 同日既触及止损又触及止盈时，**保守按止损**成交（跳空场景无法判断先后）。

已知局限（结论要打折看）：
  1. 候选池用「当前全A快照按成交额前 N 只」，存在**幸存者偏差**（已退市股不在池内）；
  2. 数据为新浪不复权 K 线，期间有除权的票，60日位置/突破判定会失真；
  3. 流通市值≥30亿无法回溯，用当日成交额代理；
  4. 短线原策略依赖实时炸板池/涨停梯队，历史不可得 → 用「冲板未封」近似。

用法：
  python backtest_engine.py --start 2026-03-01 --end 2026-09-08 --top 1200
"""
import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fetch_data  # noqa: F401  触发超时 patch + 强制直连
from fetch_data import get_market_snapshot, _industry_by_name
from config import (ALLOW_CODE_PREFIX, MAX_SAME_INDUSTRY,
                    TREND_MIN_60D_POS, TREND_MIN_60D_POS_LOW, TREND_MAX_20D_AMPLITUDE, TREND_MAX_20D_STD_RATIO,
                    TREND_BREAKOUT_VOL_RATIO, TREND_MIN_AMOUNT, TREND_BREAKOUT_CHG_RANGE,
                    TREND_HOLD_DAYS, TREND_MAX_PICKS,
                    SHORT_STOP_LOSS, SHORT_TAKE_PROFIT_1, SHORT_TAKE_PROFIT_2,
                    SHORT_HOLD_DAYS_MAX, SHORT_TIME_STOP_DAYS,
                    SHORTLINE_MIN_CHG, SHORTLINE_MAX_PICKS, SHORTLINE_MIN_VOL_RATIO,
                    SHORTLINE_MAX_UPPER_SHADOW,
                    TREND_STOP_LOSS, TREND_TAKE_PROFIT_1, TREND_TAKE_PROFIT_2)

SINA_KLINE = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
WORKERS = 10

# 趋势风控（2026-09-09：直接从 config 读，已抽成常数）
TREND_STOP = TREND_STOP_LOSS      # -0.06
TREND_TP1 = TREND_TAKE_PROFIT_1   # 0.06
TREND_TP2 = TREND_TAKE_PROFIT_2   # 0.10
TREND_MAX_HOLD = TREND_HOLD_DAYS[1]      # 28 天


# ============ 数据层 ============
def _load_hist(code):
    """新浪公开 JSON 日线（纯 HTTP、并发安全；不复权）"""
    sym = ("sh" if code.startswith(("6", "9")) else "sz") + code
    try:
        r = requests.get(SINA_KLINE, params={"symbol": sym, "scale": "240", "ma": "no",
                                             "datalen": "300"}, timeout=20)
        if r.status_code != 200:
            return code, None
        arr = json.loads(r.text)
        if not arr:
            return code, None
        df = pd.DataFrame(arr).rename(columns={"day": "date", "open": "open", "high": "high",
                                               "low": "low", "close": "close", "volume": "vol"})
        df["date"] = pd.to_datetime(df["date"])
        for c in ["open", "high", "low", "close", "vol"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.sort_values("date").reset_index(drop=True)
        df["amount"] = df["vol"] * df["close"]
        if len(df) < 120:
            return code, None
        return code, df
    except Exception:
        return code, None


def _features(df):
    """向量化预计算全部特征（每只股票只算一次，之后按日查表）"""
    f = df.copy()
    c = f["close"]
    for n in (5, 10, 20, 60):
        f[f"ma{n}"] = c.rolling(n).mean()
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    f["dif"] = ema12 - ema26
    f["dea"] = f["dif"].ewm(span=9, adjust=False).mean()
    delta = c.diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    f["rsi14"] = 100 - 100 / (1 + gain / loss.replace(0, float("nan")))
    f["chg"] = c.pct_change()
    f["vol_ma20"] = f["vol"].rolling(20).mean()
    f["vol_ratio"] = f["vol"] / f["vol_ma20"]
    hi60 = f["high"].rolling(60).max()
    lo60 = f["low"].rolling(60).min()
    f["pos60"] = (c - lo60) / (hi60 - lo60).replace(0, float("nan"))
    # 原代码用收盘价口径：(20日收盘max-min)/mean
    c20max = c.rolling(20).max()
    c20min = c.rolling(20).min()
    f["amp20"] = (c20max - c20min) / c.rolling(20).mean().replace(0, float("nan"))
    f["std20"] = c.rolling(20).std() / c.rolling(20).mean()
    f["close_max20"] = c.rolling(20).max()
    f["close_max60"] = c.rolling(60).max()
    f["amount"] = f["amount"].fillna(0)
    return f


# ============ 选股：只用 as_of 当日收盘（即 T-1）及之前的数据 ============
# 趋势规则变体（原规则自相矛盾：低位 pos60<45% 与 突破20/60日新高 互斥 → 恒 0 票）
VARIANTS = {
    "orig": dict(pos=0.45, brk20=True, brk60=True),
    "v1":   dict(pos=0.45, brk20=True, brk60=False),
    "v2":   dict(pos=0.70, brk20=True, brk60=False),
    "v3":   dict(pos=0.70, brk20=False, brk60=False),
}


def _pick_trend(feats, codes, names, as_of, fixed=False, variant="orig"):
    """fixed=False 复刻线上现状（含两个静默 bug）：
         - 位置60D 字段从未被计算，last.get("位置60D", 0.5) 恒为 0.5 → 恒被 TREND_MIN_60D_POS 过滤
         - RSI 取的是 "RSI" 但 _tech_indicators 只产 "RSI14" → 恒为默认 50
       fixed=True 用真实计算的 pos60 / rsi14。"""
    ts = pd.Timestamp(as_of)
    cfg = VARIANTS[variant]
    out = []
    for code in codes:
        f = feats.get(code)
        if f is None:
            continue
        sub = f[f["date"] <= ts]
        if len(sub) < 60 or sub["date"].iloc[-1] != ts:
            continue
        r = sub.iloc[-1]
        try:
            if not (r["amount"] > TREND_MIN_AMOUNT):
                continue
            pos60 = r["pos60"] if fixed else 0.5
            if not (pos60 < cfg["pos"]):
                continue
            if not (r["amp20"] < TREND_MAX_20D_AMPLITUDE):
                continue
            if not (r["std20"] < TREND_MAX_20D_STD_RATIO):
                continue
            if not (r["vol_ratio"] >= TREND_BREAKOUT_VOL_RATIO):
                continue
            lo, hi = TREND_BREAKOUT_CHG_RANGE
            if not (lo <= r["chg"] <= hi):
                continue
            # 原代码：(close > ma10 > ma20) or (ma5 > ma10 > ma20)
            if not ((r["close"] > r["ma10"] > r["ma20"]) or (r["ma5"] > r["ma10"] > r["ma20"])):
                continue
            if not (r["dif"] > r["dea"]):
                continue
            rsi = r["rsi14"] if fixed else 50
            if not (40 <= rsi <= 70):
                continue
            if cfg["brk20"] and not (r["close"] > r["close_max20"]):
                continue
            if cfg["brk60"] and not (r["close"] > r["close_max60"]):
                continue
            if not cfg["brk20"] and not (r["close"] > r["ma20"]):
                continue
        except Exception:
            continue
        out.append({"代码": code, "名称": names.get(code, code), "收盘": float(r["close"])})
    # 行业去重
    if MAX_SAME_INDUSTRY > 0:
        cnt, dedup = {}, []
        for r in out:
            ind = _industry_by_name(r["名称"]) or ""
            if ind and cnt.get(ind, 0) >= MAX_SAME_INDUSTRY:
                continue
            if ind:
                cnt[ind] = cnt.get(ind, 0) + 1
            dedup.append(r)
        out = dedup
    return out[:TREND_MAX_PICKS]


def _pick_short(feats, codes, names, as_of, v2=False):
    """短线激进：原策略依赖实时炸板池，历史不可得 → 用「冲板未封」近似。

    v1 近似：当日涨幅 ≥ SHORTLINE_MIN_CHG 未封涨停（主板<9.8%/创业<19.8%），
            量比≥1.5，非高位（60日位置<0.7）。
    v2（2026-09-09 新门槛）：量比≥2.0 + 剔除昨日涨停（避免高位接力）。
    """
    ts = pd.Timestamp(as_of)
    out = []
    for code in codes:
        f = feats.get(code)
        if f is None:
            continue
        sub = f[f["date"] <= ts]
        if len(sub) < 60 or sub["date"].iloc[-1] != ts:
            continue
        r = sub.iloc[-1]
        try:
            if not (r["amount"] > TREND_MIN_AMOUNT):
                continue
            chg_pct = r["chg"] * 100
            if not (chg_pct >= SHORTLINE_MIN_CHG):
                continue
            limit = 19.8 if code.startswith("30") else 9.8
            if chg_pct >= limit:          # 已封板，次日追不了
                continue
            vr = float(r["vol_ratio"])
            if v2:
                if vr < 2.0:               # 新门槛：量比≥2
                    continue
                # 剔除昨日涨停（T-1 涨幅≥9.8%/19.8%）
                if len(sub) >= 3:
                    prev = sub.iloc[-2]
                    p_chg = float(prev["chg"]) * 100
                    if p_chg >= (19.5 if code.startswith("30") else 9.5):
                        continue
            else:
                if vr < 1.5:
                    continue
            if not (r["pos60"] < 0.70):   # 不追高位
                continue
        except Exception:
            continue
        out.append({"代码": code, "名称": names.get(code, code), "收盘": float(r["close"])})
    out.sort(key=lambda x: -x["收盘"])
    return out[:SHORTLINE_MAX_PICKS]


# ============ 交易结算 ============

def _ma_close_break(f, i):
    """第 i 日收盘是否跌破当日 MA20（结构止损 / 移动止损用）"""
    try:
        row = f.iloc[i]
        if pd.isna(row["close"]) or pd.isna(row["ma20"]):
            return False
        return float(row["close"]) < float(row["ma20"])
    except Exception:
        return False


def _ma10_close_break(f, i):
    """第 i 日收盘是否跌破当日 MA10"""
    try:
        row = f.iloc[i]
        if pd.isna(row["close"]) or pd.isna(row["ma10"]):
            return False
        return float(row["close"]) < float(row["ma10"])
    except Exception:
        return False


def _simulate_v2(f, entry_idx, entry_price, stop_pct, tp1_pct, max_hold,
                 time_stop_days, use_ma20_struct=False):
    """新版结算（2026-09-09 规则）：
    - 止损：固定 stop_pct（短线 -5.5% / 趋势 -6%）
    - 趋势结构止损：收盘跌破 MA20 即离场
    - 止盈1 tp1_pct 减半仓；剩余仓位走移动止损：盈利>3% 后收盘跌破 MA10 清仓
    - 时间止损：满 time_stop_days 天仍浮亏则离场
    - 到期 max_hold 天离场
    保守：同日内同时触止损与止盈 → 按止损；移动/结构止损按收盘确认。
    返回 (收益率%, 持有天数, 了结原因)
    """
    stop = entry_price * (1 + stop_pct)
    tp1 = entry_price * (1 + tp1_pct)
    n = len(f)
    half_done, half_pnl = False, 0.0
    for d in range(1, max_hold + 1):
        i = entry_idx + d
        if i >= n:
            break
        row = f.iloc[i]
        lo = float(row["low"])
        close = float(row["close"])
        ret_now = (close / entry_price - 1)
        # 止损优先
        if lo <= stop:
            exit_r = (stop - entry_price) / entry_price
            if half_done:
                return (half_pnl * 0.5 + exit_r * 0.5) * 100, d, "止损"
            return exit_r * 100, d, "止损"
        # 盘中触及止盈1 → 减半
        if not half_done and float(row["high"]) >= tp1:
            half_done = True
            half_pnl = tp1_pct
        # 趋势结构止损：收盘跌破 MA20
        if use_ma20_struct and _ma_close_break(f, i) and d >= 1:
            exit_r = (close - entry_price) / entry_price
            if half_done:
                return (half_pnl * 0.5 + exit_r * 0.5) * 100, d, "结构止损"
            return exit_r * 100, d, "结构止损"
        # 移动止损：减半后盈利>3% 且 收盘跌破 MA10 → 清仓
        if half_done and ret_now > 0.03 and _ma10_close_break(f, i):
            exit_r = (close - entry_price) / entry_price
            return (half_pnl * 0.5 + exit_r * 0.5) * 100, d, "移动止盈"
        # 时间止损：满 N 天仍浮亏
        if time_stop_days > 0 and d >= time_stop_days and ret_now < 0:
            exit_r = (close - entry_price) / entry_price
            if half_done:
                return (half_pnl * 0.5 + exit_r * 0.5) * 100, d, "时间止损"
            return exit_r * 100, d, "时间止损"
        if d == max_hold:
            exit_r = (close - entry_price) / entry_price
            if half_done:
                return (half_pnl * 0.5 + exit_r * 0.5) * 100, d, "到期"
            return exit_r * 100, d, "到期"
    # 数据不足（回测区间末尾）
    row = f.iloc[min(entry_idx + max_hold, n - 1)]
    exit_r = (float(row["close"]) - entry_price) / entry_price
    if half_done:
        return (half_pnl * 0.5 + exit_r * 0.5) * 100, max_hold, "数据不足"
    return exit_r * 100, max_hold, "数据不足"


def _simulate(f, entry_idx, entry_price, stop_pct, tp1_pct, tp2_pct, max_hold):
    """从 entry_idx 次日开始逐日推进，返回 (收益率%, 持有天数, 了结原因)。

    保守约定：同日同时触及止损与止盈 → 按止损。
    止盈1 减半仓，剩余半仓继续到 止盈2/止损/到期。
    """
    stop = entry_price * (1 + stop_pct)
    tp1 = entry_price * (1 + tp1_pct)
    tp2 = entry_price * (1 + tp2_pct)
    n = len(f)
    half_done, half_pnl = False, 0.0
    for d in range(1, max_hold + 1):
        i = entry_idx + d
        if i >= n:
            break
        row = f.iloc[i]
        lo, hi = float(row["low"]), float(row["high"])
        # 止损优先
        if lo <= stop:
            exit_r = (stop - entry_price) / entry_price
            if half_done:
                return (half_pnl * 0.5 + exit_r * 0.5) * 100, d, "止损"
            return exit_r * 100, d, "止损"
        if not half_done and hi >= tp1:
            half_done = True
            half_pnl = tp1_pct
        if half_done and hi >= tp2:
            return (half_pnl * 0.5 + tp2_pct * 0.5) * 100, d, "止盈2"
        if d == max_hold:
            exit_r = (float(row["close"]) - entry_price) / entry_price
            if half_done:
                return (half_pnl * 0.5 + exit_r * 0.5) * 100, d, "到期"
            return exit_r * 100, d, "到期"
    # 数据不足（回测区间末尾）
    row = f.iloc[min(entry_idx + max_hold, n - 1)]
    exit_r = (float(row["close"]) - entry_price) / entry_price
    return exit_r * 100, max_hold, "数据不足"


# ============ 市场温度 proxy（回测用） ============

def _temp_proxy(feats, codes, date):
    """用「当日收盘在 MA20 上方的个股占比」近似市场温度。
    >60% 视为暖（温度≥50 近短线开闸）；可复算价格位置用 MA20，纯本地、无网络。
    返回 True=门控放行。
    """
    above = total = 0
    for c in codes:
        f = feats.get(c)
        if f is None:
            continue
        sub = f[f["date"] <= pd.Timestamp(date)]
        if len(sub) < 30 or sub["date"].iloc[-1] != pd.Timestamp(date):
            continue
        r = sub.iloc[-1]
        if pd.isna(r["ma20"]) or pd.isna(r["close"]):
            continue
        total += 1
        if float(r["close"]) > float(r["ma20"]):
            above += 1
    if total < 50:
        return True   # 样本不足时保守放行（避免系统性误杀）
    return above / total >= 0.55


def _stats(trades, label):
    if not trades:
        print(f"\n### {label}：无交易")
        return {}
    df = pd.DataFrame(trades)
    win = df[df["收益%"] > 0]
    loss = df[df["收益%"] <= 0]
    avg_win = win["收益%"].mean() if len(win) else 0
    avg_loss = loss["收益%"].mean() if len(loss) else 0
    # 资金曲线：等权组合（每笔占 1/N 仓位），按了结顺序复利
    n = len(df)
    nav = (1 + df["收益%"] / 100 / n).cumprod()
    mdd = ((nav.cummax() - nav) / nav.cummax()).max() * 100
    s = {
        "策略": label, "笔数": len(df),
        "胜率%": round(len(win) / len(df) * 100, 1),
        "平均收益%": round(df["收益%"].mean(), 2),
        "平均盈利%": round(avg_win, 2), "平均亏损%": round(avg_loss, 2),
        "盈亏比": round(abs(avg_win / avg_loss), 2) if avg_loss else 0,
        "最好%": round(df["收益%"].max(), 2), "最差%": round(df["收益%"].min(), 2),
        "累计收益%": round(df["收益%"].sum(), 1),
        "最大回撤%": round(mdd, 1),
        "平均持有天": round(df["持有天"].mean(), 1),
    }
    print(f"\n### {label}")
    for k, v in s.items():
        print(f"  {k}: {v}")
    print("  了结原因分布：" + str(df["了结原因"].value_counts().to_dict()))
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-03-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--top", type=int, default=1200)
    args = ap.parse_args()
    end = args.end or pd.Timestamp.now().strftime("%Y-%m-%d")

    print(f"[1/4] 拉取全A快照，按成交额取前 {args.top} 只 ...", flush=True)
    snap = get_market_snapshot()
    if snap is None or snap.empty:
        print("快照失败")
        return 1
    snap = snap.copy()
    snap["代码"] = snap["代码"].astype(str).str.replace(r"\.0$", "", regex=True)
    snap = snap[snap["代码"].str.match(r"^(?:" + "|".join(ALLOW_CODE_PREFIX) + ")", na=False)]
    snap = snap[~snap["名称"].astype(str).str.contains("ST|退", na=False)]
    snap["_amt"] = pd.to_numeric(snap.get("成交额"), errors="coerce").fillna(0)
    snap = snap.sort_values("_amt", ascending=False).head(args.top)
    codes = snap["代码"].tolist()
    names = dict(zip(snap["代码"], snap["名称"].astype(str)))

    print(f"[2/4] 并发拉历史K线（{WORKERS} 线程）...", flush=True)
    raw, t0 = {}, time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(_load_hist, c): c for c in codes}
        for i, fu in enumerate(as_completed(futs), 1):
            c, h = fu.result()
            if h is not None:
                raw[c] = h
            if i % 300 == 0:
                print(f"  {i}/{len(codes)}  ok={len(raw)}  {time.time()-t0:.0f}s", flush=True)
    print(f"  就绪 {len(raw)} 只，{time.time()-t0:.0f}s", flush=True)

    print("[3/4] 向量化预计算特征 ...", flush=True)
    feats = {c: _features(h) for c, h in raw.items()}
    # 交易日日历：用候选池里最长的那只
    cal = sorted(set(pd.concat([f[["date"]] for f in feats.values()]).drop_duplicates()["date"]))
    cal = [d for d in cal if pd.Timestamp(args.start) <= d <= pd.Timestamp(end)]
    print(f"  回测交易日 {len(cal)} 天（{cal[0].date()} ~ {cal[-1].date()}）", flush=True)

    print("[4/4] 逐日回放 ...", flush=True)

    def _run_trend(fixed, variant="orig"):
        """fixed=False 复刻线上现状；fixed=True 修复位置60D/RSI/风控参数后"""
        tr = []
        for i in range(1, len(cal)):
            d_prev, d_now = cal[i - 1], cal[i]
            for p in _pick_trend(feats, codes, names, d_prev, fixed=fixed, variant=variant):
                f = feats[p["代码"]]
                idx = f.index[f["date"] == d_now]
                if len(idx) == 0:
                    continue
                ei = idx[0]
                entry = float(f.iloc[ei]["open"])      # T 日开盘买入
                if entry <= 0:
                    continue
                if fixed:
                    stop_pct, tp1, tp2 = TREND_STOP_LOSS, TREND_TP1, TREND_TP2
                else:   # 线上现状：趋势错误复用了短线的 -4%/+5%/+8%
                    stop_pct, tp1, tp2 = (SHORT_STOP_LOSS, SHORT_TAKE_PROFIT_1,
                                          SHORT_TAKE_PROFIT_2)
                pnl, days, why = _simulate(f, ei, entry, stop_pct, tp1, tp2,
                                           TREND_MAX_HOLD)
                tr.append({"买入日": str(d_now.date()), "代码": p["代码"], "名称": p["名称"],
                           "买价": round(entry, 2), "收益%": round(pnl, 2),
                           "持有天": days, "了结原因": why})
        return tr

    def _run_trend_v2():
        """2026-09-09 新趋势规则：pos60<0.70 + 站上MA20 + MA多头 + MACD + 量比，
        结算用 MA20 结构止损 + 减半止盈 + 移动止损；温度门控(MA20上方占比≥55%)才开仓。
        用 VARIANTS["v3"] 的选股口径（去掉突破新高）作为 v2 选股基座。"""
        tr = []
        for i in range(1, len(cal)):
            d_prev, d_now = cal[i - 1], cal[i]
            if not _temp_proxy(feats, codes, d_prev):
                continue   # 低温不开仓
            for p in _pick_trend(feats, codes, names, d_prev, fixed=True, variant="v3"):
                f = feats[p["代码"]]
                idx = f.index[f["date"] == d_now]
                if len(idx) == 0:
                    continue
                ei = idx[0]
                entry = float(f.iloc[ei]["open"])
                if entry <= 0:
                    continue
                pnl, days, why = _simulate_v2(
                    f, ei, entry, TREND_STOP_LOSS, TREND_TP1, TREND_MAX_HOLD,
                    time_stop_days=0, use_ma20_struct=True)
                tr.append({"买入日": str(d_now.date()), "代码": p["代码"], "名称": p["名称"],
                           "买价": round(entry, 2), "收益%": round(pnl, 2),
                           "持有天": days, "了结原因": why})
        return tr

    trend_trades = _run_trend(fixed=False)
    print(f"  趋势(线上现状) {len(trend_trades)} 笔", flush=True)
    trend_fixed = _run_trend(fixed=True)
    print(f"  趋势(修复后·原规则) {len(trend_fixed)} 笔", flush=True)
    trend_v2 = _run_trend_v2()
    print(f"  趋势(2026-09-09新规则·MA20结构止损+温度门控) {len(trend_v2)} 笔", flush=True)

    def _run_short(exit_new):
        """exit_new=True=新出场(移动止盈+时间止损+止损-5.5%)；入场门槛线上已回退（g1 被回测否定）"""
        trades = []
        for i in range(1, len(cal)):
            d_prev, d_now = cal[i - 1], cal[i]
            for p in _pick_short(feats, codes, names, d_prev, v2=False):
                f = feats[p["代码"]]
                idx = f.index[f["date"] == d_now]
                if len(idx) == 0:
                    continue
                ei = idx[0]
                entry = float(f.iloc[ei]["open"])
                if entry <= 0:
                    continue
                if exit_new:
                    pnl, days, why = _simulate_v2(
                        f, ei, entry, SHORT_STOP_LOSS, SHORT_TAKE_PROFIT_1,
                        SHORT_HOLD_DAYS_MAX, SHORT_TIME_STOP_DAYS)
                else:
                    # 现状基准：复刻线上原规则（止损 -4%、止盈 +5%/+8%）
                    pnl, days, why = _simulate(f, ei, entry, -0.04, 0.05, 0.08,
                                               SHORT_HOLD_DAYS_MAX)
                trades.append({"买入日": str(d_now.date()), "代码": p["代码"], "名称": p["名称"],
                               "买价": round(entry, 2), "收益%": round(pnl, 2),
                               "持有天": days, "了结原因": why})
            if i % 60 == 0:
                print(f"  短线[exit_new={int(exit_new)}] {i}/{len(cal)}  {len(trades)}笔", flush=True)
        return trades

    short_trades = _run_short(exit_new=False)
    print(f"  短线(线上现状) {len(short_trades)} 笔", flush=True)
    short_ea = _run_short(exit_new=True)
    print(f"  短线(新出场·最终形态) {len(short_ea)} 笔", flush=True)

    print("\n" + "=" * 60)
    st0 = _stats(trend_trades, "右侧趋势(线上现状)")
    st1 = _stats(trend_fixed, "右侧趋势(修复后·原规则)")
    st_tv2 = _stats(trend_v2, "右侧趋势(2026-09-09新规则)")
    st2 = _stats(short_trades, "短线激进(线上现状)")
    st_sa = _stats(short_ea, "短线激进(新出场·最终形态)")
    print("=" * 60)
    print("\n⚠️ 已知局限：候选池有幸存者偏差、数据为不复权K线、"
          "流通市值用成交额代理、短线用「冲板未封」近似原炸板池。结论仅供策略调优参考。")
    print("⚠️ 温度门控用「收盘在MA20上方的个股占比≥55%」作历史近似，非实时温度计。")
    print("⚠️ 回测不含佣金/印花税/滑点，实盘需扣除约 0.3-0.6% 成本。")
    print("过去表现不代表未来收益。研究参考，不构成投资建议。")

    base = Path(__file__).resolve().parent.parent / "data"
    for tr, nm in ((trend_trades, "回测_右侧趋势_线上现状"), (trend_fixed, "回测_右侧趋势_修复后"),
                   (trend_v2, "回测_右侧趋势_新规则"),
                   (short_trades, "回测_短线激进_线上现状"),
                   (short_ea, "回测_短线激进_新出场")):
        if tr:
            pd.DataFrame(tr).to_csv(base / f"{nm}.csv", index=False, encoding="utf-8")
            print(f"明细已写入 data/{nm}.csv")
    summ = [x for x in [st0, st1, st_tv2, st2, st_sa] if x]
    if summ:
        pd.DataFrame(summ).to_csv(base / "回测_策略汇总.csv", index=False, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
