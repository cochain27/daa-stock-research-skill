#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""低位池 MA20乖离 归因分析（生产口径，只读事件文件，不改任何存量代码/参数）。

背景：
- 老策略(80740fe)回测发现「站上MA20」是强区分因子（站上胜率47.8% vs 未站上25.6%）。
- 初动池已有硬过滤：收盘>MA20（warm_start_entry._is_pass）。
- 低位池 A+宽 仅排除「深埋MA20下方」（MA20乖离<-5%），但「贴MA20下方(-5%~0)」仍放行。
- 本脚本用 低位池_入池事件_20260923.csv（18279 去重事件，生产口径）验证：
  贴MA20下方 是否拉低后续启动率/胜率 → 决定是否值得收紧到 乖离≥0 或加分。

输出：控制台统计 + data/ma20_bias_attribution.csv（分桶明细）。
"""
import os
import sys

import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVENT = os.path.join(BASE, "data", "低位池_入池事件_20260923.csv")
OUT = os.path.join(BASE, "data", "ma20_bias_attribution.csv")

def main():
    df = pd.read_csv(EVENT, encoding="utf-8-sig", dtype={"代码": str})
    df = df.dropna(subset=["MA20乖离%"])
    print(f"事件总数(有乖离): {len(df)}")
    print(f"日期范围: {df['入池日'].min()} ~ {df['入池日'].max()}")

    # 分桶
    bins = [-1e9, -5, 0, 3, 10, 1e9]
    labels = ["深埋<-5%", "贴下方-5~0", "刚站上0~3%", "站上3~10%", "站上>10%"]
    df["乖离桶"] = pd.cut(df["MA20乖离%"], bins=bins, labels=labels, right=False)

    print("\n=== 各乖离桶：样本 / 启动率 / 胜率 ===")
    rows = []
    for lab, g in df.groupby("乖离桶", observed=True):
        n = len(g)
        # 启动率：用5/10/60日内启动
        r5 = g["5日内启动"].mean() * 100 if "5日内启动" in g else float("nan")
        r10 = g["10日内启动"].mean() * 100 if "10日内启动" in g else float("nan")
        r60 = g["60日内启动"].mean() * 100 if "60日内启动" in g else float("nan")
        # 启动后表现（启动样本）
        gs = g.dropna(subset=["启动后T5%"])
        t5 = gs["启动后T5%"].mean() if len(gs) else float("nan")
        # 入池直接持有T5
        gd = g.dropna(subset=["入池直接T5%"])
        d5 = gd["入池直接T5%"].mean() if len(gd) else float("nan")
        win = (gd["入池直接T5%"] > 0).mean() * 100 if len(gd) else float("nan")
        rows.append({"乖离桶": lab, "n": n, "5日启动率%": round(r5, 1),
                     "10日启动率%": round(r10, 1), "60日启动率%": round(r60, 1),
                     "启动后T5均值%": round(t5, 2) if t5 == t5 else "",
                     "入池直接T5均值%": round(d5, 2) if d5 == d5 else "",
                     "入池直接T5胜率%": round(win, 1) if win == win else ""})
        print(f"{lab:>12} n={n:6d}  5日启动={r5:5.1f}%  10日启动={r10:5.1f}%  60日启动={r60:5.1f}%"
              f"  启动后T5={t5:+.2f}%  入池T5={d5:+.2f}%  入池T5胜率={win:.1f}%")

    # 双路径分开看（标准蓄势 vs 近期超卖）——贴MA20下方的票集中在哪个路径
    print("\n=== 按路径 x 乖离桶 ===")
    if "路径" in df.columns:
        for path, g in df.groupby("路径"):
            print(f"\n[{path}] n={len(g)}")
            for lab, gg in g.groupby("乖离桶", observed=True):
                n = len(gg)
                if n == 0:
                    continue
                r10 = gg["10日内启动"].mean() * 100 if "10日内启动" in gg else float("nan")
                gd = gg.dropna(subset=["入池直接T5%"])
                d5 = gd["入池直接T5%"].mean() if len(gd) else float("nan")
                win = (gd["入池直接T5%"] > 0).mean() * 100 if len(gd) else float("nan")
                print(f"  {lab:>12} n={n:6d}  10日启动={r10:5.1f}%  入池T5={d5:+.2f}%  胜率={win:.1f}%")

    # 汇总存盘
    pd.DataFrame(rows).to_csv(OUT, index=False, encoding="utf-8-sig")
    print(f"\n已存盘: {OUT}")

if __name__ == "__main__":
    main()
