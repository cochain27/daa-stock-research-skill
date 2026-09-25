# -*- coding: utf-8 -*-
"""低位观察池 收紧组合搜索（仅左侧可筛选因子）
目标：生产口径 2-4 只/天（≈事件口径 0.7-1.3 只/天，生产/事件≈3.1倍）
评价：每天只数 / 5日启动 / 10日启动 / 入池T5 / 启动后T5
排除未来标签（距启动等）作为筛选因子。
"""
import pandas as pd

df = pd.read_csv("data/低位池_入池事件_20260923.csv", dtype={"代码": str})
df["入池日"] = pd.to_datetime(df["入池日"])

# V6' 基线
B = df[
    (df["成交额亿"] <= 6)
    & (df["位置"] <= 0.40)
    & (df["距高%"] <= -25)
    & (df["MA20乖离%"] >= -5)
    & (df["20日振幅%"] < 25)
    & (df["量比"] <= 2.0)
].copy()
n_days = B["入池日"].nunique()
print(f"V6'基线: {len(B)} 事件 / {n_days} 天 / 每天 {len(B)/n_days:.2f} 只")
print()

def report(name, sub):
    n = len(sub)
    if n == 0:
        print(f"{name}: 0 事件")
        return
    daily = n / sub["入池日"].nunique()
    r5 = sub["5日内启动"].mean() * 100
    r10 = sub["10日内启动"].mean() * 100
    t5 = sub["入池直接T5%"].mean()
    st = sub[sub["启动后T5%"].notna()]
    st5 = st["启动后T5%"].mean() if len(st) >= 10 else float("nan")
    prod_est = daily * 3.1  # 生产/事件口径折算系数
    print(f"{name}: n={n:4d} 每天{daily:4.2f}(生产~{prod_est:4.1f}) 5日启{r5:4.1f}% 10日启{r10:4.1f}% 入池T5{t5:+5.2f}% 启动后T5{st5:+.2f}%(n={len(st)})")

combos = [
    # (标签, 过滤条件dict: 列->(lo,hi) 含端点)
    ("基线V6'", {}),
    ("超卖深度≥0.35", {"超卖深度": (0.35, None)}),
    ("成交额3-4亿", {"成交额亿": (3, 4)}),
    ("量比1.6-2.0", {"量比": (1.6, 2.0)}),
    ("深度≥0.35+额3-4亿", {"超卖深度": (0.35, None), "成交额亿": (3, 4)}),
    ("深度≥0.35+量比1.6-2.0", {"超卖深度": (0.35, None), "量比": (1.6, 2.0)}),
    ("深度≥0.25+额2-4亿", {"超卖深度": (0.25, None), "成交额亿": (2, 4)}),
    ("深度≥0.35+额2-4亿+量比>0.7", {"超卖深度": (0.35, None), "成交额亿": (2, 4), "量比": (0.7, None)}),
    ("额3-4亿+量比1.6-2.0", {"成交额亿": (3, 4), "量比": (1.6, 2.0)}),
    ("超卖龄20+", {"超卖龄": (20, None)}),
    ("5日涨幅<-2", {"5日涨幅%": (None, -2)}),
    ("MA20乖离≥-1", {"MA20乖离%": (-1, None)}),
    ("额3-4亿+乖离≥-1+深度≥0.25", {"成交额亿": (3, 4), "MA20乖离%": (-1, None), "超卖深度": (0.25, None)}),
]
for name, conds in combos:
    m = pd.Series(True, index=B.index)
    for col, (lo, hi) in conds.items():
        if col not in B.columns:
            continue
        v = B[col]
        if lo is not None:
            m &= (v >= lo)
        if hi is not None:
            m &= (v < hi)
    report(name, B[m])
