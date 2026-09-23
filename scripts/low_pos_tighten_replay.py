# -*- coding: utf-8 -*-
"""低位池收紧后回放验证（2026-09-21）。

目标：确认新参数下「每日入池量」回到历史量级（3-5 只/天），并观察近 15 交易日曲线。
方法：本地 klines 全历史复现 pick_low_pos_entry 的扫描逻辑（含标准蓄势+超卖路径），
      套用新收紧参数 + 行业去重(MAX_SAME_INDUSTRY=1) + 池内在册去重(filter_active_pool
      口径模拟：某代码某日首次入池后才算在册，其后 T+5 内不再重刷)，
      输出每日入池量。行业用名称关键词兜底(_industry_by_name)，无需联网。
"""
import glob
import os
import sys
from collections import defaultdict

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import low_pos_entry as lpe  # noqa: E402
from fetch_data import _industry_by_name  # noqa: E402
from low_pos_backtest_full import _local_hist  # noqa: E402
from stock_screener import MAX_SAME_INDUSTRY  # noqa: E402

LOOKBACK = lpe.LOW_POS_ENTRY_RECENT_OVERSOLD_LOOKBACK
OS_POS60_MAX = lpe.LOW_POS_ENTRY_RECENT_OVERSOLD_POS60_MAX
OS_AMT = lpe.LOW_POS_ENTRY_OS_SCAN_AMOUNT_RANGE
OS_POS_SCAN = lpe.LOW_POS_ENTRY_OS_SCAN_POS60_MAX
OS_AGE_MIN = lpe.LOW_POS_ENTRY_OS_SCAN_OS_AGE_MIN
OS_LB_MAX = lpe.LOW_POS_ENTRY_OS_SCAN_LB_MAX
OS_LBM_MAX = lpe.LOW_POS_ENTRY_OS_SCAN_LB5MEAN_MAX
OS_CHG5_MAX = lpe.LOW_POS_ENTRY_OS_SCAN_CHG5_MAX
MAX_CHG_TODAY = lpe.LOW_POS_ENTRY_MAX_CHG_TODAY


def _oversold_mask(ind):
    p60 = pd.to_numeric(ind["pos60"], errors="coerce").to_numpy(dtype=float)
    d60 = pd.to_numeric(ind["dist60"], errors="coerce").to_numpy(dtype=float)
    with np.errstate(invalid="ignore"):
        return (p60 < 0.5) | (d60 < lpe.LOW_POS_ENTRY_RECENT_OVERSOLD_DIST_MAX)


def _std_pass_vec(ind):
    def num(col):
        return pd.to_numeric(ind[col], errors="coerce").to_numpy(dtype=float)
    p60, d60 = num("pos60"), num("dist60")
    lb, lbm = num("lb"), num("lb5mean")
    c5, a20 = num("chg5"), num("amp20")
    amt, chgt = num("成交额"), num("涨跌幅")
    lo, hi = lpe.LOW_POS_ENTRY_CHG5_RANGE
    with np.errstate(invalid="ignore"):
        ok = ((p60 < lpe.LOW_POS_ENTRY_MAX_POS60) & (d60 <= lpe.LOW_POS_ENTRY_MIN_DIST60)
              & (lb <= lpe.LOW_POS_ENTRY_MAX_LB5) & (lbm <= lpe.LOW_POS_ENTRY_MAX_LB5_MEAN)
              & (c5 >= lo) & (c5 <= hi) & (chgt <= MAX_CHG_TODAY)
              & (a20 < lpe.LOW_POS_ENTRY_MAX_AMP20)
              & (amt >= lpe.LOW_POS_ENTRY_MIN_AMOUNT) & (amt <= lpe.LOW_POS_ENTRY_MAX_AMOUNT))
    return np.nan_to_num(ok, nan=0).astype(bool)


def main():
    codes = [os.path.basename(p)[:-4][2:] for p in
             sorted(glob.glob(os.path.join(BASE, "data", "klines", "*.csv")))]
    print(f"[回放] 本地宇宙 {len(codes)} 只", flush=True)

    # 每只票逐日：标准蓄势 / 超卖路径（新参数） → 入池候选（未行业去重）
    picks = []   # (日期, 代码, 名称, 路径)
    names = {}
    for k, code in enumerate(codes, 1):
        _, h = _local_hist(code, 700)
        if h is None:
            continue
        ind = lpe._indicators(h)
        n = len(ind)
        if n < 72:
            continue
        osm = _oversold_mask(ind)
        std_ok = _std_pass_vec(ind)
        p60 = pd.to_numeric(ind["pos60"], errors="coerce").to_numpy(dtype=float)
        d60 = pd.to_numeric(ind["dist60"], errors="coerce").to_numpy(dtype=float)
        lb = pd.to_numeric(ind["lb"], errors="coerce").to_numpy(dtype=float)
        lbm = pd.to_numeric(ind["lb5mean"], errors="coerce").to_numpy(dtype=float)
        amt = pd.to_numeric(ind["成交额"], errors="coerce").to_numpy(dtype=float)
        chgt = pd.to_numeric(ind["涨跌幅"], errors="coerce").to_numpy(dtype=float)
        c5 = pd.to_numeric(ind["chg5"], errors="coerce").to_numpy(dtype=float)
        dates = ind["日期"].dt.strftime("%Y-%m-%d").to_numpy()
        nm = None
        for i in range(70, n):
            if std_ok[i]:
                # 标准蓄势路径（维持现状，不收紧）
                picks.append((dates[i], code, "标准蓄势"))
                continue
            if np.isnan(p60[i]) or np.isnan(d60[i]):
                continue
            if chgt[i] > MAX_CHG_TODAY:
                continue
            if p60[i] >= OS_POS60_MAX:
                continue
            # 超卖路径新参数（2026-09-21 定案）
            amt_lo, amt_hi = OS_AMT
            if not (amt_lo <= amt[i] <= amt_hi):
                continue
            if p60[i] >= OS_POS_SCAN:
                continue
            lo_ = max(0, i + 1 - LOOKBACK)
            win = osm[lo_:i + 1]
            if not win.any():
                continue
            os_first = int(np.flatnonzero(win)[0]) + lo_
            if (i - os_first) < OS_AGE_MIN:
                continue
            if not np.isnan(lb[i]) and lb[i] > OS_LB_MAX:
                continue
            if not np.isnan(lbm[i]) and lbm[i] > OS_LBM_MAX:
                continue
            if not np.isnan(c5[i]) and c5[i] > OS_CHG5_MAX:
                continue
            if nm is None:
                nm = _local_hist(code, 700)[1].iloc[-1].get("名称") if False else None
            picks.append((dates[i], code, "近期超卖"))
        if k % 300 == 0:
            print(f"  {k}/{len(codes)} 累计候选 {len(picks)}", flush=True)

    if not picks:
        print("无候选！")
        return
    pf = pd.DataFrame(picks, columns=["日期", "代码", "路径"])

    # 代码→名称（用回测 CSV / watch_history 里的名称映射，无网络）
    name_map = {}
    for src in ["data/watch_history.csv", "data/低位埋伏回测_20260921_0128.csv"]:
        p = os.path.join(BASE, src)
        if not os.path.exists(p):
            continue
        try:
            df = pd.read_csv(p, dtype={"代码": str})
            if "名称" in df.columns:
                for _, r in df.iterrows():
                    c = str(r["代码"]).replace(".0", "").strip()
                    name_map.setdefault(c, str(r["名称"]))
        except Exception:
            pass

    pf["名称"] = pf["代码"].map(name_map).fillna(pf["代码"])
    pf["行业"] = pf["名称"].apply(lambda x: _industry_by_name(str(x)) or "未知")

    # 模拟池内在册去重：某代码在 T 日入池后，其后 T+5 个自然交易日内不再重刷。
    # 用简化口径：同代码在 5 个交易日内只保留首个入池日（filter_active_pool 语义近似）。
    pf = pf.sort_values("日期")
    in_pool_until = {}   # code -> 最后入池日 index
    all_days = sorted(pf["日期"].unique())
    day_idx = {d: i for i, d in enumerate(all_days)}
    result = []
    for _, r in pf.iterrows():
        c = r["代码"]
        cur = day_idx[r["日期"]]
        if in_pool_until.get(c, -1) >= cur:
            continue   # 已在池（观察中）
        result.append(r)
        in_pool_until[c] = cur + 5   # 入池后 T+5 交易日视为仍在册

    rf = pd.DataFrame(result)

    # 行业去重（MAX_SAME_INDUSTRY=1，按日期分组逐日执行，保留位置低优先）
    daily_rows = []
    for d, grp in rf.groupby("日期"):
        # 排序：位置低优先（近似 pick 的 sort）
        ind_cnt = defaultdict(int)
        keep = []
        for _, r in grp.iterrows():
            ind = r["行业"]
            if ind_cnt[ind] >= MAX_SAME_INDUSTRY:
                continue
            ind_cnt[ind] += 1
            keep.append(r)
        daily_rows.append(pd.DataFrame(keep))
    final = pd.concat(daily_rows) if daily_rows else pd.DataFrame(columns=pf.columns)

    cnt = final.groupby("日期").size()
    print("\n== 新参数 + 行业去重 + 池内去重 后 每日入池量 ==")
    print(f"全期均值 {cnt.mean():.1f} 中位 {cnt.median():.0f} 近15交易日均值 {cnt.tail(15).mean():.1f}")
    print(cnt.tail(20).to_string())
    print(f"\n近15交易日明细：")
    for d, n in cnt.tail(15).items():
        sub = final[final["日期"] == d]
        print(f"  {d}: {n} 只 — " + ", ".join(f"{r['名称']}({r['路径']})" for _, r in sub.iterrows()))

    out = os.path.join(BASE, "data", "超卖收紧_回放入池量_20260921.csv")
    final.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"\n明细 → {out}")


if __name__ == "__main__":
    main()
