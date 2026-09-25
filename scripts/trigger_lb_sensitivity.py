# -*- coding: utf-8 -*-
"""触发端量比门槛敏感性研究（2026-09-23）。

问题：观察池→启动的转化率低（V6' 基线内 5日启动率 2.9%），用户问
「启动门槛的量比能否调低」。

口径：
  - 样本 = 事件文件 V6' 基线内入池事件（成交额≤6亿 + 位置≤0.40 + 距高≤-25%
           + MA20乖离≥-5% + 振幅<25 + 量比≤2.0）
  - 触发判定 = 入池后 T+1..T+60 首个满足日：
      量比∈[LB, 7.0] + 涨幅∈[10,15]%(标准蓄势)/[9,15]%(近期超卖)
      + 成交额∈[4,16]亿 + 收盘>MA20 + 位置<0.55(标准)/0.65(超卖)
  - LB 敏感性：2.0 / 1.7 / 1.5 / 1.3 / 1.0
  - 启动后 T5 = 触发日收盘买入 → T+5 收盘

产物：data/trigger_lb_sensitivity.csv
"""
import glob
import os

import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data")

# ---- 1. 读入池事件 ----
ev = pd.read_csv(os.path.join(DATA, "低位池_入池事件_20260923.csv"), dtype={"代码": str})
ev["入池日"] = pd.to_datetime(ev["入池日"])
B = ev[
    (ev["成交额亿"] <= 6) & (ev["位置"] <= 0.40) & (ev["距高%"] <= -25)
    & (ev["MA20乖离%"] >= -5) & (ev["20日振幅%"] < 25) & (ev["量比"] <= 2.0)
].copy()
print(f"V6' 基线入池事件: {len(B)}  每天 {len(B)/B['入池日'].nunique():.2f}", flush=True)

# ---- 2. 载入本地 K 线 ----
KL = {}
for p in glob.glob(os.path.join(DATA, "klines", "*.csv")):
    sym = os.path.basename(p)[:-4]
    code = sym[2:]
    if not code.startswith(("60", "00", "30")):
        continue
    raw = pd.read_csv(p)
    if raw.empty:
        continue
    df = pd.DataFrame({
        "日期": pd.to_datetime(raw["date"]),
        "开盘": raw["open"].astype(float),
        "最高": raw["high"].astype(float),
        "最低": raw["low"].astype(float),
        "收盘": raw["last"].astype(float),
        "量": raw["volume"].astype(float),   # 单位：股
    })
    df = df.drop_duplicates(subset=["日期"]).sort_values("日期").reset_index(drop=True)
    df = df.dropna(subset=["收盘"])
    if len(df) >= 80:
        KL[code] = df
print(f"本地K线宇宙: {len(KL)} 只", flush=True)


def ind_at(df, i):
    """计算 i 日指标行：量比/涨跌幅/成交额/MA20/pos60"""
    c = df.loc[i]
    if i < 1 or c["收盘"] <= 0:
        return None
    prev = df.loc[i - 1, "收盘"]
    chg = (c["收盘"] / prev - 1) * 100 if prev > 0 else 0.0
    v5 = df["量"].iloc[max(0, i - 5):i].mean()
    lb = c["量"] / v5 if v5 > 0 else 0.0
    amt = c["收盘"] * c["量"]          # volume 已是股
    ma20 = df["收盘"].iloc[i - 19:i + 1].mean() if i >= 19 else c["收盘"]
    if i >= 59:
        win = df["收盘"].iloc[i - 59:i + 1]
        pos = (c["收盘"] - win.min()) / max(win.max() - win.min(), 1e-9)
    else:
        pos = 0.0
    return dict(收盘=c["收盘"], 涨跌幅=chg, 量比=lb, 成交额=amt, MA20=ma20, pos60=pos)


def find_trigger(df, i0, lb_lo, path):
    """入池日 i0 之后 T+1..T+60 找首个触发日，返回 (idx, gap)"""
    chg_lo, chg_hi = (10.0, 15.0) if path == "标准蓄势" else (9.0, 15.0)
    pos_max = 0.55 if path == "标准蓄势" else 0.65
    end = min(i0 + 61, len(df))
    for j in range(i0 + 1, end):
        r = ind_at(df, j)
        if r is None:
            continue
        if not (lb_lo <= r["量比"] <= 7.0):
            continue
        if not (chg_lo <= r["涨跌幅"] <= chg_hi):
            continue
        if not (4e8 <= r["成交额"] <= 16e8):
            continue
        if r["收盘"] <= r["MA20"]:
            continue
        if r["pos60"] >= pos_max:
            continue
        return j, j - i0
    return None, None


def t5_ret(df, trig_idx):
    if trig_idx + 5 >= len(df):
        return None
    entry = df.loc[trig_idx, "收盘"]
    if entry <= 0:
        return None
    return (df.loc[trig_idx + 5, "收盘"] / entry - 1) * 100


# ---- 3. 敏感性扫描 ----
rows = []
skipped = 0
for _, e in B.iterrows():
    code = e["代码"]
    d = e["入池日"]
    df = KL.get(code)
    if df is None:
        skipped += 1
        continue
    idx = df.index[df["日期"] == d]
    if len(idx) == 0:
        skipped += 1
        continue
    i0 = idx[0]
    for lb_lo in (2.0, 1.7, 1.5, 1.3, 1.0):
        ti, gap = find_trigger(df, i0, lb_lo, e["路径"])
        rows.append({
            "代码": code, "入池日": d, "路径": e["路径"],
            "LB下界": lb_lo,
            "距启动": gap,
            "5日内启动": bool(gap is not None and gap <= 5),
            "10日内启动": bool(gap is not None and gap <= 10),
            "60日内启动": gap is not None,
            "启动后T5%": t5_ret(df, ti) if ti is not None else None,
        })

res = pd.DataFrame(rows)
out = os.path.join(DATA, "trigger_lb_sensitivity.csv")
res.to_csv(out, index=False, encoding="utf-8-sig")
print(f"明细 → {out}  {len(res)} 行（跳过 {skipped} 事件）", flush=True)

# ---- 4. 汇总 ----
print()
print(f"{'量比下界':>8} | {'5日启动':>8} {'10日启动':>9} {'60日启动':>9} | {'启动后T5均值':>10} {'T5胜率':>7} {'T5样本':>6}")
for lb in (2.0, 1.7, 1.5, 1.3, 1.0):
    sub = res[res["LB下界"] == lb]
    n5 = sub["5日内启动"].sum()
    n10 = sub["10日内启动"].sum()
    n60 = sub["60日内启动"].sum()
    t5 = sub["启动后T5%"].dropna()
    # 5日内启动的收益（现观察池 T+5 口径能吃到的）
    fast5 = sub[sub["5日内启动"]]["启动后T5%"].dropna()
    n = len(res[res["LB下界"] == lb])
    print(f"LB={lb:<5.1f} | {n5/n*100:6.2f}% {n10/n*100:7.2f}% {n60/n*100:7.2f}% | "
          f"{t5.mean():+8.2f}%  {(t5>0).mean()*100:6.1f}%  {len(t5):5d} | 5日内T5 {fast5.mean():+.2f}%(n={len(fast5)})")

# 额外：LB=1.5 下新增启动的票质量（相对 LB=2.0 增量）
print()
print("=== 增量分析（相对基线 LB=2.0）===")
base_trig = res[res["LB下界"] == 2.0].set_index(["代码", "入池日"])
for lb in (1.7, 1.5, 1.3, 1.0):
    sub = res[res["LB下界"] == lb].set_index(["代码", "入池日"])
    new = sub[sub["60日内启动"] & ~base_trig["60日内启动"]]
    t5 = new["启动后T5%"].dropna()
    print(f"LB={lb}: 新增启动 {len(new)} 笔, 启动后T5 {t5.mean():+.2f}%(n={len(t5)}, 胜率{(t5>0).mean()*100:.0f}%)"
          if len(t5) else f"LB={lb}: 新增启动 {len(new)} 笔（无T5样本）")
