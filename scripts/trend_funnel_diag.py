# -*- coding: utf-8 -*-
"""趋势池 0 出票归因：逐条件漏斗分析（2026-10-23 新增）

背景：右侧趋势池自 10-12 起连续 2 周 0 出票。需判断是「市场真无标的」还是
「筛选阈值过严」。本脚本用本地 K 线（data/klines/*.csv，不复权）离线重放
pick_trend_stocks 的每一道条件，输出逐日漏斗与卡点分布。

局限（必须知道）：
- 本地 K 线不复权，除权日附近的价格/涨幅会失真（不影响量能类判定）。
- 无 ST/名称过滤（本地文件无名称），无换手率排序（线上取前 100 只扫描，
  本脚本对全池扫描，理论上更宽松，故「0 出票」结论只会更保守）。
- 涨停价剔除无法还原，跳过。

用法：
  python scripts/trend_funnel_diag.py                # 近 10 个交易日
  python scripts/trend_funnel_diag.py --days 20
  python scripts/trend_funnel_diag.py --date 2026-10-23
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data"
sys.path.insert(0, str(BASE / "scripts"))

from config import (  # noqa: E402
    TREND_MIN_60D_POS, TREND_MIN_60D_POS_LOW, TREND_MAX_20D_AMPLITUDE,
    TREND_MAX_20D_STD_RATIO, TREND_BREAKOUT_VOL_RATIO, TREND_BREAKOUT_CHG_RANGE,
    TREND_MIN_AMOUNT, ALLOW_CODE_PREFIX,
)


def load_pool():
    """读全量本地 K 线 → {code: DataFrame(date, 收盘, 成交量, 成交额)}"""
    out = {}
    prefix_re = "|".join(ALLOW_CODE_PREFIX)
    for p in sorted((DATA / "klines").glob("*.csv")):
        sym = p.stem                      # sh600000
        code = sym[2:]
        if sym[:2] not in ("sh", "sz", "bj"):
            continue
        if not pd.Series([code]).str.match(rf"^(?:{prefix_re})").iloc[0]:
            continue
        try:
            df = pd.read_csv(p, usecols=["date", "last", "volume", "amount"])
        except Exception:
            continue
        if len(df) < 90:
            continue
        df = df.rename(columns={"date": "日期", "last": "收盘",
                                "volume": "成交量", "amount": "成交额"})
        for c in ("收盘", "成交量", "成交额"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        out[code] = df.dropna(subset=["收盘"]).reset_index(drop=True)
    return out


def indicators(df):
    close = df["收盘"]
    d = df.copy()
    for n in (5, 10, 20, 60):
        d[f"MA{n}"] = close.rolling(n).mean()
    d["AVG_VOL20"] = d["成交量"].rolling(20).mean()
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    d["DIF"] = ema12 - ema26
    d["DEA"] = d["DIF"].ewm(span=9, adjust=False).mean()
    d["MACD"] = 2 * (d["DIF"] - d["DEA"])
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    d["RSI14"] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    d["_week"] = pd.to_datetime(d["日期"]).dt.to_period("W").astype(str)
    wk = d.groupby("_week").agg(wclose=("收盘", "last")).reset_index()
    wk["wma5"] = wk["wclose"].rolling(5).mean()
    wk["wma5_up"] = wk["wma5"] > wk["wma5"].shift(1)
    wk = wk.ffill()
    d = d.merge(wk[["_week", "wclose", "wma5", "wma5_up"]], on="_week", how="left")
    d["week_up"] = (d["wclose"] > d["wma5"]) & d["wma5_up"]
    return d.drop(columns=["_week"])


CONDS = [
    ("C1 成交额>1.5亿", "c1"),
    ("C2 60日位置30-70%", "c2"),
    ("C3 20日横盘紧凑", "c3"),
    ("C4 放量≥1.5倍", "c4"),
    ("C5 收盘>MA20", "c5"),
    ("C6 涨幅3-7%", "c6"),
    ("C7 均线多头", "c7"),
    ("C8 周线上扬", "c8"),
    ("C9 MACD水上/金叉", "c9"),
    ("C10 RSI40-70", "c10"),
]


def eval_day(pool, day):
    """返回 (逐条件通过数 dict, 通过全部条件的票)"""
    keep = {k: 0 for _, k in CONDS}
    picks = []
    chg_min, chg_max = TREND_BREAKOUT_CHG_RANGE
    for code, raw in pool.items():
        d = indicators(raw)
        idx = d.index[d["日期"] == day]
        if len(idx) == 0:
            continue
        i = idx[0]
        if i < 60:
            continue
        row = d.iloc[i]
        prev = d.iloc[i - 1]
        close = float(row["收盘"])
        if close <= 0:
            continue
        # C1
        if float(row["成交额"] or 0) <= TREND_MIN_AMOUNT:
            continue
        keep["c1"] += 1
        # C2
        h60 = d.iloc[i - 59:i + 1]
        lo60, hi60 = float(h60["收盘"].min()), float(h60["收盘"].max())
        pos60 = (close - lo60) / (hi60 - lo60) if hi60 > lo60 else 0.5
        if pos60 >= TREND_MIN_60D_POS or pos60 < TREND_MIN_60D_POS_LOW:
            continue
        keep["c2"] += 1
        # C3
        r20 = d.iloc[i - 19:i + 1]
        amp20 = (r20["收盘"].max() - r20["收盘"].min()) / r20["收盘"].mean()
        std_ratio = r20["收盘"].std() / r20["收盘"].mean()
        if amp20 >= TREND_MAX_20D_AMPLITUDE or std_ratio >= TREND_MAX_20D_STD_RATIO:
            continue
        keep["c3"] += 1
        # C4
        avg_vol20 = float(row["AVG_VOL20"] or 0)
        if avg_vol20 <= 0 or float(row["成交量"]) < avg_vol20 * TREND_BREAKOUT_VOL_RATIO:
            continue
        keep["c4"] += 1
        # C5
        ma20 = float(row["MA20"])
        if not (close > ma20):
            continue
        keep["c5"] += 1
        # C6
        chg = (close / float(prev["收盘"]) - 1) * 100
        if not (chg_min * 100 <= chg <= chg_max * 100):
            continue
        keep["c6"] += 1
        # C7
        ma5, ma10 = float(row["MA5"]), float(row["MA10"])
        if not ((close > ma10 > ma20) or (ma5 > ma10 > ma20)):
            continue
        keep["c7"] += 1
        # C8
        if not bool(row["week_up"]):
            continue
        keep["c8"] += 1
        # C9
        dif, dea, mh = float(row["DIF"]), float(row["DEA"]), float(row["MACD"])
        macd_ok = dif > dea and mh > 0
        if not macd_ok and i >= 3:
            p3 = d.iloc[i - 3]
            macd_ok = float(p3["DIF"]) <= float(p3["DEA"]) and dif > dea
        if not macd_ok:
            continue
        keep["c9"] += 1
        # C10
        rsi = float(row["RSI14"])
        if not (40 <= rsi <= 70):
            continue
        keep["c10"] += 1
        picks.append((code, round(close, 2), round(pos60, 2), round(chg, 2),
                      round(float(row["成交量"]) / avg_vol20, 2), round(rsi, 1)))
    return keep, picks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--date", help="单日 YYYY-MM-DD")
    args = ap.parse_args()

    pool = load_pool()
    print(f"本地 K 线池：{len(pool)} 只\n")

    # 基准交易日：取 K 线池中出现次数最多的日期集合
    # （上证指数本地文件实测停在 2026-09-18，陈旧不可用；改用全池众数日历）
    from collections import Counter
    cnt = Counter()
    for df in pool.values():
        cnt.update(df["日期"].astype(str).tolist())
    dates = sorted(d for d, c in cnt.items() if c >= len(pool) * 0.6)
    if args.date:
        days = [args.date]
    else:
        days = dates[-args.days:]
    if not days:
        print("无可用交易日，退出")
        return

    header = "日期".ljust(12) + "".join(c[0].split(" ")[0].ljust(5) for c in CONDS) + "  终选"
    print(header)
    print("-" * len(header))
    rows = []
    for day in days:
        keep, picks = eval_day(pool, day)
        line = day.ljust(12) + "".join(str(keep[k]).ljust(5) for _, k in CONDS) + f"  {len(picks)}"
        print(line)
        rows.append((day, keep, picks))

    print("\n== 各条件通过率（相对上一条件）==")
    for label, key in CONDS:
        vals = [r[1][key] for r in rows]
        if not vals:
            continue
        print(f"{label:<22} 均值 {np.mean(vals):7.1f}  最低 {min(vals):4d}  最高 {max(vals):4d}")

    print("\n== 全条件通过（若有）==")
    for day, keep, picks in rows:
        if picks:
            print(day, picks)

    # 单条件放宽敏感性：只看「若去掉某条件」能多出几只（以最近一日为样本）
    day = days[-1]
    keep, picks = eval_day(pool, day)
    print(f"\n== {day} 卡点诊断 ==")
    prev = None
    for label, key in CONDS:
        v = keep[key]
        drop = "" if prev is None else f"（本关淘汰 {prev - v} 只，留存 {v/max(prev,1)*100:.0f}%）"
        print(f"{label:<22} {v:5d} {drop}")
        prev = v


if __name__ == "__main__":
    main()
