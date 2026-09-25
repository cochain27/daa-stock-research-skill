# -*- coding: utf-8 -*-
"""排序因子补充回放（2026-09-24）。

round1 发现：距高% 分档区分度最强（[-50,-31) u10=20.0% vs [-27,-25) 6.75%）但未进 top3 回放；
路径因子：近期超卖 u10 高但 T5 -2.97%/胜率 35%（启动后全亏）。
本脚本补：距高% 排序组合 + 路径优先排序 的每日 top3 回放。
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

B = df[
    (df["成交额亿"] <= 6) & (df["位置"] <= 0.40) & (df["距高%"] <= -25)
    & (df["MA20乖离%"] >= -5) & (df["20日振幅%"] < 25) & (df["量比"] <= 2.0)
].copy()

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

B["ma20up"] = [ma20_up(r["代码"], r["入池日"]) for _, r in B.iterrows()]
A = B[B["ma20up"] == True].copy()
A["是标准蓄势"] = (A["路径"] == "标准蓄势").astype(int)  # 1=标准在前

def u10(s):
    return ((s["触发_10日内启动"] == True) | (s["入池直接T5%"] > 15)).mean() * 100

def topn(sub, cols, asc):
    return sub.sort_values(cols, ascending=asc).groupby("入池日", group_keys=False).head(3)

rows = []
for name, cols, asc in [
    ("基准 位置→量比", ["位置", "量比"], [True, True]),
    ("距高深→量比", ["距高%", "量比"], [True, True]),
    ("距高深→位置", ["距高%", "位置"], [True, True]),
    ("位置→距高深", ["位置", "距高%"], [True, True]),
    ("标准蓄势优先→位置→量比", ["是标准蓄势", "位置", "量比"], [False, True, True]),
    ("距高深→标准蓄势→量比", ["距高%", "是标准蓄势", "量比"], [True, False, True]),
    ("5日涨幅深→量比", ["5日涨幅%", "量比"], [True, True]),
]:
    t = topn(A, cols, asc)
    st = t[(t["触发_60日内启动"] == True)]["启动后T5%_y"].dropna()
    t5 = st.mean() if len(st) >= 10 else float("nan")
    wr = (st > 0).mean() * 100 if len(st) >= 10 else float("nan")
    # 各路径拆分
    p_std = t[t["路径"] == "标准蓄势"]
    p_os = t[t["路径"] == "近期超卖"]
    rows.append(dict(
        排序=name, n=len(t), u10=round(u10(t), 2),
        T5=None if pd.isna(t5) else round(t5, 2), 胜率=None if pd.isna(wr) else round(wr, 1),
        标准n=len(p_std), 标准u10=round(u10(p_std), 1) if len(p_std) else None,
        超卖n=len(p_os), 超卖u10=round(u10(p_os), 1) if len(p_os) else None,
    ))
out = pd.DataFrame(rows)
print(out.to_string(index=False))

# 距高% 分档单调性复核（全集 + top3 子集）
print()
print("=== 距高% 细分档（A开全集，u10/T5/胜率）===")
for lo, hi in [(-50, -40), (-40, -35), (-35, -31), (-31, -27), (-27, -25)]:
    s = A[(A["距高%"] >= lo) & (A["距高%"] < hi)]
    if len(s) < 20:
        print(f"  [{lo},{hi}): n={len(s)} 样本不足")
        continue
    st = s[(s["触发_60日内启动"] == True)]["启动后T5%_y"].dropna()
    t5 = st.mean() if len(st) >= 10 else float("nan")
    wr = (st > 0).mean() * 100 if len(st) >= 10 else float("nan")
    print(f"  [{lo},{hi}): n={len(s):4d} u10={u10(s):6.2f}% T5={t5:+6.2f}% 胜率={wr:5.1f}%")

p = os.path.join(DATA, "rank_factor_round2.csv")
out.to_csv(p, index=False, encoding="utf-8-sig")
print(f"\n→ {p}")
