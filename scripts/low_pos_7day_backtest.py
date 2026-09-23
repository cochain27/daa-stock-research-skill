# -*- coding: utf-8 -*-
"""低位启动池 7 日拉升概率/收益回测（读本地 data/klines 前复权缓存，无网络请求）。

口径（与 low_pos_entry.py 线上策略一致，防未来函数）：
  - T-1 收盘扫描「蓄势」：位置/量比/横盘/成交额等判据（复用 config 参数）
  - T 日触发：量比 2-7x + 涨幅 9-15% + 成交额 4-16 亿 + 位置<55%(标准)/<65%(超卖)
  - 入场价 = 触发日(T)收盘价
  - 7 日窗口 = T+1 .. T+7 收盘，统计：
      · T1/T3/T5/T7 收益（均值/中位/胜率/最好/最差）
      · 7 日内最大涨幅（max close / entry - 1）
      · 拉升概率：7 日内最大涨幅 >= 阈值（0% / 5% / 10% / 15%）的占比
      · 8% 止损触发比例（7 日窗口内收盘跌破入场价×0.92）

数据源：data/klines/*.csv（WestockData 前复权），字段 symbol,date,open,last,high,low,volume,amount,exchange
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

from config import (
    LOW_POS_ENTRY_MAX_POS60, LOW_POS_ENTRY_MIN_DIST60,
    LOW_POS_ENTRY_MAX_LB5, LOW_POS_ENTRY_MAX_LB5_MEAN, LOW_POS_ENTRY_CHG5_RANGE,
    LOW_POS_ENTRY_MAX_CHG_TODAY, LOW_POS_ENTRY_MAX_AMP20,
    LOW_POS_ENTRY_MIN_AMOUNT, LOW_POS_ENTRY_MAX_AMOUNT,
    LOW_POS_ENTRY_TRIGGER_LB, LOW_POS_ENTRY_TRIGGER_LB_MAX,
    LOW_POS_ENTRY_TRIGGER_CHG_MIN, LOW_POS_ENTRY_TRIGGER_CHG_MAX,
    LOW_POS_ENTRY_TRIGGER_BREAK_MA20,
    LOW_POS_ENTRY_TRIGGER_MIN_AMOUNT, LOW_POS_ENTRY_TRIGGER_MAX_AMOUNT,
    LOW_POS_ENTRY_RECENT_OVERSOLD_LOOKBACK,
    LOW_POS_ENTRY_RECENT_OVERSOLD_POS60_MAX, LOW_POS_ENTRY_RECENT_OVERSOLD_DIST_MAX,
    LOW_POS_ENTRY_TEMP_MIN, LOW_POS_ENTRY_BOARD_RESONANCE,
    LOW_POS_ENTRY_STOP_LOSS,
    LOW_POS_ENTRY_OS_TRIGGER_LB, LOW_POS_ENTRY_OS_TRIGGER_LB_MAX,
    LOW_POS_ENTRY_OS_TRIGGER_CHG_MIN, LOW_POS_ENTRY_OS_TRIGGER_CHG_MAX,
    LOW_POS_ENTRY_OS_TRIGGER_MIN_AMOUNT, LOW_POS_ENTRY_OS_TRIGGER_MAX_AMOUNT,
    LOW_POS_ENTRY_OS_TRIGGER_BREAK_MA20, LOW_POS_ENTRY_OS_STOP_LOSS,
)

# 触发/止损参数按路径分流（标准蓄势用 TRIGGER 同族，近期超卖用固化 OS_*；两路径彻底解耦）
PATH_PARAMS = {
    "标准蓄势": {
        "lb_lo": LOW_POS_ENTRY_TRIGGER_LB,
        "lb_hi": LOW_POS_ENTRY_TRIGGER_LB_MAX,
        "chg_lo": LOW_POS_ENTRY_TRIGGER_CHG_MIN,
        "chg_hi": LOW_POS_ENTRY_TRIGGER_CHG_MAX,
        "amt_lo": LOW_POS_ENTRY_TRIGGER_MIN_AMOUNT,
        "amt_hi": LOW_POS_ENTRY_TRIGGER_MAX_AMOUNT,
        "break_ma20": LOW_POS_ENTRY_TRIGGER_BREAK_MA20,
        "pos60_max": LOW_POS_ENTRY_MAX_POS60,
        "stop_loss": LOW_POS_ENTRY_STOP_LOSS,
    },
    "近期超卖": {
        "lb_lo": LOW_POS_ENTRY_OS_TRIGGER_LB,
        "lb_hi": LOW_POS_ENTRY_OS_TRIGGER_LB_MAX,
        "chg_lo": LOW_POS_ENTRY_OS_TRIGGER_CHG_MIN,
        "chg_hi": LOW_POS_ENTRY_OS_TRIGGER_CHG_MAX,
        "amt_lo": LOW_POS_ENTRY_OS_TRIGGER_MIN_AMOUNT,
        "amt_hi": LOW_POS_ENTRY_OS_TRIGGER_MAX_AMOUNT,
        "break_ma20": LOW_POS_ENTRY_OS_TRIGGER_BREAK_MA20,
        "pos60_max": LOW_POS_ENTRY_RECENT_OVERSOLD_POS60_MAX,
        "stop_loss": LOW_POS_ENTRY_OS_STOP_LOSS,
    },
}

KLINE_DIR = Path(__file__).resolve().parent.parent / "data" / "klines"
OUT_CSV = Path(__file__).resolve().parent.parent / "data" / "低位启动_7日回测.csv"


def _load_cache(code):
    sym = ("sh" if code.startswith(("6", "9")) else "sz") + code
    p = KLINE_DIR / f"{sym}.csv"
    if not p.exists():
        return code, None
    try:
        df = pd.read_csv(p, parse_dates=["date"])
        df = df.rename(columns={"last": "收盘", "open": "开盘", "high": "最高",
                                "low": "最低", "volume": "成交量", "amount": "成交额"})
        for c in ["开盘", "最高", "最低", "收盘", "成交量", "成交额"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df[["date", "开盘", "最高", "最低", "收盘", "成交量", "成交额"]].copy()
        df = df.sort_values("date").reset_index(drop=True)
        df["涨跌幅"] = df["收盘"].pct_change() * 100
        df = df.dropna(subset=["收盘"])
        if len(df) < 70:
            return code, None
        return code, df
    except Exception:
        return code, None


def _indicators(df):
    d = df.copy()
    for w in (5, 10, 20):
        d[f"MA{w}"] = d["收盘"].rolling(w).mean()
    # 量比：当日量 / 前5日均量（不含当日）
    d["v5"] = d["成交量"].shift(1).rolling(5).mean()
    d["lb"] = d["成交量"] / d["v5"]
    d["lb5mean"] = d["lb"].rolling(5).mean()
    d["hi60"] = d["最高"].rolling(60, min_periods=40).max()
    d["lo60"] = d["最低"].rolling(60, min_periods=40).min()
    d["pos60"] = (d["收盘"] - d["lo60"]) / (d["hi60"] - d["lo60"])
    d["dist60"] = (d["收盘"] / d["hi60"] - 1) * 100
    d["amp20"] = (d["收盘"].rolling(20).max() - d["收盘"].rolling(20).min()) / d["收盘"].rolling(20).mean() * 100
    d["chg5"] = (d["收盘"] / d["收盘"].shift(5) - 1) * 100
    return d


def _蓄势通过(row):
    if pd.isna(row.get("pos60")) or pd.isna(row.get("dist60")):
        return False
    if row["pos60"] >= LOW_POS_ENTRY_MAX_POS60:
        return False
    if row["dist60"] > LOW_POS_ENTRY_MIN_DIST60:
        return False
    lb = row.get("lb")
    lbm = row.get("lb5mean")
    if pd.isna(lb) or pd.isna(lbm):
        return False
    if lb > LOW_POS_ENTRY_MAX_LB5 or lbm > LOW_POS_ENTRY_MAX_LB5_MEAN:
        return False
    c5 = row.get("chg5", 0)
    lo5, hi5 = LOW_POS_ENTRY_CHG5_RANGE
    if not (lo5 <= c5 <= hi5):
        return False
    chg_today = row.get("涨跌幅", 0) or 0
    if chg_today > LOW_POS_ENTRY_MAX_CHG_TODAY:
        return False
    if row.get("amp20", 0) >= LOW_POS_ENTRY_MAX_AMP20:
        return False
    amt = row.get("成交额", 0)
    if not (LOW_POS_ENTRY_MIN_AMOUNT <= amt <= LOW_POS_ENTRY_MAX_AMOUNT):
        return False
    return True


def _recent_oversold(ind_df, cur_idx):
    lookback = LOW_POS_ENTRY_RECENT_OVERSOLD_LOOKBACK
    start = max(0, cur_idx - lookback)
    window = ind_df.iloc[start:cur_idx]
    for _, row in window.iterrows():
        p60 = row.get("pos60")
        d60 = row.get("dist60")
        if p60 is not None and p60 < 0.5:
            return True
        if d60 is not None and d60 < LOW_POS_ENTRY_RECENT_OVERSOLD_DIST_MAX:
            return True
    return False


def main():
    codes = [p.stem[2:] for p in sorted(KLINE_DIR.glob("*.csv"))]  # sh600000 -> 600000
    print(f"[1/3] 读取本地缓存 {len(codes)} 只 ...", flush=True)
    hists = {}
    for c in codes:
        code, h = _load_cache(c)
        if h is not None:
            hists[code] = h
    print(f"  K线就绪 {len(hists)} 只", flush=True)

    # 第一步：全量扫描触发候选（先不温度过滤，收集原始触发）
    print("[2/3] 扫描蓄势→触发候选 ...", flush=True)
    all_triggers = {}
    for code, h in hists.items():
        ind = _indicators(h)
        for i in range(70, len(ind) - 1):
            row = ind.iloc[i]
            ok = _蓄势通过(row)
            recent_os = _recent_oversold(ind, i + 1) if not ok else False
            if not ok and not recent_os:
                continue
            chg_today = float(row.get("涨跌幅") or 0)
            if chg_today > LOW_POS_ENTRY_MAX_CHG_TODAY:
                continue
            nxt = ind.iloc[i + 1]
            lb = float(nxt["lb"])
            chg = float(nxt["涨跌幅"])
            nxt_amt = float(nxt.get("成交额", 0))
            ma20 = float(row["MA20"])
            nxt_pos60 = float(nxt.get("pos60", 0))
            path = "标准蓄势" if ok else "近期超卖"
            p = PATH_PARAMS[path]
            triggered = (lb >= p["lb_lo"]
                         and lb <= p["lb_hi"]
                         and p["chg_lo"] <= chg <= p["chg_hi"]
                         and p["amt_lo"] <= nxt_amt <= p["amt_hi"]
                         and (not p["break_ma20"] or float(nxt["收盘"]) > ma20)
                         and nxt_pos60 < p["pos60_max"])
            if not triggered:
                continue
            trigger_date = pd.Timestamp(nxt["date"])
            entry = float(nxt["收盘"])
            all_triggers[(code, trigger_date)] = {
                "code": code, "trigger_date": trigger_date,
                "lb": round(lb, 2), "chg": round(chg, 2),
                "nxt_amt": nxt_amt, "entry": entry,
                "ind": ind, "i": i, "path": path,
                "trigger_pos60": nxt_pos60,
            }

    # 市场温度（全市场当日上涨比例）
    print("[3/3] 计算市场温度 + 7 日收益 ...", flush=True)
    date_up_ratio = {}
    for code, h in hists.items():
        ind = _indicators(h)
        for i in range(70, len(ind) - 1):
            nxt = ind.iloc[i + 1]
            td = pd.Timestamp(nxt["date"])
            if td not in date_up_ratio:
                date_up_ratio[td] = [0, 0]
            date_up_ratio[td][1] += 1
            if float(nxt["涨跌幅"]) > 0:
                date_up_ratio[td][0] += 1
    for td, v in date_up_ratio.items():
        date_up_ratio[td] = v[0] / v[1] * 100 if v[1] > 0 else 0

    rows = []
    for (code, td), info in all_triggers.items():
        temp = date_up_ratio.get(td, 0)
        # 温度过滤（最终口径）
        if LOW_POS_ENTRY_TEMP_MIN > 0 and temp < LOW_POS_ENTRY_TEMP_MIN:
            continue
        ind, i = info["ind"], info["i"]
        entry = info["entry"]
        fut = ind.iloc[i + 2:i + 9]  # T+1 .. T+7（7 个交易日）
        if fut.empty:
            continue
        closes = fut["收盘"].astype(float).tolist()
        def ret_at(k):  # k=1..7，取 T+k 收盘收益
            return (closes[k - 1] / entry - 1) * 100 if len(closes) >= k else None
        r1, r3, r5, r7 = ret_at(1), ret_at(3), ret_at(5), ret_at(7)
        max_close = max(closes)
        max_ret = (max_close / entry - 1) * 100
        max_ret_day = closes.index(max_close) + 1
        hit_sl = any(c < entry * (1 + PATH_PARAMS[info["path"]]["stop_loss"]) for c in closes)
        rows.append({
            "代码": code, "触发日": str(td.date()),
            "蓄势路径": info["path"],
            "触发量比": info["lb"], "触发涨幅%": info["chg"],
            "触发成交额亿": round(info["nxt_amt"] / 1e8, 1),
            "触发位置": round(info["trigger_pos60"], 2),
            "大盘温度": round(temp, 1),
            "入场价": round(entry, 2),
            "T1收益%": round(r1, 1) if r1 is not None else None,
            "T3收益%": round(r3, 1) if r3 is not None else None,
            "T5收益%": round(r5, 1) if r5 is not None else None,
            "T7收益%": round(r7, 1) if r7 is not None else None,
            "7日内最大涨幅%": round(max_ret, 1),
            "最大涨幅日": max_ret_day,
            "7日破止损": hit_sl,
        })

    if not rows:
        print("无触发样本（温度过滤后为空）")
        return

    df = pd.DataFrame(rows)
    df.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")

    print(f"\n=== 低位启动池 7 日回测：{len(df)} 次触发（{df['代码'].nunique()} 只票）===")
    print(f"（口径：入场=触发日收盘；温度过滤≥{LOW_POS_ENTRY_TEMP_MIN}；明细见 {OUT_CSV.name}）\n")

    # 1) 各节点收益
    print("--- T+N 收益分布 ---")
    for w, col in (("T1", "T1收益%"), ("T3", "T3收益%"), ("T5", "T5收益%"), ("T7", "T7收益%")):
        s = df[col].dropna()
        if s.empty:
            print(f"{w}: 无样本")
            continue
        win = (s > 0).mean() * 100
        print(f"{w}: 样本{s.size} 均值{s.mean():+.2f}% 中位{s.median():+.2f}% "
              f"胜率{win:.1f}% 最好{s.max():+.1f}% 最差{s.min():+.1f}%")

    # 2) 7 日内最大涨幅分布
    print("\n--- 7 日内最大涨幅分布（拉升概率核心） ---")
    mr = df["7日内最大涨幅%"]
    print(f"7日内最大涨幅：均值{mr.mean():+.2f}% 中位{mr.median():+.2f}% "
          f"最好{mr.max():+.1f}% 最差{mr.min():+.1f}%")
    for th in (0, 5, 10, 15):
        p = (mr >= th).mean() * 100
        print(f"  7日内最大涨幅 ≥ {th:+}% 的概率：{p:.1f}%（{(mr >= th).sum()}/{len(mr)}）")

    # 3) 止损触发
    sl = df["7日破止损"].mean() * 100
    print(f"\n7日内跌破 -8% 止损的比例：{sl:.1f}%")

    # 4) 分路径
    if df["蓄势路径"].nunique() > 1:
        print("\n--- 按蓄势路径分层 ---")
        for path, grp in df.groupby("蓄势路径"):
            s7 = grp["T7收益%"].dropna()
            mr_g = grp["7日内最大涨幅%"]
            big = (mr_g >= 10).mean() * 100
            win7 = (s7 > 0).mean() * 100 if len(s7) else 0
            print(f"  {path}: {len(grp)}笔  T7均值{s7.mean():+.2f}% 胜率{win7:.0f}%  "
                  f"7日最大涨幅≥10%概率{big:.0f}%")

    # 5) 大赚/大亏
    print("\n--- 大赚/大亏（7 日口径） ---")
    big_win = (mr >= 10).sum()
    big_loss = (mr <= -10).sum()
    print(f"  大赚（7日内最大涨幅≥+10%）: {big_win} 只（{big_win/len(mr)*100:.0f}%）")
    print(f"  大亏（7日内最大涨幅≤-10%）: {big_loss} 只（{big_loss/len(mr)*100:.0f}%）")

    print(f"\n明细 → {OUT_CSV}")


if __name__ == "__main__":
    main()
