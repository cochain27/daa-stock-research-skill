# -*- coding: utf-8 -*-
"""低位启动池 参数组合搜索（涨幅下界 × 成交额下限 × 成交额上限）。

验证目标：在温度≥55 基础上，找到让「7日最大涨幅≥10%概率」最大的触发参数组合，
并量化相对当前参数（涨幅9-15、成交额4-16亿）的增益。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

from config import (
    LOW_POS_ENTRY_MAX_POS60, LOW_POS_ENTRY_MIN_DIST60,
    LOW_POS_ENTRY_MAX_LB5, LOW_POS_ENTRY_MAX_LB5_MEAN, LOW_POS_ENTRY_CHG5_RANGE,
    LOW_POS_ENTRY_MAX_CHG_TODAY, LOW_POS_ENTRY_MAX_AMP20,
    LOW_POS_ENTRY_MIN_AMOUNT, LOW_POS_ENTRY_MAX_AMOUNT,
    LOW_POS_ENTRY_TRIGGER_LB, LOW_POS_ENTRY_TRIGGER_LB_MAX,
    LOW_POS_ENTRY_TRIGGER_CHG_MIN, LOW_POS_ENTRY_TRIGGER_CHG_MAX,
    LOW_POS_ENTRY_TRIGGER_BREAK_MA20,
    LOW_POS_ENTRY_RECENT_OVERSOLD_LOOKBACK,
    LOW_POS_ENTRY_RECENT_OVERSOLD_POS60_MAX, LOW_POS_ENTRY_RECENT_OVERSOLD_DIST_MAX,
    LOW_POS_ENTRY_TEMP_MIN,
)

KLINE_DIR = Path(__file__).resolve().parent.parent / "data" / "klines"


def _load_cache(code):
    sym = ("sh" if code.startswith(("6", "9")) else "sz") + code
    p = KLINE_DIR / f"{sym}.csv"
    if not p.exists():
        return None
    try:
        df = pd.read_csv(p, parse_dates=["date"])
        df = df.rename(columns={"last": "收盘", "open": "开盘", "high": "最高",
                                "low": "最低", "volume": "成交量", "amount": "成交额"})
        for c in ["开盘", "最高", "最低", "收盘", "成交量", "成交额"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df[["date", "开盘", "最高", "最低", "收盘", "成交量", "成交额"]].copy()
        df = df.sort_values("date").reset_index(drop=True)
        df["涨跌幅"] = df["收盘"].pct_change() * 100
        df = df.dropna(subset=["收盘"])
        if len(df) < 70:
            return None
        return df
    except Exception:
        return None


def _indicators(df):
    d = df.copy()
    for w in (5, 10, 20):
        d[f"MA{w}"] = d["收盘"].rolling(w).mean()
    d["v5"] = d["成交量"].shift(1).rolling(5).mean()
    d["lb"] = d["成交量"] / d["v5"]
    d["lb5mean"] = d["lb"].rolling(5).mean()
    d["hi60"] = d["最高"].rolling(60, min_periods=40).max()
    d["lo60"] = d["最低"].rolling(60, min_periods=40).min()
    d["pos60"] = (d["收盘"] - d["lo60"]) / (d["hi60"] - d["lo60"])
    d["dist60"] = (d["收盘"] / d["hi60"] - 1) * 100
    d["amp20"] = (d["收盘"].rolling(20).max() - d["收盘"].rolling(20).min()) / d["收盘"].rolling(20).mean() * 100
    d["chg5"] = (d["收盘"] / d["收盘"].shift(5) - 1) * 100
    return d


def _蓄势通过(row):
    if pd.isna(row.get("pos60")) or pd.isna(row.get("dist60")):
        return False
    if row["pos60"] >= LOW_POS_ENTRY_MAX_POS60:
        return False
    if row["dist60"] > LOW_POS_ENTRY_MIN_DIST60:
        return False
    lb = row.get("lb"); lbm = row.get("lb5mean")
    if pd.isna(lb) or pd.isna(lbm):
        return False
    if lb > LOW_POS_ENTRY_MAX_LB5 or lbm > LOW_POS_ENTRY_MAX_LB5_MEAN:
        return False
    c5 = row.get("chg5", 0); lo5, hi5 = LOW_POS_ENTRY_CHG5_RANGE
    if not (lo5 <= c5 <= hi5):
        return False
    chg_today = row.get("涨跌幅", 0) or 0
    if chg_today > LOW_POS_ENTRY_MAX_CHG_TODAY:
        return False
    if row.get("amp20", 0) >= LOW_POS_ENTRY_MAX_AMP20:
        return False
    amt = row.get("成交额", 0)
    if not (LOW_POS_ENTRY_MIN_AMOUNT <= amt <= LOW_POS_ENTRY_MAX_AMOUNT):
        return False
    return True


def _recent_oversold(ind_df, cur_idx):
    lookback = LOW_POS_ENTRY_RECENT_OVERSOLD_LOOKBACK
    window = ind_df.iloc[max(0, cur_idx - lookback):cur_idx]
    for _, row in window.iterrows():
        if row.get("pos60") is not None and row["pos60"] < 0.5:
            return True
        if row.get("dist60") is not None and row["dist60"] < LOW_POS_ENTRY_RECENT_OVERSOLD_DIST_MAX:
            return True
    return False


def main():
    codes = [p.stem[2:] for p in sorted(KLINE_DIR.glob("*.csv"))]
    print(f"读取本地缓存 {len(codes)} 只 ...", flush=True)
    hists = {}
    for c in codes:
        h = _load_cache(c)
        if h is not None:
            hists[c] = h

    print("扫描触发（量比2-7 + 涨幅9-15 + 位置，不卡成交额）...", flush=True)
    recs = []
    for code, h in hists.items():
        ind = _indicators(h)
        for i in range(70, len(ind) - 1):
            row = ind.iloc[i]
            ok = _蓄势通过(row)
            recent_os = _recent_oversold(ind, i + 1) if not ok else False
            if not ok and not recent_os:
                continue
            if float(row.get("涨跌幅") or 0) > LOW_POS_ENTRY_MAX_CHG_TODAY:
                continue
            nxt = ind.iloc[i + 1]
            lb = float(nxt["lb"]); chg = float(nxt["涨跌幅"])
            nxt_amt = float(nxt.get("成交额", 0))
            nxt_pos60 = float(nxt.get("pos60", 0))
            pos60_max = LOW_POS_ENTRY_MAX_POS60 if ok else LOW_POS_ENTRY_RECENT_OVERSOLD_POS60_MAX
            path = "标准蓄势" if ok else "近期超卖"
            if not (lb >= LOW_POS_ENTRY_TRIGGER_LB and lb <= LOW_POS_ENTRY_TRIGGER_LB_MAX
                    and LOW_POS_ENTRY_TRIGGER_CHG_MIN <= chg <= LOW_POS_ENTRY_TRIGGER_CHG_MAX
                    and (not LOW_POS_ENTRY_TRIGGER_BREAK_MA20 or float(nxt["收盘"]) > float(row["MA20"]))
                    and nxt_pos60 < pos60_max):
                continue
            entry = float(nxt["收盘"])
            fut = ind.iloc[i + 2:i + 9]
            if fut.empty:
                continue
            closes = fut["收盘"].astype(float).tolist()
            max_ret = (max(closes) / entry - 1) * 100
            recs.append({"code": code, "date": pd.Timestamp(nxt["date"]), "path": path,
                         "chg": chg, "amt": nxt_amt, "max_ret": max_ret})

    df = pd.DataFrame(recs)
    df["amt_yi"] = df["amt"] / 1e8

    # 温度过滤
    date_up = {}
    for code, h in hists.items():
        ind = _indicators(h)
        for i in range(70, len(ind) - 1):
            nxt = ind.iloc[i + 1]
            td = pd.Timestamp(nxt["date"])
            date_up.setdefault(td, [0, 0])
            date_up[td][1] += 1
            if float(nxt["涨跌幅"]) > 0:
                date_up[td][0] += 1
    df["temp"] = df["date"].map(lambda td: date_up.get(td, [0, 0])[0] / max(date_up.get(td, [0, 0])[1], 1) * 100)
    df = df[df["temp"] >= LOW_POS_ENTRY_TEMP_MIN].copy()

    def report(mask, name):
        g = df[mask]
        if g.empty:
            print(f"  {name:<34} n=  0")
            return
        big = (g["max_ret"] >= 10).mean() * 100
        win0 = (g["max_ret"] >= 0).mean() * 100
        mean = g["max_ret"].mean()
        print(f"  {name:<34} n={len(g):3d}  均值{mean:+5.1f}%  ≥0% {win0:3.0f}%  ≥10% {big:3.0f}%")

    # 组合搜索
    chg_mins = [9.0, 9.5, 10.0]
    amt_mins = [1.0, 2.0, 3.0, 4.0]   # 亿
    amt_maxs = [16.0, 999.0]          # 亿

    for path in ("全部", "近期超卖", "标准蓄势"):
        base = df if path == "全部" else df[df["path"] == path]
        print(f"\n===== {path}路径（温度≥{LOW_POS_ENTRY_TEMP_MIN} 后 n={len(base)}） =====")
        for chg_min in chg_mins:
            for amt_min in amt_mins:
                for amt_max in amt_maxs:
                    mask = (base["chg"] >= chg_min) & (base["amt_yi"] >= amt_min) & (base["amt_yi"] <= amt_max)
                    g = base[mask]
                    if g.empty:
                        continue
                    big = (g["max_ret"] >= 10).mean() * 100
                    win0 = (g["max_ret"] >= 0).mean() * 100
                    print(f"  涨幅≥{chg_min:>4.1f}% 成交额{amt_min:>3.0f}-{amt_max:>3.0f}亿: "
                          f"n={len(g):3d}  均值{g['max_ret'].mean():+5.1f}%  ≥0% {win0:3.0f}%  ≥10% {big:3.0f}%")

    # 当前参数基准（涨幅≥9 + 成交额4-16亿）
    print("\n===== 当前参数基准（涨幅≥9% + 成交额4-16亿 + 温度≥55） =====")
    report((df["chg"] >= 9) & (df["amt_yi"] >= 4) & (df["amt_yi"] <= 16), "全部")
    report((df["path"] == "近期超卖") & (df["chg"] >= 9) & (df["amt_yi"] >= 4) & (df["amt_yi"] <= 16), "近期超卖")
    report((df["path"] == "标准蓄势") & (df["chg"] >= 9) & (df["amt_yi"] >= 4) & (df["amt_yi"] <= 16), "标准蓄势")


if __name__ == "__main__":
    main()
