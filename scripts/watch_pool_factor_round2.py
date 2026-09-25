# -*- coding: utf-8 -*-
"""第二轮转化率因子挖掘（2026-09-24 凌晨）。

基线 = 当前生产扫描口径：V6' + A(MA20上行) + 距高≤-27%。
已证伪勿重复：5日涨幅下限、振幅18、大额豁免、距60日低、D+E组合、
20日涨幅>5%、DIF>0/MACD多头、量比档（排序端已穷尽）。
本轮候选（本地K线可算）：缩量趋势/缩量天数/20日波动率/60日波动率/
MA60乖离/MA120乖离/均线粘合度/pos250/距20日低/20日跌幅/RSI14/尾盘强度。
评估：分档 u10（并集口径）+ T5/胜率；最优档做「加硬过滤」模拟 +
生产排序 top3 回放 + 时间切分（2025/2026）防过拟合。
"""
import glob
import os

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data")

# ---- 1. 事件 + 触发重判（LB=1.5）----
ev = pd.read_csv(os.path.join(DATA, "低位池_入池事件_20260923.csv"), dtype={"代码": str})
ev["入池日"] = pd.to_datetime(ev["入池日"])
trig = pd.read_csv(os.path.join(DATA, "trigger_lb_sensitivity.csv"), dtype={"代码": str})
trig["入池日"] = pd.to_datetime(trig["入池日"])
t15 = trig[trig["LB下界"] == 1.5][["代码", "入池日", "5日内启动", "10日内启动", "60日内启动", "启动后T5%"]].copy()
t15 = t15.rename(columns={c: f"触发_{c}" for c in ["5日内启动", "10日内启动", "60日内启动"]})
df = ev.merge(t15, on=["代码", "入池日"], how="inner")

B = df[
    (df["成交额亿"] <= 6) & (df["位置"] <= 0.40) & (df["距高%"] <= -25)
    & (df["MA20乖离%"] >= -5) & (df["20日振幅%"] < 25) & (df["量比"] <= 2.0)
].copy()
print(f"V6' 基线: {len(B)}")

# ---- 2. 本地 K 线（全字段）----
KL = {}
for p in glob.glob(os.path.join(DATA, "klines", "*.csv")):
    sym = os.path.basename(p)[:-4]
    code = sym[2:]
    if not code.startswith(("60", "00", "30")):
        continue
    raw = pd.read_csv(p)
    if raw.empty or len(raw) < 30:
        continue
    d = pd.DataFrame({
        "日期": pd.to_datetime(raw["date"]),
        "开盘": raw["open"].astype(float), "收盘": raw["last"].astype(float),
        "最高": raw["high"].astype(float), "最低": raw["low"].astype(float),
        "量": raw["volume"].astype(float),
    }).drop_duplicates("日期").sort_values("日期").reset_index(drop=True)
    KL[code] = d
print(f"K线加载: {len(KL)} 只")

ma20map = {c: (d["日期"].values, d["收盘"].rolling(20).mean().values) for c, d in KL.items()}

def ma20_up(code, d):
    dm = ma20map.get(code)
    if dm is None:
        return None
    dates, m = dm
    idx = np.where(dates == np.datetime64(d))[0]
    if len(idx) == 0 or idx[0] < 25:
        return None
    i = idx[0]
    if np.isnan(m[i]) or np.isnan(m[i - 5]):
        return None
    return bool(m[i] > m[i - 5])

B["ma20up"] = [ma20_up(r["代码"], r["入池日"]) for _, r in B.iterrows()]
A = B[B["ma20up"] == True].copy()
F = A[A["距高%"] <= -27].copy()
print(f"A 后全集: {len(A)} ｜ 生产基线(距高≤-27): {len(F)} 事件 / {F['入池日'].nunique()} 天")

# ---- 3. 因子计算（入池日截断，防未来函数）----
def vol_ratio_5_20(d, i):
    if i < 20: return None
    v5 = d["量"].iloc[i-4:i+1].mean(); v20 = d["量"].iloc[i-19:i+1].mean()
    return v5 / v20 if v20 > 0 else None

def shrink_days(d, i):
    if i < 30: return None
    seg = d["量"].iloc[i-9:i+1].values
    mav = d["量"].iloc[i-29:i+1].rolling(20).mean().values[20:30]
    return int(np.sum(seg < mav))

def volat(d, i, n):
    if i < n: return None
    r = d["收盘"].pct_change().iloc[i-n+1:i+1]
    return float(r.std())

def ma_bias(d, i, n):
    if i < n: return None
    m = d["收盘"].iloc[i-n+1:i+1].mean()
    return float(d["收盘"].iloc[i] / m - 1) if m > 0 else None

def ma_align(d, i):
    if i < 20: return None
    m5 = d["收盘"].iloc[i-4:i+1].mean(); m10 = d["收盘"].iloc[i-9:i+1].mean(); m20 = d["收盘"].iloc[i-19:i+1].mean()
    c = d["收盘"].iloc[i]
    return float((max(m5, m10, m20) - min(m5, m10, m20)) / c) if c > 0 else None

def pos250(d, i):
    if i < 250: return None
    w = d["收盘"].iloc[i-249:i+1]
    lo, hi = w.min(), w.max()
    return float((d["收盘"].iloc[i] - lo) / (hi - lo)) if hi > lo else None

def dist20_low(d, i):
    if i < 20: return None
    lo = d["收盘"].iloc[i-19:i+1].min()
    return float(d["收盘"].iloc[i] / lo - 1) if lo > 0 else None

def chg20(d, i):
    if i < 20: return None
    p = d["收盘"].iloc[i-20]
    return float(d["收盘"].iloc[i] / p - 1) if p > 0 else None

def rsi14(d, i):
    if i < 15: return None
    ch = d["收盘"].diff().iloc[i-13:i+1]
    up = ch[ch > 0].sum(); dn = -ch[ch < 0].sum()
    return float(100 * up / (up + dn)) if (up + dn) > 0 else None

def close_strength3(d, i):
    if i < 3: return None
    vals = []
    for j in range(i-2, i+1):
        h, l, c = d["最高"].iloc[j], d["最低"].iloc[j], d["收盘"].iloc[j]
        vals.append(0.5 if h <= l else (c - l) / (h - l))
    return float(np.mean(vals))

date_idx = {}
for c, d in KL.items():
    date_idx[c] = {t: k for k, t in enumerate(d["日期"].values)}

def fget(fn, code, d, *a):
    di = date_idx.get(code)
    if di is None: return None
    key = np.datetime64(pd.Timestamp(d))
    i = di.get(key)
    if i is None: return None
    return fn(KL[code], i, *a)

F["缩量趋势5/20"] = [fget(vol_ratio_5_20, r["代码"], r["入池日"]) for _, r in F.iterrows()]
F["缩量天数10"] = [fget(shrink_days, r["代码"], r["入池日"]) for _, r in F.iterrows()]
F["波动率20"] = [fget(volat, r["代码"], r["入池日"], 20) for _, r in F.iterrows()]
F["波动率60"] = [fget(volat, r["代码"], r["入池日"], 60) for _, r in F.iterrows()]
F["MA60乖离"] = [fget(ma_bias, r["代码"], r["入池日"], 60) for _, r in F.iterrows()]
F["MA120乖离"] = [fget(ma_bias, r["代码"], r["入池日"], 120) for _, r in F.iterrows()]
F["均线粘合度"] = [fget(ma_align, r["代码"], r["入池日"]) for _, r in F.iterrows()]
F["pos250"] = [fget(pos250, r["代码"], r["入池日"]) for _, r in F.iterrows()]
F["距20日低"] = [fget(dist20_low, r["代码"], r["入池日"]) for _, r in F.iterrows()]
F["跌幅20"] = [fget(chg20, r["代码"], r["入池日"]) for _, r in F.iterrows()]
F["RSI14"] = [fget(rsi14, r["代码"], r["入池日"]) for _, r in F.iterrows()]
F["尾盘强度3"] = [fget(close_strength3, r["代码"], r["入池日"]) for _, r in F.iterrows()]

def u10(s):
    return ((s["触发_10日内启动"] == True) | (s["入池直接T5%"] > 15)).mean() * 100

def stats(s):
    st = s[(s["触发_60日内启动"] == True)]["启动后T5%_y"].dropna()
    t5 = st.mean() if len(st) >= 10 else float("nan")
    wr = (st > 0).mean() * 100 if len(st) >= 10 else float("nan")
    return t5, wr

# ---- 4. 分档表 ----
BINS = [
    ("缩量趋势5/20", [0, .5, .7, .9, 1.2, 9], "{:.2f}"),
    ("缩量天数10", [0, 3, 6, 8, 11], "{:.0f}"),
    ("波动率20", [0, .015, .025, .04, 1], "{:.3f}"),
    ("波动率60", [0, .02, .03, .045, 1], "{:.3f}"),
    ("MA60乖离", [-1, -.25, -.15, -.08, 0], "{:+.2f}"),
    ("MA120乖离", [-1, -.3, -.2, -.1, 0], "{:+.2f}"),
    ("均线粘合度", [0, .02, .04, .08, 1], "{:.3f}"),
    ("pos250", [0, .1, .25, .45, .7, 1.01], "{:.2f}"),
    ("距20日低", [0, .03, .06, .10, 9], "{:.2f}"),
    ("跌幅20", [-1, -.25, -.15, -.08, 0], "{:+.2f}"),
    ("RSI14", [0, 25, 32, 40, 55, 100], "{:.0f}"),
    ("尾盘强度3", [0, .35, .5, .65, 1.01], "{:.2f}"),
    ("MA20乖离%", [-6, -3.5, -2, -1, 0], "{:+.1f}"),
]

base_t5, base_wr = stats(F)
print(f"\n基线全集 u10={u10(F):.2f}% T5={base_t5:+.2f}% 胜率={base_wr:.1f}%\n")
print("=== 分档区分度（生产基线 距高≤-27）===")
summary = []
for col, bins, fmt in BINS:
    s_all = F[F[col].notna()]
    print(f"-- {col}（可判 {len(s_all)}/{len(F)}）--")
    for lo, hi in zip(bins[:-1], bins[1:]):
        s = s_all[(s_all[col] >= lo) & (s_all[col] < hi)]
        if len(s) < 30:
            print(f"  [{fmt.format(lo)},{fmt.format(hi)}): n={len(s)} 不足")
            continue
        t5, wr = stats(s)
        line = f"  [{fmt.format(lo)},{fmt.format(hi)}): n={len(s):4d} u10={u10(s):6.2f}% T5={t5:+6.2f}% 胜率={wr:5.1f}%"
        print(line)
        summary.append(dict(因子=col, 档=f"[{fmt.format(lo)},{fmt.format(hi)})", n=len(s),
                            u10=round(u10(s), 2), T5=None if pd.isna(t5) else round(t5, 2),
                            胜率=None if pd.isna(wr) else round(wr, 1)))

# ---- 5. 最优档硬过滤模拟 + 生产排序 top3 回放 + 时间切分 ----
print("\n=== 候选硬过滤模拟（各因子 u10 最高且 n≥50 的档，叠加生产 top3）===")
rows = []
for col, bins, fmt in BINS:
    s_all = F[F[col].notna()]
    best, best_u, best_lo, best_hi = None, -1, None, None
    for lo, hi in zip(bins[:-1], bins[1:]):
        s = s_all[(s_all[col] >= lo) & (s_all[col] < hi)]
        if len(s) < 50:
            continue
        u = u10(s)
        if u > best_u:
            best, best_u, best_lo, best_hi = s, u, lo, hi
    if best is None:
        continue
    filt = best.copy()
    filt["是标准蓄势"] = (filt["路径"] == "标准蓄势").astype(int)
    t = filt.sort_values(["是标准蓄势", "位置", "量比"], ascending=[False, True, True])
    t3 = t.groupby("入池日", group_keys=False).head(3)
    days = F["入池日"].nunique()
    t3_t5, t3_wr = stats(t3)
    f26 = filt[filt["入池日"] >= "2026-01-01"]
    f25 = filt[filt["入池日"] < "2026-01-01"]
    rows.append(dict(
        因子=col, 最优档=f"[{fmt.format(best_lo)},{fmt.format(best_hi)})",
        全集n=len(filt), 全集u10=round(u10(filt), 2), 全集T5=None if pd.isna(t3_t5) else round(stats(filt)[0], 2),
        top3_n=len(t3), top3_只天=round(len(t3) / days, 2), top3_u10=round(u10(t3), 2),
        top3_T5=None if pd.isna(t3_t5) else round(t3_t5, 2), top3_胜率=None if pd.isna(t3_wr) else round(t3_wr, 1),
        u10_2025=round(u10(f25), 2) if len(f25) >= 30 else None,
        u10_2026=round(u10(f26), 2) if len(f26) >= 30 else None,
    ))
out2 = pd.DataFrame(rows)
if len(out2):
    print(out2.to_string(index=False))

pd.DataFrame(summary).to_csv(os.path.join(DATA, "factor_round2_bins.csv"), index=False, encoding="utf-8-sig")
if len(out2):
    out2.to_csv(os.path.join(DATA, "factor_round2_filter.csv"), index=False, encoding="utf-8-sig")
F.to_csv(os.path.join(DATA, "factor_round2_events.csv"), index=False, encoding="utf-8-sig")
print("\n→ factor_round2_bins.csv / factor_round2_filter.csv / factor_round2_events.csv")
