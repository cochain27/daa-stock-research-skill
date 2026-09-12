# -*- coding: utf-8 -*-
"""两通道「成功 vs 失败」量价因子区分度体检。

对 backtest_engine.py 导出的带因子快照的回测明细 CSV，做：
  1. 成功组（收益>0）vs 失败组（收益<=0）各因子的 Welch t 检验 + Mann-Whitney U 检验
  2. 单因子区分胜负的 AUC（手写，0.5=随机/1.0=完美）
  3. 因子分桶收益表（看单调性）

仅依赖 pandas/numpy，不装 scipy/sklearn。

用法：
  python factor_discrimination.py <回测明细.csv> [--label 标签]
"""
import argparse
import math
import sys
from pathlib import Path

# WorkBuddy shim 劫持 sys.path，需抢在 import numpy/pandas 前修复 managed venv 路径
_venv_pkg = "/Users/chenyuting/.workbuddy/binaries/python/envs/default/lib/python3.13/site-packages"
if _venv_pkg not in sys.path:
    sys.path.insert(0, _venv_pkg)

import numpy as np
import pandas as pd

# 因子中文名（用于可读输出）
FACTOR_CN = {
    "pos60": "60日位置",
    "vol_ratio": "量比",
    "chg_pct": "当日涨幅%",
    "amp20": "20日振幅",
    "std20": "20日波动率",
    "amount": "成交额(亿)",
    "rsi14": "RSI14",
    "dif": "MACD-DIF",
    "dea": "MACD-DEA",
    "up_shadow": "上影线比例",
}


def _norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def welch_t(a, b):
    a = np.asarray(a, float)
    b = np.asarray(b, float)
    na, nb = len(a), len(b)
    va, vb = a.var(ddof=1), b.var(ddof=1)
    se = math.sqrt(va / na + vb / nb)
    if se == 0:
        return 0.0, 1.0
    t = (a.mean() - b.mean()) / se
    df_ = (va / na + vb / nb) ** 2 / ((va / na) ** 2 / (na - 1) + (vb / nb) ** 2 / (nb - 1))
    p = 2 * (1 - _norm_cdf(abs(t)))
    return t, p


def mannwhitney(a, b):
    a = np.asarray(a, float)
    b = np.asarray(b, float)
    allv = np.concatenate([a, b])
    ranks = pd.Series(allv).rank().values
    ra = ranks[: len(a)].sum()
    U = ra - len(a) * (len(a) + 1) / 2
    n1, n2 = len(a), len(b)
    mu = n1 * n2 / 2
    sig = math.sqrt(n1 * n2 * (n1 + n2 + 1) / 12)
    if sig == 0:
        return U, 1.0
    z = (U - mu) / sig
    p = 2 * (1 - _norm_cdf(abs(z)))
    return U, p


def auc_score(y, x, reverse=False):
    y = np.asarray(y)
    x = np.asarray(x, float)
    if reverse:
        x = -x
    order = np.argsort(x)
    y = y[order]
    n_pos = (y == 1).sum()
    n_neg = (y == 0).sum()
    if n_pos == 0 or n_neg == 0:
        return 0.5
    ranks_pos = np.where(y == 1)[0] + 1
    U = ranks_pos.sum() - n_pos * (n_pos + 1) / 2
    return U / (n_pos * n_neg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--label", default=None)
    args = ap.parse_args()

    df = pd.read_csv(args.csv)
    label = args.label or Path(args.csv).stem
    if "收益%" not in df.columns:
        print("缺「收益%」列，无法分组")
        return 1
    df["收益%"] = pd.to_numeric(df["收益%"], errors="coerce")
    df = df.dropna(subset=["收益%"])
    # 成交额换算为亿元，便于阅读
    if "amount" in df.columns:
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce") / 1e8

    win = df[df["收益%"] > 0]
    lose = df[df["收益%"] <= 0]

    print(f"\n{'=' * 64}")
    print(f"体位区分度体检：{label}")
    print(f"{'=' * 64}")
    print(f"总样本 {len(df)} 笔 | 成功组(收益>0) {len(win)} 笔 | 失败组(收益<=0) {len(lose)} 笔")
    print(f"整体：胜率 {len(win)/len(df)*100:.1f}% | 平均收益 {df['收益%'].mean():+.2f}%")
    print()

    # 只检查实际存在且有样本的因子列
    factor_cols = [c for c in FACTOR_CN if c in df.columns and df[c].notna().sum() > 5]

    print(f"{'因子':<10}{'成功均值':>9}{'失败均值':>9}{'t值':>7}{'p值':>7}  {'显著性':<6} {'AUC':>6}")
    print("-" * 78)
    rows = []
    for c in factor_cols:
        w = win[c].dropna()
        l = lose[c].dropna()
        if len(w) < 3 or len(l) < 3:
            continue
        t, p = welch_t(w, l)
        sig = "显著" if p < 0.05 else "不显著"
        # AUC：收益>0 为 1
        y = (df["收益%"] > 0).astype(int).values
        x = df[c].fillna(df[c].median()).values
        a1 = auc_score(y, x)
        a2 = auc_score(y, x, reverse=True)
        auc = max(a1, a2)
        rows.append((c, w.mean(), l.mean(), t, p, sig, auc))
        print(f"{FACTOR_CN.get(c,c):<10}{w.mean():>9.3f}{l.mean():>9.3f}{t:>7.2f}{p:>7.3f}  {sig:<6} {auc:>6.3f}")

    print()
    print("注：AUC≈0.5 表示该因子几乎无区分力（等于抛硬币）；")
    print("    p<0.05 才认为两组在该因子上有统计显著差异。")

    # 对有区分度潜力的因子做分桶收益表（按四分位）
    ranked = sorted(rows, key=lambda r: -r[6])  # 按 AUC 降序（rows: c, wmean, lmean, t, p, sig, auc）
    if ranked:
        print(f"\n{'=' * 64}")
        print("区分力最高的 3 个因子的分桶收益表")
        print("（观察收益是否随因子单调变化——单调才是有用的 alpha）")
        print(f"{'=' * 64}")
        for c, _, _, _, _, _, auc in ranked[:3]:
            col = df[c].dropna()
            if len(col) < 10:
                continue
            qs = pd.qcut(col, q=3, duplicates="drop")
            tmp = df.assign(bucket=pd.qcut(df[c], q=3, duplicates="drop"))
            g = tmp.groupby("bucket", observed=True).agg(
                样本=("收益%", "size"),
                胜率=("收益%", lambda s: (s > 0).mean() * 100),
                平均收益=("收益%", "mean"),
            ).round(1)
            print(f"\n【{FACTOR_CN.get(c, c)}】{auc:.3f}")
            print(g.to_string())

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())