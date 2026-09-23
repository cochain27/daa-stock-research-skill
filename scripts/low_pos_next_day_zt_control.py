# -*- coding: utf-8 -*-
"""低位票次日首板涨停率对照研究（2026-09-23）
对照组：本地 klines 全部票，08-22~09-22 每个交易日 T 的 T-1 行距高≤-25% → 记一个样本，
标记 T 日是否首板涨停 → 按涨停前特征分桶算「次日首板涨停率」，找预示特征。
与 low_pos_first_limit_study.py 同口径（A+宽 8 项判据）。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pandas as pd
import numpy as np

KLINE_DIR = Path(__file__).resolve().parent.parent / "data" / "klines"
D0, D1 = "2026-08-22", "2026-09-22"

def limit_pct(code):
    return 20.0 if code[2:].startswith(("30", "68")) else 10.0

samples = []
files = sorted(KLINE_DIR.glob("*.csv"))
print(f"[对照] 本地 {len(files)} 只K线", flush=True)
for fi, p in enumerate(files, 1):
    code = p.stem  # sh600418
    pure = code[2:]
    if not pure.startswith(("60", "68", "00", "30")):
        continue
    lp = 20.0 if pure.startswith(("30", "68")) else 10.0
    try:
        d = pd.read_csv(p)
    except Exception:
        continue
    if "date" not in d.columns:
        continue
    d = d.rename(columns={"date": "日期", "open": "开盘", "last": "收盘", "high": "最高",
                          "low": "最低", "volume": "成交量", "amount": "成交额"})
    d["日期"] = d["日期"].astype(str)
    d = d.sort_values("日期").reset_index(drop=True)
    if len(d) < 70:
        continue
    c = d["收盘"]
    d["pct"] = c.pct_change() * 100
    d["MA20"] = c.rolling(20).mean()
    v5 = d["成交量"].shift(1).rolling(5).mean()
    d["lb"] = d["成交量"] / v5
    d["lb5mean"] = d["lb"].rolling(5).mean()
    hi60 = d["最高"].rolling(60, min_periods=40).max()
    lo60 = d["最低"].rolling(60, min_periods=40).min()
    d["pos60"] = (c - lo60) / (hi60 - lo60)
    d["dist60"] = (c / hi60 - 1) * 100
    d["bias20"] = (c / d["MA20"] - 1) * 100
    d["chg5"] = c.pct_change(5) * 100
    d["amp20"] = (d["最高"].rolling(20).max() / d["最低"].rolling(20).min() - 1) * 100
    d["shr"] = d["成交量"] / d["成交量"].rolling(20).mean()
    d["amt"] = d["成交额"] / 1e8
    d["is_zt"] = d["pct"] >= (lp - 0.15)
    d["zt20"] = d["is_zt"].rolling(20).max().shift(1).fillna(0)
    # 样本日：T-1 ∈ [D0前一交易日, D1前一交易日]，即 T ∈ 窗口
    for i in range(6, len(d) - 1):
        t1 = d.loc[i]
        t = d.loc[i + 1]  # 次日
        if not (D0 <= t["日期"] <= D1):
            continue
        if pd.isna(t1["dist60"]) or t1["dist60"] > -25:
            continue
        samples.append({
            "代码": pure, "T日": t["日期"],
            "amt": t1["amt"], "pos60": t1["pos60"], "dist60": t1["dist60"],
            "bias20": t1["bias20"], "lb": t1["lb"], "lb5mean": t1["lb5mean"],
            "chg5": t1["chg5"], "amp20": t1["amp20"], "shr": t1["shr"],
            "zt_next": int(bool(t["is_zt"]) and t["zt20"] == 0),
        })
    if fi % 300 == 0:
        print(f"  进度 {fi}/{len(files)} 样本 {len(samples)}", flush=True)

df = pd.DataFrame(samples)
df.to_csv("data/低位对照样本_20260923.csv", index=False, encoding="utf-8-sig")
n = len(df)
nz = df["zt_next"].sum()
print(f"\n[对照] 低位票-日样本 {n}，次日首板涨停 {nz} 次，基础涨停率 {nz/n*100:.2f}%", flush=True)

# 分桶统计
def bucket(col, bins, labels):
    d = df.dropna(subset=[col]).copy()
    d["b"] = pd.cut(d[col], bins=bins, labels=labels)
    g = d.groupby("b", observed=True)["zt_next"].agg(["mean", "count"])
    return {str(k): f"{r['mean']*100:.2f}%(n={int(r['count'])})" for k, r in g.iterrows()}

print("\n===== 次日首板涨停率 分桶（基础率 %.2f%%）=====" % (nz/n*100))
print("成交额亿:", bucket("amt", [0, 2, 4, 6, 10, 15, 1000], ["<2", "2-4", "4-6", "6-10", "10-15", ">15"]))
print("pos60  :", bucket("pos60", [0, 0.10, 0.25, 0.40, 1], ["<0.10", "0.10-0.25", "0.25-0.40", ">0.40"]))
print("距高%  :", bucket("dist60", [-100, -40, -30, -25], ["≤-40", "-40~-30", "-30~-25"]))
print("MA20乖离%:", bucket("bias20", [-100, -10, -5, 0, 3, 100], ["<-10", "-10~-5", "-5~0", "0~3", ">3"]))
print("量比   :", bucket("lb", [0, 0.6, 1.0, 1.3, 2, 100], ["<0.6", "0.6-1.0", "1.0-1.3", "1.3-2", ">2"]))
print("5日涨幅%:", bucket("chg5", [-100, -10, -5, 0, 5, 100], ["<-10", "-10~-5", "-5~0", "0~5", ">5"]))
print("20日振幅%:", bucket("amp20", [0, 12, 18, 25, 100], ["<12", "12-18", "18-25", ">25"]))
print("量能收缩:", bucket("shr", [0, 0.7, 0.9, 1.1, 100], ["<0.7", "0.7-0.9", "0.9-1.1", ">1.1"]))

# A+宽 判据内 vs 外
mask = (df["amt"].between(2, 6) & (df["pos60"] < 0.40) & (df["dist60"] <= -25) &
        (df["bias20"] >= -5) & (df["lb"] <= 1.30) & (df["lb5mean"] <= 1.20) &
        df["chg5"].between(-10, 5) & (df["pct" if False else "chg5"] == df["chg5"]))
mask_today = df["lb"] <= 2.2  # 当日涨幅判据缺（T-1当日涨幅<5 未存），近似略过
print(f"\nA+宽判据内样本 {mask.sum()}，次日涨停率 {df[mask]['zt_next'].mean()*100:.2f}%")
print(f"判据外样本 {(~mask).sum()}，次日涨停率 {df[~mask]['zt_next'].mean()*100:.2f}%")
