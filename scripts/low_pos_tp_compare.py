# -*- coding: utf-8 -*-
"""低位启动池 · 两路径止盈/止损差异化对比回测（读本地 data/klines 缓存，无网络）。

背景：当前两路径止盈止损完全一致（SL -8% / TP1 +5% / TP2 +7%，且线上为「T+3 均价法」）。
问题：近期超卖（超跌反弹）与标准蓄势（趋势启动）的退出哲学应否差异化？

方法论（重要）：
  - 「上涨概率」（7日内最大涨幅≥10%）与「上涨涨幅」（最大涨幅均值/中位）是标的自身属性，
    与止盈/止损参数无关（止盈只决定何时离场，不改变票能涨多高）。
  - 止盈/止损真正影响的是「落袋收益 realized return」——何时退出、落袋多少。
  - 因此本脚本同时输出：① 标的属性（上涨概率/涨幅，验证不因止盈变动而变）② 落袋收益（各方案的均值/中位/胜率/≥10%落袋率/止损触发率）。

口径（与 low_pos_7day_backtest.py 完全一致，防未来函数）：
  - 入场价 = 触发日(T)收盘价；未来窗口 = T+1..T+7 收盘（用收盘价模拟退出，保守，盘中触及未成交按收盘计）。
  - 温度过滤阈值可调，默认 ≥55（线上口径），并提供 ≥50 做样本扩张稳健性检验。
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
    LOW_POS_ENTRY_STOP_LOSS, LOW_POS_ENTRY_TP1, LOW_POS_ENTRY_TP2,
    LOW_POS_ENTRY_OS_TRIGGER_LB, LOW_POS_ENTRY_OS_TRIGGER_LB_MAX,
    LOW_POS_ENTRY_OS_TRIGGER_CHG_MIN, LOW_POS_ENTRY_OS_TRIGGER_CHG_MAX,
    LOW_POS_ENTRY_OS_TRIGGER_MIN_AMOUNT, LOW_POS_ENTRY_OS_TRIGGER_MAX_AMOUNT,
    LOW_POS_ENTRY_OS_TRIGGER_BREAK_MA20,
    LOW_POS_ENTRY_OS_STOP_LOSS, LOW_POS_ENTRY_OS_TP1, LOW_POS_ENTRY_OS_TP2,
)

from low_pos_7day_backtest import (
    _load_cache, _indicators, _蓄势通过, _recent_oversold, KLINE_DIR,
)

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
    },
}

# 退出方案：每个方案返回 (名称, sl, tp1, tp2, tp1_frac, max_hold, time_stop)
# time_stop = (day, threshold) 在第 day 日若收盘收益 < threshold 则离场；None 表示无时间止损
SCHEMES = {
    # —— 时间持有类（对应线上「T+N 均价法」）——
    "持有T+3(-8%止损)": dict(sl=-0.08, tp1=None, tp2=None, tp1_frac=0.5, max_hold=3, time_stop=None),
    "持有T+5(-8%止损)": dict(sl=-0.08, tp1=None, tp2=None, tp1_frac=0.5, max_hold=5, time_stop=None),
    "持有T+7(-8%止损)": dict(sl=-0.08, tp1=None, tp2=None, tp1_frac=0.5, max_hold=7, time_stop=None),
    # —— 超跌反弹风格：快进快出、止损收紧、提前落袋 + 5日不涨离场 ——
    "超卖风(收紧-5%/+5半仓/+8清/5日不涨离)": dict(sl=-0.05, tp1=0.05, tp2=0.08, tp1_frac=0.5, max_hold=7, time_stop=(5, 0.0)),
    # —— 趋势启动风格：让利润奔跑、止损保留、放宽止盈 ——
    "趋势风(放宽-8%/+10半仓/+20清)": dict(sl=-0.08, tp1=0.10, tp2=0.20, tp1_frac=0.5, max_hold=7, time_stop=None),
}


def simulate_exit(closes, entry, sc):
    """按方案 sc 逐日模拟退出，返回 (落袋收益%, 退出日)。收盘价口径，止损优先（保守）。"""
    remaining = 1.0
    pnl = 0.0
    exit_day = None
    sl = sc["sl"]
    tp1, tp2 = sc["tp1"], sc["tp2"]
    frac = sc["tp1_frac"]
    max_hold = sc["max_hold"]
    time_stop = sc["time_stop"]
    n = min(max_hold, len(closes))
    for k in range(1, n + 1):
        c = closes[k - 1]
        # 1) 止损优先（保守：同日内先判定止损）
        if c <= entry * (1 + sl):
            pnl += remaining * (c / entry - 1)
            remaining = 0.0
            exit_day = k
            break
        # 2) 时间止损（超跌反弹「N日不涨离场」）
        if time_stop is not None and k >= time_stop[0]:
            if (c / entry - 1) < time_stop[1]:
                pnl += remaining * (c / entry - 1)
                remaining = 0.0
                exit_day = k
                break
        # 3) 阶梯止盈：TP1 减半仓、TP2 清仓
        if tp1 is not None and c >= entry * (1 + tp1):
            pnl += remaining * frac * (c / entry - 1)
            remaining *= (1 - frac)
        if tp2 is not None and c >= entry * (1 + tp2):
            pnl += remaining * (c / entry - 1)
            remaining = 0.0
            exit_day = k
            break
    # 收尾：持有到期，按最后一个可用收盘价结算剩余仓位
    if remaining > 0:
        last = closes[n - 1] if n > 0 else entry
        pnl += remaining * (last / entry - 1)
        if exit_day is None:
            exit_day = n
    return pnl * 100, exit_day


def scan_triggers(hists):
    """扫描全部触发候选，返回 list[dict]，与 low_pos_7day_backtest.py 口径一致。"""
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
            triggered = (lb >= p["lb_lo"] and lb <= p["lb_hi"]
                         and p["chg_lo"] <= chg <= p["chg_hi"]
                         and p["amt_lo"] <= nxt_amt <= p["amt_hi"]
                         and (not p["break_ma20"] or float(nxt["收盘"]) > ma20)
                         and nxt_pos60 < p["pos60_max"])
            if not triggered:
                continue
            td = pd.Timestamp(nxt["date"])
            entry = float(nxt["收盘"])
            all_triggers[(code, td)] = {
                "code": code, "trigger_date": td, "entry": entry,
                "ind": ind, "i": i, "path": path,
            }
    return all_triggers


def compute_temp(hists):
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
    return {td: (v[0] / v[1] * 100 if v[1] > 0 else 0) for td, v in date_up_ratio.items()}


def main():
    codes = [p.stem[2:] for p in sorted(KLINE_DIR.glob("*.csv"))]
    print(f"[1/3] 读取本地缓存 {len(codes)} 只 ...", flush=True)
    hists = {}
    for c in codes:
        code, h = _load_cache(c)
        if h is not None:
            hists[code] = h
    print(f"  K线就绪 {len(hists)} 只", flush=True)

    print("[2/3] 扫描触发候选 ...", flush=True)
    all_triggers = scan_triggers(hists)
    date_up_ratio = compute_temp(hists)

    print("[3/3] 计算各温度口径 + 各退出方案落袋收益 ...\n", flush=True)

    # 样本量探查（防过度拟合）：不同温度阈值下的触发数
    print("=== 触发样本量（按温度阈值） ===")
    for th in (0, 50, 55):
        cnt = {}
        for info in all_triggers.values():
            if date_up_ratio.get(info["trigger_date"], 0) < th:
                continue
            cnt[info["path"]] = cnt.get(info["path"], 0) + 1
        total = sum(cnt.values())
        print(f"  温度≥{th}: 总{total}笔 = 标准蓄势{cnt.get('标准蓄势', 0)} + 近期超卖{cnt.get('近期超卖', 0)}")

    # 主口径：温度≥55
    print("\n" + "=" * 70)
    for TH, tag in ((55, "线上口径"), (50, "样本扩张稳健性检验"), (0, "全样本(无温度过滤)")):
        print(f"\n########## 温度≥{TH}（{tag}） ##########")
        rows = []
        for info in all_triggers.values():
            if date_up_ratio.get(info["trigger_date"], 0) < TH:
                continue
            ind, i = info["ind"], info["i"]
            entry = info["entry"]
            fut = ind.iloc[i + 2:i + 9]  # T+1..T+7
            if fut.empty:
                continue
            closes = fut["收盘"].astype(float).tolist()
            max_ret = (max(closes) / entry - 1) * 100
            r = {
                "path": info["path"], "entry": entry,
                "closes": closes, "max_ret": max_ret,
            }
            for name, sc in SCHEMES.items():
                rr, _ = simulate_exit(closes, entry, sc)
                r[name] = rr
            rows.append(r)

        df = pd.DataFrame(rows)
        if df.empty:
            print("  无样本")
            continue

        # 标的属性（与止盈无关，验证不变）
        print("\n--- 标的自身属性（上涨概率/涨幅，与止盈无关） ---")
        for path, grp in df.groupby("path"):
            mr = grp["max_ret"]
            p10 = (mr >= 10).mean() * 100
            print(f"  {path}: {len(grp)}笔  7日最大涨幅均值{mr.mean():+.2f}% 中位{mr.median():+.2f}%  ≥10%概率{p10:.0f}%")

        # 落袋收益（各退出方案）
        print("\n--- 落袋收益（各退出方案对比，均值/中位/胜率/≥10%落袋率/止损触发率） ---")
        for path, grp in df.groupby("path"):
            print(f"\n  【{path}】 {len(grp)}笔")
            header = f"    {'方案':<34}{'均值':>8}{'中位':>8}{'胜率':>7}{'≥10%落袋':>9}{'≤-8%':>7}"
            print(header)
            print("    " + "-" * 70)
            for name in SCHEMES:
                s = grp[name]
                win = (s > 0).mean() * 100
                big = (s >= 10).mean() * 100
                bad = (s <= -8).mean() * 100
                print(f"    {name:<34}{s.mean():>+7.2f}%{s.median():>+7.2f}%{win:>6.1f}%{big:>8.1f}%{bad:>6.1f}%")

    print("\n明细说明：入场=触发日收盘；退出用未来收盘价逐日模拟（止损优先、收盘口径、保守）。")


if __name__ == "__main__":
    main()
