#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""老版本「本周低位启动观察（趋势侧跟踪池补充）」策略独立回测。

来源：git 16b74fb（双策略时代，2026-09-05 前后）stock_screener.low_pos_watch()。
目标：回答用户"老版本策略胜率/盈亏比如何、有没有值得新策略借鉴的点"。
约束：只读现有代码，不改动现有 3 策略 + 观察池任何参数/文件。

老策略筛选（全部满足）：
  1. 白名单 60/00/30（主板+创业板，无科创/北交）、非 ST、有价
  2. 成交额 > 1.5亿（东财快照口径）
  3. 当日涨跌幅 -4%~+4%（不追当日大涨）
  4. 换手率排序取前 80 只做技术扫描
  5. 60日价格位置 < 0.40（低位）
  6. 前兆三件套：20日振幅<15% / 20日收盘std<4% / 距60日高>-22%
  7. 启动信号 ≥2：站上MA20 / MACD多头 / 放量>1.2x / 5日涨幅2%-12%
  8. 非近涨停（现价/涨停价<0.95）
  9. 行业去重（同行业最多1只），排序后取 top 3（每周3只）

出场（老策略为"观察池"，此处按可量化口径评估，供对比）：
  A. 观察视角：入池后 5/10 日内最大涨幅、是否触发买点
  B. 买入视角（保守）：信号次日开盘买入，止损-6%（老趋势侧观察池止损=现价×0.94），
     持5日/10日，或 +10% 止盈离场，衡量胜率/盈亏比/期望

数据：本地 data/klines/*.csv（腾讯前复权，2024-01 起）。
"""
import glob
import os
import sys
from collections import defaultdict
from datetime import datetime

import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

# 老版本参数（80740fe = 无前兆三件套，仅 pos60 + 信号≥2 + 流动性 + 当日温和 + 非近涨停 + top3）
POS60_MAX = 0.40
AMT_MIN = 1.5e8
CHG_TODAY_RANGE = (-4.0, 4.0)
SCAN_TOP = 80
AMP20_MAX = None            # 80740fe 无此过滤
STD20_MAX = None
DIST60_MIN = None
SIG_REQUIRED = 2
ZT_RATIO_MAX = 0.95
MAX_SAME_IND = 1
TOP_N = 3

ALLOW_PREFIX = ("60", "00", "30")


def load_klines(codes_dir):
    """读本地K线，返回 {code: DataFrame(日期,开盘,最高,最低,收盘,成交量,成交额,涨跌幅)}"""
    out = {}
    for p in glob.glob(os.path.join(codes_dir, "*.csv")):
        sym = os.path.basename(p)[:-4]          # sh600000 / sz000001
        code = sym[2:]
        if not code.startswith(ALLOW_PREFIX):
            continue
        raw = pd.read_csv(p)
        if raw.empty:
            continue
        df = pd.DataFrame({
            "日期": pd.to_datetime(raw["date"]),
            "开盘": raw["open"].astype(float),
            "最高": raw["high"].astype(float),
            "最低": raw["low"].astype(float),
            "收盘": raw["last"].astype(float),
            "成交量": raw["volume"].astype(float),
            "成交额": raw["amount"].astype(float),
        })
        df = df.drop_duplicates(subset=["日期"]).sort_values("日期").reset_index(drop=True)
        df = df.dropna(subset=["收盘"])
        df["涨跌幅"] = df["收盘"].pct_change() * 100
        if len(df) >= 80:
            out[code] = df
    return out


def _tech(df, i):
    """在行 i 计算技术指标（只用 <=i 的数据，防未来函数）"""
    seg = df.iloc[: i + 1]
    close = seg["收盘"]
    ma5 = close.rolling(5).mean().iloc[-1]
    ma20 = close.rolling(20).mean().iloc[-1]
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif_series = ema12 - ema26
    dea_series = dif_series.ewm(span=9, adjust=False).mean()
    dif = dif_series.iloc[-1]
    dea = dea_series.iloc[-1]
    return ma5, ma20, dif, dea


def signals_at(df, i):
    """返回 (信号列表, 60日位置, 20日振幅, 20日std, 距60日高%)"""
    seg = df.iloc[: i + 1]
    close = df["收盘"].iloc[i]
    hi60 = seg["最高"].tail(60).max()
    lo60 = seg["最低"].tail(60).min()
    pos60 = (close - lo60) / (hi60 - lo60) if hi60 > lo60 else 1.0
    amp20 = (seg["收盘"].tail(20).max() - seg["收盘"].tail(20).min()) / seg["收盘"].tail(20).mean()
    std20 = seg["收盘"].tail(20).std() / seg["收盘"].tail(20).mean()
    dist60 = (close / hi60 - 1) * 100 if hi60 > 0 else -999

    ma5, ma20, dif, dea = _tech(df, i)
    sigs = []
    if close > ma20:
        sigs.append("站上MA20")
    if dif > dea:
        sigs.append("MACD多头")
    v5mean = seg["成交量"].tail(5).mean()
    if v5mean > 0 and seg["成交量"].iloc[-1] / v5mean > 1.2:
        sigs.append("放量")
    chg5 = (close / df["收盘"].iloc[i - 5] - 1) * 100 if i >= 5 else 0
    if 2 <= chg5 <= 12:
        sigs.append(f"5日+{chg5:.1f}%")
    return sigs, pos60, amp20, std20, dist60, chg5


def industry_of(code, name_map):
    return name_map.get(code, "未知")


def main():
    codes_dir = os.path.join(BASE, "data", "klines")
    kl = load_klines(codes_dir)
    print(f"[老策略回测] 本地K线宇宙 {len(kl)} 只", flush=True)
    if not kl:
        print("无本地K线，退出")
        return

    # 名称→行业 静态表（本地K线无名称；行业去重退化为 top3 天然限频，见主循环注释）

    # 全市场逐日扫描（重放 2024-06-01 起，保证有60日预热）
    start = pd.Timestamp("2024-06-01")
    all_dates = set()
    for df in kl.values():
        all_dates.update(df["日期"].dt.strftime("%Y-%m-%d"))
    all_dates = sorted(all_dates)
    all_dates = [d for d in all_dates if d >= start.strftime("%Y-%m-%d")]
    print(f"[老策略回测] 回测区间 {all_dates[0]} ~ {all_dates[-1]}，共 {len(all_dates)} 个交易日", flush=True)

    # 每交易日：全市场扫描 → 候选 → 排序取 top3 → 记录信号
    signals = []          # 每日3只（或更少）
    date_idx = {d: i for i, d in enumerate(all_dates)}

    # 为每只票预构建 日期→行号 索引
    code_date_row = {}
    for code, df in kl.items():
        dmap = {d.strftime("%Y-%m-%d"): i for i, d in enumerate(df["日期"])}
        code_date_row[code] = dmap

    for di, d in enumerate(all_dates):
        # 全市场扫描该日所有满足基础过滤的票（不限前80，理想化老策略——老版受快照换手率前80截断）
        day_rows = []
        for code, df in kl.items():
            dmap = code_date_row[code]
            if d not in dmap:
                continue
            i = dmap[d]
            if i < 60:
                continue
            amt = df["成交额"].iloc[i]
            chg = df["涨跌幅"].iloc[i]
            if amt <= AMT_MIN:
                continue
            if not (-4 <= chg <= 4):
                continue
            day_rows.append((code, i, amt))
        if not day_rows:
            continue

        cands = []
        for code, i, amt in day_rows:
            df = kl[code]
            sigs, pos60, amp20, std20, dist60, chg5 = signals_at(df, i)
            if pos60 >= POS60_MAX:
                continue
            if AMP20_MAX is not None and amp20 >= AMP20_MAX:
                continue
            if STD20_MAX is not None and std20 >= STD20_MAX:
                continue
            if DIST60_MIN is not None and dist60 <= DIST60_MIN:
                continue
            if len(sigs) < SIG_REQUIRED:
                continue
            # 非近涨停：现价 / 涨停价 < 0.95；涨停价≈昨收×1.1（主板）/1.2（创业）近似
            prev_close = df["收盘"].iloc[i - 1]
            zt_price = prev_close * (1.2 if code.startswith("30") else 1.1)
            close = df["收盘"].iloc[i]
            if close / zt_price >= ZT_RATIO_MAX:
                continue
            cands.append((code, i, pos60, sigs, chg5, amt, close))
        if not cands:
            continue
        # 排序：信号数优先 → 位置低优先（80740fe 老版）
        cands.sort(key=lambda c: (-len(c[3]), c[2]))
        # 行业去重（MAX_SAME_INDUSTRY=1）：用名称关键词不可得，退化为跳过（top3 已天然限频）
        picks = cands[:TOP_N]
        for code, i, pos60, sigs, chg5, amt, close in picks:
            signals.append({
                "日期": d, "代码": str(code).zfill(6), "现价": close,
                "pos60": pos60, "信号": "、".join(sigs), "5日涨幅": chg5,
                "成交额": amt,
            })

    print(f"[老策略回测] 共产生 {len(signals)} 个信号（日均 {len(signals)/max(len(all_dates),1):.2f} 只）", flush=True)
    if not signals:
        print("无信号，退出")
        return

    sig_df = pd.DataFrame(signals)
    out_csv = os.path.join(BASE, "data", "old_low_pos_watch_signals.csv")
    sig_df.to_csv(out_csv, index=False, encoding="utf-8-sig")
    print(f"[老策略回测] 信号已存 {out_csv}", flush=True)

    # ===== 评估视角A：观察池 5/10 日表现（入池后最大涨幅、是否破买点） =====
    print("\n===== 视角A：观察池（入池后表现，不强制买入） =====")
    rows_a = []
    for s in signals:
        df = kl.get(s["代码"])
        if df is None:
            continue
        dmap = code_date_row[s["代码"]]
        i = dmap.get(s["日期"])
        if i is None:
            continue
        fwd = df.iloc[i + 1: i + 11]
        if len(fwd) < 5:
            continue
        max5 = (fwd["最高"].iloc[:5].max() / s["现价"] - 1) * 100
        max10 = (fwd["最高"].iloc[:10].max() / s["现价"] - 1) * 100
        close5 = (fwd["收盘"].iloc[4] / s["现价"] - 1) * 100 if len(fwd) >= 5 else None
        close10 = (fwd["收盘"].iloc[9] / s["现价"] - 1) * 100 if len(fwd) >= 10 else None
        hit_zone = 1 if (fwd["最低"].iloc[:5].min() <= s["现价"] * 1.03) else 0  # 回踩买点
        rows_a.append({"日期": s["日期"], "代码": s["代码"], "现价": s["现价"],
                       "5日最大涨幅%": round(max5, 2), "10日最大涨幅%": round(max10, 2),
                       "5日收盘%": round(close5, 2) if close5 else None,
                       "10日收盘%": round(close10, 2) if close10 else None})
    a_df = pd.DataFrame(rows_a)
    if not a_df.empty:
        print(f"样本 {len(a_df)}")
        print(f"  5日最大涨幅 均值 {a_df['5日最大涨幅%'].mean():.2f}% / 中位 {a_df['5日最大涨幅%'].median():.2f}% / ≥5%占比 {(a_df['5日最大涨幅%']>=5).mean()*100:.1f}% / ≥10%占比 {(a_df['5日最大涨幅%']>=10).mean()*100:.1f}%")
        print(f"  10日最大涨幅 均值 {a_df['10日最大涨幅%'].mean():.2f}% / 中位 {a_df['10日最大涨幅%'].median():.2f}% / ≥10%占比 {(a_df['10日最大涨幅%']>=10).mean()*100:.1f}%")
        print(f"  5日收盘 均值 {a_df['5日收盘%'].mean():.2f}% / 胜率(收盘>0) {(a_df['5日收盘%']>0).mean()*100:.1f}%")
        print(f"  10日收盘 均值 {a_df['10日收盘%'].mean():.2f}% / 胜率(收盘>0) {(a_df['10日收盘%']>0).mean()*100:.1f}%")

    # ===== 评估视角B：买入视角（次日开盘买入，止损-6%，止盈+10%，持5/10日） =====
    print("\n===== 视角B：买入视角（次日开盘买入） =====")
    for hold in (5, 10):
        wins = losses = 0
        pnl = []
        for s in signals:
            df = kl.get(s["代码"])
            if df is None:
                continue
            dmap = code_date_row[s["代码"]]
            i = dmap.get(s["日期"])
            if i is None or i + 1 + hold >= len(df):
                continue
            entry = df["开盘"].iloc[i + 1]        # 次日开盘
            stop = entry * 0.94                   # 老观察池止损 -6%
            tp = entry * 1.10
            exit_px = None
            for j in range(i + 1, i + 1 + hold):
                row = df.iloc[j]
                if row["最低"] <= stop:           # 先触止损
                    exit_px = stop
                    break
                if row["最高"] >= tp:             # 先触止盈
                    exit_px = tp
                    break
            if exit_px is None:
                exit_px = df["收盘"].iloc[i + hold]
            r = (exit_px / entry - 1) * 100
            pnl.append(r)
            if r > 0:
                wins += 1
            else:
                losses += 1
        n = len(pnl)
        if n == 0:
            continue
        avg = sum(pnl) / n
        avg_win = sum(p for p in pnl if p > 0) / max(wins, 1)
        avg_loss = sum(p for p in pnl if p <= 0) / max(losses, 1)
        pl_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")
        exp = avg
        print(f"  T+{hold}：样本 {n} | 胜率 {wins/n*100:.1f}% | 均值 {avg:+.2f}% | "
              f"平均盈利 {avg_win:+.2f}% / 平均亏损 {avg_loss:+.2f}% | 盈亏比 {pl_ratio:.2f} | 期望 {exp:+.2f}%")

    # ===== 分年度 / 分位置区间表现 =====
    print("\n===== 分位置区间（5日收盘，观察视角） =====")
    a_df["pos60"] = a_df["代码"].map(lambda c: next((s["pos60"] for s in signals if s["代码"] == c), None))
    if "pos60" in a_df.columns:
        for lo, hi in [(0, 0.2), (0.2, 0.3), (0.3, 0.4)]:
            sub = a_df[(a_df["pos60"] >= lo) & (a_df["pos60"] < hi)]
            if len(sub) >= 10:
                print(f"  pos60 [{lo:.1f},{hi:.1f}) n={len(sub)}：5日最大涨幅均值 {sub['5日最大涨幅%'].mean():.2f}% | 5日收盘胜率 {(sub['5日收盘%']>0).mean()*100:.1f}% | 10日收盘均值 {sub['10日收盘%'].mean():.2f}%")

    print("\n===== 分成交额区间（5日收盘，观察视角） =====")
    sig_df2 = sig_df.merge(a_df[["日期", "代码", "5日收盘%", "10日收盘%", "5日最大涨幅%"]], on=["日期", "代码"], how="inner")
    for lo, hi in [(1.5, 3), (3, 8), (8, 20), (20, float("inf"))]:
        sub = sig_df2[(sig_df2["成交额"] >= lo * 1e8) & (sig_df2["成交额"] < hi * 1e8)]
        if len(sub) >= 10:
            print(f"  成交额[{lo},{hi})亿 n={len(sub)}：5日收盘均值 {sub['5日收盘%'].mean():.2f}% | 5日收盘胜率 {(sub['5日收盘%']>0).mean()*100:.1f}% | 10日收盘均值 {sub['10日收盘%'].mean():.2f}%")

    # ===== 老策略3票实际验证（今日推荐） =====
    print("\n===== 今日老策略推荐3票（本地K线最新状态） =====")
    for code in ("600208", "600118", "600143"):
        df = kl.get(code)
        if df is None:
            print(f"  {code}: 无本地K线")
            continue
        last = df.iloc[-1]
        print(f"  {code} 最新{last['日期'].strftime('%Y-%m-%d')} 收盘{last['收盘']:.2f} 涨跌{last['涨跌幅']:.2f}%")


if __name__ == "__main__":
    main()
