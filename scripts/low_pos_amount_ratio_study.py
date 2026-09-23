# -*- coding: utf-8 -*-
"""成交额「放量倍数」vs「绝对值」区分度研究（读本地 data/klines，无网络）。

目的：验证用户观点——筛选「触发日成交额 / 平时成交额」的倍数，是否比「成交额绝对值
4-16 亿」更能捕捉「资金突然关注」信号，从而提升 7 日拉升转化率。

口径（与 low_pos_7day_backtest.py 一致，防未来函数）：
  - T-1 收盘蓄势候选（标准蓄势 或 近期超卖）
  - T 日触发：量比 2-7x + 涨幅 9-15% + 位置<55%(标准)/<65%(超卖)
  - 此处【不】卡成交额绝对值，改为同时记录成交额、前5日均额、前20日均额、放量倍数
  - 入场价=T 收盘；7 日最大涨幅 = max(T+1..T+7 收盘)/entry - 1
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
    # 成交额放量倍数：触发日成交额 / 前5日(不含当日)均额、前20日均额
    d["a5"] = d["成交额"].shift(1).rolling(5).mean()
    d["a20"] = d["成交额"].shift(1).rolling(20).mean()
    d["amt_ratio5"] = d["成交额"] / d["a5"]
    d["amt_ratio20"] = d["成交额"] / d["a20"]
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
    lb = row.get("lb")
    lbm = row.get("lb5mean")
    if pd.isna(lb) or pd.isna(lbm):
        return False
    if lb > LOW_POS_ENTRY_MAX_LB5 or lbm > LOW_POS_ENTRY_MAX_LB5_MEAN:
        return False
    c5 = row.get("chg5", 0)
    lo5, hi5 = LOW_POS_ENTRY_CHG5_RANGE
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
    start = max(0, cur_idx - lookback)
    window = ind_df.iloc[start:cur_idx]
    for _, row in window.iterrows():
        p60 = row.get("pos60")
        d60 = row.get("dist60")
        if p60 is not None and p60 < 0.5:
            return True
        if d60 is not None and d60 < LOW_POS_ENTRY_RECENT_OVERSOLD_DIST_MAX:
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
    print(f"K线就绪 {len(hists)} 只", flush=True)

    # 收集所有「量比2-7x + 涨幅9-15% + 位置」触发（不卡成交额绝对值）
    print("扫描触发（不卡成交额绝对值，仅记录放量倍数）...", flush=True)
    recs = []
    for code, h in hists.items():
        ind = _indicators(h)
        for i in range(70, len(ind) - 1):
            row = ind.iloc[i]
            ok = _蓄势通过(row)
            recent_os = _recent_oversold(ind, i + 1) if not ok else False
            if not ok and not recent_os:
                continue
            chg_today = float(row.get("涨跌幅") or 0)
            if chg_today > LOW_POS_ENTRY_MAX_CHG_TODAY:
                continue
            nxt = ind.iloc[i + 1]
            lb = float(nxt["lb"])
            chg = float(nxt["涨跌幅"])
            nxt_amt = float(nxt.get("成交额", 0))
            ma20 = float(row["MA20"])
            nxt_pos60 = float(nxt.get("pos60", 0))
            pos60_max = LOW_POS_ENTRY_MAX_POS60 if ok else LOW_POS_ENTRY_RECENT_OVERSOLD_POS60_MAX
            path = "标准蓄势" if ok else "近期超卖"
            if not (lb >= LOW_POS_ENTRY_TRIGGER_LB and lb <= LOW_POS_ENTRY_TRIGGER_LB_MAX
                    and LOW_POS_ENTRY_TRIGGER_CHG_MIN <= chg <= LOW_POS_ENTRY_TRIGGER_CHG_MAX
                    and (not LOW_POS_ENTRY_TRIGGER_BREAK_MA20 or float(nxt["收盘"]) > ma20)
                    and nxt_pos60 < pos60_max):
                continue
            entry = float(nxt["收盘"])
            fut = ind.iloc[i + 2:i + 9]
            if fut.empty:
                continue
            closes = fut["收盘"].astype(float).tolist()
            max_ret = (max(closes) / entry - 1) * 100
            recs.append({
                "code": code, "date": pd.Timestamp(nxt["date"]),
                "path": path, "lb": lb, "chg": chg,
                "amt": nxt_amt,  # 触发日成交额(元)
                "amt_ratio5": float(nxt.get("amt_ratio5", 0)),
                "amt_ratio20": float(nxt.get("amt_ratio20", 0)),
                "max_ret": max_ret,
            })

    if not recs:
        print("无触发样本")
        return
    df = pd.DataFrame(recs)
    df["amt_yi"] = df["amt"] / 1e8

    print(f"\n=== 成交额放量倍数 vs 绝对值 研究：{len(df)} 次触发 ===")

    # 1) 放量倍数与收益的相关性
    print("\n--- 成交额放量倍数(前5日) 与 7日最大涨幅 ---")
    for lo, hi in ((0, 1.5), (1.5, 2), (2, 3), (3, 5), (5, 999)):
        g = df[(df["amt_ratio5"] >= lo) & (df["amt_ratio5"] < hi)]
        if g.empty:
            print(f"  ratio5 {lo}-{hi}: 无样本")
            continue
        win = (g["max_ret"] >= 0).mean() * 100
        big = (g["max_ret"] >= 10).mean() * 100
        print(f"  ratio5 {lo:>4}-{hi:<3}: n={len(g):3d}  7日最大涨幅均值{g['max_ret'].mean():+5.1f}%  "
              f"≥0%概率{win:4.0f}%  ≥10%概率{big:4.0f}%  成交额中位{g['amt_yi'].median():4.1f}亿")

    print("\n--- 成交额放量倍数(前20日) 与 7日最大涨幅 ---")
    for lo, hi in ((0, 1.5), (1.5, 2), (2, 3), (3, 5), (5, 999)):
        g = df[(df["amt_ratio20"] >= lo) & (df["amt_ratio20"] < hi)]
        if g.empty:
            print(f"  ratio20 {lo}-{hi}: 无样本")
            continue
        win = (g["max_ret"] >= 0).mean() * 100
        big = (g["max_ret"] >= 10).mean() * 100
        print(f"  ratio20 {lo:>4}-{hi:<3}: n={len(g):3d}  7日最大涨幅均值{g['max_ret'].mean():+5.1f}%  "
              f"≥0%概率{win:4.0f}%  ≥10%概率{big:4.0f}%  成交额中位{g['amt_yi'].median():4.1f}亿")

    # 2) 对比：绝对值门槛（当前） vs 倍数门槛
    print("\n--- 不同「成交额口径」门槛下的整体表现 ---")
    def report(name, mask):
        g = df[mask]
        if g.empty:
            print(f"  {name}: 无样本")
            return
        big = (g["max_ret"] >= 10).mean() * 100
        win = (g["max_ret"] >= 0).mean() * 100
        print(f"  {name}: n={len(g):3d}  7日最大涨幅均值{g['max_ret'].mean():+5.1f}%  "
              f"≥0%概率{win:4.0f}%  ≥10%概率{big:4.0f}%  成交额中位{g['amt_yi'].median():4.1f}亿")

    report("当前绝对值 4-16亿", (df["amt_yi"] >= 4) & (df["amt_yi"] <= 16))
    report("倍数≥2x(前5日) 且 绝对额≥1亿", (df["amt_ratio5"] >= 2) & (df["amt_yi"] >= 1))
    report("倍数≥2x(前5日) 且 绝对额≥2亿", (df["amt_ratio5"] >= 2) & (df["amt_yi"] >= 2))
    report("倍数≥3x(前5日) 且 绝对额≥1亿", (df["amt_ratio5"] >= 3) & (df["amt_yi"] >= 1))
    report("倍数≥2x(前20日) 且 绝对额≥2亿", (df["amt_ratio20"] >= 2) & (df["amt_yi"] >= 2))

    # 3) 大赚 vs 大亏 的放量倍数分布
    print("\n--- 大赚(≥10%) vs 大亏(≤-10%) 的成交额特征 ---")
    big_w = df[df["max_ret"] >= 10]
    big_l = df[df["max_ret"] <= -10]
    print(f"  大赚 {len(big_w)} 笔: 成交额中位{big_w['amt_yi'].median():.1f}亿  ratio5中位{big_w['amt_ratio5'].median():.2f}  ratio20中位{big_w['amt_ratio20'].median():.2f}")
    print(f"  大亏 {len(big_l)} 笔: 成交额中位{big_l['amt_yi'].median():.1f}亿  ratio5中位{big_l['amt_ratio5'].median():.2f}  ratio20中位{big_l['amt_ratio20'].median():.2f}")

    # 4) 成交额绝对值分桶收益（确认上限/下限各自的作用）
    print("\n--- 成交额绝对值分桶 → 7日最大涨幅 ---")
    for lo, hi in ((0, 2), (2, 4), (4, 8), (8, 16), (16, 999)):
        g = df[(df["amt_yi"] >= lo) & (df["amt_yi"] < hi)]
        if g.empty:
            print(f"  成交额 {lo}-{hi}亿: 无样本")
            continue
        big = (g["max_ret"] >= 10).mean() * 100
        win = (g["max_ret"] >= 0).mean() * 100
        print(f"  成交额 {lo:>2}-{hi:<3}亿: n={len(g):3d}  7日最大涨幅均值{g['max_ret'].mean():+5.1f}%  "
              f"≥0%概率{win:4.0f}%  ≥10%概率{big:4.0f}%  ratio5中位{g['amt_ratio5'].median():.2f}")

    # 5) 关键：成交额<4亿（被当前下限挡掉）里，放量倍数高的小票表现
    print("\n--- 成交额<4亿 且 放量倍数分层（是否被下限误杀） ---")
    low = df[df["amt_yi"] < 4]
    print(f"  成交额<4亿 全部: n={len(low)}  ≥10%概率{(low['max_ret']>=10).mean()*100:.0f}%  "
          f"7日最大涨幅均值{low['max_ret'].mean():+.1f}%")
    for lo, hi in ((0, 2), (2, 3), (3, 999)):
        g = low[(low["amt_ratio5"] >= lo) & (low["amt_ratio5"] < hi)]
        if g.empty:
            print(f"    ratio5 {lo}-{hi}: 无样本")
            continue
        big = (g["max_ret"] >= 10).mean() * 100
        print(f"    ratio5 {lo:>2}-{hi:<3}: n={len(g):3d}  ≥10%概率{big:4.0f}%  "
              f"7日最大涨幅均值{g['max_ret'].mean():+.1f}%  成交额中位{g['amt_yi'].median():.1f}亿")

    # 6) 成交额>16亿（被上限挡掉）里，放量倍数分布（能否用倍数替代上限）
    print("\n--- 成交额>16亿 且 放量倍数（能否用倍数替代绝对值上限） ---")
    hi = df[df["amt_yi"] > 16]
    print(f"  成交额>16亿 全部: n={len(hi)}  ≥10%概率{(hi['max_ret']>=10).mean()*100:.0f}%  "
          f"7日最大涨幅均值{hi['max_ret'].mean():+.1f}%  ratio5范围{hi['amt_ratio5'].min():.2f}-{hi['amt_ratio5'].max():.2f}  "
          f"ratio20范围{hi['amt_ratio20'].min():.2f}-{hi['amt_ratio20'].max():.2f}")


if __name__ == "__main__":
    main()
