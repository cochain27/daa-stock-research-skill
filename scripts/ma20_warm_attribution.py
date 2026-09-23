#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""初动池「站上MA20」过滤贡献归因（只读，不改 warm_start_backtest / warm_start_entry 任何代码）。

方法：用 warm_start_backtest 完全相同的信号定义（量比1.3-2.5 + 5日+3~8% + 位置<80% + MACD多头
      + 当日≤9.5% + 振幅≤7.5% + 成交额≤16亿），对比两版：
      A. 含 MA20 过滤（现状 = 生产口径）
      B. 去掉 MA20 过滤（老策略启发：观察 MA20 是否真在区分收益）
      并额外把 B 版按「是否站上MA20」拆层，验证老策略发现（未站上=负贡献）在初动池画像下是否成立。
入场：次日开盘买入，-8% 硬止损，T+5 收盘出场（与 warm_start_backtest 完全一致）。
输出：控制台 + data/ma20_warm_attribution.csv
"""
import sys
from pathlib import Path
from glob import glob

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import numpy as np

import warm_start_backtest as wsb

BASE = wsb.BASE


def _mask_no_ma20(df):
    """warm_start_backtest._signal_mask 去掉「last > ma20」的版本。"""
    m = (
        df["lb"].between(wsb.LB_LO, wsb.LB_HI)
        & df["chg5"].between(wsb.CHG5_LO, wsb.CHG5_HI)
        & (df["pos60"] < wsb.POS60_MAX)
        & (df["dif"] > df["dea"])
        & (df["涨跌幅"] <= wsb.CHG_TODAY_MAX)
        & (df["涨跌幅"] > -9.5)
        & df["ma20"].notna() & df["lb"].notna()
    )
    if wsb.AMP_TODAY_MAX:
        m &= df["振幅"].notna() & (df["振幅"] <= wsb.AMP_TODAY_MAX)
    return m


def _collect_no_ma20(df):
    m = _mask_no_ma20(df)
    idx = df.index[m].tolist()
    out = []
    last_i = -99
    for i in idx:
        if i - last_i < 10:
            continue
        last_i = i
        out.append(i)
    return out


def main():
    files = glob(str(BASE / "*.csv"))
    codes = sorted(Path(f).stem for f in files)
    print(f"扫描 {len(codes)} 只票 ...", flush=True)

    # 生产版（含MA20）直接调 wsb 收集；对照版用 _collect_no_ma20
    rows = []
    for code in codes:
        df = wsb._load(code)
        if df is None:
            continue
        # 生产版信号（现状）
        for i in wsb._collect_signals(df, code):
            r = wsb._trade_result(df, i)
            row = df.iloc[i]
            rows.append({
                "版本": "含MA20(现状)", "code": code,
                "date": str(pd.Timestamp(row["日期"]).date()),
                "站上MA20": True,
                "MA20乖离%": round((row["last"] / row["ma20"] - 1) * 100, 2),
                "pos60": round(row["pos60"], 3),
                "lb": round(row["lb"], 2), "chg5": round(row["chg5"], 1),
                "amt亿": round(row["amount"] / 1e8, 1),
                "r5": None if r is None else round(r["r5"], 2),
                "r5_stop": None if r is None else round(r["r5_stop"], 2),
                "stop_hit": None if r is None else r["stop_hit"],
            })
        # 无MA20版信号
        for i in _collect_no_ma20(df):
            r = wsb._trade_result(df, i)
            row = df.iloc[i]
            rows.append({
                "版本": "无MA20(对照)", "code": code,
                "date": str(pd.Timestamp(row["日期"]).date()),
                "站上MA20": bool(row["last"] > row["ma20"]),
                "MA20乖离%": round((row["last"] / row["ma20"] - 1) * 100, 2),
                "pos60": round(row["pos60"], 3),
                "lb": round(row["lb"], 2), "chg5": round(row["chg5"], 1),
                "amt亿": round(row["amount"] / 1e8, 1),
                "r5": None if r is None else round(r["r5"], 2),
                "r5_stop": None if r is None else round(r["r5_stop"], 2),
                "stop_hit": None if r is None else r["stop_hit"],
            })

    df = pd.DataFrame(rows)
    out = Path(__file__).resolve().parent.parent / "data" / "ma20_warm_attribution.csv"
    df.to_csv(out, index=False, encoding="utf-8-sig")

    print("\n========== 初动池 MA20 过滤归因（T+5 止损版 = 生产口径）==========\n")
    for ver, g in df.groupby("版本"):
        rr = g.dropna(subset=["r5_stop"])
        if rr.empty:
            continue
        s = rr["r5_stop"]
        wins = s[s > 0]; losses = s[s < 0]
        pl = wins.mean() / abs(losses.mean()) if len(wins) and len(losses) else None
        print(f"[{ver}] 信号{len(g)} 可执行{len(rr)}")
        print(f"  T5止损版均值 {s.mean():+.2f}% | 胜率 {(s>0).mean()*100:.1f}% | "
              f"盈亏比 {pl if pl is None else round(pl,2)} | 止损触发率 {(rr['stop_hit'].mean())*100:.1f}%")
        print()

    # 无MA20版的内部拆层：站上 vs 未站上
    print("========== 无MA20(对照)版内部拆层 ==========")
    base = df[df["版本"] == "无MA20(对照)"].dropna(subset=["r5_stop"])
    for v, lab in [(True, "站上MA20"), (False, "未站上MA20")]:
        sub = base[base["站上MA20"] == v]
        if sub.empty:
            continue
        s = sub["r5_stop"]
        wins = s[s > 0]; losses = s[s < 0]
        pl = wins.mean() / abs(losses.mean()) if len(wins) and len(losses) else None
        print(f"[{lab}] n={len(sub)} T5均值 {s.mean():+.2f}% | 胜率 {(s>0).mean()*100:.1f}% | "
              f"盈亏比 {pl if pl is None else round(pl,2)} | 止损率 {(sub['stop_hit'].mean())*100:.1f}%")
    # 乖离分桶（站上的强度）
    print("\n========== 无MA20(对照)版 · MA20乖离分桶 ==========")
    base2 = base.copy()
    bins = [-1e9, -5, 0, 3, 10, 1e9]
    labels = ["<-5%", "-5~0%", "0~3%", "3~10%", ">10%"]
    base2["乖离桶"] = pd.cut(base2["MA20乖离%"], bins=bins, labels=labels, right=False)
    for lab, g in base2.groupby("乖离桶", observed=True):
        s = g["r5_stop"]
        wins = s[s > 0]; losses = s[s < 0]
        pl = wins.mean() / abs(losses.mean()) if len(wins) and len(losses) else None
        print(f"[{lab}] n={len(g)} T5均值 {s.mean():+.2f}% | 胜率 {(s>0).mean()*100:.1f}% | "
              f"盈亏比 {pl if pl is None else round(pl,2)}")
    print(f"\n明细已存 {out}（{len(df)} 条）")


if __name__ == "__main__":
    main()
