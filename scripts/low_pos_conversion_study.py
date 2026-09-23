# -*- coding: utf-8 -*-
"""低位观察池「入池→启动」转化率与时点分布研究（2026-09-22）。

需求：扩样本回测低位启动观察池的转化率；统计入池后多久才触发启动
信号；评估 T+5 强制离场/剔除是否太短（会不会还没爆发就被踢出）。

口径（与 low_pos_entry.backtest() 完全一致）：
  - 入池日 = 蓄势日（T-1 收盘满足 _蓄势通过 或 _recent_oversold + 扫描收紧）
  - 启动信号 = 触发日：量比≥2.0(≤7.0) + 涨幅∈[10,15]%(标准蓄势)/[9,15]%(超卖)
                + 成交额∈[4,16]亿 + 破MA20 + 触发日位置<0.55(标准)/0.65(超卖)
  - 入池后第 1..60 个交易日窗口内首个满足条件的启动日
  - 启动后收益 = T+1 收盘入场 → T+3/T+5 收盘
  - T+5 窗口口径 = 入池后 5 个自然交易日内是否启动（模拟当前观察池最长展示）

产物：data/低位观察池_转化率研究_20260922.csv（明细）
      data/低位观察池_转化率研究_20260922.json（统计）
"""
import glob
import json
import os
import sys
from collections import Counter, defaultdict

import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import low_pos_entry as lpe  # noqa: E402
from low_pos_backtest_full import _local_hist  # noqa: E402

# ---- 与 backtest() 同源的触发参数 ----
PARAMS = {
    "标准蓄势": dict(lb_lo=lpe.LOW_POS_ENTRY_TRIGGER_LB,
                     lb_hi=lpe.LOW_POS_ENTRY_TRIGGER_LB_MAX,
                     chg_lo=lpe.LOW_POS_ENTRY_TRIGGER_CHG_MIN,
                     chg_hi=lpe.LOW_POS_ENTRY_TRIGGER_CHG_MAX,
                     amt_lo=lpe.LOW_POS_ENTRY_TRIGGER_MIN_AMOUNT,
                     amt_hi=lpe.LOW_POS_ENTRY_TRIGGER_MAX_AMOUNT,
                     break_ma20=lpe.LOW_POS_ENTRY_TRIGGER_BREAK_MA20,
                     pos60_max=lpe.LOW_POS_ENTRY_MAX_POS60),
    "近期超卖": dict(lb_lo=lpe.LOW_POS_ENTRY_OS_TRIGGER_LB,
                     lb_hi=lpe.LOW_POS_ENTRY_OS_TRIGGER_LB_MAX,
                     chg_lo=lpe.LOW_POS_ENTRY_OS_TRIGGER_CHG_MIN,
                     chg_hi=lpe.LOW_POS_ENTRY_OS_TRIGGER_CHG_MAX,
                     amt_lo=lpe.LOW_POS_ENTRY_OS_TRIGGER_MIN_AMOUNT,
                     amt_hi=lpe.LOW_POS_ENTRY_OS_TRIGGER_MAX_AMOUNT,
                     break_ma20=lpe.LOW_POS_ENTRY_OS_TRIGGER_BREAK_MA20,
                     pos60_max=lpe.LOW_POS_ENTRY_RECENT_OVERSOLD_POS60_MAX),
}


def _triggered(nxt, row, path):
    """触发判定（与 backtest 第四步同源）。nxt=触发日指标行, row=蓄势日指标行。"""
    p = PARAMS[path]
    lb = float(nxt["lb"])
    chg = float(nxt["涨跌幅"])
    amt = float(nxt.get("成交额", 0))
    ma20 = float(row["MA20"])
    pos = float(nxt.get("pos60", 0))
    return (p["lb_lo"] <= lb <= p["lb_hi"]
            and p["chg_lo"] <= chg <= p["chg_hi"]
            and p["amt_lo"] <= amt <= p["amt_hi"]
            and (not p["break_ma20"] or float(nxt["收盘"]) > ma20)
            and pos < p["pos60_max"])


def main():
    codes = sorted(os.path.basename(p)[2:-4] for p in glob.glob(os.path.join(BASE, "data", "klines", "*.csv")))
    print(f"[转化率研究] 本地宇宙 {len(codes)} 只", flush=True)

    lpe._load_hist = _local_hist
    lpe.LOW_POS_ENTRY_TEMP_MIN = 0

    rows = []
    for ci, code in enumerate(codes):
        if ci % 200 == 0:
            print(f"  进度 {ci}/{len(codes)} 已入池 {len(rows)}", flush=True)
        _, h = _local_hist(code, days=700)
        if h is None or len(h) < 80:
            continue
        ind = lpe._indicators(h)
        # 入池扫描（与 backtest 第一步一致）
        for i in range(70, len(ind) - 7):   # 留足后视窗口（60日找启动）
            row = ind.iloc[i]
            ok, _ = lpe._蓄势通过(row)
            recent_os, os_date, _, _, os_first_idx = lpe._recent_oversold(ind, i + 1)
            if not ok and not recent_os:
                continue
            chg_today = float(row.get("涨跌幅") or 0)
            if chg_today > lpe.LOW_POS_ENTRY_MAX_CHG_TODAY:
                continue
            # 超卖扫描收紧（与 backtest 同源）
            if recent_os:
                amt_lo, amt_hi = lpe.LOW_POS_ENTRY_OS_SCAN_AMOUNT_RANGE
                amt_i = float(row.get("成交额") or 0)
                if not (amt_lo <= amt_i <= amt_hi):
                    continue
                if float(row["pos60"]) >= lpe.LOW_POS_ENTRY_OS_SCAN_POS60_MAX:
                    continue
                if os_first_idx is not None and (i - os_first_idx) < lpe.LOW_POS_ENTRY_OS_SCAN_OS_AGE_MIN:
                    continue
                if float(row.get("lb") or 0) > lpe.LOW_POS_ENTRY_OS_SCAN_LB_MAX:
                    continue
                if float(row.get("lb5mean") or 0) > lpe.LOW_POS_ENTRY_OS_SCAN_LB5MEAN_MAX:
                    continue
                if float(row.get("chg5") or 0) > lpe.LOW_POS_ENTRY_OS_SCAN_CHG5_MAX:
                    continue
            path = "标准蓄势" if ok else "近期超卖"
            pool_date = str(pd.Timestamp(row["日期"]).date())
            pool_close = float(row["收盘"])

            # 入池后 60 个交易日窗口内找首个启动日（模拟观察池展示期）
            # 若 60 日内无启动 → 记"60日内未启动"，收益列为空
            trig_day = None
            trig_idx = None
            for j in range(i + 1, min(i + 61, len(ind))):
                nxt = ind.iloc[j]
                if _triggered(nxt, row, path):
                    trig_idx, trig_day = j, str(pd.Timestamp(nxt["日期"]).date())
                    break
            gap = trig_idx - i if trig_idx is not None else None  # 入池到启动的自然交易日数

            t3 = t5 = None
            if trig_idx is not None and trig_idx + 5 < len(ind):
                entry = float(ind.iloc[trig_idx]["收盘"])
                t3 = (float(ind.iloc[trig_idx + 3]["收盘"]) / entry - 1) * 100
                t5 = (float(ind.iloc[trig_idx + 5]["收盘"]) / entry - 1) * 100

            rows.append({
                "代码": code,
                "入池日": pool_date,
                "蓄势路径": path,
                "入池收盘": round(pool_close, 2),
                "入池位置": round(float(row["pos60"]), 2),
                "距启动天数": gap,              # None = 60日内未启动
                "启动日": trig_day,
                "启动后T3%": round(t3, 1) if t3 is not None else None,
                "启动后T5%": round(t5, 1) if t5 is not None else None,
                "5日内启动": bool(gap is not None and gap <= 5),
            })

    df = pd.DataFrame(rows)
    out_csv = os.path.join(BASE, "data", "低位观察池_转化率研究_20260922.csv")
    df.to_csv(out_csv, index=False, encoding="utf-8-sig")
    print(f"明细 → {out_csv}  入池样本 {len(df)}", flush=True)

    # ---- 统计 ----
    total = len(df)
    n_trig = df["距启动天数"].notna().sum()
    stats = {
        "入池样本": int(total),
        "票数": int(df["代码"].nunique()),
        "窗口": f"{df['入池日'].min()} ~ {df['入池日'].max()}",
        "60日内启动": int(n_trig),
        "60日转化率%": round(n_trig / total * 100, 1) if total else None,
        "5日内启动(现T+5口径)": int(df["5日内启动"].sum()),
        "5日转化率%": round(df["5日内启动"].sum() / total * 100, 1) if total else None,
        "启动时点分布": {},
        "路径分层": {},
    }
    # 启动时点分布（天数桶）
    gaps = df["距启动天数"].dropna()
    buckets = defaultdict(int)
    for g in gaps:
        b = "T+1" if g == 1 else ("T+2~5" if g <= 5 else ("T+6~10" if g <= 10 else ("T+11~20" if g <= 20 else "T+21~60")))
        buckets[b] += 1
    stats["启动时点分布"] = dict(buckets)
    # 中位数/均值启动天数
    stats["启动天数中位数"] = float(gaps.median()) if gaps.size else None
    stats["启动天数均值"] = round(float(gaps.mean()), 1) if gaps.size else None
    stats["启动天数P75"] = float(gaps.quantile(0.75)) if gaps.size else None
    # 路径分层
    for path, grp in df.groupby("蓄势路径"):
        gt = grp[grp["距启动天数"].notna()]
        stats["路径分层"][path] = {
            "入池": int(len(grp)),
            "60日转化率%": round(len(gt) / len(grp) * 100, 1) if len(grp) else None,
            "5日转化率%": round(grp["5日内启动"].sum() / len(grp) * 100, 1) if len(grp) else None,
            "启动天数中位数": float(gt["距启动天数"].median()) if gt.size else None,
            "启动后T5均值%": round(float(gt["启动后T5%"].dropna().mean()), 1) if gt["启动后T5%"].dropna().size else None,
            "启动后T5胜率%": round(float((gt["启动后T5%"].dropna() > 0).mean() * 100), 1) if gt["启动后T5%"].dropna().size else None,
        }
    # 仅统计"5日内启动"的收益（现口径下能吃到肉的部分）
    fast = df[df["5日内启动"]]
    stats["5日内启动后T5均值%"] = round(float(fast["启动后T5%"].dropna().mean()), 1) if fast["启动后T5%"].dropna().size else None
    stats["5日内启动后T5胜率%"] = round(float((fast["启动后T5%"].dropna() > 0).mean() * 100), 1) if fast["启动后T5%"].dropna().size else None
    # 慢启动（6~20日）的收益 —— 如果 T+5 剔除会错过的部分
    slow = df[(df["距启动天数"].notna()) & (df["距启动天数"] > 5) & (df["距启动天数"] <= 20)]
    stats["6~20日慢启动数"] = int(len(slow))
    stats["6~20日慢启动后T5均值%"] = round(float(slow["启动后T5%"].dropna().mean()), 1) if slow["启动后T5%"].dropna().size else None
    stats["6~20日慢启动后T5胜率%"] = round(float((slow["启动后T5%"].dropna() > 0).mean() * 100), 1) if slow["启动后T5%"].dropna().size else None

    out_json = os.path.join(BASE, "data", "低位观察池_转化率研究_20260922.json")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=1)
    print(json.dumps(stats, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
