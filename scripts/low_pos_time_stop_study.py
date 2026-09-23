# -*- coding: utf-8 -*-
"""低位启动池 · 时间止损（「N日不涨离场」）维度验证（读本地 data/klines 缓存，无网络）。

背景：超跌反弹实操派提出「5 日不涨离场」。上一轮已验证「快进快出收紧止损」「阶梯止盈」
     在本策略触发口径下都不如「持有到 T+N 一次均价卖」。但「时间止损」这个独立维度尚未单独验证。
     本脚本专门回答：在近期超卖路径「持有 T+5」基础上，加入「中途持续不涨就提前离场」，
     落袋收益（均值/中位/胜率/≥10%落袋/止损触发率）是否更优？

口径：完全复用 low_pos_tp_compare.py 的 scan_triggers / simulate_exit / compute_temp，
     入场=触发日收盘，退出=未来收盘逐日模拟（止损优先、收盘口径、保守）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

from low_pos_tp_compare import scan_triggers, simulate_exit, compute_temp
from low_pos_7day_backtest import _load_cache, KLINE_DIR

# 各方案：sl 固定 -8%（保持线上口径），tp1/tp2 置 None（线上是「一次均价卖」，非阶梯）。
# time_stop=(day, threshold) 含义：自第 day 日起，若收盘收益 < threshold 即离场。
#   - time_stop=None：持有到期（线上现状 T+5）。
#   - (3, 0.0)：第3日收盘收益仍<0 → 离场（「3日不涨」）。
#   - (3, 0.02)：第3日收盘收益仍<+2% → 离场（更严格，要求启动更强）。
SCHEMES = {
    "T+5 基准(持有到期)": dict(sl=-0.08, tp1=None, tp2=None, tp1_frac=0.5, max_hold=5, time_stop=None),
    "T+5 + 第2日不涨离场(<0%)": dict(sl=-0.08, tp1=None, tp2=None, tp1_frac=0.5, max_hold=5, time_stop=(2, 0.0)),
    "T+5 + 第3日不涨离场(<0%)": dict(sl=-0.08, tp1=None, tp2=None, tp1_frac=0.5, max_hold=5, time_stop=(3, 0.0)),
    "T+5 + 第3日弱离场(<+2%)": dict(sl=-0.08, tp1=None, tp2=None, tp1_frac=0.5, max_hold=5, time_stop=(3, 0.02)),
    "T+5 + 第4日不涨离场(<0%)": dict(sl=-0.08, tp1=None, tp2=None, tp1_frac=0.5, max_hold=5, time_stop=(4, 0.0)),
    "T+5 + 第4日弱离场(<+2%)": dict(sl=-0.08, tp1=None, tp2=None, tp1_frac=0.5, max_hold=5, time_stop=(4, 0.02)),
}


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

    print("[3/3] 各温度口径 + 时间止损方案落袋收益对比\n", flush=True)

    for TH, tag in ((55, "线上口径"), (50, "样本扩张稳健性检验"), (0, "全样本(无温度过滤)")):
        print(f"\n{'=' * 74}\n########## 温度≥{TH}（{tag}） ##########")
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
            r = {"path": info["path"], "entry": entry, "closes": closes}
            for name, sc in SCHEMES.items():
                rr, _ = simulate_exit(closes, entry, sc)
                r[name] = rr
            rows.append(r)

        df = pd.DataFrame(rows)
        if df.empty:
            print("  无样本")
            continue

        print("\n--- 落袋收益（时间止损 vs 持有到期，均值/中位/胜率/≥10%落袋/≤-8%） ---")
        for path, grp in df.groupby("path"):
            print(f"\n  【{path}】 {len(grp)}笔")
            header = f"    {'方案':<30}{'均值':>8}{'中位':>8}{'胜率':>7}{'≥10%':>7}{'≤-8%':>7}"
            print(header)
            print("    " + "-" * 66)
            for name in SCHEMES:
                s = grp[name]
                win = (s > 0).mean() * 100
                big = (s >= 10).mean() * 100
                bad = (s <= -8).mean() * 100
                print(f"    {name:<30}{s.mean():>+7.2f}%{s.median():>+7.2f}%{win:>6.1f}%{big:>6.1f}%{bad:>6.1f}%")

    print("\n明细说明：入场=触发日收盘；退出=未来收盘逐日模拟（止损优先、收盘口径、保守）。")
    print("         时间止损语义：自第 N 日起，收盘收益低于阈值即离场；阈值0=不涨，阈值+2%=启动过弱。")


if __name__ == "__main__":
    main()
