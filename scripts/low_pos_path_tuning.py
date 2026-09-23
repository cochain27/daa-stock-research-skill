# -*- coding: utf-8 -*-
"""低位启动池 两条路径拆层调优研究（读本地 data/klines，无网络）。

背景：7 日回测（温度≥55 + 成交额4-16亿）下，近期超卖路径(92笔)优于标准蓄势(25笔)。
用户要求：把重心往「近期超卖」倾斜，或单独收紧「标准蓄势」入选条件。

本脚本：
  1) 固定温度 ≥55（最终口径），【不卡】成交额绝对值，只记录成交额/放量倍数
  2) 对「近期超卖」「标准蓄势」两条路径分别拆层：
     - 触发位置 pos60 分桶
     - 触发量比、触发涨幅 分桶
     - 触发成交额 分桶
     - 超卖路径：超卖深度(最极端 dist60)
     - 标准路径：蓄势位置、距60日高点 分桶
  3) 输出分路径明细 CSV，供后续进一步切片
核心指标：7日最大涨幅 ≥10% 概率（"真拉升"）、7日最大涨幅均值、≥0% 概率。
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
    LOW_POS_ENTRY_OS_TRIGGER_LB, LOW_POS_ENTRY_OS_TRIGGER_LB_MAX,
    LOW_POS_ENTRY_OS_TRIGGER_CHG_MIN, LOW_POS_ENTRY_OS_TRIGGER_CHG_MAX,
    LOW_POS_ENTRY_OS_TRIGGER_BREAK_MA20,
    LOW_POS_ENTRY_RECENT_OVERSOLD_LOOKBACK,
    LOW_POS_ENTRY_RECENT_OVERSOLD_POS60_MAX, LOW_POS_ENTRY_RECENT_OVERSOLD_DIST_MAX,
    LOW_POS_ENTRY_TEMP_MIN,
)

# 触发参数按路径分流（标准蓄势用 TRIGGER 同族，近期超卖用固化 OS_*；两路径彻底解耦）
PATH_PARAMS = {
    "标准蓄势": {
        "lb_lo": LOW_POS_ENTRY_TRIGGER_LB,
        "lb_hi": LOW_POS_ENTRY_TRIGGER_LB_MAX,
        "chg_lo": LOW_POS_ENTRY_TRIGGER_CHG_MIN,
        "chg_hi": LOW_POS_ENTRY_TRIGGER_CHG_MAX,
        "break_ma20": LOW_POS_ENTRY_TRIGGER_BREAK_MA20,
        "pos60_max": LOW_POS_ENTRY_MAX_POS60,
    },
    "近期超卖": {
        "lb_lo": LOW_POS_ENTRY_OS_TRIGGER_LB,
        "lb_hi": LOW_POS_ENTRY_OS_TRIGGER_LB_MAX,
        "chg_lo": LOW_POS_ENTRY_OS_TRIGGER_CHG_MIN,
        "chg_hi": LOW_POS_ENTRY_OS_TRIGGER_CHG_MAX,
        "break_ma20": LOW_POS_ENTRY_OS_TRIGGER_BREAK_MA20,
        "pos60_max": LOW_POS_ENTRY_RECENT_OVERSOLD_POS60_MAX,
    },
}

KLINE_DIR = Path(__file__).resolve().parent.parent / "data" / "klines"
OUT_CSV = Path(__file__).resolve().parent.parent / "data" / "低位启动_分路径拆层.csv"


def _load_cache(code):
    sym = ("sh" if code.startswith(("6", "9")) else "sz") + code
    p = KLINE_DIR / f"{sym}.csv"
    if not p.exists():
        return None
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
            return None
        return df
    except Exception:
        return None


def _indicators(df):
    d = df.copy()
    for w in (5, 10, 20):
        d[f"MA{w}"] = d["收盘"].rolling(w).mean()
    d["v5"] = d["成交量"].shift(1).rolling(5).mean()
    d["lb"] = d["成交量"] / d["v5"]
    d["lb5mean"] = d["lb"].rolling(5).mean()
    d["a5"] = d["成交额"].shift(1).rolling(5).mean()
    d["amt_ratio5"] = d["成交额"] / d["a5"]
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


def _recent_oversold_detail(ind_df, cur_idx):
    """返回 (是否超卖, 最极端dist60值, 最极端pos60值)"""
    lookback = LOW_POS_ENTRY_RECENT_OVERSOLD_LOOKBACK
    start = max(0, cur_idx - lookback)
    window = ind_df.iloc[start:cur_idx]
    best_d = None
    best_p = None
    for _, row in window.iterrows():
        p60 = row.get("pos60")
        d60 = row.get("dist60")
        if p60 is not None and p60 < 0.5:
            if best_p is None or p60 < best_p:
                best_p = p60
            return True, float(d60) if d60 is not None else None, float(p60)
        if d60 is not None and d60 < LOW_POS_ENTRY_RECENT_OVERSOLD_DIST_MAX:
            if best_d is None or d60 < best_d:
                best_d = d60
            if best_p is None or (p60 is not None and p60 < best_p):
                best_p = p60
    if best_d is not None:
        return True, float(best_d), float(best_p) if best_p is not None else None
    return False, None, None


def main():
    codes = [p.stem[2:] for p in sorted(KLINE_DIR.glob("*.csv"))]
    print(f"读取本地缓存 {len(codes)} 只 ...", flush=True)
    hists = {}
    for c in codes:
        h = _load_cache(c)
        if h is not None:
            hists[c] = h
    print(f"K线就绪 {len(hists)} 只", flush=True)

    print("扫描触发（不卡成交额绝对值）...", flush=True)
    recs = []
    for code, h in hists.items():
        ind = _indicators(h)
        for i in range(70, len(ind) - 1):
            row = ind.iloc[i]
            ok = _蓄势通过(row)
            os_hit, os_dist, os_pos = _recent_oversold_detail(ind, i + 1)
            if not ok and not os_hit:
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
            if not (lb >= p["lb_lo"] and lb <= p["lb_hi"]
                    and p["chg_lo"] <= chg <= p["chg_hi"]
                    and (not p["break_ma20"] or float(nxt["收盘"]) > ma20)
                    and nxt_pos60 < p["pos60_max"]):
                continue
            entry = float(nxt["收盘"])
            fut = ind.iloc[i + 2:i + 9]
            if fut.empty:
                continue
            closes = fut["收盘"].astype(float).tolist()
            max_ret = (max(closes) / entry - 1) * 100
            # 蓄势日特征（标准路径用的收紧杠杆）
            recs.append({
                "code": code, "date": pd.Timestamp(nxt["date"]),
                "path": path,
                "lb": lb, "chg": chg, "amt": nxt_amt,
                "amt_ratio5": float(nxt.get("amt_ratio5", 0)),
                "trigger_pos60": nxt_pos60,
                "reserve_pos60": float(row.get("pos60", 0)),   # 蓄势日位置
                "reserve_dist60": float(row.get("dist60", 0)), # 蓄势日距60日高点
                "reserve_lb": float(row.get("lb", 0)),
                "os_dist60": os_dist,  # 超卖深度
                "os_pos60": os_pos,    # 超卖最极端位置
                "max_ret": max_ret,
            })

    if not recs:
        print("无触发样本")
        return
    df = pd.DataFrame(recs)
    df["amt_yi"] = df["amt"] / 1e8
    df.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")

    # 温度过滤（最终口径）
    print("[温度过滤] 计算市场温度...", flush=True)
    date_up = {}
    for code, h in hists.items():
        ind = _indicators(h)
        for i in range(70, len(ind) - 1):
            nxt = ind.iloc[i + 1]
            td = pd.Timestamp(nxt["date"])
            date_up.setdefault(td, [0, 0])
            date_up[td][1] += 1
            if float(nxt["涨跌幅"]) > 0:
                date_up[td][0] += 1
    df["temp"] = df["date"].map(lambda td: date_up.get(td, [0, 0])[0] / max(date_up.get(td, [0, 0])[1], 1) * 100)
    df = df[df["temp"] >= LOW_POS_ENTRY_TEMP_MIN].copy()

    print(f"\n=== 分路径拆层：温度≥{LOW_POS_ENTRY_TEMP_MIN} 后 {len(df)} 次触发（标准蓄势 {sum(df['path']=='标准蓄势')} / 近期超卖 {sum(df['path']=='近期超卖')}）===\n")

    def bucket(s, bins, labels):
        return pd.cut(s, bins=bins, labels=labels, right=False)

    def report_group(grp, name, extra=""):
        if grp.empty:
            print(f"    {name}: 无样本")
            return
        big = (grp["max_ret"] >= 10).mean() * 100
        win0 = (grp["max_ret"] >= 0).mean() * 100
        print(f"    {name:<24} n={len(grp):3d}  7日最大涨幅均值{grp['max_ret'].mean():+5.1f}%  "
              f"≥0% {win0:3.0f}%  ≥10% {big:3.0f}%{extra}")

    for path in ("近期超卖", "标准蓄势"):
        sub = df[df["path"] == path]
        print(f"===== 路径：{path}（n={len(sub)}） =====")

        # 触发位置
        print(f"  [触发位置 pos60]")
        report_group(sub[sub["trigger_pos60"] < 0.3], "pos60<0.30")
        report_group(sub[(sub["trigger_pos60"] >= 0.3) & (sub["trigger_pos60"] < 0.5)], "0.30-0.50")
        report_group(sub[(sub["trigger_pos60"] >= 0.5) & (sub["trigger_pos60"] < 0.65)], "0.50-0.65")

        # 触发量比
        print(f"  [触发量比 lb]")
        report_group(sub[sub["lb"] < 2.5], "lb 2.0-2.5")
        report_group(sub[(sub["lb"] >= 2.5) & (sub["lb"] < 3.5)], "2.5-3.5")
        report_group(sub[(sub["lb"] >= 3.5) & (sub["lb"] < 5)], "3.5-5.0")
        report_group(sub[sub["lb"] >= 5], "5.0-7.0")

        # 触发涨幅
        print(f"  [触发涨幅 chg]")
        report_group(sub[sub["chg"] < 10], "9.0-10.0")
        report_group(sub[(sub["chg"] >= 10) & (sub["chg"] < 12)], "10.0-12.0")
        report_group(sub[(sub["chg"] >= 12) & (sub["chg"] < 14)], "12.0-14.0")
        report_group(sub[sub["chg"] >= 14], "14.0-15.0")

        # 触发成交额（关键：验证下限是否误杀）
        print(f"  [触发成交额 亿]")
        report_group(sub[sub["amt_yi"] < 2], "0-2亿")
        report_group(sub[(sub["amt_yi"] >= 2) & (sub["amt_yi"] < 4)], "2-4亿")
        report_group(sub[(sub["amt_yi"] >= 4) & (sub["amt_yi"] < 8)], "4-8亿")
        report_group(sub[(sub["amt_yi"] >= 8) & (sub["amt_yi"] < 16)], "8-16亿")
        report_group(sub[sub["amt_yi"] >= 16], "≥16亿")

        # 成交额放量倍数
        print(f"  [成交额放量倍数 ratio5]")
        report_group(sub[sub["amt_ratio5"] < 2], "ratio5<2x")
        report_group(sub[(sub["amt_ratio5"] >= 2) & (sub["amt_ratio5"] < 3)], "2-3x")
        report_group(sub[(sub["amt_ratio5"] >= 3) & (sub["amt_ratio5"] < 5)], "3-5x")
        report_group(sub[sub["amt_ratio5"] >= 5], "≥5x")

        if path == "近期超卖":
            print(f"  [超卖深度 dist60 最极端]")
            report_group(sub[sub["os_dist60"] < -40], "超卖<-40%")
            report_group(sub[(sub["os_dist60"] >= -40) & (sub["os_dist60"] < -30)], "-40~-30%")
        else:
            print(f"  [蓄势日位置 pos60]")
            report_group(sub[sub["reserve_pos60"] < 0.3], "蓄势pos<0.30")
            report_group(sub[(sub["reserve_pos60"] >= 0.3) & (sub["reserve_pos60"] < 0.4)], "0.30-0.40")
            report_group(sub[(sub["reserve_pos60"] >= 0.4) & (sub["reserve_pos60"] < 0.55)], "0.40-0.55")
            print(f"  [蓄势日距60日高点 dist60]")
            report_group(sub[sub["reserve_dist60"] < -20], "距高点<-20%")
            report_group(sub[(sub["reserve_dist60"] >= -20) & (sub["reserve_dist60"] < -15)], "-20~-15%")
            report_group(sub[sub["reserve_dist60"] >= -15], "≥-15%")
        print()

    print(f"明细 → {OUT_CSV}")


if __name__ == "__main__":
    main()
