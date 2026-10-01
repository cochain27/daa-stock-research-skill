# -*- coding: utf-8 -*-
"""低位启动观察池 · 生产口径（A+B + 距高-27）入池→启动转化率回测（2026-09-29）。

背景：09-22 转化率研究是旧口径（触发LB=2.0、距高-25、无A+B）。09-23/24 生产口径
      已定版：A=MA20上行 + B=每日top3 + 距高≤-27% + 触发量比1.5。该口径的
      转化率/启动时点分布/T+5剔除评估从未完整算过（ab_combo 是 -25 旧口径且只算并集）。

口径（与生产完全对齐）：
  - 样本 = 事件文件（V6' 全条件 + 距高≤-27）
  - A = MA20 上行（当日MA20 > 5日前MA20，本地K线现算，生产shift5口径）
  - B = 按入池日分组 top3（排序 标准蓄势→位置→量比，生产定版）
  - 触发 = data/trigger_lb_sensitivity.csv 的 LB=1.5 行（生产现行触发口径）
  - 并集启动 = 触发启动 ∪ 入池直接T5%>15%（温和启动兜底）
  - 启动时点 = trigger 的「距启动」列（入池到60日内首个启动的自然交易日数）

产物：data/低位观察池_转化率_生产口径_20260929.csv / .json
"""
import glob
import json
import os

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data")

# ---- 1. 事件 + 触发重判（LB=1.5，生产口径）----
ev = pd.read_csv(os.path.join(DATA, "低位池_入池事件_20260923.csv"), dtype={"代码": str})
ev["入池日"] = pd.to_datetime(ev["入池日"])
trig = pd.read_csv(os.path.join(DATA, "trigger_lb_sensitivity.csv"), dtype={"代码": str})
trig["入池日"] = pd.to_datetime(trig["入池日"])
t15 = trig[trig["LB下界"] == 1.5][
    ["代码", "入池日", "5日内启动", "10日内启动", "60日内启动", "距启动", "启动后T5%"]
].copy()
t15 = t15.rename(columns={c: f"触发_{c}" for c in ["5日内启动", "10日内启动", "60日内启动"]})
df = ev.merge(t15, on=["代码", "入池日"], how="inner")
print(f"事件×重判 join: {len(df)}（事件 {len(ev)}，重判 {len(t15)}）")

# ---- 2. 本地 K 线 → MA20 上行（生产 shift5 口径）----
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

# ---- 3. 生产口径样本：V6' 全条件 + 距高≤-27 + A + B ----
V = df[
    (df["成交额亿"] <= 6) & (df["位置"] <= 0.40) & (df["距高%"] <= -27)
    & (df["MA20乖离%"] >= -5) & (df["20日振幅%"] < 25) & (df["量比"] <= 2.0)
].copy()
print(f"V6'-27 基线: {len(V)} 事件 / {V['入池日'].nunique()} 天")

V["ma20up"] = [ma20_up(r["代码"], r["入池日"]) for _, r in V.iterrows()]
A = V[V["ma20up"] == True].copy()
A["是标准蓄势"] = (A["路径"] == "标准蓄势").astype(int)
print(f"A(MA20上行)可判: {len(A)} 事件 / {A['入池日'].nunique()} 天")

def topn(sub, n):
    s = sub.sort_values(["是标准蓄势", "位置", "量比"], ascending=[False, True, True])
    return s.groupby("入池日", group_keys=False).head(n)

AB = topn(A, 3)
print(f"A+B(每日top3): {len(AB)} 事件 / {AB['入池日'].nunique()} 天 / 每天 {len(AB)/AB['入池日'].nunique():.2f}")

# ---- 4. 转化率与启动时点统计 ----
def union_col(s, col):
    return ((s[col] == True) | (s["入池直接T5%"] > 15)).mean() * 100

def block_stats(sub, name):
    n = len(sub)
    if n == 0:
        print(f"{name}: 0 事件")
        return None
    u5 = union_col(sub, "触发_5日内启动")
    u10 = union_col(sub, "触发_10日内启动")
    u60 = union_col(sub, "触发_60日内启动")
    trig_any = sub["触发_60日内启动"] == True
    st = sub[trig_any]["启动后T5%_y"].dropna()
    t5m = st.mean() if len(st) >= 10 else float("nan")
    wr = (st > 0).mean() * 100 if len(st) >= 10 else float("nan")
    # 启动时点分布（60日内启动的子样本，用距启动）
    gaps = sub.loc[trig_any, "距启动_y"].dropna()
    fast = sub[trig_any & (sub["距启动_y"] <= 5)]
    slow = sub[trig_any & (sub["距启动_y"] > 5) & (sub["距启动_y"] <= 20)]
    st_fast = fast["启动后T5%_y"].dropna()
    st_slow = slow["启动后T5%_y"].dropna()
    print(f"{name}: n={n} 每天{len(sub)/sub['入池日'].nunique():.2f} "
          f"并集5日{u5:.2f}% / 10日{u10:.2f}% / 60日{u60:.2f}% | "
          f"启动后T5 {t5m:+.2f}% 胜率{wr:.1f}%")
    print(f"   启动时点: 中位{gaps.median() if gaps.size else 'NA'}日 P75{gaps.quantile(0.75) if gaps.size else 'NA'} "
          f"| T+1 {int((gaps==1).sum())} T+2~5 {int(((gaps>1)&(gaps<=5)).sum())} "
          f"T+6~10 {int(((gaps>5)&(gaps<=10)).sum())} T+11~20 {int(((gaps>10)&(gaps<=20)).sum())} T+21~60 {int(((gaps>20)&(gaps<=60)).sum())}")
    print(f"   现T+5口径能吃到(fast): {len(fast)}笔 T5 {st_fast.mean() if len(st_fast)>=10 else float('nan'):+.2f}% 胜率{(st_fast>0).mean()*100 if len(st_fast)>=10 else float('nan'):.1f}%")
    print(f"   T+5剔除会错过(6~20日): {len(slow)}笔 T5 {st_slow.mean() if len(st_slow)>=10 else float('nan'):+.2f}% 胜率{(st_slow>0).mean()*100 if len(st_slow)>=10 else float('nan'):.1f}%")
    return dict(name=name, n=n, daily=len(sub)/sub['入池日'].nunique(), u5=u5, u10=u10, u60=u60,
                t5=t5m, wr=wr,
                gap_median=float(gaps.median()) if gaps.size else None,
                gap_p75=float(gaps.quantile(0.75)) if gaps.size else None,
                gap_buckets={b: int(c) for b, c in {
                    "T+1": (gaps == 1).sum(), "T+2~5": ((gaps > 1) & (gaps <= 5)).sum(),
                    "T+6~10": ((gaps > 5) & (gaps <= 10)).sum(), "T+11~20": ((gaps > 10) & (gaps <= 20)).sum(),
                    "T+21~60": ((gaps > 20) & (gaps <= 60)).sum()}.items()},
                fast_n=len(fast), fast_t5=float(st_fast.mean()) if len(st_fast) >= 10 else None,
                fast_wr=float((st_fast > 0).mean() * 100) if len(st_fast) >= 10 else None,
                slow_n=len(slow), slow_t5=float(st_slow.mean()) if len(st_slow) >= 10 else None,
                slow_wr=float((st_slow > 0).mean() * 100) if len(st_slow) >= 10 else None)

print()
rows = []
for name, sub in [("①生产口径 A+B(-27)", AB), ("②对照 A-only(-27)", A),
                  ("③对照 V6'-27 基线", V), ("④历史口径 A+B(-25)", None)]:
    if name.startswith("④"):
        V25 = df[(df["成交额亿"] <= 6) & (df["位置"] <= 0.40) & (df["距高%"] <= -25)
                 & (df["MA20乖离%"] >= -5) & (df["20日振幅%"] < 25) & (df["量比"] <= 2.0)].copy()
        V25["ma20up"] = [ma20_up(r["代码"], r["入池日"]) for _, r in V25.iterrows()]
        A25 = V25[V25["ma20up"] == True].copy()
        A25["是标准蓄势"] = (A25["路径"] == "标准蓄势").astype(int)
        AB25 = topn(A25, 3)
        r = block_stats(AB25, name)
    else:
        r = block_stats(sub, name)
    if r:
        rows.append(r)

# 路径分层（生产口径 A+B）
print()
print("=== 生产口径 A+B(-27) 路径分层 ===")
path_rows = []
for path, grp in AB.groupby("路径"):
    r = block_stats(grp, f"  {path}")
    if r:
        path_rows.append(r)

# ---- 5. 汇总 ----
out = pd.DataFrame(rows)
csv_p = os.path.join(DATA, "低位观察池_转化率_生产口径_20260929.csv")
out.to_csv(csv_p, index=False, encoding="utf-8-sig")
json_p = os.path.join(DATA, "低位观察池_转化率_生产口径_20260929.json")
with open(json_p, "w", encoding="utf-8") as f:
    json.dump({"总览": rows, "路径分层": path_rows}, f, ensure_ascii=False, indent=1)
print(f"\n汇总 → {csv_p}")
print(f"JSON → {json_p}")
