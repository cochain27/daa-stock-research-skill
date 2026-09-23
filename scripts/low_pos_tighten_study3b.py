# -*- coding: utf-8 -*-
"""补充：距高<=-25 档位 + top方案分年稳健性（2026-09-23）"""
import json
import pandas as pd

BASE = "/Users/chenyuting/Documents/workbuddy/workbuddy-daa/daa-stock-research-skill"
df = pd.read_csv(f"{BASE}/data/低位池_入池事件_20260923.csv")
df["年"] = df["入池日"].str[:4]
mild = df[(df["成交额亿"] <= 6) & (df["位置"] <= 0.40)].copy()

def stats(sub, name):
    n = len(sub)
    s60 = sub[sub["60日内启动"] == True]  # noqa: E712
    s10 = sub[sub["10日内启动"] == True]  # noqa: E712
    t5 = s60["启动后T5%"].dropna()
    med = s10["距启动"].median()
    return {
        "方案": name, "样本": int(n), "占温和版%": round(100*n/len(mild), 1),
        "估算日均": round(16.2*n/len(mild), 1),
        "5日启动%": round(100*sub["5日内启动"].mean(), 2),
        "10日启动%": round(100*sub["10日内启动"].mean(), 2),
        "60日启动%": round(100*sub["60日内启动"].mean(), 2),
        "10日中位天": float(med) if pd.notna(med) else None,
        "T5均值%": round(float(t5.mean()), 2) if len(t5) else None,
        "T5胜率%": round(100*(t5 > 0).mean(), 1) if len(t5) else None,
    }

plans = [
    stats(mild, "0 温和版基线"),
    stats(mild[mild["距高%"] <= -25], "A 温和版+距高<=-25"),
    stats(mild[(mild["距高%"] <= -25) & (mild["量比"] > 1.1)], "A2 温和版+距高<=-25+量比>1.1"),
    stats(mild[(mild["距高%"] <= -28) & (mild["位置"] >= 0.10)], "B 温和版+距高<=-28+pos>=0.10"),
]
for p in plans:
    print(f"{p['方案']:<30} 样本{p['样本']:>6} 日均~{p['估算日均']:>5} 5日{p['5日启动%']:>5}% "
          f"10日{p['10日启动%']:>5}% 中位{p['10日中位天']} T5均值{p['T5均值%']}% 胜率{p['T5胜率%']}%")

# 分年稳健性：基线 / A / B
print("\n分年稳健性（10日启动% / 样本数）:")
for name, sub in [("基线", mild),
                  ("A 距高<=-25", mild[mild["距高%"] <= -25]),
                  ("B 距高<=-28+pos>=0.10", mild[(mild["距高%"] <= -28) & (mild["位置"] >= 0.10)])]:
    row = []
    for y in ["2024", "2025", "2026"]:
        s = sub[sub["年"] == y]
        row.append(f"{y}: {100*s['10日内启动'].mean():.2f}% (n={len(s)})")
    print(f"  {name:<24} " + " | ".join(row))

# 胜率分年
print("\n分年 启动后T5胜率%（A方案）:")
a = mild[mild["距高%"] <= -25]
for y in ["2024", "2025", "2026"]:
    s = a[(a["年"] == y) & (a["60日内启动"] == True)]["启动后T5%"].dropna()  # noqa: E712
    if len(s):
        print(f"  {y}: 均值{s.mean():.2f}% 胜率{100*(s > 0).mean():.1f}% (n={len(s)})")
