# -*- coding: utf-8 -*-
"""全市场涨停票「涨停前一周特征」研究（2026-09-23 用户需求②）
样本：data/涨停池全量_20260822_20260922.csv（接口仅保留最近14个交易日 09-03~09-22，
839 事件 / 555 只）。对每个涨停事件计算 T-1 特征 + 前一周(T-5..T-1)特征，
分「低位埋伏型(T-1距高≤-25%) vs 非低位型」两桶画像对比。
输出：data/全市场涨停票前一周研究_20260923.csv
"""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pandas as pd
import numpy as np

import requests
requests.Session.trust_env = False
import os
os.environ["trust_env"] = "False"

from warm_start_entry import KLINE_DIR, update_klines

T0 = time.time()
POOL_CSV = Path(__file__).resolve().parent.parent / "data" / "涨停池全量_20260822_20260922.csv"
OUT = Path(__file__).resolve().parent.parent / "data" / "全市场涨停票前一周研究_20260923.csv"

pool = pd.read_csv(POOL_CSV, dtype={"代码": str, "_date": str})
pool["代码"] = pool["代码"].str.zfill(6)
pool = pool[~pool["名称"].astype(str).str.contains("ST|退", na=False)]
pool["涨停日"] = pool["_date"].str.replace("-", "")  # yyyyMMdd
print(f"[样本] {pool['_date'].nunique()} 天 {len(pool)} 事件 / {pool['代码'].nunique()} 只", flush=True)

# ---------- K 线补拉 ----------
def tencent_code(c):
    if c.startswith(("60", "68")): return "sh" + c
    if c.startswith(("00", "30")): return "sz" + c
    return None

codes = sorted(pool["代码"].unique())
codes_tt = {c: tencent_code(c) for c in codes}
codes_tt = {c: t for c, t in codes_tt.items() if t}
missing = [codes_tt[c] for c in codes_tt if not (KLINE_DIR / f"{codes_tt[c]}.csv").exists()]
print(f"[K线] 本地已有 {len(codes_tt)-len(missing)}，需下载 {len(missing)}", flush=True)
if missing:
    update_klines(missing, days=140, quiet=True)
print(f"[K线] 就绪 耗时 {time.time()-T0:.0f}s", flush=True)

# ---------- 指标 ----------
def load_k(code_tt):
    p = KLINE_DIR / f"{code_tt}.csv"
    if not p.exists(): return None
    try: df = pd.read_csv(p)
    except Exception: return None
    if "date" not in df.columns: return None
    df = df.rename(columns={"date": "日期", "open": "开盘", "last": "收盘",
                            "high": "最高", "low": "最低", "volume": "成交量", "amount": "成交额"})
    df["日期"] = df["日期"].astype(str)
    return df.sort_values("日期").reset_index(drop=True)

def calc_ind(d, lp):
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
    d["shr"] = d["成交量"] / d["成交量"].rolling(20).mean()
    d["amt"] = d["成交额"] / 1e8
    d["is_zt"] = d["pct"] >= (lp - 0.15)
    d["zt20"] = d["is_zt"].rolling(20).max().shift(1).fillna(0)
    return d

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

# ---------- 逐事件 ----------
rows, no_kline, miss_day = [], set(), 0
for c, g in pool.groupby("代码"):
    tc = codes_tt.get(c)
    if not tc: continue
    d = load_k(tc)
    if d is None or len(d) < 70:
        no_kline.add(c); continue
    lp = 20.0 if c.startswith(("30", "68")) else 10.0
    ind = calc_ind(d, lp)
    for _, ev in g.iterrows():
        zt_day = str(ev["涨停日"])
        zt_day = f"{zt_day[:4]}-{zt_day[4:6]}-{zt_day[6:]}"  # yyyyMMdd → yyyy-MM-dd（对齐K线）
        idx = ind.index[ind["日期"] == zt_day]
        if len(idx) == 0:
            miss_day += 1; continue
        i = idx[0]
        if i < 6: continue
        t1 = ind.loc[i - 1]
        w5 = ind.loc[i - 5:i - 1]
        if pd.isna(t1["dist60"]): continue
        lbc = int(ev["连板数"])
        is_first = bool(ind.loc[i, "zt20"] == 0)
        pass_days, block_cnt = 0, {}
        for _, r in w5.iterrows():
            ck = pass_a_wide(r)
            if not ck: pass_days += 1
            for k in ck: block_cnt[k] = block_cnt.get(k, 0) + 1
        rows.append({
            "代码": c, "名称": ev["名称"], "行业": ev.get("所属行业", ""),
            "涨停日": f"{zt_day[:4]}-{zt_day[4:6]}-{zt_day[6:]}",
            "连板数": lbc, "低位埋伏型": int(t1["dist60"] <= -25),
            "T1收盘": round(t1["收盘"], 2), "T1成交额亿": round(t1["amt"], 2),
            "T1_pos60": round(t1["pos60"], 3), "T1_距高%": round(t1["dist60"], 1),
            "T1_MA20乖离%": round(t1["bias20"], 1), "T1_量比": round(t1["lb"], 2),
            "T1_5日量比均": round(t1["lb5mean"], 2), "T1_5日涨幅%": round(t1["chg5"], 1),
            "T1_20日振幅%": round(t1["amp20"], 1), "T1_量能收缩": round(t1["shr"], 2),
            "前周累计涨幅%": round((w5["收盘"].iloc[-1] / w5["收盘"].iloc[0] - 1) * 100, 1),
            "前周日均量比": round(w5["lb"].mean(), 2),
            "前周判据全过天数(0-4)": pass_days,
            "前周卡点Top": ",".join(sorted(block_cnt, key=block_cnt.get, reverse=True)[:3]),
        })

out = pd.DataFrame(rows)
out.to_csv(OUT, index=False, encoding="utf-8-sig")
print(f"\n[结果] 事件 {len(out)} 条（K线缺失 {len(no_kline)} 只，涨停日缺行 {miss_day} 事件）→ {OUT.name}")

# ---------- 画像对比 ----------
def profile(sub, name):
    if len(sub) == 0:
        print(f"\n{name}: 无样本"); return
    print(f"\n===== {name}（n={len(sub)}，首板占 {(sub['连板数']==1).mean()*100:.0f}%）=====")
    med = sub[["T1成交额亿","T1_pos60","T1_距高%","T1_MA20乖离%","T1_量比","T1_5日量比均",
               "T1_5日涨幅%","T1_20日振幅%","前周累计涨幅%","前周日均量比"]].median()
    print("T-1/前周中位数:", {k: round(v,2) for k,v in med.items()})
    print("前周判据全过天数分布:", sub["前周判据全过天数(0-4)"].value_counts().sort_index().to_dict())
    print("T1成交额分档:", pd.cut(sub["T1成交额亿"],[0,1,2,4,6,10,15,1e3],
          labels=["<1","1-2","2-4","4-6","6-10","10-15",">15"]).value_counts().sort_index().to_dict())
    print("T1距高分档:", pd.cut(sub["T1_距高%"],[-1e3,-45,-35,-25,-15,-5,1e3],
          labels=["≤-45","-45~-35","-35~-25","-25~-15","-15~-5",">-5"]).value_counts().sort_index().to_dict())
    print("T1量比分档:", pd.cut(sub["T1_量比"],[0,0.7,1.0,1.3,2,1e3],
          labels=["<0.7","0.7-1","1-1.3","1.3-2",">2"]).value_counts().sort_index().to_dict())
    print("T1乖离分档:", pd.cut(sub["T1_MA20乖离%"],[-1e3,-10,-5,0,3,1e3],
          labels=["<-10","-10~-5","-5~0","0~3",">3"]).value_counts().sort_index().to_dict())
    print("前周涨幅分档:", pd.cut(sub["前周累计涨幅%"],[-1e3,-10,-5,0,5,10,1e3],
          labels=["≤-10","-10~-5","-5~0","0~5","5~10",">10"]).value_counts().sort_index().to_dict())

low = out[out["低位埋伏型"] == 1]
high = out[out["低位埋伏型"] == 0]
low_first = low[low["连板数"] == 1]
profile(out, "全部涨停事件")
profile(low, "低位埋伏型(距高≤-25) 全部事件")
profile(low_first, "低位埋伏型 仅首板")
profile(high, "非低位型(距高>-25) 全部事件")
# 卡点统计（低位首板口径，与上轮可比）
bc = {}
for s in low_first["前周卡点Top"]:
    for k in str(s).split(","):
        if k: bc[k] = bc.get(k, 0) + 1
print("\n低位首板 前周卡点Top计次:", dict(sorted(bc.items(), key=lambda x: -x[1])))
print(f"耗时 {time.time()-T0:.0f}s")
