# -*- coding: utf-8 -*-
"""四策略横向对比回测（2024-01-02 ~ 2026-09-24，本地 klines 前复权缓存，无网络）。

统一口径（防未来函数）：
  - 信号日 T 用 T 收盘数据判定 → 成交价统一 T+1 开盘价（初动/趋势/短线）；
    低位池沿用其生产口径「触发日收盘」入场，另给 T+1 开盘备查。
  - 同一票 10 交易日冷却（初动/低位沿用各自实现；趋势/短线按每日 topN）。
  - 温度闸门：趋势/短线用「收盘在MA20上方个股占比」（backtest_engine._temp_proxy 同口径，
      趋势≥55/短线≥60）；初动无闸门（回测证伪）；低位温度=关但落列离线分层（low_pos_backtest_full 同口径）。
      另算「当日上涨占比」作初动/低位分层参考。
  - 每笔占 1/N 仓位等权复利算资金曲线与回撤（与 backtest_engine._stats 同口径）。

四个池子：
  1 右侧趋势（生产 v3 画像 + 变体B出场：-6%硬止损/MA20结构止损/盈利>3%破MA10移动/最长28天）
  2 短线激进（冲板未封近似 + 新出场：-5.5%止损/+5%减半/3天时间止损/最长5天）
  3 温和放量初动（生产画像含周线上扬+保守蓄势+≤16亿；V2出场：T+1开盘/-8%硬止损/T+2起破MA5次日开盘离场/最长T+5收盘）
  4 低位埋伏（触发即买口径，仅观察池定位参考；入场=触发日收盘，-8%止损，T+5）

输出：data/四池回测_统计_20260924.json + data/四池回测_明细_20260924.csv
"""
import glob
import json
import os
import sys
import time

import pandas as pd
import numpy as np

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))
KLINE_DIR = os.path.join(BASE, "data", "klines")

# ---------- 生产参数（config.py 同步） ----------
# 趋势
TREND_MIN_AMOUNT = 1.5e8
TREND_POS_LO, TREND_POS_HI = 0.30, 0.70
TREND_AMP20_MAX, TREND_STD20_MAX = 0.25, 0.05
TREND_VOL_RATIO = 1.5
TREND_CHG_LO, TREND_CHG_HI = 0.03, 0.07
TREND_STOP, TREND_TP1 = -0.06, 0.08
TREND_MAX_HOLD = 28
TREND_TEMP_MIN = 55
TREND_MAX_PICKS = 2
# 短线
SHORT_MIN_CHG = 5.0
SHORT_VOL_RATIO = 1.5
SHORT_UPPER_SHADOW = 0.03
SHORT_POS60_MAX = 0.70
SHORT_STOP, SHORT_TP1 = -0.055, 0.05
SHORT_HOLD_MAX, SHORT_TIME_STOP = 5, 3
SHORT_TEMP_MIN = 50
SHORT_MAX_PICKS = 2
# 初动
W_LB_LO, W_LB_HI = 1.3, 2.5
W_CHG5_LO, W_CHG5_HI = 3.0, 8.0
W_POS60_MAX = 0.80
W_CHG_TODAY_MIN, W_CHG_TODAY_MAX = -9.5, 9.5
W_AMP_TODAY_MAX = 7.5
W_AMT_MAX = 16e8
W_CONSOL_AMP10_MAX, W_CONSOL_VOL10_60_MAX = 3.5, 0.70
W_STOP_PCT = 0.92          # -8% 硬止损
W_MAX_HOLD = 5             # T+5
W_COOL = 10                # 交易日冷却
# 低位（config LOW_POS_ENTRY_*，温度闸门离线分层）
LOW_TEMP_MIN = 55

# ---------- 数据加载 ----------
def _load_all():
    feat, names = {}, {}
    for p in sorted(glob.glob(os.path.join(KLINE_DIR, "*.csv"))):
        sym = os.path.basename(p)[:-4]
        code = sym[2:]
        try:
            raw = pd.read_csv(p)
        except Exception:
            continue
        if raw.empty or len(raw) < 80:
            continue
        d = pd.DataFrame({
            "日期": pd.to_datetime(raw["date"]),
            "开盘": raw["open"].astype(float),
            "最高": raw["high"].astype(float),
            "最低": raw["low"].astype(float),
            "收盘": raw["last"].astype(float),
            "成交量": raw["volume"].astype(float),
            "成交额": raw["amount"].astype(float),
        }).drop_duplicates(subset=["日期"]).sort_values("日期").reset_index(drop=True)
        d = d.dropna(subset=["收盘"])
        d["涨跌幅"] = d["收盘"].pct_change() * 100
        # 均线
        for w in (5, 10, 20):
            d[f"MA{w}"] = d["收盘"].rolling(w).mean()
        # 量比
        d["v5"] = d["成交量"].shift(1).rolling(5).mean()
        d["lb"] = d["成交量"] / d["v5"]
        d["lb5mean"] = d["lb"].rolling(5).mean()
        # 60日位置 / 距高：趋势用收盘口径（stock_screener 一致）；初动/低位用高低价口径（warm/low_pos 一致）
        h60_c = d["收盘"].rolling(60, min_periods=40)
        d["pos60_c"] = (d["收盘"] - h60_c.min()) / (h60_c.max() - h60_c.min())
        hi60 = d["最高"].rolling(60, min_periods=40).max()
        lo60 = d["最低"].rolling(60, min_periods=40).min()
        d["pos60"] = (d["收盘"] - lo60) / (hi60 - lo60)
        d["dist60"] = (d["收盘"] / hi60 - 1) * 100
        # 横盘 / 振幅
        d["amp20"] = (d["收盘"].rolling(20).max() - d["收盘"].rolling(20).min()) / d["收盘"].rolling(20).mean()
        d["std20"] = d["收盘"].rolling(20).std() / d["收盘"].rolling(20).mean()
        d["振幅"] = (d["最高"] - d["最低"]) / d["收盘"].shift(1) * 100
        d["amp10_mean"] = d["振幅"].shift(1).rolling(10).mean()
        d["v10"] = d["成交量"].shift(1).rolling(10).mean()
        d["v60"] = d["成交量"].shift(1).rolling(60).mean()
        d["vol10_60"] = d["v10"] / d["v60"]
        # 量比倍数（当日/20日均量，趋势用）
        d["vol_ma20"] = d["成交量"].rolling(20).mean()
        d["vol_ratio"] = d["成交量"] / d["vol_ma20"]
        # 5日涨幅
        d["chg5"] = (d["收盘"] / d["收盘"].shift(5) - 1) * 100
        # MACD
        ema12 = d["收盘"].ewm(span=12, adjust=False).mean()
        ema26 = d["收盘"].ewm(span=26, adjust=False).mean()
        d["dif"] = ema12 - ema26
        d["dea"] = d["dif"].ewm(span=9, adjust=False).mean()
        d["macd_hist"] = (d["dif"] - d["dea"]) * 2
        # RSI14
        delta = d["收盘"].diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        d["rsi14"] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
        # MA20 上行（低位池 A 方案）
        d["ma20up"] = d["MA20"] > d["MA20"].shift(5)
        # MA20 乖离
        d["bias20"] = (d["收盘"] / d["MA20"] - 1) * 100
        # 周线上扬（初动池）
        d["week"] = d["日期"].dt.to_period("W").astype(str)
        wk = d.groupby("week").agg(wclose=("收盘", "last"), wvol=("成交量", "sum")).reset_index()
        wk["wma5"] = wk["wclose"].rolling(5).mean()
        wk["wma5_up"] = wk["wma5"] > wk["wma5"].shift(1)
        wk = wk.ffill()
        d = d.merge(wk[["week", "wclose", "wma5", "wma5_up"]], on="week", how="left")
        d["week_up"] = (d["wclose"] > d["wma5"]) & d["wma5_up"]
        feat[code] = d.reset_index(drop=True)
    return feat

# ---------- 通用结算 ----------
def _sim_generic(f, idx, entry, stop_pct, tp1_pct, max_hold, time_stop_days=0,
                 ma_struct=False):
    """止损优先 / 止盈1减半 / (可选)MA20结构止损 / 盈利>3%破MA10移动 / 时间止损 / 到期。"""
    stop = entry * (1 + stop_pct)
    tp1 = entry * (1 + tp1_pct)
    n = len(f)
    half_done, half_pnl = False, 0.0
    for dd in range(1, max_hold + 1):
        i = idx + dd
        if i >= n:
            break
        row = f.iloc[i]
        lo, hi, close = float(row["最低"]), float(row["最高"]), float(row["收盘"])
        ret = close / entry - 1
        if lo <= stop:
            exit_r = stop / entry - 1
            return (half_pnl * 0.5 + exit_r * 0.5) * 100 if half_done else exit_r * 100, dd, "止损"
        if not half_done and hi >= tp1:
            half_done, half_pnl = True, tp1_pct
        if ma_struct and close < float(row["MA20"]):
            exit_r = close / entry - 1
            return (half_pnl * 0.5 + exit_r * 0.5) * 100 if half_done else exit_r * 100, dd, "结构止损"
        if half_done and ret > 0.03 and close < float(row["MA10"]):
            exit_r = close / entry - 1
            return (half_pnl * 0.5 + exit_r * 0.5) * 100, dd, "移动止盈"
        if time_stop_days > 0 and dd >= time_stop_days and ret < 0:
            exit_r = close / entry - 1
            return (half_pnl * 0.5 + exit_r * 0.5) * 100 if half_done else exit_r * 100, dd, "时间止损"
        if dd == max_hold:
            exit_r = close / entry - 1
            return (half_pnl * 0.5 + exit_r * 0.5) * 100 if half_done else exit_r * 100, dd, "到期"
    row = f.iloc[min(idx + max_hold, n - 1)]
    exit_r = float(row["收盘"]) / entry - 1
    return (half_pnl * 0.5 + exit_r * 0.5) * 100 if half_done else exit_r * 100, max_hold, "数据不足"

def _sim_warm_v2(f, idx, entry):
    """初动池 V2 出场（用户拍板固化）：
       T+1 开盘介入；-8% 硬止损（现价×0.92，盘中触）；T+2 收盘起破 MA5 → 次日开盘离场；
       无固定止盈；最长 T+5 收盘。返回 (收益%, 持有天, 原因)。"""
    stop = entry * W_STOP_PCT
    n = len(f)
    for dd in range(1, W_MAX_HOLD + 1):
        i = idx + dd
        if i >= n:
            break
        row = f.iloc[i]
        if float(row["最低"]) <= stop:
            return (stop / entry - 1) * 100, dd, "止损-8%"
        if dd >= 2 and float(row["收盘"]) < float(row["MA5"]):
            # 破 MA5 → 次日开盘离场
            j = i + 1
            if j >= n:
                return (float(row["收盘"]) / entry - 1) * 100, dd, "破MA5-数据不足"
            px = float(f.iloc[j]["开盘"])
            if px <= 0:
                px = float(row["收盘"])
            return (px / entry - 1) * 100, dd + 1, "破MA5"
        if dd == W_MAX_HOLD:
            return (float(row["收盘"]) / entry - 1) * 100, dd, "T+5到期"
    row = f.iloc[min(idx + W_MAX_HOLD, n - 1)]
    return (float(row["收盘"]) / entry - 1) * 100, W_MAX_HOLD, "数据不足"

# ---------- 信号器 ----------
def _trend_signal(f):
    m = (
        (f["成交额"] > TREND_MIN_AMOUNT)
        & f["pos60_c"].between(TREND_POS_LO, TREND_POS_HI)
        & (f["amp20"] < TREND_AMP20_MAX) & (f["std20"] < TREND_STD20_MAX)
        & (f["vol_ratio"] >= TREND_VOL_RATIO)
        & f["涨跌幅"].between(TREND_CHG_LO * 100, TREND_CHG_HI * 100)
        & (f["收盘"] > f["MA20"])
        & ((f["收盘"] > f["MA10"]) & (f["MA10"] > f["MA20"]) | (f["MA5"] > f["MA10"]) & (f["MA10"] > f["MA20"]))
        & (f["dif"] > f["dea"])
        & f["rsi14"].between(40, 70)
        & f["MA20"].notna() & f["vol_ratio"].notna() & f["rsi14"].notna() & f["pos60_c"].notna()
    )
    # MACD 柱>0 或近3日金叉
    hist_ok = f["macd_hist"] > 0
    cross = (f["dif"] > f["dea"]) & (f["dif"].shift(1) <= f["dea"].shift(1))
    cross_3d = cross | cross.shift(1) | cross.shift(2)
    m &= (hist_ok | cross_3d)
    return m

def _short_signal(f):
    m = (
        (f["成交额"] > TREND_MIN_AMOUNT)
        & (f["涨跌幅"] >= SHORT_MIN_CHG)
        & (f["vol_ratio"] >= SHORT_VOL_RATIO)
        & (f["pos60_c"] < SHORT_POS60_MAX)
        & f["MA20"].notna() & f["pos60_c"].notna()
    )
    return m

def _warm_signal(f):
    m = (
        f["lb"].between(W_LB_LO, W_LB_HI)
        & f["chg5"].between(W_CHG5_LO, W_CHG5_HI)
        & (f["pos60"] < W_POS60_MAX)
        & (f["收盘"] > f["MA20"])
        & (f["dif"] > f["dea"])
        & f["涨跌幅"].between(W_CHG_TODAY_MIN, W_CHG_TODAY_MAX)
        & f["振幅"].notna() & (f["振幅"] <= W_AMP_TODAY_MAX)
        & (f["成交额"] <= W_AMT_MAX)
        & f["amp10_mean"].notna() & (f["amp10_mean"] <= W_CONSOL_AMP10_MAX)
        & f["vol10_60"].notna() & (f["vol10_60"] <= W_CONSOL_VOL10_60_MAX)
        & f["week_up"].notna() & f["week_up"]
        & f["MA20"].notna() & f["lb"].notna() & f["chg5"].notna()
    )
    return m

def _recent_oversold(f, idx, lookback=25):
    """近 lookback 日是否有超卖记录（pos60<0.5 或 dist60<-30）。返回 (found, first_idx)。"""
    for j in range(max(0, idx - lookback), idx):
        r = f.iloc[j]
        if r["pos60"] < 0.5 or r["dist60"] < -30:
            return True, j
    return False, None

def _low_scan(f, idx):
    """低位池蓄势日扫描：返回 (标准蓄势ok, 近期超卖ok)。与生产 backtest 同口径。

    生产 backtest 的判定顺序（关键）：
      1. `ok, _ = _蓄势通过(row)`（标准蓄势判据）
      2. `recent_os = _recent_oversold(ind, i+1)`（近25日有超卖记录，与 ok 无关）
      3. `if recent_os:` → 无条件套 OS_SCAN 段（额/pos60/dist60/乖离/ma20up/age/lb/lb5mean/chg5/涨幅）
         —— 即使 ok=True（标准蓄势通过）也被 OS 段再滤一道
      4. 两路都过 → 触发；路径归属：ok=True 标标准，否则近期超卖
    本函数复刻该顺序：std_ok=ok 且（若 recent_os 则过 OS 段）；os_ok=not ok 且 recent_os 且过 OS 段。
    """
    row = f.iloc[idx]
    # ---- 标准蓄势（_蓄势通过 同口径） ----
    ok = False
    if not (pd.isna(row.get("pos60")) or pd.isna(row.get("dist60"))):
        if (row["pos60"] < 0.40 and row["dist60"] <= -27
                and (pd.isna(row["MA20"]) or row["bias20"] >= -5)
                and row["ma20up"]
                and pd.notna(row["lb"]) and pd.notna(row["lb5mean"])
                and row["lb"] <= 2.0 and row["lb5mean"] <= 1.20
                and -10.0 <= row["chg5"] <= 5.0
                and row["涨跌幅"] <= 5.0
                and row["amp20"] < 0.25      # 生产 amp20 是百分比(×100)，此处归一化等值 25%
                and 2e8 <= row["成交额"] <= 6e8):
            ok = True
    # ---- 近期超卖扫描段（生产 backtest 中 recent_os=True 时无条件执行） ----
    found, first_idx = _recent_oversold(f, idx + 1)
    os_scan_ok = False
    if found and first_idx is not None:
        if ((idx - first_idx) >= 10              # os_age ≥10
                and 2e8 <= row["成交额"] <= 6e8
                and row["pos60"] < 0.40
                and row["dist60"] <= -27
                and (pd.isna(row["MA20"]) or row["bias20"] >= -5)
                and row["ma20up"]
                and row["lb"] <= 3.0 and row["lb5mean"] <= 2.0
                and row["chg5"] <= 0.0
                and row["涨跌幅"] <= 5.0):
            os_scan_ok = True
    # 路径归属复刻生产：ok=True 标标准；否则若 recent_os 且过 OS 段标近期超卖
    if ok:
        std_ok = os_scan_ok if found else True   # 近期有超卖记录 → 还需过 OS 段
        os_ok = False
    else:
        std_ok = False
        os_ok = os_scan_ok
    return std_ok, os_ok

def _low_trigger(f, idx, std_ok, os_ok):
    """蓄势日 idx → 触发日 idx+1 判定（生产 backtest 同口径）。
    路径归属：标准蓄势优先（ok=True → 标准），否则近期超卖（recent_os=True）。
    触发参数按路径分流：标准 chg∈[10,15]/pos<0.55；超卖 chg∈[9,15]/pos<0.65；
    量比∈[1.5,7]、成交额∈[4e8,16e8]、收盘>蓄势日MA20。返回 (bool, path)。"""
    if not (std_ok or os_ok):
        return False, None
    nxt = f.iloc[idx + 1]
    lb, chg = float(nxt["lb"]), float(nxt["涨跌幅"])
    amt = float(nxt["成交额"])
    ma20 = float(f.iloc[idx]["MA20"])
    pos = float(nxt["pos60"])
    path = "标准蓄势" if std_ok else "近期超卖"
    if path == "标准蓄势":
        ok = (1.5 <= lb <= 7.0 and 10.0 <= chg <= 15.0
              and 4e8 <= amt <= 16e8 and nxt["收盘"] > ma20 and pos < 0.55)
    else:
        ok = (1.5 <= lb <= 7.0 and 9.0 <= chg <= 15.0
              and 4e8 <= amt <= 16e8 and nxt["收盘"] > ma20 and pos < 0.65)
    return (ok, path) if ok else (False, path)

# ---------- 主回放 ----------
def run():
    t0 = time.time()
    print("加载本地K线...", flush=True)
    feat = _load_all()
    codes = sorted(feat.keys())
    print(f"宇宙 {len(codes)} 只，加载 {time.time()-t0:.0f}s", flush=True)

    # 交易日历（并集）
    cal = sorted(set().union(*[set(d["日期"]) for d in feat.values()]))
    cal = [d for d in cal if pd.Timestamp("2024-01-02") <= d <= pd.Timestamp("2026-09-24")]
    print(f"交易日 {len(cal)} 天（{cal[0].date()} ~ {cal[-1].date()}）", flush=True)

    # 温度代理：每日上涨占比（%）（低位/初动分层用，low_pos_backtest_full 同口径）
    up_ratio = {}
    for d in cal:
        up = tot = 0
        for c in codes:
            f = feat[c]
            sub = f[f["日期"] == d]
            if sub.empty:
                continue
            tot += 1
            if float(sub.iloc[0]["涨跌幅"]) > 0:
                up += 1
        up_ratio[d] = up / tot * 100 if tot else 0

    # 趋势/短线闸门温度：当日收盘在 MA20 上方个股占比（backtest_engine._temp_proxy 同口径）
    ma20_ratio = {}
    for d in cal:
        above = tot = 0
        for c in codes:
            f = feat[c]
            sub = f[f["日期"] == d]
            if sub.empty:
                continue
            r = sub.iloc[0]
            if pd.isna(r["MA20"]):
                continue
            tot += 1
            if float(r["收盘"]) > float(r["MA20"]):
                above += 1
        ma20_ratio[d] = above / tot * 100 if tot else 0

    # 信号索引：{code: list of signal idx}，10日冷却
    def _cooldown(idx_list, cool=W_COOL):
        out, last = [], -99
        for i in sorted(idx_list):
            if i - last < cool:
                continue
            last = i
            out.append(i)
        return out

    trend_sig, short_sig, warm_sig = {}, {}, {}
    low_cand = {}  # 蓄势日
    for c in codes:
        f = feat[c]
        ts = _trend_signal(f)
        trend_sig[c] = _cooldown(f.index[ts].tolist(), cool=10)
        ss = _short_signal(f)
        short_sig[c] = _cooldown(f.index[ss].tolist(), cool=5)
        ws = _warm_signal(f)
        warm_sig[c] = _cooldown(f.index[ws].tolist(), cool=W_COOL)
        lc = [i for i in range(60, len(f) - 1) if _low_scan(f, i) != (False, False)]
        low_cand[c] = lc

    trades = {k: [] for k in ("趋势", "短线", "初动", "低位")}

    # ---- 趋势（每日 top2 by 成交额 + MA20上方占比≥55% 闸门，backtest_engine 同口径） ----
    for k in range(1, len(cal)):
        d_prev, d_now = cal[k - 1], cal[k]
        if ma20_ratio[d_prev] < TREND_TEMP_MIN:
            continue
        picks = []
        for c in codes:
            if c not in trend_sig:
                continue
            f = feat[c]
            idxs = [i for i in trend_sig[c] if f.iloc[i]["日期"] == d_prev]
            if not idxs:
                continue
            picks.append((c, idxs[0]))
        picks.sort(key=lambda x: -float(feat[x[0]].iloc[x[1]]["成交额"]))
        for c, i in picks[:TREND_MAX_PICKS]:
            f = feat[c]
            j = i + 1
            if j >= len(f):
                continue
            entry = float(f.iloc[j]["开盘"])
            if entry <= 0:
                continue
            pnl, days, why = _sim_generic(f, j, entry, TREND_STOP, TREND_TP1, TREND_MAX_HOLD, ma_struct=True)
            trades["趋势"].append({"买入日": str(d_now.date()), "代码": c, "名称": c,
                                   "买价": round(entry, 2), "收益%": round(pnl, 2),
                                   "持有天": days, "了结原因": why,
                                   "温度": round(up_ratio[d_prev], 1)})

    # ---- 短线（每日 top2 by 收盘 + MA20上方占比≥60% 闸门 + 涨停剔除） ----
    for k in range(1, len(cal)):
        d_prev, d_now = cal[k - 1], cal[k]
        if ma20_ratio[d_prev] < SHORT_TEMP_MIN:
            continue
        picks = []
        for c in codes:
            if c not in short_sig:
                continue
            f = feat[c]
            idxs = [i for i in short_sig[c] if f.iloc[i]["日期"] == d_prev]
            if not idxs:
                continue
            i = idxs[0]
            chg = float(f.iloc[i]["涨跌幅"])
            limit = 19.8 if c.startswith("30") else 9.8
            if chg >= limit:
                continue  # 已封板
            up_shadow = (float(f.iloc[i]["最高"]) - float(f.iloc[i]["收盘"])) / float(f.iloc[i]["收盘"])
            if up_shadow > SHORT_UPPER_SHADOW:
                continue
            picks.append((c, i))
        picks.sort(key=lambda x: -float(feat[x[0]].iloc[x[1]]["收盘"]))
        for c, i in picks[:SHORT_MAX_PICKS]:
            f = feat[c]
            j = i + 1
            if j >= len(f):
                continue
            entry = float(f.iloc[j]["开盘"])
            if entry <= 0:
                continue
            pnl, days, why = _sim_generic(f, j, entry, SHORT_STOP, SHORT_TP1, SHORT_HOLD_MAX,
                                          time_stop_days=SHORT_TIME_STOP)
            trades["短线"].append({"买入日": str(d_now.date()), "代码": c, "名称": c,
                                   "买价": round(entry, 2), "收益%": round(pnl, 2),
                                   "持有天": days, "了结原因": why,
                                   "温度": round(up_ratio[d_prev], 1)})

    # ---- 初动（无温度闸门；信号 T → T+1 开盘） ----
    for c in codes:
        f = feat[c]
        for i in warm_sig[c]:
            j = i + 1
            if j >= len(f):
                continue
            nxt = f.iloc[j]
            if nxt["开盘"] == nxt["最高"] == nxt["最低"] == nxt["收盘"]:
                continue  # 一字板买不进
            entry = float(nxt["开盘"])
            if entry <= 0:
                continue
            pnl, days, why = _sim_warm_v2(f, j, entry)
            trades["初动"].append({"买入日": str(f.iloc[j]["日期"].date()), "代码": c, "名称": c,
                                   "买价": round(entry, 2), "收益%": round(pnl, 2),
                                   "持有天": days, "了结原因": why,
                                   "温度": round(up_ratio.get(f.iloc[j]["日期"], 0), 1)})

    # ---- 低位（蓄势日→次日触发→触发日收盘入场；温度闸门=关，生产全历史回测同口径，温度落列离线分层） ----
    for c in codes:
        f = feat[c]
        for i in range(60, len(f) - 1):
            std_ok, os_ok = _low_scan(f, i)
            if not (std_ok or os_ok):
                continue
            if i + 1 >= len(f):
                continue
            trig, path = _low_trigger(f, i, std_ok, os_ok)
            if not trig:
                continue
            td = f.iloc[i + 1]["日期"]
            entry = float(f.iloc[i + 1]["收盘"])
            if entry <= 0:
                continue
            seg = f.iloc[i + 2:i + 7]
            if len(seg) < 5:
                continue
            r5 = (float(seg.iloc[4]["收盘"]) / entry - 1) * 100  # 生产不修正止损
            trades["低位"].append({"买入日": str(td.date()), "代码": c, "名称": c,
                                   "买价": round(entry, 2), "收益%": round(r5, 2),
                                   "持有天": 5, "了结原因": "T+5",
                                   "温度": round(up_ratio.get(td, 0), 1),
                                   "路径": path})

    # ---- 统计 ----
    def _stats(rows):
        if not rows:
            return None
        df = pd.DataFrame(rows)
        r = df["收益%"]
        win, loss = r[r > 0], r[r <= 0]
        nav = (1 + r / 100 / len(df)).cumprod()
        mdd = ((nav.cummax() - nav) / nav.cummax()).max() * 100
        s = {
            "笔数": len(df), "票数": int(df["代码"].nunique()),
            "胜率%": round(len(win) / len(df) * 100, 1),
            "平均收益%": round(r.mean(), 2),
            "平均盈利%": round(win.mean(), 2) if len(win) else 0,
            "平均亏损%": round(loss.mean(), 2) if len(loss) else 0,
            "盈亏比": round(abs(win.mean() / loss.mean()), 2) if len(win) and len(loss) else 0,
            "最好%": round(r.max(), 2), "最差%": round(r.min(), 2),
            "累计收益%": round(r.sum(), 1),
            "最大回撤%": round(mdd, 1),
            "平均持有天": round(df["持有天"].mean(), 1),
        }
        # 分年度
        df["年"] = df["买入日"].str[:4]
        s["分年度"] = {}
        for y, g in df.groupby("年"):
            rr = g["收益%"]
            w = rr[rr > 0]
            l = rr[rr <= 0]
            s["分年度"][y] = {
                "笔数": len(g),
                "胜率%": round(len(w) / len(g) * 100, 1),
                "平均收益%": round(rr.mean(), 2),
                "盈亏比": round(abs(w.mean() / l.mean()), 2) if len(w) and len(l) else 0,
            }
        return s

    stats = {}
    for k in trades:
        stats[k] = _stats(trades[k])

    # 相似度：趋势信号 ∩ 初动信号（同日同票）
    trend_pairs = set()
    for c, idxs in trend_sig.items():
        f = feat[c]
        for i in idxs:
            trend_pairs.add((c, str(f.iloc[i]["日期"].date())))
    warm_pairs = set()
    for c, idxs in warm_sig.items():
        f = feat[c]
        for i in idxs:
            warm_pairs.add((c, str(f.iloc[i]["日期"].date())))
    overlap = trend_pairs & warm_pairs
    stats["重叠"] = {
        "趋势信号": len(trend_pairs), "初动信号": len(warm_pairs),
        "同日同票重叠": len(overlap),
        "重叠占趋势%": round(len(overlap) / len(trend_pairs) * 100, 1) if trend_pairs else 0,
        "重叠占初动%": round(len(overlap) / len(warm_pairs) * 100, 1) if warm_pairs else 0,
    }

    # 输出
    out_dir = os.path.join(BASE, "data")
    detail = pd.concat([pd.DataFrame(v).assign(池子=k) for k, v in trades.items() if v], ignore_index=True)
    detail.to_csv(os.path.join(out_dir, "四池回测_明细_20260924.csv"), index=False)
    with open(os.path.join(out_dir, "四池回测_统计_20260924.json"), "w", encoding="utf-8") as fp:
        json.dump(stats, fp, ensure_ascii=False, indent=1)

    print("\n========== 四池对比（2024-01-02 ~ 2026-09-24，本地前复权） ==========", flush=True)
    hdr = f"{'池子':<6}{'笔数':>5}{'胜率%':>7}{'均收益%':>8}{'盈亏比':>7}{'最好%':>8}{'最差%':>8}{'累计%':>8}{'回撤%':>7}{'持有天':>6}"
    print(hdr)
    print("-" * len(hdr))
    for k in ("趋势", "短线", "初动", "低位"):
        s = stats[k]
        if not s:
            print(f"{k:<6} 无样本")
            continue
        print(f"{k:<6}{s['笔数']:>5}{s['胜率%']:>7}{s['平均收益%']:>8}{s['盈亏比']:>7}"
              f"{s['最好%']:>8}{s['最差%']:>8}{s['累计收益%']:>8}{s['最大回撤%']:>7}{s['平均持有天']:>6}")
    print("\n分年度（胜率/均收益/盈亏比）：")
    for k in ("趋势", "短线", "初动", "低位"):
        s = stats[k]
        if not s:
            continue
        yrs = "  ".join(f"{y}:{v['笔数']}笔 {v['胜率%']}%/{v['平均收益%']:+.2f}%/{v['盈亏比']}"
                        for y, v in s["分年度"].items())
        print(f"  {k}: {yrs}")
    print("\n信号重叠：", stats["重叠"])
    print(f"\n总耗时 {time.time()-t0:.0f}s → data/四池回测_统计_20260924.json / 明细csv", flush=True)

if __name__ == "__main__":
    run()
