#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""观察池存量票回溯体检 + 清理。

对 watch_history.csv 最新归档日的存量票，按当前研究结论（剔劣三件套 + 位置/成交额
最优区间）逐只复算，违规的追加"已失效"标记行（不删历史，保审计）。

用法：
    python cleanup_watch_pool.py           # 只体检，打印报告，不写盘
    python cleanup_watch_pool.py --apply   # 体检 + 写盘（标记失效）
"""
import sys, csv, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, ".")

from fetch_data import get_stock_hist
from stock_screener import _tech_indicators
from config import (LOW_POS_MAX_20D_AMP, LOW_POS_MAX_20D_STD, LOW_POS_MIN_DIST_60D_HIGH,
                    LOW_POS_BEST_POS, LOW_POS_BEST_AMOUNT)

CSV_PATH = "../data/watch_history.csv"
FIELDS = ["日期", "代码", "名称", "现价", "60日位置", "信号", "5日涨幅",
          "行业", "买点区间", "止损价", "关注逻辑", "状态"]


def check_stock(code):
    """返回 (violations, pos, amp20, std20, dist60, amt)。violations 为空=合规。"""
    h = get_stock_hist(code, days=120)
    if h is None or len(h) < 60:
        return (["数据不足"], None, None, None, None, None)
    ind = _tech_indicators(h)
    last = ind.iloc[-1]
    close = float(last["收盘"])
    hi60 = ind["最高"].tail(60).max(); lo60 = ind["最低"].tail(60).min()
    pos = (close - lo60) / (hi60 - lo60)
    amp20 = (ind["收盘"].tail(20).max() - ind["收盘"].tail(20).min()) / ind["收盘"].tail(20).mean()
    std20 = ind["收盘"].tail(20).std() / ind["收盘"].tail(20).mean()
    dist60 = (close / hi60 - 1) * 100

    v = []
    if amp20 >= LOW_POS_MAX_20D_AMP:
        v.append(f"振幅{amp20:.0%}≥15%")
    if std20 >= LOW_POS_MAX_20D_STD:
        v.append(f"std{std20:.0%}≥4%")
    if dist60 <= LOW_POS_MIN_DIST_60D_HIGH:
        v.append(f"距高{dist60:.0f}%≤-22%")
    if not (LOW_POS_BEST_POS[0] <= pos < LOW_POS_BEST_POS[1]):
        v.append(f"位置{pos:.0%}∉[{LOW_POS_BEST_POS[0]:.0%},{LOW_POS_BEST_POS[1]:.0%})")
    return (v, pos, amp20, std20, dist60, None)


def main():
    apply = "--apply" in sys.argv
    rows = list(csv.DictReader(open(CSV_PATH, encoding="utf-8")))
    latest = max(r["日期"] for r in rows)
    pool = [r for r in rows if r["日期"] == latest]

    print(f"=== 观察池存量体检  归档日 {latest}  共 {len(pool)} 只 ===")
    if apply:
        print(">>> --apply 模式：违规票将标记「已失效」写盘 <<<")
    print()

    bad = []
    for r in pool:
        code, name = r["代码"], r["名称"]
        v, pos, amp20, std20, dist60, _ = check_stock(code)
        ok = not v
        if not ok:
            bad.append((code, name, v))
        mark = "✓ 合规" if ok else "✗ " + "；".join(v)
        print(f"{code} {name:<8} 位置{pos if pos is None else format(pos,'.0%'):>6} "
              f"距高{'-' if dist60 is None else format(dist60,'.0f')+'%':>6}  {mark}")

    if not bad:
        print("\n全部合规，无需清理。")
        return

    print(f"\n共 {len(bad)} 只违规。")
    if not apply:
        print("未写盘。加 --apply 参数执行标记。")
        return

    # 改写原行状态为"已失效"（不追加重复行，避免同日同代码出现"观察中+已失效"两条）
    # _track_watch_pool 读 csv 时按 code 最新记录状态判定，已失效则整组跳过跟踪。
    added = 0
    for r in rows:
        if r["日期"] == latest and r["代码"] in {c for c, _, _ in bad}:
            if r.get("状态", "") != "已失效":
                v = next(v for c, _, v in bad if c == r["代码"])
                r["状态"] = "已失效"
                r["关注逻辑"] = "【已失效】" + ("；".join(v))
                added += 1
    rows.sort(key=lambda r: (r["日期"], r["代码"], r.get("状态", "")))

    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=FIELDS)
        wr.writeheader()
        for r in rows:
            wr.writerow({k: r.get(k, "") for k in FIELDS})
    print(f"已标记 {added} 只失效票写入 {CSV_PATH}（历史记录保留）")


if __name__ == "__main__":
    main()
