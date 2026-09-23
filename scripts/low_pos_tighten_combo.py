# -*- coding: utf-8 -*-
"""低位池收紧组合验证（2026-09-23）：在入池事件明细上对比候选收紧条件。

输入：data/低位池_入池事件_20260923.csv（low_pos_tighten_study2.py 产物，18279 事件）
输出：终端对比表 + data/低位池_收紧组合_20260923.json
原则：每个条件必须逻辑自洽 + 单档样本占比 ≥5%，组合最多 3 个条件防过拟合。
"""
import json
import os
import sys

import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
df = pd.read_csv(os.path.join(BASE, "data", "低位池_入池事件_20260923.csv"))
df["成交额亿"] = pd.to_numeric(df["成交额亿"], errors="coerce")

CANDS = {
    "基线(现口径)": df.index,
    "A 距高≤-30(深跌)": df.index[df["距高%"] <= -30],
    "A+ 距高≤-35(更深)": df.index[df["距高%"] <= -35],
    "B 成交额2-4亿(小票)": df.index[df["成交额亿"] <= 4],
    "C 位置≤0.40(低位)": df.index[df["位置"] <= 0.40],
    "C+ 位置≤0.25(更低)": df.index[df["位置"] <= 0.25],
    "D=A|C 深跌+低位": df.index[(df["距高%"] <= -30) & (df["位置"] <= 0.40)],
    "E=B|C 小票+低位": df.index[(df["成交额亿"] <= 4) & (df["位置"] <= 0.40)],
    "F=A|B 深跌+小票": df.index[(df["距高%"] <= -30) & (df["成交额亿"] <= 4)],
    "G=A|B|C 三合": df.index[(df["距高%"] <= -30) & (df["成交额亿"] <= 4) & (df["位置"] <= 0.40)],
    "H=C|量比1.1-1.3": df.index[(df["位置"] <= 0.40) & (df["量比"] > 1.1)],
}


def stat(sub):
    n = len(sub)
    trig = sub["60日内启动"].sum()
    gaps = sub.loc[sub["距启动"].notna(), "距启动"]
    t5 = sub.loc[sub["启动后T5%"].notna(), "启动后T5%"]
    pt5 = sub.loc[sub["入池直接T5%"].notna(), "入池直接T5%"]
    fast = sub[sub["5日内启动"]]
    ft5 = fast.loc[fast["启动后T5%"].notna(), "启动后T5%"]
    return {
        "样本": int(n), "占比%": round(n / len(df) * 100, 1),
        "5日启动%": round(sub["5日内启动"].sum() / n * 100, 2),
        "10日启动%": round(sub["10日内启动"].sum() / n * 100, 2),
        "60日启动%": round(trig / n * 100, 2),
        "启动中位天": float(gaps.median()) if gaps.size else None,
        "启动后T5均值%": round(float(t5.mean()), 1) if t5.size else None,
        "启动后T5胜率%": round(float((t5 > 0).mean() * 100), 1) if t5.size else None,
        "5日内启动后T5%": round(float(ft5.mean()), 1) if ft5.size else None,
        "入池直买T5%": round(float(pt5.mean()), 1) if pt5.size else None,
    }


rows = {name: stat(df.loc[idx]) for name, idx in CANDS.items()}
out = pd.DataFrame(rows).T
pd.set_option("display.width", 200)
print(out.to_string())

# 分路径验证 A 条件（排除路径混杂）
print("\n--- 分路径验证「距高≤-30」---")
for path, grp in df.groupby("路径"):
    for tag, sub in (("全部", grp), ("距高≤-30", grp[grp["距高%"] <= -30]),
                     ("距高>-30", grp[grp["距高%"] > -30])):
        if len(sub) < 30:
            continue
        s = stat(sub)
        print(f"{path} | {tag:10s} n={s['样本']:6d} 5日{s['5日启动%']:.2f}% "
              f"10日{s['10日启动%']:.2f}% 60日{s['60日启动%']:.2f}% 中位{s['启动中位天']} "
              f"T5均值{s['启动后T5均值%']}%")

# 交叉验证组合增益的稳健性：按年份切半
print("\n--- 时间稳健性（G 三合 vs 基线，按入池年切分）---")
df["年"] = df["入池日"].str[:4]
for yr, g in df.groupby("年"):
    base = stat(g)
    gsub = g[(g["距高%"] <= -30) & (g["成交额亿"] <= 4) & (g["位置"] <= 0.40)]
    s = stat(gsub)
    print(f"{yr} | 基线 n={base['样本']:6d} 5日{base['5日启动%']:.2f}% 60日{base['60日启动%']:.2f}%"
          f" || G n={s['样本']:5d} 5日{s['5日启动%']:.2f}% 60日{s['60日启动%']:.2f}%")

out_j = os.path.join(BASE, "data", "低位池_收紧组合_20260923.json")
with open(out_j, "w", encoding="utf-8") as f:
    json.dump(rows, f, ensure_ascii=False, indent=1)
print(f"\nJSON → {out_j}")
