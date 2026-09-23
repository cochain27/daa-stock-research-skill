# -*- coding: utf-8 -*-
"""首板启动回测：验证「题材首板→连板」路径（通鼎互联 09-15/16 类型，用户 09-21 提问驱动）。

背景：通鼎互联 09-09 四池全不中（+3.6% 普通放量阳线；振幅 6.7%/成交 43 亿超初动池线，
20日振幅 29.3% 超趋势池线）。09-15 首板涨停 → 09-16 二板（首板当日成交 59.4 亿）。
用户问「这类票该用什么策略筛」→ 答案候选：首板启动池。本脚本用本地K线全市场回测验证。

信号画像（T 日收盘确认）：
  1) 当日收盘涨停：close >= round(昨收×ratio, 2) - 0.005，ratio=1.1(主板60/00)/1.2(创业板30)
  2) 前一日未涨停（首板，非连板）
  3) 成交额 ≥ 2亿（流动性下限）
  4) 分层变量（验证蓄势前置价值）：
     A. 蓄势程度：前20日涨幅 ≤30% / ≤15% / 不限
     B. 60日位置：<70% / <50% / 不限
     C. 首板当日成交额档：≤30亿 / ≤60亿 / 不限（通鼎首板 59.4 亿，只可能落 ≤60亿/不限）

入场：T+1 开盘（T+1 一字板 → 买不进跳过）
出场：A. 固定 T+5 收盘；B. -8% 止损（收盘<入场×0.92 → -8% 出场）——与初动池 V2 同口径，直接可比
信号冷却：同票 10 交易日 1 信号（与 warm_start_backtest 一致）

局限（如实说明）：
  - 本地K线无题材/热度字段，无法区分「题材首板」与「垃圾脉冲首板」，结果偏保守
  - 无ST标记；ST 够不着 10% 涨停价，天然被排除
"""
import sys
from pathlib import Path
from glob import glob

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import numpy as np

BASE = Path(__file__).resolve().parent.parent / "data" / "klines"

MIN_AMOUNT = 2e8        # 成交额下限 2亿
STOP_PCT = 0.92         # -8% 止损（与初动 V2 一致）
COOL_DAYS = 10          # 信号冷却

# 分层档位
TIERS = {
    "T1_基准_首板仅成交额≥2亿":   dict(chg20_max=None, pos60_max=None),
    "T2_温和蓄势_20日≤30%_位置<70": dict(chg20_max=30.0, pos60_max=0.70),
    "T3_严格蓄势_20日≤15%_位置<50": dict(chg20_max=15.0, pos60_max=0.50),
}
AMT_TIERS = {"≤30亿": 30e8, "≤60亿": 60e8, "不限": None}


def zt_ratio(code):
    """涨停幅度：创业板30/科创68=20%，主板=10%"""
    return 1.2 if code.startswith(("3", "68")) else 1.1


def _load(code):
    f = BASE / f"{code}.csv"
    if not f.exists():
        return None
    df = pd.read_csv(f)
    if len(df) < 60:
        return None
    df = df.copy()
    df["日期"] = pd.to_datetime(df["date"])
    df = df.sort_values("日期").reset_index(drop=True)
    df["prev_close"] = df["last"].shift(1)
    ratio = zt_ratio(code)
    df["zt_price"] = (df["prev_close"] * ratio).round(2)
    df["涨停"] = df["last"] >= df["zt_price"] - 0.005
    df["涨跌幅"] = df["last"].pct_change() * 100
    df["chg20"] = df["last"].pct_change(20) * 100       # 前20日涨幅
    df["hi60"] = df["high"].rolling(60, min_periods=40).max()
    df["lo60"] = df["low"].rolling(60, min_periods=40).min()
    df["pos60"] = (df["last"] - df["lo60"]) / (df["hi60"] - df["lo60"])
    return df


def _is_limit_open(row):
    """一字板：开=高=低=收 且涨停"""
    return row["open"] == row["high"] == row["low"] == row["last"] and row["涨停"]


def _collect_signals(df):
    """首板信号：当日涨停 且 前一日未涨停；10日冷却。"""
    first = df["涨停"] & (~df["涨停"].shift(1, fill_value=False))
    idx = df.index[first].tolist()
    out = []
    last_i = -99
    for i in idx:
        if i - last_i < COOL_DAYS:
            continue
        last_i = i
        out.append(i)
    return out


def _trade_result(df, i):
    """T+1 开盘入场，A: T+5 收盘 / B: -8% 止损。一字板返回 None。"""
    if i + 1 >= len(df):
        return None
    nxt = df.iloc[i + 1]
    if _is_limit_open(nxt):
        return None
    entry = nxt["open"]
    if entry <= 0:
        return None
    end_i = min(i + 6, len(df) - 1)
    r5 = df.iloc[end_i]["last"] / entry - 1
    seg = df.iloc[i + 1:end_i + 1]
    min_low = seg["low"].min()
    stop_hit = min_low <= entry * STOP_PCT
    r5_stop = r5 if not stop_hit else STOP_PCT - 1
    max_r5 = seg["high"].max() / entry - 1
    return {"entry": entry, "r5": r5 * 100, "r5_stop": r5_stop * 100,
            "stop_hit": stop_hit, "max_r5": max_r5 * 100}


def main():
    files = glob(str(BASE / "*.csv"))
    codes = sorted(Path(f).stem for f in files)
    print(f"扫描 {len(codes)} 只票，首板启动回测 ...", flush=True)

    detail = {t: {k: [] for k in AMT_TIERS} for t in TIERS}
    for code in codes:
        df = _load(code)
        if df is None:
            continue
        for i in _collect_signals(df):
            row = df.iloc[i]
            amt = row["amount"]
            rec = {
                "code": code, "date": str(pd.Timestamp(row["日期"]).date()),
                "收盘": round(row["last"], 2), "涨跌幅": round(row["涨跌幅"], 1),
                "chg20": round(row["chg20"], 1) if pd.notna(row["chg20"]) else None,
                "pos60": round(row["pos60"], 3) if pd.notna(row["pos60"]) else None,
                "amt亿": round(amt / 1e8, 1),
            }
            r = _trade_result(df, i)
            if r is None:
                rec.update({k: None for k in ("entry", "r5", "r5_stop", "stop_hit", "max_r5")})
            else:
                rec.update(r)
            for tname, tier in TIERS.items():
                if tier["chg20_max"] is not None and (rec["chg20"] is None or rec["chg20"] > tier["chg20_max"]):
                    continue
                if tier["pos60_max"] is not None and (rec["pos60"] is None or rec["pos60"] > tier["pos60_max"]):
                    continue
                for cap_name, cap in AMT_TIERS.items():
                    if cap is None or amt <= cap:
                        detail[tname][cap_name].append(rec)

    def stat(recs):
        rr = [x for x in recs if x["r5"] is not None]
        s = {"n": len(recs), "可执行": len(rr)}
        if rr:
            r5 = pd.Series([x["r5"] for x in rr])
            r5s = pd.Series([x["r5_stop"] for x in rr])
            wins, losses = r5s[r5s > 0], r5s[r5s < 0]
            s.update({
                "T5均值%": round(r5.mean(), 2),
                "T5胜率%": round((r5 > 0).mean() * 100, 1),
                "止损版均值%": round(r5s.mean(), 2),
                "止损版胜率%": round((r5s > 0).mean() * 100, 1),
                "盈亏比": round(wins.mean() / abs(losses.mean()), 2) if len(wins) and len(losses) else None,
                "止损触发率%": round(pd.Series([x["stop_hit"] for x in rr]).mean() * 100, 1),
                "T5≥10%占比%": round((r5 >= 10).mean() * 100, 1),
                "最大单笔盈%": round(r5s.max(), 1),
                "最大单笔亏%": round(r5s.min(), 1),
            })
        return s

    print("\n========== 首板启动 · 全市场回测 ==========\n")
    summary_rows = []
    for tname, amts in detail.items():
        for cap_name, recs in amts.items():
            s = stat(recs)
            summary_rows.append({"分层": tname, "成交额": cap_name, **s})
            print(f"【{tname} | 首板成交额{cap_name}】信号 {s['n']}，可执行 {s.get('可执行', 0)}")
            for k in ("T5均值%", "T5胜率%", "止损版均值%", "止损版胜率%", "盈亏比",
                      "止损触发率%", "T5≥10%占比%", "最大单笔盈%", "最大单笔亏%"):
                print(f"  {k}: {s.get(k)}")
            print()

    # ===== 通鼎命中验证 =====
    print("========== 通鼎互联(sz002491) 命中验证 ==========\n")
    df = _load("sz002491")
    if df is not None:
        m = df["涨停"] & (~df["涨停"].shift(1, fill_value=False))
        hits = df.loc[m, ["日期", "last", "涨跌幅", "amount"]]
        print("近60个涨停日（首板标记，冷却后）:")
        for _, r in hits.tail(10).iterrows():
            i = df.index[df["日期"] == r["日期"]][0]
            tr = _trade_result(df, i)
            extra = f"→ 次日开盘{tr['entry']:.2f}, T+5 {tr['r5']:+.1f}%(止损版{tr['r5_stop']:+.1f}%)" if tr else "→ 一字板买不进"
            print(f"  {pd.Timestamp(r['日期']).date()} 收{r['last']:.2f} +{r['涨跌幅']:.1f}% 额{r['amount']/1e8:.1f}亿 {extra}")

    # ===== 保存 =====
    all_rows = []
    for tname, amts in detail.items():
        for cap_name, recs in amts.items():
            for r in recs:
                all_rows.append({**r, "分层": tname, "成交额档": cap_name})
    out = Path(__file__).resolve().parent.parent / "data" / "首板启动回测_明细.csv"
    if all_rows:
        pd.DataFrame(all_rows).to_csv(out, index=False, encoding="utf-8-sig")
        print(f"\n明细已存 {out}（{len(all_rows)} 条）")
    out_s = Path(__file__).resolve().parent.parent / "data" / "首板启动回测_汇总.csv"
    pd.DataFrame(summary_rows).to_csv(out_s, index=False, encoding="utf-8-sig")
    print(f"汇总已存 {out_s}")


if __name__ == "__main__":
    main()
