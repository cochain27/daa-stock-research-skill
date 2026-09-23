# -*- coding: utf-8 -*-
"""低位观察池「收紧入池标准」分层研究 v2（2026-09-23）。

背景（用户 2026-09-23）：
  前三个正式策略固定不动；研究低位埋伏观察池——入池后几天内启动？
  能否收紧入池标准，提高启动概率与启动速度？

与 low_pos_conversion_study.py 的差异：
  1. 事件去重：同票相邻入池日间隔 ≤3 交易日 → 合并为一个蓄势事件（保留首个），
     避免「同一波蓄势连续 20 天重复入池」把样本灌水 90439 行。
  2. 记录完整入池日特征（量比/5日量比/振幅/位置/距高/成交额/MA20乖离/
     连续蓄势天数/超卖龄/超卖深度/量能收缩），供分层分析。
  3. 对照口径：入池收盘直接买入 T+5（不等触发），评估「等触发」的增量价值。
  4. 向量化（numpy bool 数组），全宇宙 1092 只预计 5~10 分钟。

口径（与 low_pos_entry.backtest() 完全一致，防口径漂移）：
  入池日 = 蓄势日；启动信号 = 量比2-7 + 涨幅[10,15]%标准/[9,15]%超卖
           + 成交额4-16亿 + 收盘>蓄势日MA20 + 触发日位置<0.55标准/0.65超卖
产物：data/低位池_入池事件_20260923.csv（事件明细）
      data/低位池_收紧分层_20260923.json（单因子分层统计）
"""
import glob
import json
import os
import sys

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import low_pos_entry as lpe  # noqa: E402
from low_pos_backtest_full import _local_hist  # noqa: E402

# ---- 与 config 同源参数（硬拷贝防运行期漂移，改动时对照 config） ----
STD = dict(pos60_max=0.55, dist60_max=-25, lb_max=1.30, lb5_max=1.20,
           chg5_lo=-10.0, chg5_hi=5.0, chg_today_max=5.0, amp20_max=30,
           amt_lo=2e8, amt_hi=15e8,
           trig_lb=(2.0, 7.0), trig_chg=(10.0, 15.0), trig_pos60=0.55)
OS = dict(lookback=25, os_pos60=0.5, os_dist60=-30,
          amt_lo=2e8, amt_hi=15e8, pos60_max=0.55, age_min=10,
          lb_max=3.0, lb5_max=2.0, chg5_max=0.0, chg_today_max=5.0,
          trig_lb=(2.0, 7.0), trig_chg=(9.0, 15.0), trig_pos60=0.65)

TRIG_WIN = 60          # 入池后 60 个交易日内找首个启动
DEDUP_GAP = 3          # 事件去重：相邻入池间隔 ≤3 交易日视为同一事件


def _streak(ok):
    """每日往前连续 True 的天数（含当日）。"""
    s = np.zeros(len(ok), dtype=int)
    run = 0
    for i in range(len(ok)):
        run = run + 1 if ok[i] else 0
        s[i] = run
    return s


def _process(code):
    _, h = _local_hist(code, days=700)
    if h is None or len(h) < 80:
        return []
    ind = lpe._indicators(h)
    n = len(ind)
    dates = ind["日期"].dt.strftime("%Y-%m-%d").values
    close = ind["收盘"].values.astype(float)
    lb = ind["lb"].values.astype(float)
    lb5 = ind["lb5mean"].values.astype(float)
    chg5 = ind["chg5"].values.astype(float)
    amp20 = ind["amp20"].values.astype(float)
    pos60 = ind["pos60"].values.astype(float)
    dist60 = ind["dist60"].values.astype(float)
    amt = ind["成交额"].values.astype(float)
    ma20 = ind["MA20"].values.astype(float)
    chgt = ind["涨跌幅"].values.astype(float)

    valid = ~(np.isnan(pos60) | np.isnan(dist60) | np.isnan(lb) | np.isnan(lb5)
              | np.isnan(chg5) | np.isnan(amp20))
    # ---- 标准蓄势 bool（与 _蓄势通过 同源） ----
    std_ok = (valid & (pos60 < STD["pos60_max"]) & (dist60 <= STD["dist60_max"])
              & (lb <= STD["lb_max"]) & (lb5 <= STD["lb5_max"])
              & (chg5 >= STD["chg5_lo"]) & (chg5 <= STD["chg5_hi"])
              & (chgt <= STD["chg_today_max"]) & (amp20 < STD["amp20_max"])
              & (amt >= STD["amt_lo"]) & (amt <= STD["amt_hi"]))
    # ---- 近期超卖 bool（与 _recent_oversold + OS_SCAN_* 同源） ----
    os_flag = (pos60 < OS["os_pos60"]) | (dist60 < OS["os_dist60"])
    trues = np.where(os_flag)[0]
    os_age = np.full(n, np.nan)
    os_pos60_at = np.full(n, np.nan)
    for i in range(70, n):
        p = np.searchsorted(trues, i - OS["lookback"], side="left")
        if p < len(trues) and trues[p] < i:
            f = trues[p]
            os_age[i] = i - f
            if not np.isnan(pos60[f]):
                os_pos60_at[i] = pos60[f]
    has_os = ~np.isnan(os_age)
    os_ok = (valid & ~std_ok & has_os & (os_age >= OS["age_min"])
             & (pos60 < OS["pos60_max"])
             & (amt >= OS["amt_lo"]) & (amt <= OS["amt_hi"])
             & (lb <= OS["lb_max"]) & (lb5 <= OS["lb5_max"])
             & (chg5 <= OS["chg5_max"]) & (chgt <= OS["chg_today_max"]))

    # ---- 连续蓄势天数 / 量能收缩 ----
    std_streak = _streak(std_ok)
    os_streak = _streak(os_ok)
    v5 = ind["成交量"].shift(1).rolling(5).mean().values
    v20 = ind["成交量"].shift(1).rolling(20).mean().values
    vol_ratio = v5 / v20  # <1 = 近5日较20日缩量

    # ---- 触发 bool（按触发日，MA20/位置上限事件级锚定） ----
    trig_common = (lb >= 2.0) & (lb <= 7.0) & (amt >= 4e8) & (amt <= 16e8)
    trig_std = trig_common & (chgt >= 10.0) & (chgt <= 15.0) & (pos60 < STD["trig_pos60"])
    trig_os = trig_common & (chgt >= 9.0) & (chgt <= 15.0) & (pos60 < OS["trig_pos60"])

    # ---- 事件收集（idx≥70 且留 7 日后视，与转化率研究一致） ----
    pool_idx = [i for i in range(70, n - 7) if (std_ok[i] or os_ok[i])]
    # 事件去重：间隔 ≤3 交易日合并，保留首个
    events, last_i = [], -999
    for i in pool_idx:
        if i - last_i <= DEDUP_GAP:
            last_i = i
            continue
        events.append(i)
        last_i = i

    rows = []
    for i in events:
        is_std = bool(std_ok[i])
        tg = trig_std if is_std else trig_os
        ma20_i = ma20[i]
        # 启动查找：j∈(i, i+60]，收盘>蓄势日MA20
        end = min(i + 1 + TRIG_WIN, n)
        win = np.arange(i + 1, end)
        cond = tg[i + 1:end] & (close[i + 1:end] > ma20_i)
        gap = None
        j = None
        if cond.any():
            j = int(win[int(np.argmax(cond))])
            gap = j - i
        t3 = t5 = None
        if j is not None and j + 5 < n:
            t3 = (close[j + 3] / close[j] - 1) * 100
            t5 = (close[j + 5] / close[j] - 1) * 100
        pool_t5 = (close[i + 5] / close[i] - 1) * 100 if i + 5 < n else None
        rows.append({
            "代码": code, "入池日": dates[i], "路径": "标准蓄势" if is_std else "近期超卖",
            "入池收盘": round(float(close[i]), 2),
            "量比": round(float(lb[i]), 2), "5日量比": round(float(lb5[i]), 2),
            "5日涨幅%": round(float(chg5[i]), 1), "20日振幅%": round(float(amp20[i]), 1),
            "位置": round(float(pos60[i]), 3), "距高%": round(float(dist60[i]), 1),
            "成交额亿": round(float(amt[i]) / 1e8, 2),
            "MA20乖离%": round(float(close[i] / ma20_i - 1) * 100, 1) if not np.isnan(ma20_i) else None,
            "量能收缩": round(float(vol_ratio[i]), 2) if not np.isnan(vol_ratio[i]) else None,
            "连续蓄势": int(std_streak[i] if is_std else os_streak[i]),
            "超卖龄": int(os_age[i]) if (not is_std and not np.isnan(os_age[i])) else None,
            "超卖深度": round(float(os_pos60_at[i]), 3) if (not is_std and not np.isnan(os_pos60_at[i])) else None,
            "距启动": gap, "启动日": dates[j] if j is not None else None,
            "启动后T3%": round(t3, 1) if t3 is not None else None,
            "启动后T5%": round(t5, 1) if t5 is not None else None,
            "入池直接T5%": round(pool_t5, 1) if pool_t5 is not None else None,
            "5日内启动": bool(gap is not None and gap <= 5),
            "10日内启动": bool(gap is not None and gap <= 10),
            "60日内启动": gap is not None,
        })
    return rows


def _bucket_stat(df, col, edges, labels):
    """按分桶统计转化率/速度/收益。edges 为左开右闭分界（首桶含 -inf 尾桶含 inf）。"""
    out = {}
    b = pd.cut(df[col], bins=[-np.inf] + edges + [np.inf], labels=labels)
    for lab, grp in df.groupby(b, observed=True):
        out[str(lab)] = _stat(grp)
    na = df[df[col].isna()]
    if len(na):
        out["缺失"] = _stat(na)
    return out


def _stat(grp):
    n = len(grp)
    if n == 0:
        return {"样本": 0}
    trig = grp["60日内启动"].sum()
    fast5 = grp["5日内启动"].sum()
    fast10 = grp["10日内启动"].sum()
    gaps = grp.loc[grp["距启动"].notna(), "距启动"]
    t5s = grp.loc[grp["启动后T5%"].notna(), "启动后T5%"]
    pt5 = grp.loc[grp["入池直接T5%"].notna(), "入池直接T5%"]
    return {
        "样本": int(n),
        "5日启动%": round(fast5 / n * 100, 1),
        "10日启动%": round(fast10 / n * 100, 1),
        "60日启动%": round(trig / n * 100, 1),
        "启动中位天": float(gaps.median()) if gaps.size else None,
        "启动后T5均值%": round(float(t5s.mean()), 1) if t5s.size else None,
        "启动后T5胜率%": round(float((t5s > 0).mean() * 100), 1) if t5s.size else None,
        "入池直买T5均值%": round(float(pt5.mean()), 1) if pt5.size else None,
    }


def main():
    codes = sorted(os.path.basename(p)[2:-4]
                   for p in glob.glob(os.path.join(BASE, "data", "klines", "*.csv")))
    print(f"[收紧研究v2] 本地宇宙 {len(codes)} 只", flush=True)
    all_rows = []
    for ci, code in enumerate(codes):
        if ci % 150 == 0:
            print(f"  进度 {ci}/{len(codes)} 事件 {len(all_rows)}", flush=True)
        try:
            all_rows.extend(_process(code))
        except Exception as e:
            print(f"  ! {code}: {e}", flush=True)
    df = pd.DataFrame(all_rows)
    out_csv = os.path.join(BASE, "data", "低位池_入池事件_20260923.csv")
    df.to_csv(out_csv, index=False, encoding="utf-8-sig")
    print(f"事件明细 → {out_csv}  共 {len(df)} 事件", flush=True)

    stats = {"事件总数": int(len(df)), "票数": int(df["代码"].nunique()),
             "窗口": f"{df['入池日'].min()} ~ {df['入池日'].max()}"}
    stats["总体"] = _stat(df)
    stats["路径分层"] = {p: _stat(g) for p, g in df.groupby("路径")}

    # ---- 单因子分层 ----
    fac = {}
    fac["量比"] = _bucket_stat(df, "量比", [0.5, 0.8, 1.1], ["≤0.5", "0.5-0.8", "0.8-1.1", ">1.1"])
    fac["5日量比"] = _bucket_stat(df, "5日量比", [0.7, 0.9, 1.1], ["≤0.7", "0.7-0.9", "0.9-1.1", ">1.1"])
    fac["20日振幅"] = _bucket_stat(df, "20日振幅%", [8, 12, 18], ["≤8", "8-12", "12-18", ">18"])
    fac["位置"] = _bucket_stat(df, "位置", [0.10, 0.25, 0.40], ["≤0.10", "0.10-0.25", "0.25-0.40", ">0.40"])
    fac["距高%"] = _bucket_stat(df, "距高%", [-40, -30, -28], ["≤-40", "-40~-30", "-30~-28", "-28~-25"])
    fac["成交额亿"] = _bucket_stat(df, "成交额亿", [4, 8, 12], ["2-4", "4-8", "8-12", "12-15"])
    fac["5日涨幅"] = _bucket_stat(df, "5日涨幅%", [-5, -2, 0], ["-10~-5", "-5~-2", "-2~0", ">0"])
    fac["MA20乖离"] = _bucket_stat(df, "MA20乖离%", [-5, 0, 3], ["<-5", "-5~0", "0~3", ">3"])
    fac["量能收缩"] = _bucket_stat(df, "量能收缩", [0.7, 0.9, 1.0], ["≤0.7", "0.7-0.9", "0.9-1.0", ">1.0"])
    fac["连续蓄势"] = _bucket_stat(df, "连续蓄势", [1.5, 4.5, 9.5], ["1天", "2-4天", "5-9天", "≥10天"])
    osdf = df[df["路径"] == "近期超卖"]
    fac["超卖龄_仅超卖"] = _bucket_stat(osdf, "超卖龄", [15, 25, 40], ["10-15", "16-25", "26-40", ">40"])
    fac["超卖深度_仅超卖"] = _bucket_stat(osdf, "超卖深度", [0.20, 0.35, 0.50], ["≤0.20", "0.20-0.35", "0.35-0.50", ">0.50"])
    stats["单因子分层"] = fac

    out_json = os.path.join(BASE, "data", "低位池_收紧分层_20260923.json")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=1)
    print(json.dumps({k: stats[k] for k in ("事件总数", "票数", "窗口", "总体", "路径分层")},
                     ensure_ascii=False, indent=1))
    print(f"分层统计 → {out_json}", flush=True)


if __name__ == "__main__":
    main()
