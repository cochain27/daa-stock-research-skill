# -*- coding: utf-8 -*-
"""第二轮优化：组合叠加已验证正因子 + 出场参数扫描。

基线 = 已落地生产规则（趋势 pos30-70 / 短线剔长上影）。
方向：
  趋势 TR1-TR3：温度60 / MACD零轴 / 双叠加；TR4-TR5：止盈1 参数扫描
  短线 S1：上影+只留近涨停梯队；S2-S3：上影阈值扫描；S4：近涨停阈值；S5-S6：止盈1 参数扫描
用法：python round2_optimize.py --start 2026-03-01 --end 2026-09-08 --top 1200
"""
import argparse
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
                    SHORT_TIME_STOP_DAYS, SHORTLINE_MIN_CHG, SHORTLINE_MAX_PICKS,
                    TREND_STOP_LOSS, TREND_TAKE_PROFIT_1)
from backtest_engine import _load_hist, _features, _simulate_v2, _stats

TREND_MAX_HOLD = TREND_HOLD_DAYS[1]


def _temp_proxy_th(feats, codes, date, th=0.55):
    """市场温度 proxy，阈值参数化"""
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
    """趋势选股（生产基线 = pos_range(0.30,0.70)）"""
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
            pr = cfg.get("pos_range")
            if pr and not (pr[0] <= r["pos60"] <= pr[1]):
                continue
            if cfg.get("dif_pos") and not (r["dif"] > 0):
                continue
        except Exception:
            continue
        out.append({"代码": code, "名称": names.get(code, code), "收盘": float(r["close"])})
    return out[:TREND_MAX_PICKS]


def _pick_short_cfg(feats, codes, names, as_of, cfg):
    """短线选股；min_chg/shadow_max 可参数化"""
    ts = pd.Timestamp(as_of)
    min_chg = cfg.get("min_chg", SHORTLINE_MIN_CHG)
    shadow_max = cfg.get("shadow_max", 0.03)
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
            if not (chg_pct >= min_chg):
                continue
            limit = 19.8 if code.startswith("30") else 9.8
            if chg_pct >= limit:
                continue
            if not (r["vol_ratio"] >= 1.5):
                continue
            if not (r["pos60"] < 0.70):
                continue
            up = (float(r["high"]) - float(r["close"])) / float(r["close"])
            if up > shadow_max:
                continue
        except Exception:
            continue
        out.append({"代码": code, "名称": names.get(code, code), "收盘": float(r["close"])})
    out.sort(key=lambda x: -x["收盘"])
    return out[:SHORTLINE_MAX_PICKS]


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
                tp1 = cfg.get("tp1", TREND_TAKE_PROFIT_1)
                pnl, days, why = _simulate_v2(
                    f, ei, entry, TREND_STOP_LOSS, tp1,
                    TREND_MAX_HOLD, time_stop_days=0, use_ma20_struct=True)
                tr.append({"收益%": round(pnl, 2), "持有天": days, "了结原因": why})
        results[label] = tr

    def _short_run(cfg, label):
        tr = []
        for i in range(1, len(cal)):
            d_prev, d_now = cal[i - 1], cal[i]
            for p in _pick_short_cfg(feats, codes, names, d_prev, cfg):
                f = feats[p["代码"]]
                idx = f.index[f["date"] == d_now]
                if len(idx) == 0:
                    continue
                ei = idx[0]
                entry = float(f.iloc[ei]["open"])
                if entry <= 0:
                    continue
                tp1 = cfg.get("tp1", SHORT_TAKE_PROFIT_1)
                pnl, days, why = _simulate_v2(
                    f, ei, entry, SHORT_STOP_LOSS, tp1,
                    SHORT_HOLD_DAYS_MAX, SHORT_TIME_STOP_DAYS)
                tr.append({"收益%": round(pnl, 2), "持有天": days, "了结原因": why})
        results[label] = tr

    # ===== 趋势（生产基线 pos30-70） =====
    _trend_run({"pos_range": (0.30, 0.70)}, "TR0_生产基线")
    _trend_run({"pos_range": (0.30, 0.70), "temp_th": 0.60}, "TR1_位置+温度60")
    _trend_run({"pos_range": (0.30, 0.70), "dif_pos": True}, "TR2_位置+MACD零轴")
    _trend_run({"pos_range": (0.30, 0.70), "temp_th": 0.60, "dif_pos": True}, "TR3_三叠加")
    _trend_run({"pos_range": (0.30, 0.70), "tp1": 0.08}, "TR4_止盈1=8%")
    _trend_run({"pos_range": (0.30, 0.70), "tp1": 0.05}, "TR5_止盈1=5%")

    # ===== 短线（生产基线 shadow_max 3%） =====
    _short_run({"shadow_max": 0.03}, "S0_生产基线")
    _short_run({"shadow_max": 0.03, "min_chg": 8.5}, "S1_上影+近涨停8.5")
    _short_run({"shadow_max": 0.02}, "S2_上影2%")
    _short_run({"shadow_max": 0.04}, "S3_上影4%")
    _short_run({"shadow_max": 0.03, "min_chg": 8.0}, "S4_近涨停8.0")
    _short_run({"shadow_max": 0.03, "tp1": 0.04}, "S5_止盈1=4%")
    _short_run({"shadow_max": 0.03, "tp1": 0.06}, "S6_止盈1=6%")

    print("\n" + "=" * 100)
    print(f"{'变体':<18}{'笔数':>6}{'胜率%':>8}{'均收%':>8}{'盈亏比':>8}{'胜×盈亏':>9}{'累计%':>9}{'回撤%':>8}")
    print("-" * 100)
    rows = []
    for label in sorted(results.keys()):
        tr = results[label]
        if not tr:
            print(f"{label:<18}{'无交易':>6}")
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
        print(f"{label:<18}{n:>6}{wr:>8.1f}{avg:>8.2f}{pl:>8.2f}{wr*pl:>9.2f}{cum:>9.1f}{mdd:>8.1f}")
        rows.append({"变体": label, "笔数": n, "胜率%": round(wr, 1), "均收%": round(avg, 2),
                     "盈亏比": round(pl, 2), "胜×盈亏": round(wr * pl, 2),
                     "累计%": round(cum, 1), "回撤%": round(mdd, 1)})
    print("=" * 100)
    out = Path(__file__).resolve().parent.parent / "data"
    pd.DataFrame(rows).to_csv(out / "回测_第二轮组合优化.csv", index=False, encoding="utf-8")
    print("已写入 data/回测_第二轮组合优化.csv")
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
