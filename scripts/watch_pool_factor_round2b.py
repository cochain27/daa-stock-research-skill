# -*- coding: utf-8 -*-
"""round2b：新因子候选验证（2026-09-24）。

round2 发现（基线 距高≤-27 全集 n=316, u10=14.56%, T5 +2.76%, 胜率 56.8%）：
  - 转化率信号：波动率60≥0.045 → u10 24.18%（n=91）；均线粘合≤0.02 → 22.92%（n=48）
  - 质量信号：RSI≥55 → u10 持平但 T5 +5.22%/胜率 65.2%；pos250∈[0.45,0.70) → T5 +6.01%/胜率 70.6%
本脚本验证：
  1. 年度切分基线（2025/2026 各自 u10），判断分档 uplift 是否两年一致
  2. 新因子进排序端（零供给成本）top3 回放：波动率60 优先 / RSI 优先 / 组合
  3. 冗余性：波动率60 vs 距高%、RSI vs pos250/位置 相关性
数据：factor_round2_events.csv（round2 已存盘，含全部因子列）
"""
import os

import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data")

F = pd.read_csv(os.path.join(DATA, "factor_round2_events.csv"), dtype={"代码": str})
F["入池日"] = pd.to_datetime(F["入池日"])
F["是标准蓄势"] = (F["路径"] == "标准蓄势").astype(int)

def u10(s):
    return ((s["触发_10日内启动"] == True) | (s["入池直接T5%"] > 15)).mean() * 100

def stats(s):
    st = s[(s["触发_60日内启动"] == True)]["启动后T5%_y"].dropna()
    t5 = st.mean() if len(st) >= 10 else float("nan")
    wr = (st > 0).mean() * 100 if len(st) >= 10 else float("nan")
    return t5, wr

def row(name, s):
    if len(s) == 0:
        return dict(口径=name, n=0)
    t5, wr = stats(s)
    d = dict(口径=name, n=len(s), u10=round(u10(s), 2),
             T5=None if pd.isna(t5) else round(t5, 2), 胜率=None if pd.isna(wr) else round(wr, 1))
    return d

# ---- 1. 年度切分基线 ----
print("=== 年度切分基线（距高≤-27 全集）===")
F25 = F[F["入池日"] < "2026-01-01"]
F26 = F[F["入池日"] >= "2026-01-01"]
base_rows = [row("基线2025", F25), row("基线2026", F26)]
print(pd.DataFrame(base_rows).to_string(index=False))

# ---- 2. 分档 uplift 年度一致性（关键 4 个档）----
print("\n=== 关键分档 × 年度切分 ===")
check = [
    ("波动率60≥0.045", F[F["波动率60"] >= 0.045]),
    ("波动率60<0.03", F[F["波动率60"] < 0.03]),
    ("RSI≥55", F[F["RSI14"] >= 55]),
    ("RSI 40-55", F[(F["RSI14"] >= 40) & (F["RSI14"] < 55)]),
    ("均线粘合≤0.02", F[F["均线粘合度"] <= 0.02]),
    ("pos250 0.45-0.70", F[(F["pos250"] >= 0.45) & (F["pos250"] < 0.70)]),
]
rows = []
for name, s in check:
    r = row(name, s)
    s25, s26 = s[s["入池日"] < "2026-01-01"], s[s["入池日"] >= "2026-01-01"]
    r["u10_2025"] = round(u10(s25), 2) if len(s25) >= 20 else None
    r["u10_2026"] = round(u10(s26), 2) if len(s26) >= 20 else None
    r["n25"] = len(s25); r["n26"] = len(s26)
    rows.append(r)
print(pd.DataFrame(rows).to_string(index=False))

# ---- 3. 排序端 top3 回放（零供给成本）----
print("\n=== 排序端 top3 回放（每天 top3，全样本 + 年度切分）===")
def topn(sub, cols, asc):
    return sub.sort_values(cols, ascending=asc, na_position="last").groupby("入池日", group_keys=False).head(3)

sorts = [
    ("基准 标蓄→pos60→量比", ["是标准蓄势", "位置", "量比"], [False, True, True]),
    ("波动率60深→标蓄→量比", ["波动率60", "是标准蓄势", "量比"], [False, False, True]),
    ("RSI高→标蓄→量比", ["RSI14", "是标准蓄势", "量比"], [False, False, True]),
    ("RSI高→波动率60深→量比", ["RSI14", "波动率60", "量比"], [False, False, True]),
    ("波动率60深→RSI高→量比", ["波动率60", "RSI14", "量比"], [False, False, True]),
    ("pos250中高→标蓄→量比", ["pos250", "是标准蓄势", "量比"], [False, False, True]),
]
days = F["入池日"].nunique()
for name, cols, asc in sorts:
    t = topn(F, cols, asc)
    r = row(name, t)
    r["只/天"] = round(len(t) / days, 2)
    t25 = t[t["入池日"] < "2026-01-01"]; t26 = t[t["入池日"] >= "2026-01-01"]
    r["u10_2025"] = round(u10(t25), 2) if len(t25) >= 20 else None
    r["u10_2026"] = round(u10(t26), 2) if len(t26) >= 20 else None
    r["T5_2026"], r["胜率_2026"] = None, None
    t5, wr = stats(t26)
    r["T5_2026"] = None if pd.isna(t5) else round(t5, 2)
    r["胜率_2026"] = None if pd.isna(wr) else round(wr, 1)
    print(pd.DataFrame([r]).to_string(index=False, header=not rows, na_rep="—"))
    rows.append(r)

# ---- 4. 冗余性 ----
print("\n=== 相关性（Pearson）===")
print(F[["波动率60", "距高%", "RSI14", "pos250", "位置", "均线粘合度"]].corr().round(2).to_string())
