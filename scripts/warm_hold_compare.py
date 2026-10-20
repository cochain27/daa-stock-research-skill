# -*- coding: utf-8 -*-
"""初动池持有周期对比回测：同一批信号，T+5 vs T+10 vs T+20（含 -8% 止损版）。
读本地 data/klines 前复权缓存，无网络。复用 warm_start_backtest 的画像与信号收集逻辑。
口径：候选日 T 收盘确认 → 次日开盘买入（一字板跳过）→ 到期收盘卖出；止损版：任意收盘 < 入场×0.92 → -8% 出场。
附加统计：T+5 到期后继续持有 5 天的增量收益分布（回答"T+5 会不会砍掉后续涨幅"）。
"""
import sys
from pathlib import Path
from glob import glob

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import numpy as np

BASE = Path(__file__).resolve().parent.parent / "data" / "klines"

LB_LO, LB_HI = 1.3, 2.5
CHG5_LO, CHG5_HI = 3.0, 8.0
POS60_MAX = 0.80
CHG_TODAY_MAX = 9.5
STOP_PCT = 0.92
AMP_TODAY_MAX = 7.5
HORIZONS = [5, 10, 20]   # T+N 收盘离场

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
    df["chg5"] = df["last"].pct_change(5) * 100
    df["v5"] = df["volume"].shift(1).rolling(5).mean()
    df["lb"] = df["volume"] / df["v5"]
    df["hi60"] = df["high"].rolling(60, min_periods=40).max()
    df["lo60"] = df["low"].rolling(60, min_periods=40).min()
    df["pos60"] = (df["last"] - df["lo60"]) / (df["hi60"] - df["lo60"])
    df["ma20"] = df["last"].rolling(20).mean()
    ema12 = df["last"].ewm(span=12, adjust=False).mean()
    ema26 = df["last"].ewm(span=26, adjust=False).mean()
    df["dif"] = ema12 - ema26
    df["dea"] = df["dif"].ewm(span=9, adjust=False).mean()
    df["prev_close"] = df["last"].shift(1)
    df["振幅"] = (df["high"] - df["low"]) / df["prev_close"] * 100
    return df

def _signal_mask(df):
    m = (
        df["lb"].between(LB_LO, LB_HI)
        & df["chg5"].between(CHG5_LO, CHG5_HI)
        & (df["pos60"] < POS60_MAX)
        & (df["last"] > df["ma20"])
        & (df["dif"] > df["dea"])
        & (df["涨跌幅"] <= CHG_TODAY_MAX)
        & (df["涨跌幅"] > -9.5)
        & df["ma20"].notna() & df["lb"].notna()
    )
    if AMP_TODAY_MAX:
        m &= df["振幅"].notna() & (df["振幅"] <= AMP_TODAY_MAX)
    return m

def _collect_signals(df):
    m = _signal_mask(df)
    idx = df.index[m].tolist()
    last_i = -99
    out = []
    for i in idx:
        if i - last_i < 10:
            continue
        last_i = i
        out.append(i)
    return out

def _ma5_trail(df, i, max_hold):
    """生产规则口径：次日开盘入场 → T+2 收盘起破MA5 → 次日开盘离场；-8% 止损；无固定止盈；最长 max_hold 收盘离场。
    返回收益%（含止损版），若 max_hold 内既未破MA5也未止损则到期离场。"""
    nxt = df.iloc[i + 1] if i + 1 < len(df) else None
    if nxt is None:
        return None
    if nxt["open"] == nxt["high"] == nxt["low"] == nxt["last"]:
        return None
    entry = nxt["open"]
    if entry <= 0:
        return None
    df["ma5"] = df["last"].rolling(5).mean()
    exit_i = None
    why = "到期"
    for d in range(1, max_hold + 1):
        j = i + d
        if j >= len(df):
            exit_i = len(df) - 1
            why = "数据不足"
            break
        row = df.iloc[j]
        if row["last"] <= entry * STOP_PCT:      # -8% 止损，收盘破即出
            exit_i = j
            why = "止损"
            break
        if d >= 2 and row["last"] < row["ma5"]:  # T+2 起破MA5 → 次日开盘离场
            exit_i = min(j + 1, len(df) - 1)
            why = "破MA5"
            break
    if exit_i is None:
        exit_i = min(i + max_hold, len(df) - 1)
        why = "到期"
    exit_r = df.iloc[exit_i]["last"] / entry - 1
    return {"r": exit_r * 100, "why": why}

def _multi_horizon(df, i):
    """次日开盘入场（一字板跳过），返回各 horizon 的 (裸收益%, 止损版收益%, 是否止损, 区间最大%)。"""
    nxt = df.iloc[i + 1] if i + 1 < len(df) else None
    if nxt is None:
        return None
    if nxt["open"] == nxt["high"] == nxt["low"] == nxt["last"]:
        return None
    entry = nxt["open"]
    if entry <= 0:
        return None
    res = {}
    for h in HORIZONS:
        end_i = min(i + h, len(df) - 1)
        exit_r = df.iloc[end_i]["last"] / entry - 1
        seg = df.iloc[i + 1:end_i + 1]
        min_low = seg["low"].min()
        stop_hit = min_low <= entry * STOP_PCT
        stop_r = STOP_PCT - 1 if stop_hit else exit_r
        max_r = seg["high"].max() / entry - 1
        res[h] = {"r": exit_r * 100, "r_stop": stop_r * 100, "stop": stop_hit, "max": max_r * 100}
    return res

def main():
    files = glob(str(BASE / "*.csv"))
    codes = sorted(Path(f).stem for f in files)
    print(f"扫描 {len(codes)} 只票 ...", flush=True)
    recs = []
    for code in codes:
        df = _load(code)
        if df is None:
            continue
        for i in _collect_signals(df):
            res = _multi_horizon(df, i)
            if res is None:
                continue
            ma = {h: _ma5_trail(df, i, h) for h in HORIZONS}
            recs.append({
                "code": code,
                "date": str(pd.Timestamp(df.iloc[i]["日期"]).date()),
                "amt亿": round(df.iloc[i]["amount"] / 1e8, 1),
                "entry": res[5]["max"] and round(df.iloc[i + 1]["open"], 2),
                **{f"r{h}": round(res[h]["r"], 2) for h in HORIZONS},
                **{f"rs{h}": round(res[h]["r_stop"], 2) for h in HORIZONS},
                **{f"stop{h}": res[h]["stop"] for h in HORIZONS},
                **{f"max{h}": round(res[h]["max"], 2) for h in HORIZONS},
                **{f"ma{h}": round(ma[h]["r"], 2) if ma[h] else None for h in HORIZONS},
                **{f"why{h}": ma[h]["why"] if ma[h] else None for h in HORIZONS},
            })
    if not recs:
        print("无信号")
        return
    df = pd.DataFrame(recs)
    # 全量（含成交额>16亿）与 ≤16亿 两档
    for amt_filter, name in [(None, "全部"), (16.0, "≤16亿")]:
        d = df if amt_filter is None else df[df["amt亿"] <= amt_filter]
        if d.empty:
            continue
        n = len(d)
        print(f"\n========== {name}（{n} 条可执行信号）==========")
        for h in HORIZONS:
            r = d[f"r{h}"].to_numpy()
            rs = d[f"rs{h}"].to_numpy()
            wins = rs[rs > 0]; losses = rs[rs < 0]
            pl = round(wins.mean() / abs(losses.mean()), 2) if len(wins) and len(losses) else None
            # T+5→T+h 增量
            inc = d[f"r{h}"] - d["r5"]
            print(f"T+{h} 裸均值 {r.mean():+.2f}% | 止损版均值 {rs.mean():+.2f}% | "
                  f"胜率 {(rs>0).mean()*100:.1f}% | 盈亏比 {pl} | 止损率 {d[f'stop{h}'].mean()*100:.1f}%")
            if h > 5:
                print(f"    vs T+5 增量: 均值 {inc.mean():+.2f}% | 中位 {inc.median():+.2f}% | "
                      f"增量>0占比 {(inc>0).mean()*100:.1f}% | 增量≥+10%占比 {(inc>=10).mean()*100:.1f}% | 增量<-5%占比 {(inc<=-5).mean()*100:.1f}%")
    # 关键统计：T+5 到期后被"砍掉"的后续涨幅
    d = df[df["amt亿"] <= 16.0] if (df["amt亿"] <= 16.0).sum() else df
    r5 = d["r5"]; r10 = d["r10"]; r20 = d["r20"]
    print("\n========== T+5 到期后走势（≤16亿档）==========")
    print(f"T+5 为正但 T+10 更高的票: {(r10 > r5).sum()}/{len(d)} = {(r10 > r5).mean()*100:.1f}%")
    print(f"T+5 为正但 T+20 更高的票: {(r20 > r5).sum()}/{len(d)} = {(r20 > r5).mean()*100:.1f}%")
    print(f"T+5 为负但 T+20 转正的票: {int((r5 < 0).sum())} 个（其中 T+20 转正 {int((r20[r5<0] > 0).sum())} 个）")
    print(f"T+5 已盈≥10% 的票，T+20 继续涨到 ≥20%: {int((r5>=10).sum())} 个 → {int((r20[r5>=10]>=20).sum())} 个")
    # 生产规则口径（破MA5移动止盈）对比
    print("\n========== 生产规则口径（次日开盘入 + T+2起破MA5次日开盘离场 + -8%止损 + 最长T+N）==========")
    for amt_filter, name in [(None, "全部"), (16.0, "≤16亿")]:
        d2 = df if amt_filter is None else df[df["amt亿"] <= amt_filter]
        if d2.empty:
            continue
        print(f"[{name} {len(d2)} 条]")
        for h in HORIZONS:
            ma = d2[f"ma{h}"].to_numpy()
            why = d2[f"why{h}"]
            print(f"  T+{h}: 均值 {ma.mean():+.2f}% | 胜率 {(ma>0).mean()*100:.1f}% | "
                  f"破MA5率 {(why=='破MA5').mean()*100:.1f}% | 止损率 {(why=='止损').mean()*100:.1f}% | 到期率 {(why=='到期').mean()*100:.1f}%")
    # 被 T+5 到期"砍掉"的票：延长到 T+10 是赚是亏
    d2 = df[df["amt亿"] <= 16.0] if (df["amt亿"] <= 16.0).sum() else df
    exp5 = d2[d2["why5"] == "到期"]
    if len(exp5):
        ma5_v = exp5["ma5"].to_numpy(); ma10_v = exp5["ma10"].to_numpy()
        print(f"\n========== T+5 到期票画像（≤16亿，{len(exp5)} 条）==========")
        print(f"到期时收益：均值 {ma5_v.mean():+.2f}% | 为正占比 {(ma5_v>0).mean()*100:.1f}% | ≥+10%占比 {(ma5_v>=10).mean()*100:.1f}% | ≤-5%占比 {(ma5_v<=-5).mean()*100:.1f}%")
        print(f"延长至 T+10（破MA5/止损仍生效）：均值 {ma10_v.mean():+.2f}% | 胜率 {(ma10_v>0).mean()*100:.1f}%")
        print(f"  增量：均值 {ma10_v.mean()-ma5_v.mean():+.2f}pp | 增量>0占比 {(ma10_v>ma5_v).mean()*100:.1f}% | 增量≥+10%占比 {(ma10_v-ma5_v>=10).mean()*100:.1f}%")
    out = Path(__file__).resolve().parent.parent / "data" / "warm_hold_compare.csv"
    df.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"\n明细已存 {out}（{len(df)} 条）")

if __name__ == "__main__":
    main()
