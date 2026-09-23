# -*- coding: utf-8 -*-
"""低位池「近期超卖」路径参数收紧研究（2026-09-21）。

背景：09-21 单日入池 22 只（19 只超卖路径），历史量级 3-5 只/天。
根因：超卖路径扫描段近乎无过滤（仅 chg_today≤5% + pos60<0.65）——
      没有成交额区间、没有缩量要求、没有距超卖日远近要求；
      弱市里 25 日窗口内 pos60<0.5 或 dist60<-30% 的票遍地都是。
方法：本地 klines 全历史（~700 交易日 × 1092 只）复现「扫描→触发→T5」全链路，
      按 T-1 扫描特征分层 T5 收益，找出「保边际、砍水量」的收紧组合。
      触发口径与 low_pos_entry.backtest() 完全一致（OS 参数组），
      扫描窗口与回测对齐（含蓄势日本身，修复 pick_low_pos_entry 漏掉 T-1 行的不一致）。
输出：data/超卖路径收紧研究_样本明细_20260921.csv + 控制台分层表。
"""
import glob
import os
import sys

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import low_pos_entry as lpe  # noqa: E402
from low_pos_backtest_full import _local_hist  # noqa: E402

LOOKBACK = lpe.LOW_POS_ENTRY_RECENT_OVERSOLD_LOOKBACK      # 25
OS_POS60_MAX = lpe.LOW_POS_ENTRY_RECENT_OVERSOLD_POS60_MAX  # 0.65
STD_POS60_MAX = lpe.LOW_POS_ENTRY_MAX_POS60                 # 0.55
STD_DIST60 = lpe.LOW_POS_ENTRY_MIN_DIST60                   # -25
STD_LB = lpe.LOW_POS_ENTRY_MAX_LB5                          # 1.30
STD_LBM = lpe.LOW_POS_ENTRY_MAX_LB5_MEAN                    # 1.20
CHG5_LO, CHG5_HI = lpe.LOW_POS_ENTRY_CHG5_RANGE             # (-10, 5)
MAX_CHG_TODAY = lpe.LOW_POS_ENTRY_MAX_CHG_TODAY             # 5
STD_AMP = lpe.LOW_POS_ENTRY_MAX_AMP20                       # 30
STD_AMT_LO, STD_AMT_HI = lpe.LOW_POS_ENTRY_MIN_AMOUNT, lpe.LOW_POS_ENTRY_MAX_AMOUNT  # 2e8/15e8
# 触发参数（OS 固化组，与 backtest() 一致）
T_LB_LO, T_LB_HI = lpe.LOW_POS_ENTRY_OS_TRIGGER_LB, lpe.LOW_POS_ENTRY_OS_TRIGGER_LB_MAX
T_CHG_LO, T_CHG_HI = lpe.LOW_POS_ENTRY_OS_TRIGGER_CHG_MIN, lpe.LOW_POS_ENTRY_OS_TRIGGER_CHG_MAX
T_AMT_LO, T_AMT_HI = lpe.LOW_POS_ENTRY_OS_TRIGGER_MIN_AMOUNT, lpe.LOW_POS_ENTRY_OS_TRIGGER_MAX_AMOUNT


def _oversold_mask(ind):
    """pos60<0.5 或 dist60<-30 的逐日布尔（与 _recent_oversold 同口径，向量化提速）。"""
    p60 = ind["pos60"].to_numpy(dtype=float)
    d60 = ind["dist60"].to_numpy(dtype=float)
    with np.errstate(invalid="ignore"):
        return (p60 < 0.5) | (d60 < lpe.LOW_POS_ENTRY_RECENT_OVERSOLD_DIST_MAX)


def _std_pass_vec(ind):
    """标准蓄势五判据的向量化版（与 _蓄势通过 同口径，NaN 一律判否）。"""
    def num(col):
        return pd.to_numeric(ind[col], errors="coerce").to_numpy(dtype=float)
    p60, d60 = num("pos60"), num("dist60")
    lb, lbm = num("lb"), num("lb5mean")
    c5, a20 = num("chg5"), num("amp20")
    amt, chgt = num("成交额"), num("涨跌幅")
    with np.errstate(invalid="ignore"):
        ok = ((p60 < STD_POS60_MAX) & (d60 <= STD_DIST60)
              & (lb <= STD_LB) & (lbm <= STD_LBM)
              & (c5 >= CHG5_LO) & (c5 <= CHG5_HI)
              & (chgt <= MAX_CHG_TODAY)
              & (a20 < STD_AMP)
              & (amt >= STD_AMT_LO) & (amt <= STD_AMT_HI))
    return np.nan_to_num(ok, nan=0).astype(bool)


def main():
    codes = [os.path.basename(p)[:-4][2:] for p in
             sorted(glob.glob(os.path.join(BASE, "data", "klines", "*.csv")))]
    print(f"[研究] 本地宇宙 {len(codes)} 只", flush=True)

    scan_rows = []   # 超卖路径扫描候选（T-1 特征）
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
        a20 = pd.to_numeric(ind["amp20"], errors="coerce").to_numpy(dtype=float)
        dates = ind["日期"].dt.strftime("%Y-%m-%d").to_numpy()
        close = ind["收盘"].to_numpy(dtype=float)
        ma20 = pd.to_numeric(ind["MA20"], errors="coerce").to_numpy(dtype=float)

        for i in range(70, n - 1):
            if std_ok[i]:
                continue                     # 标准蓄势路径，不在本研究范围
            if np.isnan(p60[i]) or np.isnan(d60[i]):
                continue
            if chgt[i] > MAX_CHG_TODAY:      # 候选日已大涨，双路径统一过滤
                continue
            if p60[i] >= OS_POS60_MAX:
                continue
            # 近期超卖判定（窗口含蓄势日 i，与 backtest() 对齐）
            lo = max(0, i + 1 - LOOKBACK)
            win = osm[lo:i + 1]
            if not win.any():
                continue
            os_idx = np.flatnonzero(win) + lo
            os_first, os_last = int(os_idx[0]), int(os_idx[-1])
            scan_rows.append({
                "代码": code, "蓄势日": dates[i], "i": i, "n": n,
                "蓄势日成交额亿": round(amt[i] / 1e8, 2) if not np.isnan(amt[i]) else None,
                "蓄势日量比": round(lb[i], 2) if not np.isnan(lb[i]) else None,
                "蓄势日5日量比均值": round(lbm[i], 2) if not np.isnan(lbm[i]) else None,
                "蓄势位置": round(p60[i], 3), "距高%": round(d60[i], 1),
                "5日涨幅%": round(c5[i], 1) if not np.isnan(c5[i]) else None,
                "20日振幅%": round(a20[i], 1) if not np.isnan(a20[i]) else None,
                "距首次超卖日": i - os_first, "距最近超卖日": i - os_last,
                "修复度": round(p60[i] - p60[os_last], 3),
            })
        if k % 200 == 0:
            print(f"  扫描进度 {k}/{len(codes)} 候选累计 {len(scan_rows)}", flush=True)

    scan = pd.DataFrame(scan_rows)
    scan.to_csv(os.path.join(BASE, "data", "超卖路径收紧研究_扫描明细_20260921.csv"),
                index=False, encoding="utf-8-sig")
    print(f"[扫描候选] {len(scan)} 条（{scan['蓄势日'].min()} ~ {scan['蓄势日'].max()}）", flush=True)

    # ---- 每日候选量（现状）----
    daily = scan.groupby("蓄势日").size()
    last15 = daily.tail(15)
    print(f"\n== 扫描候选量/日（现状参数）：全期均值 {daily.mean():.1f}，"
          f"中位 {daily.median():.0f}，近15个交易日均值 {last15.mean():.1f} ==")
    print(last15.to_string())

    # ---- 第二遍：对触发样本补 T1/T3/T5 ----
    # 触发口径 = backtest() 的 OS 参数组（触发日 i+1）
    trig = []
    cache = {}
    for code, grp in scan.groupby("代码"):
        _, h = _local_hist(code, 700)
        if h is None:
            continue
        ind = lpe._indicators(h)
        dates = ind["日期"].dt.strftime("%Y-%m-%d").to_numpy()
        close = ind["收盘"].to_numpy(dtype=float)
        lb = pd.to_numeric(ind["lb"], errors="coerce").to_numpy(dtype=float)
        chg = pd.to_numeric(ind["涨跌幅"], errors="coerce").to_numpy(dtype=float)
        amt = pd.to_numeric(ind["成交额"], errors="coerce").to_numpy(dtype=float)
        p60 = pd.to_numeric(ind["pos60"], errors="coerce").to_numpy(dtype=float)
        ma20 = pd.to_numeric(ind["MA20"], errors="coerce").to_numpy(dtype=float)
        cache[code] = ind
        for _, r in grp.iterrows():
            i = int(r["i"])
            j = i + 1
            if j >= len(ind):
                continue
            if not (T_LB_LO <= lb[j] <= T_LB_HI and T_CHG_LO <= chg[j] <= T_CHG_HI
                    and T_AMT_LO <= amt[j] <= T_AMT_HI
                    and close[j] > ma20[i] and p60[j] < OS_POS60_MAX):
                continue
            futs = ind.iloc[j + 1:j + 6]
            entry = close[j]

            def _r(k):
                return (float(futs.iloc[k]["收盘"]) / entry - 1) * 100 if len(futs) > k else None
            trig.append({**r.to_dict(), "代码": code,
                         "触发日": dates[j], "触发量比": round(lb[j], 2),
                         "触发涨幅%": round(chg[j], 2),
                         "触发成交额亿": round(amt[j] / 1e8, 1),
                         "入场价": round(entry, 2),
                         "T1收益%": _r(0), "T3收益%": _r(2), "T5收益%": _r(4)})
    t = pd.DataFrame(trig)
    t.to_csv(os.path.join(BASE, "data", "超卖路径收紧研究_触发样本_20260921.csv"),
             index=False, encoding="utf-8-sig")
    t5 = t["T5收益%"].dropna()
    print(f"\n[触发样本] {len(t)} 条，T5 样本 {t5.size}：均值 {t5.mean():+.2f}% "
          f"胜率 {(t5 > 0).mean() * 100:.1f}% 中位 {t5.median():+.1f}%")

    # ---- 分层函数 ----
    def strat(col, bins, labels):
        print(f"\n-- 按 {col} 分层（T5）--")
        x = t[col].dropna()
        sub = t.loc[x.index]
        b = pd.cut(x, bins=bins, labels=labels)
        for lab in labels:
            s = sub[b == lab]["T5收益%"].dropna()
            if s.empty:
                print(f"  {lab}: 样本0")
                continue
            print(f"  {lab}: 样本{s.size} 均值{s.mean():+.2f}% 胜率{(s > 0).mean() * 100:.0f}% "
                  f"中位{s.median():+.1f}% 大亏(≤-10%) {(s <= -10).mean() * 100:.0f}%")

    strat("蓄势日成交额亿", [0, 5, 10, 15, 25, 1e9], ["≤5亿", "5-10亿", "10-15亿", "15-25亿", ">25亿"])
    strat("蓄势日量比", [0, 1, 1.5, 3, 100], ["≤1.0x", "1.0-1.5x", "1.5-3x", ">3x"])
    strat("蓄势日5日量比均值", [0, 1.2, 2, 100], ["≤1.2x", "1.2-2x", ">2x"])
    strat("距最近超卖日", [-0.1, 0.9, 3.9, 7.9, 15.9, 30], ["0(当日)", "1-3日", "4-7日", "8-15日", "16-25日"])
    strat("蓄势位置", [0, 0.2, 0.4, 0.55, 1], ["<0.2", "0.2-0.4", "0.4-0.55", "0.55-0.65"])
    strat("5日涨幅%", [-100, -8, 0, 100], ["<-8%", "-8~0%", "0~5%"])
    strat("20日振幅%", [0, 20, 30, 1000], ["<20%", "20-30%", ">30%"])
    strat("距首次超卖日", [-0.1, 9.9, 14.9, 19.9, 30], ["≤9日", "10-14日", "15-19日", "20-24日"])
    strat("距高%", [-1000, -30, -20, -10, 1000], ["<-30%", "-30~-20%", "-20~-10%", ">-10%"])

    # ---- 组合收紧 → 触发样本留存 + 扫描量/日 ----
    print("\n== 收紧组合对照（触发样本 T5 / 扫描候选量每日均值）==")
    t5all = t["T5收益%"].dropna()
    print(f"{'组合':<52}{'触发样本':>6}{'T5均值':>8}{'胜率':>7}{'扫描量/日':>9}")
    base = "现状(回看25/pos60<0.65/无限额)"
    print(f"{base:<52}{t5.size:>6}{t5all.mean():>+8.2f}{(t5all > 0).mean() * 100:>6.1f}%{daily.mean():>9.1f}")

    def cut(lookback=None, pos60=None, amt_hi=None, recency_min=None, lb_hi=None, lbm_hi=None, chg5_hi=None):
        m = pd.Series(True, index=t.index)
        ms = pd.Series(True, index=scan.index)
        if lookback is not None:
            m &= t["距最近超卖日"] <= lookback
            ms &= scan["距最近超卖日"] <= lookback
        if pos60 is not None:
            m &= t["蓄势位置"] < pos60
            ms &= scan["蓄势位置"] < pos60
        if amt_hi is not None:
            m &= t["蓄势日成交额亿"] <= amt_hi
            ms &= scan["蓄势日成交额亿"] <= amt_hi
        if recency_min is not None:
            m &= t["距首次超卖日"] >= recency_min
            ms &= scan["距首次超卖日"] >= recency_min
        if lb_hi is not None:
            m &= t["蓄势日量比"] <= lb_hi
            ms &= scan["蓄势日量比"] <= lb_hi
        if lbm_hi is not None:
            m &= t["蓄势日5日量比均值"] <= lbm_hi
            ms &= scan["蓄势日5日量比均值"] <= lbm_hi
        if chg5_hi is not None:
            m &= t["5日涨幅%"] <= chg5_hi
            ms &= scan["5日涨幅%"] <= chg5_hi
        s = t.loc[m, "T5收益%"].dropna()
        vol = scan.loc[ms].groupby("蓄势日").size()
        v = vol.mean() if len(vol) else 0.0
        label = []
        if lookback is not None: label.append(f"近超卖≤{lookback}日")
        if pos60 is not None: label.append(f"pos60<{pos60}")
        if amt_hi is not None: label.append(f"额≤{amt_hi}亿")
        if recency_min is not None: label.append(f"首超卖≥{recency_min}日")
        if lb_hi is not None: label.append(f"量比≤{lb_hi}")
        if lbm_hi is not None: label.append(f"5日量比≤{lbm_hi}")
        if chg5_hi is not None: label.append(f"5日涨幅≤{chg5_hi}%")
        stat = f"{s.mean():+.2f}%/{(s > 0).mean() * 100:.0f}%" if s.size else "—"
        print(f"{'+'.join(label) or '—':<52}{s.size:>6}{stat:>15}{v:>9.1f}")

    cut(amt_hi=15)
    cut(pos60=0.55)
    cut(recency_min=10)
    cut(recency_min=7)
    cut(lb_hi=3.0)
    cut(lbm_hi=2.0)
    cut(amt_hi=15, pos60=0.55, recency_min=10)
    cut(amt_hi=15, pos60=0.55, recency_min=10, lb_hi=3.0, lbm_hi=2.0)
    cut(amt_hi=15, pos60=0.55, recency_min=10, lb_hi=3.0, lbm_hi=2.0, chg5_hi=0)
    cut(amt_hi=15, pos60=0.55, recency_min=7, lb_hi=3.0, lbm_hi=2.0)
    print("\n明细已落 data/超卖路径收紧研究_扫描明细_20260921.csv / _触发样本_20260921.csv")


if __name__ == "__main__":
    main()
