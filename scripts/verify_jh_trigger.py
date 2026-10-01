# -*- coding: utf-8 -*-
"""江淮 9/22 触发端核验（只看触发段，池子已在的前提）。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as C

print("=== 江淮 600418 9/22 触发端逐条核验（标准蓄势路径）===\n")
chk = [
    ("触发量比>=1.5",   2.07,  C.LOW_POS_ENTRY_TRIGGER_LB,        2.07 >= C.LOW_POS_ENTRY_TRIGGER_LB),
    ("触发量比<=7.0",   2.07,  C.LOW_POS_ENTRY_TRIGGER_LB_MAX,    2.07 <= C.LOW_POS_ENTRY_TRIGGER_LB_MAX),
    ("触发涨幅>=10%",   10.01, C.LOW_POS_ENTRY_TRIGGER_CHG_MIN,   10.01 >= C.LOW_POS_ENTRY_TRIGGER_CHG_MIN),
    ("触发涨幅<=15%",   10.01, C.LOW_POS_ENTRY_TRIGGER_CHG_MAX,   10.01 <= C.LOW_POS_ENTRY_TRIGGER_CHG_MAX),
    ("触发额>=4亿",     30.50, C.LOW_POS_ENTRY_TRIGGER_MIN_AMOUNT/1e8, 30.50 >= C.LOW_POS_ENTRY_TRIGGER_MIN_AMOUNT/1e8),
    ("触发额<=16亿",    30.50, C.LOW_POS_ENTRY_TRIGGER_MAX_AMOUNT/1e8, 30.50 <= C.LOW_POS_ENTRY_TRIGGER_MAX_AMOUNT/1e8),
    ("温和:涨幅>=3%",   10.01, C.LOW_POS_ENTRY_WATCH_TRIGGER_CHG, 10.01 >= C.LOW_POS_ENTRY_WATCH_TRIGGER_CHG),
    ("温和:量比>=1.5",  2.07,  C.LOW_POS_ENTRY_WATCH_TRIGGER_LB,  2.07 >= C.LOW_POS_ENTRY_WATCH_TRIGGER_LB),
    ("涨停直判>=9.5%",  10.01, 9.5,                               10.01 >= 9.5),
]
for name, val, thr, ok in chk:
    print(f"{'✅' if ok else '❌'} {name:<18} 实际={val:<7} 阈值={thr}")
print("\n★ 涨停直判启动 → 命中（该分支不依赖量比）")
print("★ 触发价（9/22 收盘）= 21.32")

print("\n=== 触发后持有跟踪模拟（新规则：豁免T+5 + 分批止盈 + 破MA5离场 + T+20）===\n")
import pandas as pd
df = pd.read_csv(Path(__file__).resolve().parent.parent / "data" / "klines" / "sh600418.csv")
df = df.rename(columns={"date": "日期", "last": "收盘", "high": "最高", "low": "最低"})
df["日期"] = pd.to_datetime(df["日期"])
df = df.sort_values("日期").reset_index(drop=True)
df["MA5"] = df["收盘"].rolling(5).mean()

fired_price = 21.32
seg = df[df["日期"] >= "2026-09-22"].copy()
seg["浮盈%"] = (seg["收盘"] / fired_price - 1) * 100
seg["破MA5"] = seg["收盘"] < seg["MA5"]
for i, (_, r) in enumerate(seg.iterrows(), 1):
    d = r["日期"].strftime("%m-%d")
    gain = r["浮盈%"]
    if gain >= C.LOW_POS_ENTRY_FIRED_TP_FULL * 100:
        sug = "清仓"
    elif gain >= C.LOW_POS_ENTRY_FIRED_TP_HALF * 100:
        sug = "减半仓"
    else:
        sug = "持有"
    if i > C.LOW_POS_ENTRY_FIRED_MAX_HOLD:
        sug += " / T+20到期评估"
    print(f"T+{i:<2} {d} 收{r['收盘']:.2f} MA5={r['MA5']:.2f} 浮盈{gain:+.1f}% 破MA5={r['破MA5']} → {sug}")
