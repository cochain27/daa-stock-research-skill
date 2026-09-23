# -*- coding: utf-8 -*-
"""右侧趋势「彻底让利润奔跑」出场规则回测。

问题（2026-09-13 海龟交易法则调研的待验证项）：
  现有趋势出场 = 固定止损-6% + 止盈1(+8%减半锁半仓) + 移动止盈(盈利>3%破MA10清仓) + MA20结构止损。
  海龟「让利润奔跑」主张不设固定目标价，只靠趋势跟踪离场。
  问：去掉 +8% 减半、全仓持有到「破MA10移动止盈 / 破MA20结构止损 / 到期」，盈亏比×胜率是否更优？

方法：
  - 完全复用 backtest_engine 的数据层（_load_hist_cache 前复权 K 线 + _features + _pick_trend），
    保证信号生成与「backtest_engine --cache」的 118 笔口径完全一致；
  - 对同一组信号，分别跑 3 种出场，只改出场这一个变量：
      基准   = _simulate_v2（现有线上规则）
      变体A  = 去减半，全仓，盈利>3%后破MA10清仓（沿用移动止盈阈值，唯一差异=去掉+8%减半）
      变体B  = 去减半，全仓，收盘破MA10即清仓（无3%门槛，纯MA10趋势跟踪）
  - 输出胜率/盈亏比/均收/胜×盈亏/累计/回撤/了结原因分布，逐笔明细落盘。

用法：
  python trend_let_profit_study.py --start 2026-03-01 --end 2026-09-11
（默认离线读 data/klines/ 全部代码，无任何网络请求。）
"""
import argparse
import glob
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

# 复用 backtest_engine 的数据层与选股（保证信号口径一致）
from backtest_engine import (_load_hist_cache, _features, _pick_trend, _simulate_v2,
                             _ma_close_break, _ma10_close_break)
from config import (ALLOW_CODE_PREFIX, TREND_STOP_LOSS, TREND_TAKE_PROFIT_1,
                    TREND_HOLD_DAYS)

TREND_MAX_HOLD = TREND_HOLD_DAYS[1]


def _simulate_let_profit(f, entry_idx, entry_price, stop_pct, max_hold,
                         move_thresh=None):
    """彻底让利润奔跑：去掉 +8% 减半，全仓持有到趋势跟踪离场。

    - 止损：固定 stop_pct（盘中 low 触及，最优先）
    - 结构止损：收盘跌破 MA20 清仓
    - 移动止盈：收盘跌破 MA10 清仓
        move_thresh=None  → 破 MA10 即走（无盈利门槛，变体B）
        move_thresh=0.03  → 盈利>3% 后才启用破 MA10（变体A，沿用线上阈值）
    - 到期 max_hold 离场
    返回 (收益率%, 持有天数, 了结原因)
    """
    stop = entry_price * (1 + stop_pct)
    n = len(f)
    for d in range(1, max_hold + 1):
        i = entry_idx + d
        if i >= n:
            break
        row = f.iloc[i]
        lo = float(row["low"])
        close = float(row["close"])
        ret_now = (close / entry_price - 1)
        # 1) 止损优先（盘中）
        if lo <= stop:
            return (stop - entry_price) / entry_price * 100, d, "止损"
        # 2) 结构止损（收盘破 MA20，趋势破坏）
        if _ma_close_break(f, i):
            return (close - entry_price) / entry_price * 100, d, "结构止损"
        # 3) 移动止盈（收盘破 MA10）
        if _ma10_close_break(f, i):
            if move_thresh is None or ret_now > move_thresh:
                return (close - entry_price) / entry_price * 100, d, "移动止盈"
        # 4) 到期
        if d == max_hold:
            return (close - entry_price) / entry_price * 100, d, "到期"
    # 数据不足（回测区间末尾）
    row = f.iloc[min(entry_idx + max_hold, n - 1)]
    return (float(row["close"]) - entry_price) / entry_price * 100, max_hold, "数据不足"


def _stats_df(trades):
    if not trades:
        return None
    df = pd.DataFrame(trades)
    win = df[df["收益%"] > 0]
    loss = df[df["收益%"] <= 0]
    avg_w = win["收益%"].mean() if len(win) else 0.0
    avg_l = loss["收益%"].mean() if len(loss) else 0.0
    wr = len(win) / len(df) * 100
    pl = abs(avg_w / avg_l) if avg_l else 0.0
    n = len(df)
    nav = (1 + df["收益%"] / 100 / n).cumprod()
    mdd = ((nav.cummax() - nav) / nav.cummax()).max() * 100
    return {
        "笔数": n, "胜率%": round(wr, 1), "均收%": round(df["收益%"].mean(), 2),
        "平均盈利%": round(avg_w, 2), "平均亏损%": round(avg_l, 2),
        "盈亏比": round(pl, 2), "胜×盈亏": round(wr * pl, 2),
        "最好%": round(df["收益%"].max(), 2), "最差%": round(df["收益%"].min(), 2),
        "累计%": round(df["收益%"].sum(), 1), "最大回撤%": round(mdd, 1),
        "平均持有天": round(df["持有天"].mean(), 1),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-03-01")
    ap.add_argument("--end", default="2026-09-11")
    args = ap.parse_args()

    cache_dir = Path(__file__).resolve().parent.parent / "data" / "klines"
    codes = []
    for p in sorted(glob.glob(str(cache_dir / "*.csv"))):
        c = Path(p).stem[2:]  # 去掉 sh/sz 前缀
        if c.startswith(ALLOW_CODE_PREFIX):
            codes.append(c)
    print(f"[1/3] 候选池 {len(codes)} 只（klines 缓存，前复权）", flush=True)

    raw = {}
    for c in codes:
        _, h = _load_hist_cache(c)
        if h is not None:
            raw[c] = h
    print(f"  就绪 {len(raw)} 只", flush=True)

    feats = {c: _features(h) for c, h in raw.items()}
    names = {c: c for c in raw}
    cal = sorted(set(pd.concat([f[["date"]] for f in feats.values()]).drop_duplicates()["date"]))
    cal = [d for d in cal if pd.Timestamp(args.start) <= d <= pd.Timestamp(args.end)]
    print(f"[2/3] 回放 {len(cal)} 个交易日（{cal[0].date()} ~ {cal[-1].date()}）", flush=True)

    base, var_a, var_b = [], [], []
    signals = 0
    for i in range(1, len(cal)):
        d_prev, d_now = cal[i - 1], cal[i]
        for p in _pick_trend(feats, codes, names, d_prev, fixed=True, variant="v3"):
            f = feats[p["代码"]]
            idx = f.index[f["date"] == d_now]
            if len(idx) == 0:
                continue
            ei = idx[0]
            entry = float(f.iloc[ei]["open"])
            if entry <= 0:
                continue
            signals += 1
            meta = {"买入日": str(d_now.date()), "代码": p["代码"], "名称": p["名称"],
                    "买价": round(entry, 2)}

            pnl, days, why = _simulate_v2(
                f, ei, entry, TREND_STOP_LOSS, TREND_TAKE_PROFIT_1,
                TREND_MAX_HOLD, time_stop_days=0, use_ma20_struct=True)
            base.append({**meta, "收益%": round(pnl, 2), "持有天": days, "了结原因": why})

            pnl, days, why = _simulate_let_profit(
                f, ei, entry, TREND_STOP_LOSS, TREND_MAX_HOLD, move_thresh=0.03)
            var_a.append({**meta, "收益%": round(pnl, 2), "持有天": days, "了结原因": why})

            pnl, days, why = _simulate_let_profit(
                f, ei, entry, TREND_STOP_LOSS, TREND_MAX_HOLD, move_thresh=None)
            var_b.append({**meta, "收益%": round(pnl, 2), "持有天": days, "了结原因": why})

    print(f"[3/3] 信号 {signals} 笔，三种出场已结算", flush=True)

    print("\n" + "=" * 90)
    for label, tr in [("基准(线上:-6%/+8%减半/移动/结构)", base),
                      ("变体A(去减半,盈利>3%破MA10)", var_a),
                      ("变体B(去减半,破MA10即走)", var_b)]:
        s = _stats_df(tr)
        print(f"\n### {label}")
        for k, v in s.items():
            print(f"  {k}: {v}")
        df = pd.DataFrame(tr)
        print("  了结原因分布：" + str(df["了结原因"].value_counts().to_dict()))
    print("=" * 90)

    out = Path(__file__).resolve().parent.parent / "data"
    for nm, tr in [("回测_右侧趋势_让利润奔跑_基准", base),
                   ("回测_右侧趋势_让利润奔跑_变体A", var_a),
                   ("回测_右侧趋势_让利润奔跑_变体B", var_b)]:
        pd.DataFrame(tr).to_csv(out / f"{nm}.csv", index=False, encoding="utf-8")
        print(f"明细已写入 data/{nm}.csv")

    rows = []
    for label, tr in [("基准(线上)", base), ("变体A(盈利>3%破MA10)", var_a),
                      ("变体B(破MA10即走)", var_b)]:
        s = _stats_df(tr)
        s["变体"] = label
        rows.append(s)
    pd.DataFrame(rows).to_csv(out / "回测_右侧趋势_让利润奔跑_汇总.csv",
                              index=False, encoding="utf-8")
    print("汇总已写入 data/回测_右侧趋势_让利润奔跑_汇总.csv")
    print("\n注：不计手续费/滑点（实盘约 -0.3~-0.6%）；研究参考，不构成投资建议。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
