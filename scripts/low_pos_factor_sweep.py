# -*- coding: utf-8 -*-
"""地毯式验证：温和版内哪些特征真正影响 10日启动转化率（2026-09-23）
Part1 温和版内全特征分桶 → 10日启动率
Part2 距高≤-25 子集内全特征再分层 → 找距高之外的增强/稀释项
Part3 二值化特征组合搜索（2-3因子，样本>=500）+ top 组合分年稳健性
只读研究，不改生产参数。
"""
import itertools
import pandas as pd
import numpy as np

BASE = "/Users/chenyuting/Documents/workbuddy/workbuddy-daa/daa-stock-research-skill"
df = pd.read_csv(f"{BASE}/data/低位池_入池事件_20260923.csv")
df["年"] = df["入池日"].str[:4]
mild = df[(df["成交额亿"] <= 6) & (df["位置"] <= 0.40)].copy()
deep = mild[mild["距高%"] <= -25].copy()  # A方案子集

BINS = {
    "量比":       [0, 0.5, 0.8, 1.1, 10],
    "5日量比":    [0, 0.7, 0.9, 1.1, 10],
    "5日涨幅%":   [-100, -10, -5, -2, 0, 100],
    "20日振幅%":  [0, 8, 12, 18, 100],
    "位置":       [-0.01, 0.10, 0.25, 0.401],
    "距高%":      [-100, -40, -35, -30, -28, -25, 0],
    "成交额亿":   [0, 2, 4, 6.01],
    "MA20乖离%":  [-100, -5, 0, 3, 100],
    "量能收缩":   [0, 0.7, 0.9, 1.0, 10],
    "超卖龄":     [0, 10, 15, 26],
    "超卖深度":   [0, 0.20, 0.35, 0.51, 1],
}

def table(sub, col, bins, label=""):
    """分桶统计 10日启动率；返回格式化行列表"""
    out = []
    if col in ("超卖龄", "超卖深度"):
        sub_os = sub[sub["路径"] == "近期超卖"]
        cuts = pd.cut(sub_os[col], bins=bins)
        grp = sub_os.groupby(cuts, observed=False)["10日内启动"]
    else:
        cuts = pd.cut(sub[col], bins=bins)
        grp = sub.groupby(cuts, observed=False)["10日内启动"]
    base = 100 * sub["10日内启动"].mean()
    for k, s in grp:
        n = len(s)
        if n < 100:
            continue
        rate = 100 * s.mean()
        lift = rate / base
        out.append(f"  {label}{str(k):<16} n={n:>5}  10日{rate:>5.2f}%  增益x{lift:.2f}")
    return out

print("=" * 72)
print(f"Part1 温和版内全特征分桶（基线 10日启动 = {100*mild['10日内启动'].mean():.2f}%，n={len(mild)}）")
print("=" * 72)
for col, bins in BINS.items():
    rows = table(mild, col, bins)
    if rows:
        print(f"\n[{col}]")
        print("\n".join(rows))

print("\n" + "=" * 72)
print(f"Part2 距高<=-25 子集内再分层（基线 10日 = {100*deep['10日内启动'].mean():.2f}%，n={len(deep)}）")
print("=" * 72)
for col, bins in BINS.items():
    if col == "距高%":
        continue
    rows = table(deep, col, bins)
    if rows:
        print(f"\n[{col}]")
        print("\n".join(rows))

# ---- Part3 组合搜索：二值化特征（取业务有利档），2-3 因子组合，n>=500 ----
print("\n" + "=" * 72)
print("Part3 二值化特征组合搜索（温和版内，n>=500，按10日启动率降序 top15）")
print("=" * 72)
feat_mask = {
    "距高<=-25": mild["距高%"] <= -25,
    "距高<=-30": mild["距高%"] <= -30,
    "pos0.10-0.25": (mild["位置"] >= 0.10) & (mild["位置"] <= 0.25),
    "5日涨幅>0": mild["5日涨幅%"] > 0,
    "5日涨幅-2~0": (mild["5日涨幅%"] > -2) & (mild["5日涨幅%"] <= 0),
    "量比>1.1": mild["量比"] > 1.1,
    "量比0.8-1.1": (mild["量比"] > 0.8) & (mild["量比"] <= 1.1),
    "振幅12-18": (mild["20日振幅%"] > 12) & (mild["20日振幅%"] <= 18),
    "振幅>18": mild["20日振幅%"] > 18,
    "MA20乖离-5~0": (mild["MA20乖离%"] > -5) & (mild["MA20乖离%"] <= 0),
    "量能收缩<=0.7": mild["量能收缩"] <= 0.7,
    "量能收缩0.9-1.0": (mild["量能收缩"] > 0.9) & (mild["量能收缩"] <= 1.0),
    "超卖深度<=0.20": (mild["路径"] == "近期超卖") & (mild["超卖深度"] <= 0.20),
    "5日量比0.9-1.1": (mild["5日量比"] > 0.9) & (mild["5日量比"] <= 1.1),
    "成交额2-4亿": (mild["成交额亿"] > 2) & (mild["成交额亿"] <= 4),
}
names = list(feat_mask)
combos = []
for r in (2, 3):
    for combo in itertools.combinations(names, r):
        m = feat_mask[combo[0]]
        for c in combo[1:]:
            m = m & feat_mask[c]
        n = int(m.sum())
        if 500 <= n <= 6000:
            rate = 100 * mild.loc[m, "10日内启动"].mean()
            combos.append((rate, n, "+".join(combo)))
combos.sort(reverse=True)
base_rate = 100 * mild["10日内启动"].mean()
for rate, n, name in combos[:15]:
    print(f"  {name:<55} n={n:>5}  10日{rate:>5.2f}%  x{rate/base_rate:.2f}")

# top3 分年稳健性
print("\ntop3 组合分年 10日启动率：")
years = df["年"].unique()
for rate, n, name in combos[:3]:
    parts = name.split("+")
    m_all = pd.Series(True, index=mild.index)
    for p in parts:
        m_all = m_all & feat_mask[p]
    ys = []
    for y in ["2024", "2025", "2026"]:
        m_y = m_all & (mild["年"] == y)
        ny = int(m_y.sum())
        ry = 100 * mild.loc[m_y, "10日内启动"].mean() if ny else float("nan")
        ys.append(f"{y}:{ry:.2f}%(n={ny})")
    print(f"  {name}\n    " + " | ".join(ys))
