# -*- coding: utf-8 -*-
"""验证短线策略「当日近涨停剔除」是否该保留/改掉（2026-09-10）

回答三个问题：
Q1 现状（只剔封死≥9.8/19.8%，保留5%~涨停冲板未封）的短线胜率分布：
   按入选当日涨幅分桶 [5-6.9, 7-8.4, 8.5-9.7]，看哪一段最赚钱/最亏。
Q2 如果真给短线加回「近涨停剔除」（剔当日涨幅≥9.0% 的票），胜率/盈亏比怎么变？
Q3 如果反过来只保留「近涨停梯队」（涨幅 ≥8.5%），胜率/盈亏比怎么变？
"""
import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

from config import (ALLOW_CODE_PREFIX, TREND_MIN_AMOUNT, SHORTLINE_MIN_CHG,
                    SHORT_STOP_LOSS, SHORT_TAKE_PROFIT_1,
                    SHORT_HOLD_DAYS_MAX, SHORT_TIME_STOP_DAYS, SHORTLINE_MAX_PICKS)
from backtest_engine import _load_hist, _features, _simulate_v2
from fetch_data import get_market_snapshot


def _pick_short(feats, codes, names, as_of, near_zt="keep"):
    """短线选股。near_zt:
      keep/remove_only_keep  -> 现状：只剔封死(9.8/19.8)，保留冲板未封
      ban   -> 加回近涨停剔除：剔当日涨幅≥9.0%（近似现价/涨停价≥0.95，主板）
      only  -> 只留近涨停梯队：涨幅≥8.5% 且未封死
    """
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
            if chg_pct >= limit:          # 封死，T+1 买不进
                continue
            if near_zt == "ban" and chg_pct >= 9.0:   # 加回「当日近涨停剔除」
                continue
            if near_zt == "main_ban" and not code.startswith("30") and chg_pct >= 8.5:
                continue  # 只剔主板近涨停（创业20cm 保留）
            if near_zt == "only" and chg_pct < 8.5:   # 只留近涨停梯队
                continue
            if not (float(r["vol_ratio"]) >= 1.5):
                continue
            if not (r["pos60"] < 0.70):
                continue
            # 上影过滤（与线上一致：冲高回落>3% 剔除）
            up = (float(r["high"]) - float(r["close"])) / float(r["close"])
            if up > 0.03:
                continue
        except Exception:
            continue
        out.append({"代码": code, "名称": names.get(code, code),
                    "收盘": float(r["close"]), "幅%": round(chg_pct, 2)})
    out.sort(key=lambda x: -x["收盘"])
    return out[:SHORTLINE_MAX_PICKS]


def _sim(tr, label, detail=None):
    """汇总统计；detail 收到明细行用于分桶"""
    n = len(tr)
    if n == 0:
        return {"变体": label, "笔数": 0}
    df = pd.DataFrame(tr)
    win = df[df["收益%"] > 0]
    loss = df[df["收益%"] <= 0]
    wr = len(win) / n * 100
    avg = df["收益%"].mean()
    avg_w = win["收益%"].mean() if len(win) else 0
    avg_l = loss["收益%"].mean() if len(loss) else 0
    pl = abs(avg_w / avg_l) if avg_l else 0
    return {"变体": label, "笔数": n, "胜率%": round(wr, 1), "均收%": round(avg, 2),
            "盈亏比": round(pl, 2), "胜×盈亏": round(wr * pl, 2)}


def run(feats, codes, names, cal, start, end):
    results = {}

    def _run(near_zt, label):
        tr, detail = [], []
        for i in range(1, len(cal)):
            d_prev, d_now = cal[i - 1], cal[i]
            for p in _pick_short(feats, codes, names, d_prev, near_zt):
                f = feats[p["代码"]]
                idx = f.index[f["date"] == d_now]
                if len(idx) == 0:
                    continue
                ei = idx[0]
                entry = float(f.iloc[ei]["open"])
                if entry <= 0:
                    continue
                pnl, days, why = _simulate_v2(
                    f, ei, entry, SHORT_STOP_LOSS, SHORT_TAKE_PROFIT_1,
                    SHORT_HOLD_DAYS_MAX, SHORT_TIME_STOP_DAYS)
                tr.append({"收益%": round(pnl, 2), "持有天": days, "了结原因": why})
                detail.append({**p, "收益%": round(pnl, 2), "持有天": days, "了结原因": why})
        results[label] = (_sim(tr, label), detail)

    print("回放中 ...", flush=True)
    _run("keep", "S_现状(只剔封死)")
    _run("ban", "S_加回近涨停剔除(≥9%)")
    _run("only", "S_只留近涨停梯队(≥8.5%)")
    _run("main_ban", "S_只剔主板近涨停(≥8.5%)")

    # 汇总表
    print("\n" + "=" * 78)
    print(f"{'变体':<24}{'笔数':>6}{'胜率%':>8}{'均收%':>8}{'盈亏比':>8}{'胜×盈亏':>9}")
    print("-" * 78)
    rows = []
    for label, (row, _d) in results.items():
        if row["笔数"] == 0:
            print(f"{label:<24}{'无交易':>6}")
            continue
        print(f"{label:<24}{row['笔数']:>6}{row['胜率%']:>8}{row['均收%']:>8}"
              f"{row['盈亏比']:>8}{row['胜×盈亏']:>9}")
        rows.append(row)
    print("=" * 78)

    # 分桶明细：现状变体按入选当日涨幅分桶
    print("\n现状 S_keep 按入选当日涨幅分桶（回答：近涨停梯队是不是利润来源）:")
    print(f"{'涨幅桶':<16}{'笔数':>6}{'胜率%':>8}{'均收%':>8}{'盈亏比':>8}")
    print("-" * 60)
    df = pd.DataFrame(results["S_现状(只剔封死)"][1])
    if not df.empty:
        df["桶"] = pd.cut(df["幅%"], bins=[4.99, 6.9, 8.4, 9.7, 20],
                          labels=["5.0-6.9", "7.0-8.4", "8.5-9.7(近涨停)", "创业近涨停"])
        for b, g in df.groupby("桶", observed=True):
            w = g[g["收益%"] > 0]
            l = g[g["收益%"] <= 0]
            wr = len(w) / len(g) * 100
            avg = g["收益%"].mean()
            pl = abs(w["收益%"].mean() / l["收益%"].mean()) if len(l) and l["收益%"].mean() else 0
            print(f"{str(b):<16}{len(g):>6}{wr:>8.1f}{avg:>8.2f}{pl:>8.2f}")
            # 导出明细
    out = pd.DataFrame(rows)
    data_dir = Path(__file__).resolve().parent.parent / "data"
    out.to_csv(data_dir / "回测_短线近涨停验证.csv", index=False, encoding="utf-8")
    df.to_csv(data_dir / "回测_短线近涨停明细.csv", index=False, encoding="utf-8")
    print(f"\n已写入 {data_dir}")
    print("注：不计手续费/滑点；研究参考，不构成投资建议。")


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