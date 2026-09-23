# -*- coding: utf-8 -*-
"""低位埋伏首板涨停研究（2026-09-23 用户需求）
最近一个月（08-22~09-22）全市场首板涨停票 → 筛「低位埋伏」→ 涨停前一周特征统计
→ 对照池子 A+宽 判据算覆盖率/漏抓原因 → 改进建议。

数据源：akshare 东财涨停池（连板数=1 为首板）+ 本地 klines + update_klines 补缺。
输出：data/低位首板涨停研究_20260923.csv + 控制台统计。
"""
import sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pandas as pd
import numpy as np

# 进程内绕代理（国内源直连，勿动系统代理）
import requests
requests.Session.trust_env = False
import os
os.environ["trust_env"] = "False"

import akshare as ak
from warm_start_entry import KLINE_DIR, update_klines

T0 = time.time()
D0, D1 = "2026-08-22", "2026-09-22"

def tencent_code(c):
    c = str(c).zfill(6)
    if c.startswith(("60", "68")):
        return "sh" + c
    if c.startswith(("00", "30")):
        return "sz" + c
    return None  # 北交所跳过

def limit_pct(code):
    code = str(code).zfill(6)
    return 20.0 if code.startswith(("30", "68")) else 10.0

# ---------- 1. 拉涨停池，收集首板 ----------
first_boards = {}  # code -> dict(名称/行业/涨停日列表)
dates = pd.bdate_range(D0, D1).strftime("%Y%m%d").tolist()
zt_days = 0
for d in dates:
    try:
        df = ak.stock_zt_pool_em(date=d)
    except Exception as e:
        print(f"[涨停池] {d} 失败 {e}", flush=True)
        time.sleep(1)
        continue
    if df is None or df.empty:
        time.sleep(0.6)
        continue
    zt_days += 1
    fb = df[df["连板数"] == 1]
    fb = fb[~fb["名称"].astype(str).str.contains("ST|退", na=False)]
    for _, r in fb.iterrows():
        c = str(r["代码"]).zfill(6)
        if c not in first_boards:
            first_boards[c] = {"名称": r["名称"], "行业": r.get("所属行业", ""), "涨停日": []}
        first_boards[c]["涨停日"].append(f"{d[:4]}-{d[4:6]}-{d[6:]}")
    time.sleep(0.8)
print(f"[涨停池] {zt_days} 个交易日，首板票去重 {len(first_boards)} 只，耗时 {time.time()-T0:.0f}s", flush=True)

# ---------- 2. 补 K 线 ----------
codes_tt = {}
for c in first_boards:
    tc = tencent_code(c)
    if tc:
        codes_tt[c] = tc
missing = [c for c in codes_tt if not (KLINE_DIR / f"{codes_tt[c]}.csv").exists()]
print(f"[K线] 本地已有 {len(codes_tt)-len(missing)}，需下载 {len(missing)}", flush=True)
if missing:
    update_klines([codes_tt[c] for c in missing], days=140, quiet=True)
print(f"[K线] 补拉完成 耗时 {time.time()-T0:.0f}s", flush=True)

# ---------- 3. 逐票算特征 + 判定 ----------
def load_k(code_tt):
    p = KLINE_DIR / f"{code_tt}.csv"
    if not p.exists():
        return None
    try:
        df = pd.read_csv(p)
    except Exception:
        return None
    if "date" not in df.columns:
        return None
    df = df.rename(columns={"date": "日期", "open": "开盘", "last": "收盘",
                            "high": "最高", "low": "最低", "volume": "成交量",
                            "amount": "成交额"})
    df["日期"] = df["日期"].astype(str)
    return df.sort_values("日期").reset_index(drop=True)

def calc_ind(d):
    d = d.copy()
    d["pct"] = d["收盘"].pct_change() * 100
    d["MA20"] = d["收盘"].rolling(20).mean()
    d["v5"] = d["成交量"].shift(1).rolling(5).mean()
    d["lb"] = d["成交量"] / d["v5"]
    d["lb5mean"] = d["lb"].rolling(5).mean()
    d["hi60"] = d["最高"].rolling(60, min_periods=40).max()
    d["lo60"] = d["最低"].rolling(60, min_periods=40).min()
    d["pos60"] = (d["收盘"] - d["lo60"]) / (d["hi60"] - d["lo60"])
    d["dist60"] = (d["收盘"] / d["hi60"] - 1) * 100
    d["bias20"] = (d["收盘"] / d["MA20"] - 1) * 100
    d["chg5"] = d["收盘"].pct_change(5) * 100
    d["amp20"] = (d["最高"].rolling(20).max() / d["最低"].rolling(20).min() - 1) * 100
    d["shr"] = d["成交量"] / d["成交量"].rolling(20).mean()  # 量能收缩: 当日量/20日均量
    d["amt"] = d["成交额"] / 1e8
    # 是否涨停（精确价判定）
    prev = d["收盘"].shift(1)
    lp = d["pct"].index.to_series().map(lambda i: limit_pct_prefix(d, i))
    d["is_zt"] = d["pct"] >= (lp - 0.15)
    d["zt20"] = d["is_zt"].rolling(20).max().shift(1).fillna(0)  # 前20日内有无涨停
    return d

def limit_pct_prefix(d, i):
    # 由代码定：调用方传不了，用全局映射
    return LIMIT_P.get(d.loc[i, "_code"], 10.0)

# 判据（A+宽 扫描口径，逐日）
def pass_a_wide(r):
    checks = []
    if not (2 <= r["amt"] <= 6): checks.append("额")
    if r["pos60"] >= 0.40: checks.append("pos60")
    if r["dist60"] > -25: checks.append("距高")
    if r["bias20"] < -5: checks.append("乖离")
    if r["lb"] > 1.30 or r["lb5mean"] > 1.20: checks.append("量比")
    if not (-10 <= r["chg5"] <= 5): checks.append("5日涨幅")
    if r["pct"] > 5: checks.append("当日涨幅")
    if r["amp20"] >= 30: checks.append("振幅")
    return checks

rows = []
LIMIT_P = {}
for c, info in first_boards.items():
    tc = codes_tt.get(c)
    if not tc:
        continue
    d = load_k(tc)
    if d is None or len(d) < 70:
        continue
    d["_code"] = c
    LIMIT_P[c] = limit_pct(c)
    ind = calc_ind(d)
    for zt_day in info["涨停日"]:
        idx = ind.index[ind["日期"] == zt_day]
        if len(idx) == 0:
            continue
        i = idx[0]
        if i == 0:
            continue
        # 复核：当日涨停 + 前20日无涨停（首板）
        if not bool(ind.loc[i, "is_zt"]) or ind.loc[i, "zt20"] > 0:
            continue
        if i < 6:
            continue
        t1 = ind.loc[i - 1]   # T-1 涨停前一天
        w5 = ind.loc[i - 5:i - 1]  # 前一周（T-5..T-1）
        if pd.isna(t1["dist60"]):
            continue
        # 低位埋伏判定：T-1 距高≤-25%（主口径）
        if t1["dist60"] > -25:
            continue
        # 前一周判据通过天数与卡点
        pass_days = 0
        block_cnt = {}
        for _, r in w5.iterrows():
            ck = pass_a_wide(r)
            if not ck:
                pass_days += 1
            for k in ck:
                block_cnt[k] = block_cnt.get(k, 0) + 1
        rows.append({
            "代码": c, "名称": info["名称"], "行业": info["行业"], "涨停日": zt_day,
            "T1收盘": round(t1["收盘"], 2), "T1成交额亿": round(t1["amt"], 2),
            "T1_pos60": round(t1["pos60"], 3), "T1_距高%": round(t1["dist60"], 1),
            "T1_MA20乖离%": round(t1["bias20"], 1), "T1_量比": round(t1["lb"], 2),
            "T1_5日量比均": round(t1["lb5mean"], 2), "T1_5日涨幅%": round(t1["chg5"], 1),
            "T1_20日振幅%": round(t1["amp20"], 1), "T1_量能收缩": round(t1["shr"], 2),
            "前周累计涨幅%": round((w5["收盘"].iloc[-1] / w5["收盘"].iloc[0] - 1) * 100, 1),
            "前周判据全过天数(0-4)": pass_days,
            "前周卡点Top": ",".join(sorted(block_cnt, key=block_cnt.get, reverse=True)[:3]),
            "前周特征-乖离max%": round(w5["bias20"].max(), 1),
            "前周特征-量比max": round(w5["lb"].max(), 2),
        })

out = pd.DataFrame(rows)
out.to_csv("data/低位首板涨停研究_20260923.csv", index=False, encoding="utf-8-sig")
print(f"\n[结果] 一月内低位埋伏(距高≤-25)首板涨停 {len(out)} 只 → data/低位首板涨停研究_20260923.csv")
print(f"耗时 {time.time()-T0:.0f}s")
