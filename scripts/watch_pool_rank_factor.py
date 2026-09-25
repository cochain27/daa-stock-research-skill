# -*- coding: utf-8 -*-
"""排序因子区分度研究（2026-09-24 凌晨）。

背景：用户拍板撤掉排序端「MA20上行优先」（+1pp 转化率换 5pp 胜率 + 0.8pp T5，不值），
     排序回退 pos60→量比。任务：找有没有比 pos60→量比 更好的排序因子。

基准：A 开 + pos→lb 每日 top3 = watch_pool_ab_combo.py ③A+B（并集10 12.93%, n=348, T5 +2.22%, 胜率50%）
方法：
  1. A 开全集（n≈470）单因子分档扫描——并集10/T5/胜率，看方向与区分度
  2. 候选因子每日 top3 回放（两个方向都试）——直接对比基准 12.93%
口径与 ab_combo 完全一致（V6' 基线 + LB1.5 触发重判 + 并集 = 触发 ∪ 入池直接T5>15%）。
"""
import os

import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data")

# ---- join 与 A 开全集（与 watch_pool_ab_combo.py 完全一致）----
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

# MA20 上行（生产口径）——事件 CSV 无此列，用入池收盘/MA20乖离 不可替代，需 K 线。
# 简化：CSV 无 MA20 绝对值，此处复用 ab_combo 的 K 线逻辑太重（470 事件 2 分钟）。
# 改用研究口径的轻量近似不可取（已证口径差 1pp）。直接读 ab_combo 中间产物？无。
# → 老实算 K 线。
import glob
import numpy as np

KL = {}
for p in glob.glob(os.path.join(DATA, "klines", "*.csv")):
    sym = os.path.basename(p)[:-4]
    code = sym[2:]
    if not code.startswith(("60", "00", "30")):
        continue
    raw = pd.read_csv(p)
    if raw.empty:
        continue
    c = raw["last"].astype(float)
    if len(c) < 30:
        continue
    KL[code] = pd.DataFrame({"日期": pd.to_datetime(raw["date"]), "收盘": c}) \
        .drop_duplicates("日期").sort_values("日期").reset_index(drop=True)

ma20map = {}
for code, d in KL.items():
    ma20map[code] = (d["日期"].values, d["收盘"].rolling(20).mean().values)

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
print(f"A 开全集: n={len(A)} / {A['入池日'].nunique()} 天")
print(f"基准（A开+pos→lb top3）= ③A+B: 并集10 12.93% / T5 +2.22% / 胜率 50.0%（n=348）")
print()

def u10(s):
    return ((s["触发_10日内启动"] == True) | (s["入池直接T5%"] > 15)).mean() * 100

def stat_line(s):
    n = len(s)
    st = s[(s["触发_60日内启动"] == True)]["启动后T5%_y"].dropna()
    t5 = st.mean() if len(st) >= 10 else float("nan")
    wr = (st > 0).mean() * 100 if len(st) >= 10 else float("nan")
    return f"n={n:4d} u10={u10(s):6.2f}% T5={t5:+6.2f}% 胜率={wr:5.1f}%"

# ---- 1. 分档扫描（连续因子按三分位，离散因子按取值）----
print("=== 分档扫描（A 开全集，并集10%）===")
CONT = [("量比", "asc"), ("5日量比", "asc"), ("5日涨幅%", "?"), ("20日振幅%", "asc"),
        ("位置", "asc"), ("距高%", "asc"), ("成交额亿", "asc"), ("MA20乖离%", "?"),
        ("超卖龄", "?"), ("超卖深度", "?"), ("连续蓄势", "?")]
DISC = ["量能收缩", "路径"]

for col, hint in CONT:
    s = A[col].dropna()
    if len(s) < 60:
        print(f"-- {col}: 可判样本不足({len(s)})")
        continue
    try:
        _, edges = pd.qcut(s, 3, retbins=True, duplicates="drop")
    except Exception:
        print(f"-- {col}: qcut 失败")
        continue
    print(f"-- {col} (方向参考:{hint})")
    edges = sorted(set(edges))
    for lo, hi in zip(edges[:-1], edges[1:]):
        sub = A[(A[col] >= lo) & (A[col] <= hi if hi == edges[-1] else A[col] < hi)]
        if len(sub) < 20:
            continue
        print(f"   [{lo:7.2f},{hi:7.2f}) {stat_line(sub)}")

for col in DISC:
    print(f"-- {col} (离散)")
    for v, sub in A.groupby(col):
        if len(sub) < 20:
            continue
        print(f"   {v}: {stat_line(sub)}")

# ---- 2. 每日 top3 回放（因子两方向 × 对比基准）----
print()
print("=== 每日 top3 回放（A 开全集，基准=位置→量比）===")

def topn(sub, cols, asc):
    return sub.sort_values(cols, ascending=asc).groupby("入池日", group_keys=False).head(3)

cands = [
    ("基准 位置→量比", ["位置", "量比"], [True, True]),
    ("量比→位置", ["量比", "位置"], [True, True]),
    ("5日量比→位置", ["5日量比", "位置"], [True, True]),
    ("20日振幅→位置", ["20日振幅%", "位置"], [True, True]),
    ("位置→量比→20日振幅", ["位置", "量比", "20日振幅%"], [True, True, True]),
    ("MA20乖离→位置", ["MA20乖离%", "位置"], [False, True]),   # 乖离高(贴MA20)优先
    ("乖离贴MA20→量比", ["MA20乖离%", "量比"], [False, True]),
    ("5日涨幅深跌优先", ["5日涨幅%", "位置"], [True, True]),
    ("成交额小优先", ["成交额亿", "位置"], [True, True]),
    ("连续蓄势久优先", ["连续蓄势", "位置"], [False, True]),
    ("超卖龄久优先", ["超卖龄", "位置"], [False, True]),
    ("超卖深度→位置", ["超卖深度", "位置"], [True, True]),
]
rows = []
for name, cols, asc in cands:
    t = topn(A, cols, asc)
    n = len(t)
    u10v = u10(t)
    st = t[(t["触发_60日内启动"] == True)]["启动后T5%_y"].dropna()
    t5 = st.mean() if len(st) >= 10 else float("nan")
    wr = (st > 0).mean() * 100 if len(st) >= 10 else float("nan")
    rows.append(dict(排序=name, n=n, u10=round(u10v, 2), T5=None if pd.isna(t5) else round(t5, 2),
                     胜率=None if pd.isna(wr) else round(wr, 1)))
out = pd.DataFrame(rows)
print(out.to_string(index=False))
p = os.path.join(DATA, "rank_factor_study.csv")
out.to_csv(p, index=False, encoding="utf-8-sig")
print(f"\n→ {p}")
