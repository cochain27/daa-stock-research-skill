# -*- coding: utf-8 -*-
"""MA20 二阶导（斜率收敛）作软加分 —— 回放检验（2026-10-01）。

背景：江淮 9/22 启动前 MA20 全程下行，方案C 排序把它甩到第 38/94（9/21）。
      用户提出「MA20 二阶导（由降转平）作软加分」——即不看 MA20 是否上行，
      而看「MA20 下行斜率是否在收敛」。江淮正是典型：slope5 从 -1.111(9/14)
      收敛到 -0.442(9/21)，降幅收敛 60%，9/22 才翻正。

口径（与 watch_pool_ab_combo.py 完全对齐）：
  - 样本 = 事件文件 V6' 基线（成交额≤6亿+位置≤0.40+距高≤-25%+乖离≥-5%+振幅<25+量比≤2.0）
         + 新增 2-15 亿档实验组（江淮 13.22 亿属此档，事件表原按≤6亿构建）
  - 触发重判 = data/trigger_lb_sensitivity.csv 的 LB=1.5 行
  - 并集启动 = 触发 ∪ 入池直接T5%>15%
  - 二阶导定义：slope5(d) = MA20(d) - MA20(d-5)；收敛 = slope5(d) > slope5(d-5)（即降幅收窄）
"""
import glob
import os

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data")

ev = pd.read_csv(os.path.join(DATA, "低位池_入池事件_20260923.csv"), dtype={"代码": str})
ev["入池日"] = pd.to_datetime(ev["入池日"])
trig = pd.read_csv(os.path.join(DATA, "trigger_lb_sensitivity.csv"), dtype={"代码": str})
trig["入池日"] = pd.to_datetime(trig["入池日"])
t15 = trig[trig["LB下界"] == 1.5][["代码", "入池日", "5日内启动", "10日内启动", "60日内启动", "启动后T5%"]].copy()
t15 = t15.rename(columns={c: f"触发_{c}" for c in ["5日内启动", "10日内启动", "60日内启动"]})
df = ev.merge(t15, on=["代码", "入池日"], how="inner")
print(f"事件×重判 join: {len(df)}（事件 {len(ev)}，重判 {len(t15)}）")

# V6' 基线（含 2-15 亿档：原事件表按≤6亿建，此处放宽上限观察 6-15 亿有无样本）
B = df[
    (df["成交额亿"] <= 6) & (df["位置"] <= 0.40) & (df["距高%"] <= -25)
    & (df["MA20乖离%"] >= -5) & (df["20日振幅%"] < 25) & (df["量比"] <= 2.0)
].copy()
print(f"V6' 基线(≤6亿): {len(B)} 事件 / {B['入池日'].nunique()} 天 / 每天 {len(B)/B['入池日'].nunique():.2f}")
B15 = B[B["成交额亿"] <= 15].copy()
print(f"V6' 基线(≤15亿): {len(B15)} 事件（6-15亿档新增 {len(B15)-len(B)} 条）")

# ---- 本地 K 线 ----
KL = {}
for p in glob.glob(os.path.join(DATA, "klines", "*.csv")):
    sym = os.path.basename(p)[:-4]
    code = sym[2:]
    if not code.startswith(("60", "00", "30")):
        continue
    raw = pd.read_csv(p)
    if raw.empty:
        continue
    raw = raw.rename(columns={"date": "日期", "last": "收盘"})
    raw["日期"] = pd.to_datetime(raw["日期"])
    raw["收盘"] = raw["收盘"].astype(float)
    raw = raw.drop_duplicates("日期").sort_values("日期").reset_index(drop=True)
    if len(raw) < 40:
        continue
    KL[code] = raw[["日期", "收盘"]].copy()

ma20 = {}
for code, d in KL.items():
    ma20[code] = (d["日期"].values, d["收盘"].rolling(20).mean().values)


def feats(code, dt):
    """返回 MA20 上行 / 斜率(slope5) / 斜率收敛(二阶导>0)"""
    dm = ma20.get(code)
    if dm is None:
        return None, None, None
    dates, m = dm
    idx = np.where(dates == np.datetime64(dt))[0]
    if len(idx) == 0:
        return None, None, None
    i = idx[0]
    if i < 25 or np.isnan(m[i]) or np.isnan(m[i - 5]) or np.isnan(m[i - 10]):
        return None, None, None
    up = bool(m[i] > m[i - 5])
    s_now = m[i] - m[i - 5]
    s_prev = m[i - 5] - m[i - 10]
    conv = bool(s_now > s_prev)          # 斜率收敛（二阶导>0，含由降转平）
    return up, s_now, conv


for tag, D in [("≤6亿", B), ("≤15亿", B15)]:
    D[["ma20up", "slope5", "conv"]] = [feats(r["代码"], r["入池日"]) for _, r in D.iterrows()]
    D.dropna(subset=["ma20up"], inplace=True)
    D["ma20up"] = D["ma20up"].astype(bool)
    D["conv"] = D["conv"].astype(bool)


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
    print(f"{name}: n={n:5d} 每天{daily:.2f} 并集5 {u5:5.2f}% / 10 {u10:5.2f}% / 60 {u60:5.2f}% "
          f"| 启动后T5 {t5m:+.2f}% 胜率{wr:.1f}%")
    return dict(name=name, n=n, daily=daily, u5=u5, u10=u10, u60=u60, t5=t5m, wr=wr)


print("\n========== ① ≤6 亿域 ==========")
union_stats(B, "基线（锚点应≈9.65）          ")
union_stats(B[B["ma20up"]], "MA20上行（锚点应≈13.31）     ")
union_stats(B[B["conv"]], "MA20斜率收敛（二阶导>0）     ")
union_stats(B[B["conv"] & ~B["ma20up"]], "收敛但未上行（方案C盲区）    ")
union_stats(B[B["ma20up"] | B["conv"]], "上行 ∪ 收敛（并集）          ")
union_stats(B[B["ma20up"] & B["conv"]], "上行 ∩ 收敛（交集）          ")

print("\n========== ② ≤15 亿域（含江淮档）==========")
union_stats(B15, "基线                          ")
union_stats(B15[B15["ma20up"]], "MA20上行                      ")
union_stats(B15[B15["conv"]], "MA20斜率收敛（二阶导>0）     ")
union_stats(B15[B15["conv"] & ~B15["ma20up"]], "收敛但未上行（方案C盲区）    ")
union_stats(B15[B15["ma20up"] | B15["conv"]], "上行 ∪ 收敛（并集）          ")

# ---- 逐年稳定性（≤6亿，年度切分是硬要求）----
print("\n========== ③ 年度稳定性（≤6亿）==========")
B["年"] = B["入池日"].dt.year
for y in sorted(B["年"].unique()):
    s = B[B["年"] == y]
    print(f"-- {y} (n={len(s)}) --")
    union_stats(s, f"  {y} 基线        ")
    union_stats(s[s["ma20up"]], f"  {y} MA20上行    ")
    union_stats(s[s["conv"]], f"  {y} 斜率收敛    ")

# ---- ④ 排序端检验：江淮类「收敛但未上行」票加入排序优先键后，能否进 top3 ----
print("\n========== ④ 排序端：三键 vs 四键（≤6亿，每日top3）==========")


def topn(sub, keys, asc):
    s = sub.sort_values(keys, ascending=asc)
    return s.groupby("入池日", group_keys=False).head(3)


A3 = topn(B, ["ma20up", "位置", "量比"], [False, True, True])
union_stats(A3, "现方案C（up优先→pos→量比）   ")
A4 = topn(B, ["ma20up", "conv", "位置", "量比"], [False, False, True, True])
union_stats(A4, "四键（up优先→收敛优先→pos）  ")

out = []
for tag, D in [("<=6亿", B), ("<=15亿", B15)]:
    out.append(union_stats(D, f"{tag} 基线"))
    out.append(union_stats(D[D["ma20up"]], f"{tag} MA20上行"))
    out.append(union_stats(D[D["conv"]], f"{tag} 斜率收敛"))
    out.append(union_stats(D[D["conv"] & ~D["ma20up"]], f"{tag} 收敛未上行"))
p = os.path.join(DATA, "ma20_second_order_conversion.csv")
pd.DataFrame([r for r in out if r]).to_csv(p, index=False, encoding="utf-8-sig")
print(f"\n汇总 → {p}")
