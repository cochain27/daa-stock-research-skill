# -*- coding: utf-8 -*-
"""
低位池前兆信号验证（转化率 + 7日前向收益 双口径）
================================================
在 analyze_transform 研究出的前兆特征基础上，把这些特征固化成过滤条件，
用两个口径验证是否真能提高"低位池选到真启动"的概率：
  1) 转化率：低位池信号后 20 个交易日内进入右侧趋势池的比例
  2) 7日前向收益：按 low_pos_backtest 口径（7日持有 / -6%止损 / +8%止盈）

只报 n≥30 的组合，避免小样本过拟合（2026-09-09 组合叠加 16 笔 68.8% 的教训）。
"""
import sys, os, time, argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import config as C
from analyze_transform import _load_hist, _features, is_trend_signal, is_low_pool, extract_precursor
from fetch_data import get_market_snapshot, _industry_by_name


def forward_ret(f, idx, hold=7, stop=0.94, tp=1.08):
    """7日前向收益，与 low_pos_backtest 一致：止损-6% / 止盈+8% / 持有到期"""
    win = f.iloc[idx + 1: idx + 1 + hold]
    if len(win) < 2:
        return None
    buy = f["close"].iloc[idx]
    if win["low"].min() / buy <= stop:
        return (stop - 1) * 100
    if win["high"].max() / buy >= tp:
        return (tp - 1) * 100
    return (win["close"].iloc[-1] / buy - 1) * 100


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-03-01")
    ap.add_argument("--end", default="2026-09-08")
    ap.add_argument("--top", type=int, default=1200)
    ap.add_argument("--workers", type=int, default=10)
    args = ap.parse_args()

    # ===== 数据 =====
    print("[候选] 拉取全A快照 ...", flush=True)
    snap = get_market_snapshot()
    snap = snap.copy()
    snap["代码"] = snap["代码"].astype(str).str.replace(r"\.0$", "", regex=True)
    snap = snap[snap["代码"].str.match(r"^(?:" + "|".join(C.ALLOW_CODE_PREFIX) + ")", na=False)]
    snap = snap[~snap["名称"].astype(str).str.contains("ST|退", na=False)]
    snap["_amt"] = pd.to_numeric(snap.get("成交额"), errors="coerce").fillna(0)
    snap = snap.sort_values("_amt", ascending=False).head(args.top)
    codes = snap["代码"].tolist()
    names = dict(zip(snap["代码"], snap["名称"].astype(str)))
    print(f"[候选] {len(codes)} 只，并发拉历史K线 ...", flush=True)

    hists, t0 = {}, time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_load_hist, c): c for c in codes}
        for i, f in enumerate(as_completed(futs), 1):
            c, h = f.result()
            if h is not None:
                hists[c] = h
            if i % 300 == 0:
                print(f"  进度 {i}/{len(codes)} ok={len(hists)} {time.time()-t0:.0f}s", flush=True)
    print(f"[候选] 历史K线就绪 {len(hists)} 只，用时 {time.time()-t0:.0f}s\n", flush=True)
    feats = {c: _features(h) for c, h in hists.items()}

    day_sets = [set(h["date"]) for c, h in list(hists.items())[:50]]
    common_days = sorted(set.intersection(*day_sets))
    ts_start, ts_end = pd.Timestamp(args.start), pd.Timestamp(args.end)
    days = [d for d in common_days if ts_start <= d <= ts_end]
    print(f"[日期] {len(days)} 个交易日\n", flush=True)

    def _idx_of(f, dd):
        sub = f[f["date"] <= dd]
        if len(sub) < 70 or sub["date"].iloc[-1] != dd:
            return None
        return len(sub) - 1

    # ===== 逐日扫描低位池信号，标定转化结果 =====
    print("[扫描] 逐日低位池信号 + 转化标定 ...", flush=True)
    rows = []
    for di, d in enumerate(days):
        for code in codes:
            f = feats.get(code)
            if f is None:
                continue
            idx = _idx_of(f, d)
            if idx is None or not is_low_pool(f, idx):
                continue
            prec = extract_precursor(f, idx)
            if prec is None:
                continue
            # 成功 = 未来20个交易日内进入趋势池
            entered = False
            for dd in days[di + 1: di + 1 + 20]:
                j = _idx_of(f, dd)
                if j is not None and is_trend_signal(f, j):
                    entered = True
                    break
            fwd = forward_ret(f, idx)
            rows.append({
                "日期": str(d.date()), "代码": code,
                "行业": _industry_by_name(names.get(code, "")) or "",
                "成功": int(entered), "7日收益": fwd,
                **prec,
            })
        if di % 20 == 0:
            print(f"  扫描 {di+1}/{len(days)} 已标定 {len(rows)}", flush=True)

    df = pd.DataFrame(rows)
    df.to_csv(ROOT / "data/转化研究_全量信号.csv", index=False, encoding="utf-8-sig")
    print(f"\n[数据] 低位池信号共 {len(df)} 条，其中 {df['成功'].sum()} 条成功转化\n", flush=True)

    # ===== 基线 =====
    def _stats(sub):
        n = len(sub)
        conv = sub["成功"].mean() * 100
        r = sub["7日收益"].dropna()
        wr = (r > 0).mean() * 100 if len(r) else float("nan")
        avg = r.mean() if len(r) else float("nan")
        med = r.median() if len(r) else float("nan")
        return n, conv, wr, avg, med

    base = _stats(df)
    print(f"{'='*96}")
    print(f"  基线（全部低位池信号）")
    print(f"{'='*96}")
    print(f"  n={base[0]}  转化率{base[1]:.1f}%  7日胜率{base[2]:.1f}%  7日均收{base[3]:+.2f}%  中位{base[4]:+.2f}%\n")

    # ===== 候选过滤条件 =====
    FILTERS = {
        "A1 位置25-40%":        lambda r: 0.25 <= r["pos60_prev"] < 0.40,
        "A2 位置30-40%":        lambda r: 0.30 <= r["pos60_prev"] < 0.40,
        "B1 站上MA20≥8/20天":   lambda r: r["close_above_ma20_days"] >= 8,
        "B2 蓄势振幅<15%":       lambda r: r["amp20_prev"] < 0.15,
        "B3 波动率<4%":          lambda r: r["std20_prev"] < 0.04,
        "C1 5日涨幅<3.5%":       lambda r: r["chg5_prev"] < 3.5,
        "C2 距20日高>-6%":       lambda r: r["dist_close_max20"] > -6,
        "C3 距60日高>-22%":      lambda r: r["dist_close_max60"] > -22,
        "D1 蓄势量比峰值≥1.8":  lambda r: r["vol_ratio_max20"] >= 1.8,
        "F1 MACD柱温和(<30)":   lambda r: r["dif_dea_prev"] < 30,
        "E1 成交额3-15亿":      lambda r: 3e8 <= r["amount_mean"] <= 15e8,
    }
    COMBOS = {
        "核心组合(A2+B1+C1+C2+C3)":
            ["A2 位置30-40%", "B1 站上MA20≥8/20天", "C1 5日涨幅<3.5%", "C2 距20日高>-6%", "C3 距60日高>-22%"],
        "完整组合(核+B2+D1+F1)":
            ["A2 位置30-40%", "B1 站上MA20≥8/20天", "B2 蓄势振幅<15%", "C1 5日涨幅<3.5%",
             "C2 距20日高>-6%", "C3 距60日高>-22%", "D1 蓄势量比峰值≥1.8", "F1 MACD柱温和(<30)"],
    }

    print(f"{'='*96}")
    print(f"  单因子过滤效果（对比基线 n={base[0]} 转化{base[1]:.1f}% 7日胜{base[2]:.1f}% 均收{base[3]:+.2f}%）")
    print(f"{'='*96}")
    print(f"{'过滤条件':<24}{'n':>6}{'转化率':>9}{'转化提升':>9}{'7日胜率':>9}{'7日均收':>9}{'中位':>8}")
    print("-" * 96)
    summary = []
    for name, fn in FILTERS.items():
        m = df[df.apply(fn, axis=1)]
        if len(m) < 30:
            continue
        n, conv, wr, avg, med = _stats(m)
        lift = conv - base[1]
        summary.append({"条件": name, "n": n, "转化率%": round(conv, 1),
                        "转化提升pt": round(lift, 1), "7日胜率%": round(wr, 1),
                        "7日均收%": round(avg, 2), "中位%": round(med, 2)})
        print(f"{name:<24}{n:>6}{conv:>8.1f}%{lift:>+8.1f}{wr:>8.1f}%{avg:>+8.2f}%{med:>+7.2f}%")
    summary.sort(key=lambda x: x["转化提升pt"], reverse=True)

    print(f"\n{'='*96}")
    print(f"  组合过滤效果")
    print(f"{'='*96}")
    print(f"{'组合':<30}{'n':>6}{'转化率':>9}{'转化提升':>9}{'7日胜率':>9}{'7日均收':>9}{'中位':>8}")
    print("-" * 96)
    for name, conds in COMBOS.items():
        m = df
        for cn in conds:
            m = m[m.apply(FILTERS[cn], axis=1)]
        n, conv, wr, avg, med = _stats(m)
        lift = conv - base[1]
        summary.append({"条件": name, "n": n, "转化率%": round(conv, 1),
                        "转化提升pt": round(lift, 1), "7日胜率%": round(wr, 1),
                        "7日均收%": round(avg, 2), "中位%": round(med, 2)})
        print(f"{name:<30}{n:>6}{conv:>8.1f}%{lift:>+8.1f}{wr:>8.1f}%{avg:>+8.2f}%{med:>+7.2f}%")

    out = pd.DataFrame(summary)
    out.to_csv(ROOT / "data/转化研究_过滤验证.csv", index=False, encoding="utf-8-sig")
    print(f"\n[保存] data/转化研究_过滤验证.csv / 转化研究_全量信号.csv")


if __name__ == "__main__":
    main()
