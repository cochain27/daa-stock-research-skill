# -*- coding: utf-8 -*-
"""距高% 折中档回测（2026-09-24）。

背景：距高% 分档单调（≤-35% u10≈23.8% vs -27~-25 7.05%），-31 档转化翻倍但日供给 -65%。
用户问「收紧但不收那么紧，能否每天 1-2 只」→ 扫 -26~-31 各档：
  排序统一用已拍板的「标准蓄势优先→位置→量比」+ top3 截断（生产口径），
  每档报 事件数/天供给（事件口径+top3口径）/u10/T5/胜率。
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

ma20map = {code: (d["日期"].values, d["收盘"].rolling(20).mean().values) for code, d in KL.items()}

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

# A 开全集（V6' 全条件，先不卡距高，由循环按档位卡）
B0 = df[
    (df["成交额亿"] <= 6) & (df["位置"] <= 0.40)
    & (df["MA20乖离%"] >= -5) & (df["20日振幅%"] < 25) & (df["量比"] <= 2.0)
].copy()
B0["ma20up"] = [ma20_up(r["代码"], r["入池日"]) for _, r in B0.iterrows()]
A0 = B0[B0["ma20up"] == True].copy()
A0["是标准蓄势"] = (A0["路径"] == "标准蓄势").astype(int)

n_days = A0["入池日"].nunique()  # 有候选的天数（分母口径1）；全回测天数更长，事件口径一致可比

def u10(s):
    return ((s["触发_10日内启动"] == True) | (s["入池直接T5%"] > 15)).mean() * 100

rows = []
for dmax in [-25, -26, -27, -28, -29, -30, -31]:
    A = A0[A0["距高%"] <= dmax].copy()
    if len(A) < 20:
        rows.append(dict(档位=dmax, 事件n=len(A), u10=np.nan))
        continue
    # top3 回放（生产口径：标准蓄势优先→位置→量比）
    t = A.sort_values(["是标准蓄势", "位置", "量比"], ascending=[False, True, True]) \
         .groupby("入池日", group_keys=False).head(3)
    st = t[(t["触发_60日内启动"] == True)]["启动后T5%_y"].dropna()
    t5 = st.mean() if len(st) >= 10 else float("nan")
    wr = (st > 0).mean() * 100 if len(st) >= 10 else float("nan")
    rows.append(dict(
        档位=dmax, 事件n=len(A), 事件每天=round(len(A) / n_days, 2),
        top3n=len(t), top3每天=round(len(t) / n_days, 2),
        u10=round(u10(t), 2), u5=round(((t["触发_5日内启动"] == True) | (t["入池直接T5%"] > 15)).mean() * 100, 2),
        u60=round(((t["触发_60日内启动"] == True) | (t["入池直接T5%"] > 15)).mean() * 100, 2),
        T5=None if pd.isna(t5) else round(t5, 2), 胜率=None if pd.isna(wr) else round(wr, 1),
        有候选天占比=round(A["入池日"].nunique() / n_days * 100, 1),
    ))

out = pd.DataFrame(rows)
print(f"（分母：A开全集候选覆盖天数 n_days={n_days}；top3 排序=标准蓄势优先→位置→量比）")
print(out.to_string(index=False))

p = os.path.join(DATA, "dist_tune_20260924.csv")
out.to_csv(p, index=False, encoding="utf-8-sig")
print(f"\n→ {p}")
