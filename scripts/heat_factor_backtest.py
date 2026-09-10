# -*- coding: utf-8 -*-
"""行业热度因子回测：验证"行业持续性 = 主线确认"假设
对每条低位启动信号，计算信号日之前 N 日内该行业在 watch_history 出现天数（热度），
按热度分层统计胜率/收益，验证：持续主线行业的信号是否显著优于首次出现。
"""
import csv, sys
from collections import defaultdict

def read_csv(path):
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))

WATCH = "../data/watch_history.csv"
RESULT = "../04_每日复盘/低位池回测_2026-07-30_2026-09-07.csv"

# 1. 读 watch_history，构建 行业->日期集合
ind_days = defaultdict(set)
watch_rows = read_csv(WATCH)
for r in watch_rows:
    ind_days[r["行业"]].add(r["日期"])

all_days = sorted(set(r["日期"] for r in watch_rows))
day_idx = {d: i for i, d in enumerate(all_days)}

def heat_before(ind, d, window=10):
    """信号日 d 之前 window 个交易日内，该行业出现的天数"""
    di = day_idx[d]
    win = set(all_days[max(0, di - window):di])
    return len(ind_days.get(ind, set()) & win)

# 2. 读回测明细
signals = read_csv(RESULT)

print(f"回测明细 {len(signals)} 条\n")

# 3. 按信号日之前10日热度分层
layers = {"0次(首次/冷门)": [], "1-2次": [], "3-5次(升温)": [], "6+次(持续主线)": []}

for s in signals:
    h = heat_before(s["行业"], s["信号日"], window=10)
    ret = float(s["最终涨幅"])
    if h == 0:
        layers["0次(首次/冷门)"].append(ret)
    elif h <= 2:
        layers["1-2次"].append(ret)
    elif h <= 5:
        layers["3-5次(升温)"].append(ret)
    else:
        layers["6+次(持续主线)"].append(ret)

print(f"{'热度分层':<16}{'信号数':<6}{'胜率':<8}{'均收益':<10}{'中位':<10}期望值")
for name, rets in layers.items():
    if not rets:
        print(f"{name:<16}{0:<6}{'-':<8}{'-':<10}{'-':<10}-")
        continue
    win = sum(1 for x in rets if x > 0) / len(rets)
    avg = sum(rets) / len(rets)
    med = sorted(rets)[len(rets) // 2]
    print(f"{name:<16}{len(rets):<6}{win:<8.1%}{avg:<+9.2f}%{med:<+9.2f}%{avg:+.2f}%")

# 4. 关键对比：热度>=3 的 2/3 仓位信号 vs 全部
hot = [x for k in ("3-5次(升温)", "6+次(持续主线)") for x in layers[k]]
cold = layers["0次(首次/冷门)"] + layers["1-2次"]
for label, g in (("热度>=3(主线确认)", hot), ("热度<3(非主线)", cold), ("全部", [x for v in layers.values() for x in v])):
    if not g:
        continue
    win = sum(1 for x in g if x > 0) / len(g)
    avg = sum(g) / len(g)
    print(f"\n{label}: n={len(g)} 胜率{win:.1%} 均收益{avg:+.2f}%")
