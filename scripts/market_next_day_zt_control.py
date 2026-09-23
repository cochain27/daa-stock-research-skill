# -*- coding: utf-8 -*-
"""全市场票-日对照：次日涨停率分桶（2026-09-23）
与 all_zt_weekly_study.py 同窗口（09-03~09-22，涨停池可覆盖的14个交易日）。
本地 1416 只全扫，无低位过滤；标记次日任意涨停 + 次日首板涨停两个口径。
修复上轮缺陷：存 T-1 当日涨幅 pct。
输出：data/全市场对照样本_20260923.csv
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pandas as pd

KLINE_DIR = Path(__file__).resolve().parent.parent / "data" / "klines"
D0, D1 = "2026-09-03", "2026-09-22"

samples = []
files = sorted(KLINE_DIR.glob("*.csv"))
print(f"[对照] 本地 {len(files)} 只K线，窗口 {D0}~{D1}", flush=True)
for fi, p in enumerate(files, 1):
    code = p.stem
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
    for i in range(6, len(d) - 1):
        t1 = d.loc[i]
        t = d.loc[i + 1]
        if not (D0 <= t["日期"] <= D1):
            continue
        if pd.isna(t1["dist60"]):
            continue
        samples.append({
            "代码": pure, "T日": t["日期"],
            "amt": t1["amt"], "pct": t1["pct"], "pos60": t1["pos60"], "dist60": t1["dist60"],
            "bias20": t1["bias20"], "lb": t1["lb"], "lb5mean": t1["lb5mean"],
            "chg5": t1["chg5"], "amp20": t1["amp20"], "shr": t1["shr"],
            "zt_next": int(bool(t["is_zt"])),
            "first_zt_next": int(bool(t["is_zt"]) and t["zt20"] == 0),
        })
    if fi % 300 == 0:
        print(f"  进度 {fi}/{len(files)} 样本 {len(samples)}", flush=True)

df = pd.DataFrame(samples)
df.to_csv("data/全市场对照样本_20260923.csv", index=False, encoding="utf-8-sig")
n = len(df)
print(f"\n[对照] 全市场票-日样本 {n}，次日涨停 {df['zt_next'].sum()} 次"
      f"（{df['zt_next'].mean()*100:.2f}%），次日首板 {df['first_zt_next'].sum()} 次"
      f"（{df['first_zt_next'].mean()*100:.2f}%）", flush=True)

def bucket(col, bins, labels, target="first_zt_next"):
    d = df.dropna(subset=[col]).copy()
    d["b"] = pd.cut(d[col], bins=bins, labels=labels)
    g = d.groupby("b", observed=True)[target].agg(["mean", "count"])
    return {str(k): f"{r['mean']*100:.2f}%(n={int(r['count'])})" for k, r in g.iterrows()}

for tgt, name in [("first_zt_next", "次日首板"), ("zt_next", "次日任意涨停")]:
    base = df[tgt].mean() * 100
    print(f"\n===== 分桶目标={name}（基础率 {base:.2f}%）=====")
    print("成交额亿:", bucket("amt", [0, 1, 2, 4, 6, 10, 15, 1e3], ["<1","1-2","2-4","4-6","6-10","10-15",">15"], tgt))
    print("T-1涨幅%:", bucket("pct", [-100, -5, -2, 0, 2, 5, 100], ["<-5","-5~-2","-2~0","0~2","2~5",">5"], tgt))
    print("pos60  :", bucket("pos60", [0, 0.10, 0.25, 0.40, 0.60, 1], ["<0.10","0.10-0.25","0.25-0.40","0.40-0.60",">0.60"], tgt))
    print("距高%  :", bucket("dist60", [-1e3, -45, -35, -25, -15, -5, 1e3], ["≤-45","-45~-35","-35~-25","-25~-15","-15~-5",">-5"], tgt))
    print("MA20乖离%:", bucket("bias20", [-1e3, -10, -5, 0, 3, 1e3], ["<-10","-10~-5","-5~0","0~3",">3"], tgt))
    print("量比   :", bucket("lb", [0, 0.6, 1.0, 1.3, 2, 1e3], ["<0.6","0.6-1.0","1.0-1.3","1.3-2",">2"], tgt))
    print("5日涨幅%:", bucket("chg5", [-1e3, -10, -5, 0, 5, 1e3], ["<-10","-10~-5","-5~0","0~5",">5"], tgt))
    print("20日振幅%:", bucket("amp20", [0, 12, 18, 25, 1e3], ["<12","12-18","18-25",">25"], tgt))
    print("量能收缩:", bucket("shr", [0, 0.7, 0.9, 1.1, 1e3], ["<0.7","0.7-0.9","0.9-1.1",">1.1"], tgt))

# 低位子样本内复核（与上轮 11607 样本可比）
lowm = df["dist60"] <= -25
print(f"\n===== 低位子样本(T-1距高≤-25, n={lowm.sum()}) 首板基础率 {df[lowm]['first_zt_next'].mean()*100:.2f}% =====")
print("成交额亿:", bucket("amt", [0, 1, 2, 4, 6, 10, 1e3], ["<1","1-2","2-4","4-6","6-10",">10"]))
print("量比   :", bucket("lb", [0, 0.6, 1.0, 1.3, 2, 1e3], ["<0.6","0.6-1.0","1.0-1.3","1.3-2",">2"]))
print("20日振幅%:", bucket("amp20", [0, 12, 18, 25, 1e3], ["<12","12-18","18-25",">25"]))
