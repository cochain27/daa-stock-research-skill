# -*- coding: utf-8 -*-
"""A+B 组合转化率回测（2026-09-23 晚）。

问题：A（MA20上行硬过滤）单独并集10=13.31%，B（top3截断）单独=10.82%，
     A+B 组合口径的转化率从未算过（台账 115 行留空）。

口径（与既有研究完全对齐）：
  - 样本 = 事件文件 V6' 基线（成交额≤6亿+位置≤0.40+距高≤-25%+乖离≥-5%+振幅<25+量比≤2.0）
  - 触发重判 = data/trigger_lb_sensitivity.csv 的 LB=1.5 行（生产现行触发口径）
  - 并集启动 = 触发 ∪ 入池直接T5%>15%（温和启动兜底）
  - A = MA20 上行（当日MA20 > 5日前MA20，本地K线现算）
  - B = 按入池日分组 top3（排序 MA20上行 desc → 位置 asc → 量比 asc，A后全True退化为位置→量比）
  - 锚点校准：全基线并集10 应≈9.65%，A后应≈13.31%，对齐则组合数可信
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
print(f"事件×重判 join: {len(df)}（事件 {len(ev)}，重判 {len(t15)}）")

# V6' 基线
B = df[
    (df["成交额亿"] <= 6) & (df["位置"] <= 0.40) & (df["距高%"] <= -25)
    & (df["MA20乖离%"] >= -5) & (df["20日振幅%"] < 25) & (df["量比"] <= 2.0)
].copy()
print(f"V6' 基线: {len(B)} 事件 / {B['入池日'].nunique()} 天 / 每天 {len(B)/B['入池日'].nunique():.2f}")

# ---- 2. 本地 K 线 → 每事件 MA20 上行 ----
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

ma20 = {}
close_hist = {}
for code, d in KL.items():
    m = d["收盘"].rolling(20).mean()
    ma20[code] = (d["日期"], m)
    close_hist[code] = d["日期"].values, d["收盘"].values

def ma20_up(code, d):
    dm = ma20.get(code)
    if dm is None:
        return None
    dates, m = dm
    idx = dates[dates == d].index
    if len(idx) == 0 or idx[0] < 25:
        return None
    i = idx[0]
    return bool(m.iloc[i] > m.iloc[i - 5])

def ma20_up_research(code, d):
    """研究脚本口径复现：ma20(i) vs close[i-24:i-19] 均值，样本窗 i>=65"""
    ch = close_hist.get(code)
    if ch is None:
        return None
    dates, close = ch
    idx = np.where(dates == np.datetime64(d))[0]
    if len(idx) == 0 or idx[0] < 65:
        return None
    i = idx[0]
    ma_now = close[i - 19:i + 1].mean()
    ma_prev = close[i - 24:i - 19].mean()
    return bool(ma_now > ma_prev)

B["ma20up"] = [ma20_up(r["代码"], r["入池日"]) for _, r in B.iterrows()]
B["ma20up_rs"] = [ma20_up_research(r["代码"], r["入池日"]) for _, r in B.iterrows()]
print(f"生产口径可判: {B['ma20up'].notna().sum()} / 研究口径可判: {B['ma20up_rs'].notna().sum()}")
B = B[B["ma20up"].notna()].copy()
B["ma20up"] = B["ma20up"].astype(bool)
# 研究口径子样本（模拟 newfactor_study 的 i>=65 截断）：缺失研究口径值的剔除
BRS = B[B["ma20up_rs"].notna()].copy()
BRS["ma20up_rs"] = BRS["ma20up_rs"].astype(bool)
print(f"MA20上行可判: {len(B)} 事件（生产口径）")

# ---- 3. 并集口径工具 ----
def union_stats(sub, name):
    n = len(sub)
    if n == 0:
        print(f"{name}: 0 事件")
        return None
    u5 = ((sub["触发_5日内启动"] == True) | (sub["入池直接T5%"] > 15)).mean() * 100
    u10 = ((sub["触发_10日内启动"] == True) | (sub["入池直接T5%"] > 15)).mean() * 100
    u60 = ((sub["触发_60日内启动"] == True) | (sub["入池直接T5%"] > 15)).mean() * 100
    daily = n / sub["入池日"].nunique()
    st = sub[(sub["触发_60日内启动"] == True)]["启动后T5%_y"].dropna()
    t5m = st.mean() if len(st) >= 10 else float("nan")
    wr = (st > 0).mean() * 100 if len(st) >= 10 else float("nan")
    print(f"{name}: n={n} 每天{daily:.2f}(生产~{daily*3.1:.1f}) 并集5日 {u5:.2f}% / 10日 {u10:.2f}% / 60日 {u60:.2f}% | 启动后T5 {t5m:+.2f}% 胜率{wr:.1f}%")
    return dict(name=name, n=n, daily=daily, u5=u5, u10=u10, u60=u60, t5=t5m, wr=wr)

print()
A = B[B["ma20up"]]
r_base = union_stats(B, "①V6'基线（锚点应≈9.65）     ")
r_a = union_stats(A, "②A后=MA20上行（生产shift5口径）")
AR = BRS[BRS["ma20up_rs"]]
union_stats(AR, "②'A后·研究口径复现（应≈13.31）")

# ---- 4. B：按日 top3 截断（A 后样本）----
def topn(sub, n):
    s = sub.sort_values(["ma20up", "位置", "量比"], ascending=[False, True, True])
    return s.groupby("入池日", group_keys=False).head(n)

AB = topn(A, 3)
r_ab = union_stats(AB, "③A+B组合（MA20上行+每日top3）")

# 对照：A后按旧排序（仅位置→量比）应与③同（A后 ma20up 全 True）
# 额外：B 无 A 时的口径（历史 10.82% 附近校验）
BN = topn(B, 3)
union_stats(BN, "④仅B无A（历史校验~10.8）      ")

# ---- 5. 排序维度区分度检验（A 后样本，解释 top3 损失）----
print()
print("=== A后样本排序维度分桶（并集10%）===")
for col, lab, bins in [("位置", "pos60", [0, .2, .3, .4]), ("量比", "量比", [0, .8, 1.2, 2.0])]:
    print(f"-- {lab} --")
    for lo, hi in zip(bins[:-1], bins[1:]):
        s = A[(A[col] >= lo) & (A[col] < hi)]
        if len(s) < 20:
            print(f"  [{lo},{hi}): n={len(s)} 样本不足")
            continue
        u10 = ((s["触发_10日内启动"] == True) | (s["入池直接T5%"] > 15)).mean() * 100
        print(f"  [{lo},{hi}): n={len(s):4d} 并集10 {u10:.2f}%")

out = pd.DataFrame([r for r in (r_base, r_a, r_ab) if r])
p = os.path.join(DATA, "ab_combo_conversion.csv")
out.to_csv(p, index=False, encoding="utf-8-sig")
print(f"\n汇总 → {p}")
