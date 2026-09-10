# -*- coding: utf-8 -*-
"""补录30个历史交易日的低位启动池信号，并追加写入watch_history.csv"""
import sys, time, csv
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from backfill_watch import (
    _load_hist, screen_on_date, MIN_AMOUNT, MAX_POS,
    MAX_SAME_INDUSTRY, ALLOW_CODE_PREFIX, _is_zt, _tech_indicators
)
from fetch_data import get_market_snapshot, _industry_by_name

# 近30个交易日（手动构造，跳过周末）
def _trading_days(n=30):
    d = pd.Timestamp("2026-09-09")
    days = []
    while len(days) < n:
        dow = d.dayofweek
        if dow < 5:  # 非周末
            days.append(d)
        d -= pd.Timedelta(days=1)
    return sorted(days)

trading_days = _trading_days(30)
print(f"[交易日] {trading_days[0].date()} ~ {trading_days[-1].date()} ({len(trading_days)}天)")

# 拉全市场快照
print("[快照] 拉取全A快照...")
snap = get_market_snapshot()
snap["代码"] = snap["代码"].astype(str).str.replace(r"\.0$", "", regex=True)
snap = snap[snap["代码"].str.match(r"^(?:" + "|".join(ALLOW_CODE_PREFIX) + ")", na=False)]
snap = snap[~snap["名称"].astype(str).str.contains("ST|退", na=False)]
snap["_amt"] = pd.to_numeric(snap.get("成交额"), errors="coerce").fillna(0)
snap = snap.sort_values("_amt", ascending=False).head(1200)
codes = snap["代码"].tolist()
names = dict(zip(snap["代码"], snap["名称"].astype(str)))
print(f"[候选] {len(codes)} 只")

# 并发拉K线
print("[K线] 并发拉取历史...")
hists = {}
t0 = time.time()
with ThreadPoolExecutor(max_workers=10) as ex:
    futs = {ex.submit(_load_hist, c): c for c in codes}
    for i, f in enumerate(as_completed(futs), 1):
        c, h = f.result()
        if h is not None:
            hists[c] = h
        if i % 200 == 0:
            print(f"  {i}/{len(codes)} ok={len(hists)} {time.time()-t0:.0f}s")
print(f"[就绪] {len(hists)} 只K线, 耗时{time.time()-t0:.0f}s\n")

# 逐日扫描
new_rows = []
for d in trading_days:
    d_str = str(d.date())
    picks = screen_on_date(hists, codes, names, d_str)
    for p in picks:
        new_rows.append({
            "日期": d_str, "代码": p["代码"], "名称": p["名称"], "现价": str(p["现价"]),
            "60日位置": str(p["60日位置"]), "信号": p["信号"], "5日涨幅": str(p["5日涨幅"]),
            "行业": p["行业"], "买点区间": p["买点区间"], "止损价": str(p["止损价"]),
            "关注逻辑": p["关注逻辑"],
        })
    print(f"  {d_str}: {len(picks)} 只  [累计新增 {len(new_rows)}]")

# 合并写入
hist_path = ROOT / "data" / "watch_history.csv"
fields = ["日期","代码","名称","现价","60日位置","信号","5日涨幅","行业","买点区间","止损价","关注逻辑"]
existing = {}
if hist_path.exists():
    with hist_path.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            existing[(r["日期"], r["代码"])] = r

for r in new_rows:
    existing[(r["日期"], r["代码"])] = r

with hist_path.open("w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=fields)
    w.writeheader()
    for v in sorted(existing.values(), key=lambda x: x["日期"]):
        w.writerow(v)

print(f"\n[完成] watch_history 共 {len(existing)} 条 (新增{len(new_rows)})")
print(f"[保存] {hist_path}")
