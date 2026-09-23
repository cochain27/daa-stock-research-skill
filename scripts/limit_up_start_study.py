# -*- coding: utf-8 -*-
"""「连续涨停启动」模式专项回测（读本地 data/klines 前复权缓存，无网络）。

研究问题：
  亚盛集团(600108) 09-03~08 连续4天涨停启动，但策略从未推荐——候选池进过、触发被挡。
  盲区假设：连续涨停启动票，其「启动日」量比爆表(>7x) 或 涨幅超上限(>15%)，被当前
  触发规则「量比2-7x + 涨幅9-15%」系统性误杀。

方法：
  1) 识别「连续涨停启动」：T 日涨幅≥9.5%(涨停)，且 T-1 或 T-2 也≥9.5% → 连续板启动。
     「启动日」= 连续涨停段的首个涨停日。
  2) 统计启动日的量比/涨幅/成交额/60日位置/距高点，对照当前触发规则，
     量化「被量比>7x 过滤」「被涨幅>15% 过滤」的比例。
  3) 对「被量比上限挡掉的连续板票」，回测 T+3/T+5/T+7 收益与 8%止损触发，
     回答「放宽上限能否抓到、会不会引入大亏」。

数据源：data/klines/*.csv（字段 symbol,date,open,last,high,low,volume,amount,exchange）
"""
import sys
from pathlib import Path
from glob import glob

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

BASE = Path(__file__).resolve().parent.parent / "data" / "klines"

# 当前策略触发规则（config.py 固化值）
LB_LO, LB_HI = 2.0, 7.0          # 量比 2-7x
CHG_LO, CHG_HI = 9.0, 15.0       # 涨幅 9-15%（超卖路径 9-15，标准路径 10-15）
AMT_LO, AMT_HI = 4e8, 16e8       # 成交额 4-16亿
POS60_MAX = 0.65                 # 位置 <65%（超卖路径上限）
LIMIT_UP_10 = 9.5                # 10% 涨停判定阈值（前复权 pct_change 可能略低于10）
LIMIT_UP_20 = 19.0               # 20cm 涨停判定阈值


def _load(code):
    f = BASE / f"{code}.csv"
    if not f.exists():
        return None
    df = pd.read_csv(f)
    if len(df) < 70:
        return None
    df = df.copy()
    df["日期"] = pd.to_datetime(df["date"])
    df = df.sort_values("日期").reset_index(drop=True)
    df["涨跌幅"] = df["last"].pct_change() * 100
    # 量比 = 当日量 / 前5日均量（不含当日）
    df["v5"] = df["volume"].shift(1).rolling(5).mean()
    df["lb"] = df["volume"] / df["v5"]
    # 60日位置 / 距高点
    df["hi60"] = df["high"].rolling(60, min_periods=40).max()
    df["lo60"] = df["low"].rolling(60, min_periods=40).min()
    df["pos60"] = (df["last"] - df["lo60"]) / (df["hi60"] - df["lo60"])
    df["dist60"] = (df["last"] / df["hi60"] - 1) * 100
    df["_amt"] = df["amount"]  # 本地 amount 已是元
    return df


def _is_limit_up(chg, code):
    """按代码判断涨停阈值（6/9 开头主板10cm，其余20cm；ST 已在前置剔除范围）。"""
    th = LIMIT_UP_20 if code.startswith(("3", "68")) else LIMIT_UP_10
    return chg >= th


def main():
    files = glob(str(BASE / "*.csv"))
    codes = [Path(f).stem for f in files]
    print(f"扫描 {len(codes)} 只票 ...", flush=True)

    starts = []  # 连续涨停启动记录
    for code in codes:
        df = _load(code)
        if df is None:
            continue
        n = len(df)
        for i in range(2, n):  # 需要 i-1, i-2 可回看
            chg_i = df["涨跌幅"].iloc[i]
            if not _is_limit_up(chg_i, code):
                continue
            # 前一日或前两日也是涨停 → 连续板
            chg_1 = df["涨跌幅"].iloc[i - 1]
            chg_2 = df["涨跌幅"].iloc[i - 2] if i >= 2 else -99
            if not (_is_limit_up(chg_1, code) or _is_limit_up(chg_2, code)):
                continue
            # 定位连续涨停段的「启动日」= 往前找第一个非涨停日
            s = i
            while s - 1 >= 0 and _is_limit_up(df["涨跌幅"].iloc[s - 1], code):
                s -= 1
            start = s  # 首个涨停日
            if start < 5:  # 启动日之前需有足够历史算量比/位置
                continue
            row = df.iloc[start]
            # 启动日自身是否满足触发规则
            lb = row["lb"]
            chg = row["涨跌幅"]
            amt = row["_amt"]
            pos60 = row["pos60"]
            # 判定被哪条规则挡
            blocked_by_lb = (lb > LB_HI)          # 量比超上限（动能衰竭误杀）
            blocked_by_chg = (chg > CHG_HI)        # 涨幅超上限（>15%过热）
            blocked_by_amt = not (AMT_LO <= amt <= AMT_HI)
            blocked_by_pos = (pd.notna(pos60) and pos60 >= POS60_MAX)
            # 当前规则能否触发
            passed = (LB_LO <= lb <= LB_HI and CHG_LO <= chg <= CHG_HI
                      and AMT_LO <= amt <= AMT_HI
                      and (pd.notna(pos60) and pos60 < POS60_MAX))
            # 后续收益：启动日收盘入场，T+3/T+5/T+7
            entry = row["last"]
            fwd = {}
            for h in (3, 5, 7):
                j = start + h
                if j < n:
                    fwd[f"r{h}"] = (df["last"].iloc[j] / entry - 1) * 100
                else:
                    fwd[f"r{h}"] = None
            # 8% 止损：启动日后 T+1..T+7 是否收盘跌破 entry*0.92
            stop = False
            for k in range(start + 1, min(start + 8, n)):
                if df["last"].iloc[k] < entry * 0.92:
                    stop = True
                    break
            starts.append({
                "code": code, "start_date": str(row["日期"].date()),
                "lb": round(lb, 2) if pd.notna(lb) else None,
                "chg": round(chg, 2), "amt_yi": round(amt / 1e8, 1),
                "pos60": round(pos60, 3) if pd.notna(pos60) else None,
                "dist60": round(row["dist60"], 1) if pd.notna(row["dist60"]) else None,
                "passed": passed,
                "blocked_by_lb": blocked_by_lb,
                "blocked_by_chg": blocked_by_chg,
                "blocked_by_amt": blocked_by_amt,
                "blocked_by_pos": blocked_by_pos,
                "r3": fwd["r3"], "r5": fwd["r5"], "r7": fwd["r7"],
                "stop8": stop,
                "entry": round(entry, 2),
            })

    if not starts:
        print("未识别到连续涨停启动票。")
        return

    sdf = pd.DataFrame(starts).drop_duplicates(subset=["code", "start_date"])
    sdf.to_csv(Path(__file__).resolve().parent.parent / "data" / "连续涨停启动_专项回测.csv",
               index=False, encoding="utf-8-sig")

    print(f"\n===== 连续涨停启动票总数: {len(sdf)} =====")

    # 1) 当前规则通过 vs 被挡
    n_pass = int(sdf["passed"].sum())
    n_lb = int(sdf["blocked_by_lb"].sum())
    n_chg = int(sdf["blocked_by_chg"].sum())
    n_amt = int(sdf["blocked_by_amt"].sum())
    n_pos = int(sdf["blocked_by_pos"].sum())
    print(f"\n[触发规则对照] 当前规则(量比2-7x+涨幅9-15%+额4-16亿+位置<65%)")
    print(f"  可通过触发: {n_pass} ({n_pass/len(sdf):.0%})")
    print(f"  被量比>7x挡: {n_lb} ({n_lb/len(sdf):.0%})  <- 动能衰竭误杀盲区")
    print(f"  被涨幅>15%挡: {n_chg} ({n_chg/len(sdf):.0%})")
    print(f"  被成交额挡: {n_amt} ({n_amt/len(sdf):.0%})")
    print(f"  被位置挡: {n_pos} ({n_pos/len(sdf):.0%})")

    # 2) 全样本 T+3/5/7 收益
    print(f"\n[全样本收益] (启动日收盘入场)")
    for h in (3, 5, 7):
        col = f"r{h}"
        v = sdf[col].dropna()
        if len(v) == 0:
            continue
        print(f"  T+{h}: 均值{v.mean():+.1f}%  中位{v.median():+.1f}%  "
              f"胜率{(v>0).mean():.0%}  大亏(<-8%){(v<-8).mean():.0%}  样本{len(v)}")

    # 3) 被量比>7x挡掉的子集（关键：放宽上限能否抓到、会不会大亏）
    sub = sdf[sdf["blocked_by_lb"]]
    print(f"\n[被量比>7x挡掉的连续板票] 共 {len(sub)} 只")
    if len(sub):
        for h in (3, 5, 7):
            col = f"r{h}"
            v = sub[col].dropna()
            if len(v) == 0:
                continue
            print(f"  T+{h}: 均值{v.mean():+.1f}%  中位{v.median():+.1f}%  "
                  f"胜率{(v>0).mean():.0%}  大亏(<-8%){(v<-8).mean():.0%}  样本{len(v)}")
        stop_r = sub["stop8"].mean()
        print(f"  8%止损触发率: {stop_r:.0%}")
        print(f"\n  明细(按start_date倒序, top 25):")
        cols = ["code", "start_date", "lb", "chg", "amt_yi", "pos60", "r5", "r7", "stop8"]
        print(sub.sort_values("start_date", ascending=False)[cols].head(25).to_string(index=False))

    # 4) 对照：当前规则能通过的子集收益
    sub_pass = sdf[sdf["passed"]]
    print(f"\n[当前规则能通过的连续板票] 共 {len(sub_pass)} 只")
    if len(sub_pass):
        for h in (5, 7):
            col = f"r{h}"
            v = sub_pass[col].dropna()
            if len(v) == 0:
                continue
            print(f"  T+{h}: 均值{v.mean():+.1f}%  胜率{(v>0).mean():.0%}  样本{len(v)}")

    print(f"\nCSV 已存: data/连续涨停启动_专项回测.csv")


if __name__ == "__main__":
    main()
