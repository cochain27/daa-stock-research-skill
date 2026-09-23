# -*- coding: utf-8 -*-
"""涨停池全量补拉（2026-09-23）
08-22~09-22 全部涨停事件（首板+连板，含连板数/行业/成交额），失败重试，结果存盘。
输出：data/涨停池全量_20260822_20260922.csv
"""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pandas as pd

import requests
requests.Session.trust_env = False
import os
os.environ["trust_env"] = "False"

import akshare as ak

T0 = time.time()
D0, D1 = "2026-08-22", "2026-09-22"
OUT = Path(__file__).resolve().parent.parent / "data" / "涨停池全量_20260822_20260922.csv"

dates = pd.bdate_range(D0, D1).strftime("%Y%m%d").tolist()
frames, failed = [], []
for d in dates:
    df = None
    for attempt in range(3):
        try:
            df = ak.stock_zt_pool_em(date=d)
            break
        except Exception as e:
            if attempt == 2:
                print(f"[涨停池] {d} 三次均失败: {e}", flush=True)
            time.sleep(2)
    if df is None or df.empty:
        if df is not None:
            print(f"[涨停池] {d} 空（可能非交易日或无涨停）", flush=True)
        else:
            failed.append(d)
        time.sleep(0.8)
        continue
    df["_date"] = d
    frames.append(df)
    print(f"[涨停池] {d} {len(df)} 只涨停", flush=True)
    time.sleep(1.0)

if frames:
    all_df = pd.concat(frames, ignore_index=True)
    all_df.to_csv(OUT, index=False, encoding="utf-8-sig")
    fb = all_df[all_df["连板数"] == 1]
    print(f"\n[结果] 成功 {len(frames)}/23 天，共 {len(all_df)} 条涨停事件，"
          f"首板 {len(fb)} 条，去重票 {all_df['代码'].nunique()} 只")
    print(f"[结果] 失败日期: {failed if failed else '无'}")
    print(f"[结果] 已存盘 {OUT}")
else:
    print("[结果] 全部失败，未存盘")
print(f"耗时 {time.time()-T0:.0f}s")
