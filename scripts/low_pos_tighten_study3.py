# -*- coding: utf-8 -*-
"""低位埋伏观察池「10日内启动率」提速研究（2026-09-23）
基线 = 09-23 温和版口径（成交额<=6亿 & 60日位置<=0.40）。
在温和版之上叠加第二因子，寻找 10日启动率更高、样本量可接受、胜率不掉的中间档。
数据源：低位池_入池事件_20260923.csv（18279 去重事件，全历史回放）。
只读研究脚本，不改任何生产参数。
"""
import json
import numpy as np
import pandas as pd

BASE = "/Users/chenyuting/Documents/workbuddy/workbuddy-daa/daa-stock-research-skill"
CSV = f"{BASE}/data/低位池_入池事件_20260923.csv"
OUT = f"{BASE}/data/低位池_提速10日_20260923.json"

df = pd.read_csv(CSV)
mild = df[(df["成交额亿"] <= 6) & (df["位置"] <= 0.40)].copy()

def stats(sub: pd.DataFrame, name: str) -> dict:
    n = len(sub)
    started60 = sub[sub["60日内启动"] == True]  # noqa: E712
    started10 = sub[sub["10日内启动"] == True]  # noqa: E712
    t5 = started60["启动后T5%"].dropna()
    direct = sub["入池直接T5%"].dropna()
    med = started60.loc[started60["距启动"] >= 0, "距启动"].median()
    return {
        "方案": name,
        "样本": int(n),
        "占温和版%": round(100.0 * n / len(mild), 1),
        "估算日均入池": round(16.2 * n / len(mild), 1),
        "5日启动%": round(100.0 * sub["5日内启动"].mean(), 2) if n else 0.0,
        "10日启动%": round(100.0 * sub["10日内启动"].mean(), 2) if n else 0.0,
        "60日启动%": round(100.0 * sub["60日内启动"].mean(), 2) if n else 0.0,
        "10日中位天": float(started10["距启动"].median()) if len(started10) else None,
        "启动后T5均值%": round(float(t5.mean()), 2) if len(t5) else None,
        "启动后T5胜率%": round(100.0 * (t5 > 0).mean(), 1) if len(t5) else None,
        "入池直买T5%": round(float(direct.mean()), 2) if len(direct) else None,
    }

# 温和版内 dist60 分布（设计叠加档位用）
dist_bins = [-100, -40, -35, -30, -28, -25, 0]
dist_cut = pd.cut(mild["距高%"], bins=dist_bins)
dist_dist = mild.groupby(dist_cut, observed=False).agg(
    样本=("距高%", "size"),
    十日启动率=("10日内启动", lambda s: round(100 * s.mean(), 2)),
    胜率=("启动后T5%", lambda s: round(100 * (s.dropna() > 0).mean(), 1)),
)

plans = [
    stats(mild, "0 温和版基线(<=6亿+pos<=0.40)"),
    stats(mild[mild["距高%"] <= -28], "1 温和版+距高<=-28"),
    stats(mild[mild["距高%"] <= -30], "2 温和版+距高<=-30"),
    stats(mild[mild["5日涨幅%"] > 0], "3 温和版+5日涨幅>0"),
    stats(mild[mild["量比"] > 1.1], "4 温和版+量比>1.1"),
    stats(mild[(mild["路径"] == "近期超卖") & (mild["超卖深度"] <= 0.20)], "5 温和版+超卖深度<=0.20(仅超卖)"),
    stats(mild[(mild["位置"] >= 0.10)], "6 温和版+pos>=0.10(剔最底)"),
    stats(mild[(mild["距高%"] <= -28) & (mild["量比"] > 1.1)], "7 温和版+距高<=-28+量比>1.1"),
    stats(mild[(mild["距高%"] <= -28) & (mild["5日涨幅%"] > 0)], "8 温和版+距高<=-28+5日涨幅>0"),
    stats(mild[(mild["距高%"] <= -30) & (mild["量比"] > 1.1)], "9 温和版+距高<=-30+量比>1.1"),
    stats(mild[(mild["距高%"] <= -28) & (mild["位置"] >= 0.10)], "10 温和版+距高<=-28+pos>=0.10"),
]

out = {
    "口径": "温和版=成交额<=6亿 & 60日位置<=0.40；事件去重间距<=3交易日；估算日均=16.2只×样本占比",
    "温和版事件数": int(len(mild)),
    "温和版内距高分布": {
        str(k): {"样本": int(v["样本"]), "10日启动%": v["十日启动率"], "启动后T5胜率%": v["胜率"]}
        for k, v in dist_dist.iterrows()
    },
    "方案对比": plans,
}
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)

for p in plans:
    print(f"{p['方案']:<32} 样本{p['样本']:>6} 日均~{p['估算日均入池']:>5} "
          f"10日{p['10日启动%']:>5}% 60日{p['60日启动%']:>5}% "
          f"T5均值{p['启动后T5均值%']:>5}% 胜率{p['启动后T5胜率%']:>5}%")
print("\n温和版内距高分布:")
print(dist_dist.to_string())
