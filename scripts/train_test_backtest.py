#!/Users/chenyuting/.workbuddy/binaries/python/versions/3.13.12/bin/python3
# -*- coding: utf-8 -*-
"""训练/测试集分离四池回测（2022-2023 训练 vs 2025-2026 测试）。

以 four_pool_compare.py 为骨架，改造点：
  - 交易日历分 train/test 两段分别过滤；
  - 趋势/短线逐日回放循环参数化（各自接受日历段 + 段标签）；
  - 初动/低位按信号索引逐票扫描天然跨段，买入日落入哪段归哪段；
  - 输出带「段」列，统计按 段×池 与 分年度 展开。

统一口径（防未来函数，与 four_pool_compare.py 完全一致）：
  - 信号日 T 用 T 收盘数据判定 → 成交价统一 T+1 开盘价（初动/趋势/短线）；
    低位池沿用生产口径「触发日收盘」入场。
  - 同一票 10 交易日冷却（初动/低位沿用各自实现；趋势/短线按每日 topN）。
  - 温度闸门：趋势/短线用「收盘在MA20上方个股占比」（趋势≥55/短线≥60）；
    初动无闸门；低位温度=关但落列离线分层。
  - 每笔占 1/N 仓位等权复利算资金曲线与回撤（与 backtest_engine._stats 同口径）。

用法：python train_test_backtest.py
  [--train-start 2022-01-01] [--train-end 2023-12-31]
  [--test-start 2025-01-01]  [--test-end 2026-09-28]

输出：data/回测_训练2022-23_测试2025-26_明细.csv + 统计.json
"""
import argparse
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

# ---------- 生产参数（config.py 同步，与 four_pool_compare.py 完全一致） ----------
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
# 低位（config LOW_POS_ENTRY_*）
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
        # 60日位置 / 距高：趋势用收盘口径；初动/低位用高低价口径
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

# ---------- 通用结算（与 four_pool_compare.py 一致） ----------
def _sim_generic(f, idx, entry, stop_pct, tp1_pct, max_hold, time_stop_days=0,
                 ma_struct=False):
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

# ---------- 信号器（与 four_pool_compare.py 一致） ----------
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
        & f["week_up"].notna() & f["week_up"]   # 2026-09-28 周线上扬硬过滤
        & f["MA20"].notna() & f["vol_ratio"].notna() & f["rsi14"].notna() & f["pos60_c"].notna()
    )
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
        & f["week_up"].notna() & f["week_up"]   # 2026-09-28 周线上扬硬过滤
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
    for j in range(max(0, idx - lookback), idx):
        r = f.iloc[j]
        if r["pos60"] < 0.5 or r["dist60"] < -30:
            return True, j
    return False, None

def _low_scan(f, idx):
    row = f.iloc[idx]
    ok = False
    if not (pd.isna(row.get("pos60")) or pd.isna(row.get("dist60"))):
        if (row["pos60"] < 0.40 and row["dist60"] <= -27
                and (pd.isna(row["MA20"]) or row["bias20"] >= -5)
                and row["ma20up"]
                and pd.notna(row["lb"]) and pd.notna(row["lb5mean"])
                and row["lb"] <= 2.0 and row["lb5mean"] <= 1.20
                and -10.0 <= row["chg5"] <= 5.0
                and row["涨跌幅"] <= 5.0
                and row["amp20"] < 0.25
                and 2e8 <= row["成交额"] <= 6e8):
            ok = True
    found, first_idx = _recent_oversold(f, idx + 1)
    os_scan_ok = False
    if found and first_idx is not None:
        if ((idx - first_idx) >= 10
                and 2e8 <= row["成交额"] <= 6e8
                and row["pos60"] < 0.40
                and row["dist60"] <= -27
                and (pd.isna(row["MA20"]) or row["bias20"] >= -5)
                and row["ma20up"]
                and row["lb"] <= 3.0 and row["lb5mean"] <= 2.0
                and row["chg5"] <= 0.0
                and row["涨跌幅"] <= 5.0):
            os_scan_ok = True
    if ok:
        std_ok = os_scan_ok if found else True
        os_ok = False
    else:
        std_ok = False
        os_ok = os_scan_ok
    return std_ok, os_ok

def _low_trigger(f, idx, std_ok, os_ok):
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

# ---------- 统计 ----------
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

# ---------- 主回放 ----------
def run():
    ap = argparse.ArgumentParser()
    ap.add_argument("--train-start", default="2022-01-01")
    ap.add_argument("--train-end", default="2023-12-31")
    ap.add_argument("--test-start", default="2025-01-01")
    ap.add_argument("--test-end", default="2026-09-28")
    args = ap.parse_args()

    t0 = time.time()
    print("加载本地K线...", flush=True)
    feat = _load_all()
    codes = sorted(feat.keys())
    print(f"宇宙 {len(codes)} 只，加载 {time.time()-t0:.0f}s", flush=True)

    cal_all = sorted(set().union(*[set(d["日期"]) for d in feat.values()]))
    cal_train = [d for d in cal_all
                 if pd.Timestamp(args.train_start) <= d <= pd.Timestamp(args.train_end)]
    cal_test = [d for d in cal_all
                if pd.Timestamp(args.test_start) <= d <= pd.Timestamp(args.test_end)]
    print(f"交易日：训练 {len(cal_train)} 天（{cal_train[0].date()}~{cal_train[-1].date()}），"
          f"测试 {len(cal_test)} 天（{cal_test[0].date()}~{cal_test[-1].date()}）", flush=True)

    # 温度代理：每日上涨占比（%）；用 dict 加速（避免逐日逐票 df 过滤）
    up_map = {c: dict(zip(f["日期"].dt.strftime("%Y-%m-%d"), f["涨跌幅"])) for c, f in feat.items()}
    ma_map = {c: dict(zip(f["日期"].dt.strftime("%Y-%m-%d"),
                          list(zip(f["收盘"], f["MA20"])))) for c, f in feat.items()}
    up_ratio, ma20_ratio = {}, {}
    for d in cal_train + cal_test:
        ds = d.strftime("%Y-%m-%d")
        up = tot = above = atm = 0
        for c in codes:
            v = up_map[c].get(ds)
            if v is not None and not pd.isna(v):
                tot += 1
                if v > 0:
                    up += 1
            m = ma_map[c].get(ds)
            if m and m[1] is not None and not pd.isna(m[1]):
                atm += 1
                if m[0] > m[1]:
                    above += 1
        up_ratio[d] = up / tot * 100 if tot else 0
        ma20_ratio[d] = above / atm * 100 if atm else 0

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
    low_cand = {}
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

    # 趋势/短线：逐日回放，segment 参数化
    def _run_trend(cal, seg):
        for k in range(1, len(cal)):
            d_prev, d_now = cal[k - 1], cal[k]
            if ma20_ratio.get(d_prev, 0) < TREND_TEMP_MIN:
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
                trades["趋势"].append({"段": seg, "买入日": str(d_now.date()), "代码": c, "名称": c,
                                       "买价": round(entry, 2), "收益%": round(pnl, 2),
                                       "持有天": days, "了结原因": why,
                                       "温度": round(up_ratio.get(d_prev, 0), 1)})

    def _run_short(cal, seg):
        for k in range(1, len(cal)):
            d_prev, d_now = cal[k - 1], cal[k]
            if ma20_ratio.get(d_prev, 0) < SHORT_TEMP_MIN:
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
                    continue
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
                trades["短线"].append({"段": seg, "买入日": str(d_now.date()), "代码": c, "名称": c,
                                       "买价": round(entry, 2), "收益%": round(pnl, 2),
                                       "持有天": days, "了结原因": why,
                                       "温度": round(up_ratio.get(d_prev, 0), 1)})

    # 初动（无温度闸门；信号 T → T+1 开盘）
    def _run_warm(cal, seg):
        cal_set = set(cal)
        for c in codes:
            f = feat[c]
            for i in warm_sig[c]:
                j = i + 1
                if j >= len(f):
                    continue
                nxt = f.iloc[j]
                buy_date = nxt["日期"]
                if buy_date not in cal_set:
                    continue
                if nxt["开盘"] == nxt["最高"] == nxt["最低"] == nxt["收盘"]:
                    continue
                entry = float(nxt["开盘"])
                if entry <= 0:
                    continue
                pnl, days, why = _sim_warm_v2(f, j, entry)
                trades["初动"].append({"段": seg, "买入日": str(buy_date.date()), "代码": c, "名称": c,
                                       "买价": round(entry, 2), "收益%": round(pnl, 2),
                                       "持有天": days, "了结原因": why,
                                       "温度": round(up_ratio.get(buy_date, 0), 1)})

    # 低位（蓄势日→次日触发→触发日收盘入场；温度闸门=关）
    def _run_low(cal, seg):
        cal_set = set(cal)
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
                if td not in cal_set:
                    continue
                entry = float(f.iloc[i + 1]["收盘"])
                if entry <= 0:
                    continue
                seg_rows = f.iloc[i + 2:i + 7]
                if len(seg_rows) < 5:
                    continue
                r5 = (float(seg_rows.iloc[4]["收盘"]) / entry - 1) * 100
                trades["低位"].append({"段": seg, "买入日": str(td.date()), "代码": c, "名称": c,
                                       "买价": round(entry, 2), "收益%": round(r5, 2),
                                       "持有天": 5, "了结原因": "T+5",
                                       "温度": round(up_ratio.get(td, 0), 1),
                                       "路径": path})

    print("[回放] 训练段...", flush=True)
    _run_trend(cal_train, "训练")
    _run_short(cal_train, "训练")
    _run_warm(cal_train, "训练")
    _run_low(cal_train, "训练")
    print("[回放] 测试段...", flush=True)
    _run_trend(cal_test, "测试")
    _run_short(cal_test, "测试")
    _run_warm(cal_test, "测试")
    _run_low(cal_test, "测试")

    # 统计：段×池
    stats = {}
    for pool in ("趋势", "短线", "初动", "低位"):
        stats[pool] = {"训练": _stats([r for r in trades[pool] if r["段"] == "训练"]),
                       "测试": _stats([r for r in trades[pool] if r["段"] == "测试"])}

    # 输出
    out_dir = os.path.join(BASE, "data")
    detail = pd.concat([pd.DataFrame(v).assign(池子=k) for k, v in trades.items() if v],
                       ignore_index=True)
    dpath = os.path.join(out_dir, "回测_训练2022-23_测试2025-26_明细.csv")
    detail.to_csv(dpath, index=False)
    spath = os.path.join(out_dir, "回测_训练2022-23_测试2025-26_统计.json")
    with open(spath, "w", encoding="utf-8") as fp:
        json.dump(stats, fp, ensure_ascii=False, indent=1)

    print("\n========== 训练(2022-2023) vs 测试(2025-2026) ==========", flush=True)
    hdr = f"{'池子':<4}{'段':<4}{'笔数':>5}{'胜率%':>7}{'均收益%':>8}{'盈亏比':>7}{'回撤%':>7}{'持有天':>6}"
    print(hdr)
    print("-" * len(hdr))
    for pool in ("趋势", "短线", "初动", "低位"):
        for seg in ("训练", "测试"):
            s = stats[pool][seg]
            if not s:
                print(f"{pool:<4}{seg:<4} 无样本")
                continue
            print(f"{pool:<4}{seg:<4}{s['笔数']:>5}{s['胜率%']:>7}{s['平均收益%']:>8}{s['盈亏比']:>7}"
                  f"{s['最大回撤%']:>7}{s['平均持有天']:>6}")
    print("\n分年度（笔数/胜率/均收益/盈亏比）：")
    for pool in ("趋势", "短线", "初动", "低位"):
        for seg in ("训练", "测试"):
            s = stats[pool][seg]
            if not s or not s["分年度"]:
                continue
            yrs = "  ".join(f"{y}:{v['笔数']}笔 {v['胜率%']}%/{v['平均收益%']:+.2f}%/{v['盈亏比']}"
                            for y, v in s["分年度"].items())
            print(f"  {pool}·{seg}: {yrs}")
    print(f"\n总耗时 {time.time()-t0:.0f}s → data/回测_训练2022-23_测试2025-26_明细.csv / 统计.json", flush=True)

if __name__ == "__main__":
    run()
