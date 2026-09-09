# -*- coding: utf-8 -*-
"""提高胜率的变体对比（复用回测引擎数据层，一次拉数、多变体结算）。

目标：找出能显著提升「胜率×盈亏比」的过滤条件。
方向：
  趋势 T1-T7：温度收紧 / 量比2 / MA20斜率 / MACD零轴 / 位置区间 / 开盘确认 / 叠加
  短线 S1-S3：温度收紧 / 开盘确认 / 剔长上影
用法：python optimize_winrate.py --start 2026-03-01 --end 2026-09-08 --top 1200
"""
import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fetch_data  # noqa: F401  触发强制直连 patch
from fetch_data import get_market_snapshot
from config import (ALLOW_CODE_PREFIX, TREND_MIN_AMOUNT, TREND_MAX_20D_AMPLITUDE,
                    TREND_MAX_20D_STD_RATIO, TREND_BREAKOUT_VOL_RATIO,
                    TREND_BREAKOUT_CHG_RANGE, TREND_HOLD_DAYS, TREND_MAX_PICKS,
                    SHORT_STOP_LOSS, SHORT_TAKE_PROFIT_1, SHORT_HOLD_DAYS_MAX,
                    SHORT_TIME_STOP_DAYS, SHORTLINE_MIN_CHG,
                    TREND_STOP_LOSS, TREND_TAKE_PROFIT_1)
from backtest_engine import _load_hist, _features, _simulate_v2, _stats

TREND_MAX_HOLD = TREND_HOLD_DAYS[1]
SHORT_MAX_PICKS = 2


def _temp_proxy_th(feats, codes, date, th=0.55):
    """市场温度 proxy，阈值参数化（原 0.55）"""
    above = total = 0
    ts = pd.Timestamp(date)
    for c in codes:
        f = feats.get(c)
        if f is None:
            continue
        sub = f[f["date"] <= ts]
        if len(sub) < 30 or sub["date"].iloc[-1] != ts:
            continue
        r = sub.iloc[-1]
        if pd.isna(r["ma20"]) or pd.isna(r["close"]):
            continue
        total += 1
        if float(r["close"]) > float(r["ma20"]):
            above += 1
    if total < 50:
        return True
    return above / total >= th


def _pick_trend_cfg(feats, codes, names, as_of, cfg):
    """趋势选股，cfg 为可选过滤开关 dict"""
    ts = pd.Timestamp(as_of)
    out = []
    for code in codes:
        f = feats.get(code)
        if f is None:
            continue
        sub = f[f["date"] <= ts]
        if len(sub) < 60 or sub["date"].iloc[-1] != ts:
            continue
        r = sub.iloc[-1]
        try:
            if not (r["amount"] > TREND_MIN_AMOUNT):
                continue
            if not (r["pos60"] < 0.70):
                continue
            if not (r["amp20"] < TREND_MAX_20D_AMPLITUDE):
                continue
            if not (r["std20"] < TREND_MAX_20D_STD_RATIO):
                continue
            if not (r["vol_ratio"] >= TREND_BREAKOUT_VOL_RATIO):
                continue
            lo, hi = TREND_BREAKOUT_CHG_RANGE
            if not (lo <= r["chg"] <= hi):
                continue
            if not ((r["close"] > r["ma10"] > r["ma20"]) or (r["ma5"] > r["ma10"] > r["ma20"])):
                continue
            if not (r["dif"] > r["dea"]):
                continue
            if not (40 <= r["rsi14"] <= 70):
                continue
            if not (r["close"] > r["ma20"]):
                continue
            # ===== 变体过滤 =====
            if cfg.get("vol2") and not (r["vol_ratio"] >= 2.0):
                continue
            if cfg.get("ma20_slope"):
                # MA20 比 5 日前 MA20 更高（均线走平向上）
                if len(sub) < 26:
                    continue
                prev = sub.iloc[-6]["ma20"]
                if pd.isna(prev) or r["ma20"] <= prev:
                    continue
            if cfg.get("dif_pos") and not (r["dif"] > 0):
                continue
            if cfg.get("pos_range"):
                lo_p, hi_p = cfg["pos_range"]
                if not (lo_p <= r["pos60"] <= hi_p):
                    continue
        except Exception:
            continue
        out.append({"代码": code, "名称": names.get(code, code), "收盘": float(r["close"])})
    return out[:TREND_MAX_PICKS]


def _pick_short_cfg(feats, codes, names, as_of, cfg):
    """短线选股，cfg 可选过滤：no_upper_shadow"""
    ts = pd.Timestamp(as_of)
    out = []
    for code in codes:
        f = feats.get(code)
        if f is None:
            continue
        sub = f[f["date"] <= ts]
        if len(sub) < 60 or sub["date"].iloc[-1] != ts:
            continue
        r = sub.iloc[-1]
        try:
            if not (r["amount"] > TREND_MIN_AMOUNT):
                continue
            chg_pct = r["chg"] * 100
            if not (chg_pct >= SHORTLINE_MIN_CHG):
                continue
            limit = 19.8 if code.startswith("30") else 9.8
            if chg_pct >= limit:
                continue
            if not (r["vol_ratio"] >= 1.5):
                continue
            if not (r["pos60"] < 0.70):
                continue
            if cfg.get("no_shadow"):
                # 冲高回落：上影线（最高-收盘）/收盘 > 3% 剔除
                up = (float(r["high"]) - float(r["close"])) / float(r["close"])
                if up > 0.03:
                    continue
        except Exception:
            continue
        out.append({"代码": code, "名称": names.get(code, code), "收盘": float(r["close"])})
    out.sort(key=lambda x: -x["收盘"])
    return out[:SHORT_MAX_PICKS]


def _open_gap(f, ei):
    """T 日开盘相对 T-1 收盘的涨幅；无法计算返回 None"""
    try:
        if ei < 1:
            return None
        prev_close = float(f.iloc[ei - 1]["close"])
        if prev_close <= 0:
            return None
        return float(f.iloc[ei]["open"]) / prev_close - 1
    except Exception:
        return None


def run(feats, codes, names, cal, start, end):
    results = {}

    def _trend_run(cfg, label):
        tr = []
        for i in range(1, len(cal)):
            d_prev, d_now = cal[i - 1], cal[i]
            th = cfg.get("temp_th", 0.55)
            if not _temp_proxy_th(feats, codes, d_prev, th):
                continue
            for p in _pick_trend_cfg(feats, codes, names, d_prev, cfg):
                f = feats[p["代码"]]
                idx = f.index[f["date"] == d_now]
                if len(idx) == 0:
                    continue
                ei = idx[0]
                entry = float(f.iloc[ei]["open"])
                if entry <= 0:
                    continue
                # 开盘确认：跳过不满足 gap 区间的（买不进/追高/低开）
                gap_cfg = cfg.get("open_gap")
                if gap_cfg:
                    g = _open_gap(f, ei)
                    if g is None or not (gap_cfg[0] <= g <= gap_cfg[1]):
                        continue
                pnl, days, why = _simulate_v2(
                    f, ei, entry, TREND_STOP_LOSS, TREND_TAKE_PROFIT_1,
                    TREND_MAX_HOLD, time_stop_days=0, use_ma20_struct=True)
                tr.append({"收益%": round(pnl, 2), "持有天": days, "了结原因": why})
        results[label] = tr

    def _short_run(cfg, label):
        tr = []
        for i in range(1, len(cal)):
            d_prev, d_now = cal[i - 1], cal[i]
            th = cfg.get("temp_th", 0.0)
            if th > 0 and not _temp_proxy_th(feats, codes, d_prev, th):
                continue
            for p in _pick_short_cfg(feats, codes, names, d_prev, cfg):
                f = feats[p["代码"]]
                idx = f.index[f["date"] == d_now]
                if len(idx) == 0:
                    continue
                ei = idx[0]
                entry = float(f.iloc[ei]["open"])
                if entry <= 0:
                    continue
                gap_cfg = cfg.get("open_gap")
                if gap_cfg:
                    g = _open_gap(f, ei)
                    if g is None or not (gap_cfg[0] <= g <= gap_cfg[1]):
                        continue
                pnl, days, why = _simulate_v2(
                    f, ei, entry, SHORT_STOP_LOSS, SHORT_TAKE_PROFIT_1,
                    SHORT_HOLD_DAYS_MAX, SHORT_TIME_STOP_DAYS)
                tr.append({"收益%": round(pnl, 2), "持有天": days, "了结原因": why})
        results[label] = tr

    # ===== 趋势变体 =====
    _trend_run({}, "T0_基准_v3")
    _trend_run({"temp_th": 0.60}, "T1_温度60")
    _trend_run({"temp_th": 0.65}, "T2_温度65")
    _trend_run({"vol2": True}, "T3_量比2")
    _trend_run({"ma20_slope": True}, "T4_MA20斜率")
    _trend_run({"dif_pos": True}, "T5_MACD零轴上")
    _trend_run({"pos_range": (0.30, 0.70)}, "T6_位置30-70")
    _trend_run({"open_gap": (0.0, 0.035)}, "T7_开盘确认0-3.5%")
    _trend_run({"temp_th": 0.60, "ma20_slope": True, "dif_pos": True,
                "pos_range": (0.30, 0.70), "open_gap": (0.0, 0.035)}, "T8_组合叠加")

    # ===== 短线变体 =====
    _short_run({}, "S0_基准")
    _short_run({"temp_th": 0.60}, "S1_温度60")
    _short_run({"open_gap": (0.005, 0.07)}, "S2_开盘确认0.5-7%")
    _short_run({"no_shadow": True}, "S3_剔长上影")
    _short_run({"temp_th": 0.60, "open_gap": (0.005, 0.07), "no_shadow": True}, "S4_组合叠加")

    print("\n" + "=" * 100)
    print(f"{'变体':<16}{'笔数':>6}{'胜率%':>8}{'均收%':>8}{'盈亏比':>8}{'胜×盈亏':>9}{'累计%':>9}{'回撤%':>8}")
    print("-" * 100)
    rows = []
    for label in sorted(results.keys()):
        tr = results[label]
        if not tr:
            print(f"{label:<16}{'无交易':>6}")
            continue
        df = pd.DataFrame(tr)
        win = df[df["收益%"] > 0]
        loss = df[df["收益%"] <= 0]
        wr = len(win) / len(df) * 100
        avg = df["收益%"].mean()
        avg_w = win["收益%"].mean() if len(win) else 0
        avg_l = loss["收益%"].mean() if len(loss) else 0
        pl = abs(avg_w / avg_l) if avg_l else 0
        n = len(df)
        nav = (1 + df["收益%"] / 100 / n).cumprod()
        mdd = ((nav.cummax() - nav) / nav.cummax()).max() * 100
        cum = df["收益%"].sum()
        print(f"{label:<16}{n:>6}{wr:>8.1f}{avg:>8.2f}{pl:>8.2f}{wr*pl:>9.2f}{cum:>9.1f}{mdd:>8.1f}")
        rows.append({"变体": label, "笔数": n, "胜率%": round(wr, 1), "均收%": round(avg, 2),
                     "盈亏比": round(pl, 2), "胜×盈亏": round(wr * pl, 2),
                     "累计%": round(cum, 1), "回撤%": round(mdd, 1)})
    print("=" * 100)
    out = Path(__file__).resolve().parent.parent / "data"
    pd.DataFrame(rows).to_csv(out / "回测_胜率优化对比.csv", index=False, encoding="utf-8")
    print(f"已写入 data/回测_胜率优化对比.csv")
    print("\n注：不计手续费/滑点（实盘约 -0.3~-0.6%）；研究参考，不构成投资建议。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-03-01")
    ap.add_argument("--end", default="2026-09-08")
    ap.add_argument("--top", type=int, default=1200)
    args = ap.parse_args()

    print("[1/3] 拉取全A快照 ...", flush=True)
    snap = get_market_snapshot()
    if snap is None or snap.empty:
        print("快照失败")
        return 1
    snap = snap.copy()
    snap["代码"] = snap["代码"].astype(str).str.replace(r"\.0$", "", regex=True)
    snap = snap[snap["代码"].str.match(r"^(?:" + "|".join(ALLOW_CODE_PREFIX) + ")", na=False)]
    snap = snap[~snap["名称"].astype(str).str.contains("ST|退", na=False)]
    snap["_amt"] = pd.to_numeric(snap.get("成交额"), errors="coerce").fillna(0)
    snap = snap.sort_values("_amt", ascending=False).head(args.top)
    codes = snap["代码"].tolist()
    names = dict(zip(snap["代码"], snap["名称"].astype(str)))

    print(f"[2/3] 并发拉历史K线（{len(codes)} 只）...", flush=True)
    raw, t0 = {}, time.time()
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(_load_hist, c): c for c in codes}
        for i, fu in enumerate(as_completed(futs), 1):
            c, h = fu.result()
            if h is not None:
                raw[c] = h
            if i % 300 == 0:
                print(f"  {i}/{len(codes)} ok={len(raw)} {time.time()-t0:.0f}s", flush=True)
    print(f"  就绪 {len(raw)} 只，{time.time()-t0:.0f}s", flush=True)

    feats = {c: _features(h) for c, h in raw.items()}
    cal = sorted(set(pd.concat([f[["date"]] for f in feats.values()]).drop_duplicates()["date"]))
    cal = [d for d in cal if pd.Timestamp(args.start) <= d <= pd.Timestamp(args.end)]
    print(f"[3/3] 回放 {len(cal)} 个交易日 ...", flush=True)
    run(feats, codes, names, cal, args.start, args.end)
    return 0


if __name__ == "__main__":
    sys.exit(main())
