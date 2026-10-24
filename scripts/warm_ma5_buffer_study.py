# -*- coding: utf-8 -*-
"""初动池「破MA5离场」缓冲变体研究（2026-10-24）。

背景：观点学习日记 2026-10-16 思考③ 提出——弱市里 MA5 压制强，多数票只能吃到
0~+3% 的小段，建议对「破MA5」设置更宽容的洗盘缓冲（原话「需回测验证，勿拍脑袋改」）。
10-23 的 25 条小样本归因显示 21/25 走破MA5离场，本次用全历史最优档样本验证缓冲是否值得。

基线（现行生产 V2）：持仓 T+2 收盘起，收盘 < MA5 → 次日开盘离场；-8% 盘中硬止损；T+5 收盘到期。

变体（其余规则不变，仅改 MA5 判定）：
  B0  现行为基线
  M1  连续 2 日收盘破 MA5 才离场
  M2  收盘 < MA5 × 0.98（2% 缓冲）
  M3  收盘 < MA5 且 收盘 < 入场价（盈利中不因破 MA5 离场）
  M4  收盘 < MA10 替代 MA5
  M5  收盘 < MA5 且 收盘 < MA20（双重确认）

口径：入场沿用 warm_start_backtest.py —— 信号日 T 收盘确认、T+1 开盘买入、一字板跳过。
      -8% 止损为盘中触及（low <= entry*0.92），与生产 V2 一致。
      所有离场判定用 T+k 收盘数据、T+k+1 开盘成交，无未来函数。

用法：python warm_ma5_buffer_study.py
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

BASE = Path(__file__).resolve().parent.parent
KLINE_DIR = BASE / "data" / "klines"
DETAIL = BASE / "data" / "warm_start_backtest.csv"

HARD_STOP = 0.92
HOLD_MAX = 5

_cache = {}


def load_stock(code):
    """返回 numpy 数组字典：open/last/high/low/ma5/ma10/ma20。"""
    if code in _cache:
        return _cache[code]
    f = KLINE_DIR / f"{code}.csv"
    if not f.exists():
        _cache[code] = None
        return None
    try:
        df = pd.read_csv(f, encoding="utf-8-sig")
    except Exception:
        _cache[code] = None
        return None
    if len(df) < 30:
        _cache[code] = None
        return None
    df = df.sort_values("date").reset_index(drop=True)
    arr = {
        "date": df["date"].values,
        "open": df["open"].astype(float).values,
        "last": df["last"].astype(float).values,
        "high": df["high"].astype(float).values,
        "low": df["low"].astype(float).values,
        "ma5": df["last"].astype(float).rolling(5).mean().values,
        "ma10": df["last"].astype(float).rolling(10).mean().values,
        "ma20": df["last"].astype(float).rolling(20).mean().values,
    }
    _cache[code] = arr
    return arr


def replay(code, sig_date, variant):
    """重放一笔。返回 (收益%, 出场原因, 持仓交易日数) 或 None。"""
    a = load_stock(code)
    if a is None:
        return None
    hits = np.where(a["date"] == sig_date)[0]
    if len(hits) == 0:
        return None
    i = int(hits[0])
    if i + 1 >= len(a["date"]):
        return None
    entry = a["open"][i + 1]
    if entry <= 0 or np.isnan(entry):
        return None
    # 一字板买不进
    if a["open"][i + 1] == a["high"][i + 1] == a["low"][i + 1] == a["last"][i + 1]:
        return None

    break_days = 0
    for k in range(1, HOLD_MAX + 1):
        j = i + 1 + k
        if j >= len(a["date"]):
            j = len(a["date"]) - 1
            return (a["last"][j] / entry - 1) * 100, "maturity", k
        # 1) 盘中 -8% 硬止损（当日成交，保守按止损价）
        if a["low"][j] <= entry * HARD_STOP:
            return -8.0, "stop", k
        c = a["last"][j]
        exit_now = False
        if k >= 2:
            ma5 = a["ma5"][j]
            ma10 = a["ma10"][j]
            ma20 = a["ma20"][j]
            if variant == "B0":
                exit_now = (not np.isnan(ma5)) and c < ma5
            elif variant == "M1":
                broken = (not np.isnan(ma5)) and c < ma5
                break_days = break_days + 1 if broken else 0
                exit_now = break_days >= 2
            elif variant == "M2":
                exit_now = (not np.isnan(ma5)) and c < ma5 * 0.98
            elif variant == "M3":
                exit_now = (not np.isnan(ma5)) and c < ma5 and c < entry
            elif variant == "M4":
                exit_now = (not np.isnan(ma10)) and c < ma10
            elif variant == "M5":
                exit_now = ((not np.isnan(ma5)) and c < ma5
                            and (not np.isnan(ma20)) and c < ma20)
        if exit_now:
            if j + 1 < len(a["date"]):
                return (a["open"][j + 1] / entry - 1) * 100, "ma5", k + 1
            return (c / entry - 1) * 100, "ma5", k

    r = (a["last"][i + 1 + HOLD_MAX] / entry - 1) * 100
    return r, "maturity", HOLD_MAX


def stats(rows):
    if not rows:
        return None
    r = np.array([x[0] for x in rows])
    days = np.array([x[2] for x in rows])
    wins, losses = r[r > 0], r[r < 0]
    pl = wins.mean() / abs(losses.mean()) if len(wins) and len(losses) else float("nan")
    reasons = pd.Series([x[1] for x in rows]).value_counts(normalize=True) * 100
    return {
        "n": len(r),
        "胜率": (r > 0).mean() * 100,
        "均值": r.mean(),
        "盈亏比": pl,
        "累计": r.sum(),
        "均持仓": days.mean(),
        "止损率": reasons.get("stop", 0.0),
        "MA5离场率": reasons.get("ma5", 0.0),
        "中位": np.median(r),
        "大赚≥10%占比": (r >= 10).mean() * 100,
        "大亏≤-8%占比": (r <= -8).mean() * 100,
    }


def main():
    df = pd.read_csv(DETAIL)
    sub = df[(df["成交额档"] == "≤16亿") & df["r5"].notna()].copy()
    print(f"样本：≤16亿最优档可执行信号 {len(sub)} 条（{sub['date'].min()} ~ {sub['date'].max()}）\n")

    variants = ["B0", "M1", "M2", "M3", "M4", "M5"]
    desc = {
        "B0": "现行：破MA5次日离场",
        "M1": "连续2日破MA5",
        "M2": "破MA5×0.98 才走",
        "M3": "破MA5且亏钱才走",
        "M4": "破MA10 才走",
        "M5": "破MA5且破MA20",
    }
    res = {v: [] for v in variants}
    miss = 0
    for _, row in sub.iterrows():
        any_ok = False
        for v in variants:
            out = replay(row["code"], row["date"], v)
            if out is None:
                continue
            any_ok = True
            res[v].append(out)
        if not any_ok:
            miss += 1
    if miss:
        print(f"（{miss} 条无法重放，已跳过）\n")

    base = stats(res["B0"])
    print(f"{'变体':<4}{'说明':<22}{'n':>6}{'胜率':>8}{'均值':>9}{'盈亏比':>8}"
          f"{'累计':>10}{'均持':>7}{'MA5离场%':>10}{'止损%':>8}{'≥10%占比':>10}{'净增量':>10}")
    print("-" * 118)
    for v in variants:
        s = stats(res[v])
        if not s:
            continue
        delta = s["累计"] - base["累计"]
        print(f"{v:<4}{desc[v]:<22}{s['n']:>6}{(s['胜率']):>7.1f}%{s['均值']:>+8.2f}%"
              f"{s['盈亏比']:>8.2f}{s['累计']:>+10.1f}{s['均持仓']:>6.2f}天"
              f"{s['MA5离场率']:>9.1f}%{s['止损率']:>7.1f}%{s['大赚≥10%占比']:>9.1f}%"
              f"{delta:>+10.1f}")

    # 年度稳定性：只对净增量为正且逻辑合理的变体做
    print("\n=== 年度：候选变体 vs 基线（均值% / 胜率%）===")
    df_y = sub.copy()
    df_y["year"] = df_y["date"].str[:4]
    print(f"{'年份':<6}{'B0 均值/胜率':>20}" + "".join(f"{v:>18}" for v in variants[1:]))
    for y, g in df_y.groupby("year"):
        if len(g) < 80:
            continue
        row = f"{y:<6}"
        b = stats(res["B0"])
        # 按年份重算
        def ystats(v):
            rows = []
            for _, r in g.iterrows():
                o = replay(r["code"], r["date"], v)
                if o:
                    rows.append(o)
            return stats(rows)
        bs = ystats("B0")
        row += f"{bs['均值']:>+9.2f}/{bs['胜率']:>5.1f}%"
        for v in variants[1:]:
            s = ystats(v)
            if s and s["n"] > 30:
                row += f"{s['均值']:>+10.2f}/{s['胜率']:>5.1f}%"
            else:
                row += f"{'—':>18}"
        print(row)

    out = BASE / "data" / "warm_ma5_buffer_study.csv"
    recs = []
    for v in variants:
        s = stats(res[v])
        if s:
            recs.append({"变体": v, "说明": desc[v], **{k: round(float(x), 3) for k, x in s.items()},
                         "净增量累计": round(s["累计"] - base["累计"], 2)})
    pd.DataFrame(recs).to_csv(out, index=False, encoding="utf-8-sig")
    print(f"\n明细已存 {out}")


if __name__ == "__main__":
    main()
