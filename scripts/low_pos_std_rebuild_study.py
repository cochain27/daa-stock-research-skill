# -*- coding: utf-8 -*-
"""标准蓄势路径入选判据重构研究（读本地 data/klines，无网络）。

背景：标准蓄势路径样本薄(25-41笔)、≥10%概率仅24-26%，靠微调现有参数已到天花板。
用户要求"重构标准蓄势的入选判据"，而非继续调参。

核心假设（待数据验证）：
  当前判据 `dist60 <= -25`（距60日高点跌超25%）本质是"深度调整"，与"低估价值票
  健康回调后缩量企稳、等待资金启动"的语义冲突——它把标准蓄势推向了"深跌"范畴，
  与"近期超卖"路径高度重叠，反而漏掉了真正的中浅回调企稳形态。

重构方向（假设）：
  · 标准蓄势 = 健康回调(中浅跌幅) + 趋势企稳(站上/贴近MA20) + 缩量 + 横盘
  · 与近期超卖(深跌后反弹)形成互补分工，而非重叠

本脚本：在"缩量+横盘"蓄势内核之上，放宽 dist60/pos60 约束，对每个触发样本
记录蓄势日形态特征，分桶看 7 日最大涨幅 ≥10% 概率 / 均值 / 样本数。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

from config import (
    LOW_POS_ENTRY_MAX_LB5, LOW_POS_ENTRY_MAX_LB5_MEAN, LOW_POS_ENTRY_CHG5_RANGE,
    LOW_POS_ENTRY_MAX_CHG_TODAY, LOW_POS_ENTRY_MAX_AMP20,
    LOW_POS_ENTRY_MIN_AMOUNT, LOW_POS_ENTRY_MAX_AMOUNT,
    LOW_POS_ENTRY_TRIGGER_LB, LOW_POS_ENTRY_TRIGGER_LB_MAX,
    LOW_POS_ENTRY_TRIGGER_CHG_MIN, LOW_POS_ENTRY_TRIGGER_CHG_MAX,
    LOW_POS_ENTRY_TRIGGER_BREAK_MA20,
    LOW_POS_ENTRY_TRIGGER_MIN_AMOUNT, LOW_POS_ENTRY_TRIGGER_MAX_AMOUNT,
    LOW_POS_ENTRY_RECENT_OVERSOLD_LOOKBACK,
    LOW_POS_ENTRY_RECENT_OVERSOLD_POS60_MAX, LOW_POS_ENTRY_RECENT_OVERSOLD_DIST_MAX,
    LOW_POS_ENTRY_TEMP_MIN,
)

KLINE_DIR = Path(__file__).resolve().parent.parent / "data" / "klines"
OUT_CSV = Path(__file__).resolve().parent.parent / "data" / "标准蓄势_重构研究.csv"


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
    # 趋势企稳特征
    d["close_ma20"] = d["收盘"] / d["MA20"] - 1          # 收盘相对MA20（+站上/-下方）
    d["ma5_ma20"] = d["MA5"] / d["MA20"] - 1             # MA5相对MA20（金叉/死叉）
    d["ma20_slope"] = d["MA20"].pct_change(5) * 100      # MA20 5日斜率（走平/向下/向上）
    return d


def _deep_oversold_grade(ind_df, cur_idx):
    """返回深度超卖分级：0=无深度超卖(健康回调) 1=中度 2=深度。
    用更严格的阈值，避免把"健康回调到中低位"误判为超卖：
      深度超卖 = pos60<0.30 或 dist60<-35；中度 = pos60<0.40 或 dist60<-30。"""
    lookback = LOW_POS_ENTRY_RECENT_OVERSOLD_LOOKBACK
    start = max(0, cur_idx - lookback)
    window = ind_df.iloc[start:cur_idx]
    grade = 0
    for _, row in window.iterrows():
        p60 = row.get("pos60")
        d60 = row.get("dist60")
        if p60 is not None and p60 < 0.30:
            return 2
        if d60 is not None and d60 < -35:
            return 2
        if p60 is not None and p60 < 0.40:
            grade = max(grade, 1)
        if d60 is not None and d60 < -30:
            grade = max(grade, 1)
    return grade


def _横盘企稳通过(row):
    """蓄势内核（放宽缩量，聚焦横盘企稳）：横盘 + 成交额 + 候选日不追高。"""
    if pd.isna(row.get("pos60")) or pd.isna(row.get("dist60")):
        return False
    lb = row.get("lb")
    lbm = row.get("lb5mean")
    if pd.isna(lb) or pd.isna(lbm):
        return False
    if lb > 1.5 or lbm > 1.5:   # 放宽缩量：价值票未必极致缩量，温和即可
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


def main():
    codes = [p.stem[2:] for p in sorted(KLINE_DIR.glob("*.csv"))]
    print(f"读取本地缓存 {len(codes)} 只 ...", flush=True)
    hists = {}
    for c in codes:
        h = _load_cache(c)
        if h is not None:
            hists[c] = h
    print(f"K线就绪 {len(hists)} 只", flush=True)

    print("扫描触发（蓄势内核=横盘企稳，放宽位置/距高点，触发涨幅放宽到5-15%）...", flush=True)
    CHG_LO, CHG_HI = 5.0, 15.0   # 放宽触发涨幅下界，观察"温和启动"vs"强启动"
    recs = []
    for code, h in hists.items():
        ind = _indicators(h)
        for i in range(70, len(ind) - 1):
            row = ind.iloc[i]
            if not _横盘企稳通过(row):
                continue
            # 深度超卖分级（0=健康回调 1=中度 2=深度）
            grade = _deep_oversold_grade(ind, i + 1)
            nxt = ind.iloc[i + 1]
            lb = float(nxt["lb"])
            chg = float(nxt["涨跌幅"])
            nxt_amt = float(nxt.get("成交额", 0))
            ma20 = float(row["MA20"])
            nxt_pos60 = float(nxt.get("pos60", 0))
            # 触发：量比2-7 + 涨幅5-15 + 成交额4-16 + 破MA20（位置上限放到0.65）
            if not (lb >= LOW_POS_ENTRY_TRIGGER_LB and lb <= LOW_POS_ENTRY_TRIGGER_LB_MAX
                    and CHG_LO <= chg <= CHG_HI
                    and LOW_POS_ENTRY_TRIGGER_MIN_AMOUNT <= nxt_amt <= LOW_POS_ENTRY_TRIGGER_MAX_AMOUNT
                    and (not LOW_POS_ENTRY_TRIGGER_BREAK_MA20 or float(nxt["收盘"]) > ma20)
                    and nxt_pos60 < LOW_POS_ENTRY_RECENT_OVERSOLD_POS60_MAX):
                continue
            entry = float(nxt["收盘"])
            fut = ind.iloc[i + 2:i + 9]
            if fut.empty:
                continue
            closes = fut["收盘"].astype(float).tolist()
            max_ret = (max(closes) / entry - 1) * 100
            recs.append({
                "code": code, "date": pd.Timestamp(nxt["date"]),
                "grade": grade,                          # 0=健康回调 1=中度 2=深度超卖
                "reserve_pos60": float(row.get("pos60", 0)),
                "reserve_dist60": float(row.get("dist60", 0)),
                "reserve_lb": float(row.get("lb", 0)),
                "close_ma20": float(row.get("close_ma20", 0) or 0),
                "ma5_ma20": float(row.get("ma5_ma20", 0) or 0),
                "ma20_slope": float(row.get("ma20_slope", 0) or 0),
                "trigger_pos60": nxt_pos60,
                "chg": chg, "lb": lb,
                "max_ret": max_ret,
            })

    if not recs:
        print("无触发样本")
        return
    df = pd.DataFrame(recs)
    df.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")

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
    df["temp"] = df["date"].map(
        lambda td: date_up.get(td, [0, 0])[0] / max(date_up.get(td, [0, 0])[1], 1) * 100)
    df = df[df["temp"] >= LOW_POS_ENTRY_TEMP_MIN].copy()

    print(f"\n=== 标准蓄势重构研究：温度≥{LOW_POS_ENTRY_TEMP_MIN} 后 {len(df)} 次触发 ===")
    g2 = (df["grade"] == 2).sum()
    g1 = (df["grade"] == 1).sum()
    g0 = (df["grade"] == 0).sum()
    print(f"  深度超卖(grade=2) {g2} / 中度(grade=1) {g1} / 健康回调(grade=0) {g0}\n")

    def report(grp, name):
        if grp.empty:
            print(f"    {name:<28} n=  0")
            return
        big = (grp["max_ret"] >= 10).mean() * 100
        win0 = (grp["max_ret"] >= 0).mean() * 100
        print(f"    {name:<28} n={len(grp):3d}  7日最大涨幅均值{grp['max_ret'].mean():+5.1f}%  "
              f"≥0% {win0:3.0f}%  ≥10% {big:3.0f}%")

    # A. 深度超卖分级对比
    print("== A. 超卖深度分级对比 ==")
    report(df[df["grade"] == 0], "健康回调(grade=0)")
    report(df[df["grade"] == 1], "中度回调(grade=1)")
    report(df[df["grade"] == 2], "深度超卖(grade=2)")

    std = df[df["grade"] == 0].copy()
    print(f"\n== B. 健康回调候选（grade=0，n={len(std)}）按形态拆层 ==")

    print("  [触发涨幅 chg（关键：健康回调的启动方式）]")
    report(std[std["chg"] < 7], "触发涨幅 5-7%")
    report(std[(std["chg"] >= 7) & (std["chg"] < 9)], "7-9%")
    report(std[(std["chg"] >= 9) & (std["chg"] < 10)], "9-10%")
    report(std[(std["chg"] >= 10) & (std["chg"] < 12)], "10-12%")
    report(std[std["chg"] >= 12], "12-15%")

    print("  [蓄势日距60日高点 dist60]")
    report(std[std["reserve_dist60"] >= -8], "dist60 ≥ -8%（浅回调）")
    report(std[(std["reserve_dist60"] >= -15) & (std["reserve_dist60"] < -8)], "-15%~-8%")
    report(std[(std["reserve_dist60"] >= -25) & (std["reserve_dist60"] < -15)], "-25%~-15%")
    report(std[std["reserve_dist60"] < -25], "< -25%")

    print("  [蓄势日位置 pos60]")
    report(std[std["reserve_pos60"] < 0.3], "pos60 < 0.30")
    report(std[(std["reserve_pos60"] >= 0.3) & (std["reserve_pos60"] < 0.4)], "0.30-0.40")
    report(std[(std["reserve_pos60"] >= 0.4) & (std["reserve_pos60"] < 0.5)], "0.40-0.50")
    report(std[(std["reserve_pos60"] >= 0.5) & (std["reserve_pos60"] < 0.65)], "0.50-0.65")

    print("  [蓄势日收盘 vs MA20]")
    report(std[std["close_ma20"] >= 0], "站上MA20")
    report(std[(std["close_ma20"] >= -0.03) & (std["close_ma20"] < 0)], "贴近MA20下方(-3%~0)")
    report(std[std["close_ma20"] < -0.03], "MA20下方>3%")

    print("  [蓄势日 MA5 vs MA20]")
    report(std[std["ma5_ma20"] >= 0], "MA5≥MA20")
    report(std[std["ma5_ma20"] < 0], "MA5<MA20")

    print("  [蓄势日 MA20 5日斜率]")
    report(std[std["ma20_slope"] >= 0.5], "MA20向上")
    report(std[(std["ma20_slope"] >= -0.5) & (std["ma20_slope"] < 0.5)], "MA20走平")
    report(std[std["ma20_slope"] < -0.5], "MA20向下")

    # C. 关键组合（健康回调）
    print("\n== C. 关键组合（健康回调 grade=0） ==")
    report(std[(std["reserve_dist60"] >= -25) & (std["close_ma20"] >= -0.03)],
           "回调≤25% + 贴近/站上MA20")
    report(std[(std["reserve_dist60"] >= -25) & (std["ma5_ma20"] >= 0)],
           "回调≤25% + MA5≥MA20")
    report(std[(std["reserve_dist60"] >= -15) & (std["close_ma20"] >= 0)],
           "回调≤15% + 站上MA20")
    report(std[(std["chg"] >= 7) & (std["chg"] < 10)],
           "温和启动(涨幅7-10%)")

    print(f"\n明细 → {OUT_CSV}")


if __name__ == "__main__":
    main()
