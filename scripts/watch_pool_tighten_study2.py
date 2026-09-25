# -*- coding: utf-8 -*-
"""组合搜索：2-4 只/天 + 转化率最高
基于单因子研究里有效的收紧方向做组合
只读研究，不改策略代码。
"""
import pandas as pd
from itertools import combinations

df = pd.read_csv('data/低位池_入池事件_20260923.csv')
df['入池日'] = pd.to_datetime(df['入池日'])

def v6():
    return (
        (df['成交额亿'] <= 6) & (df['位置'] <= 0.40) & (df['距高%'] <= -25) &
        (df['MA20乖离%'] >= -5) & (df['20日振幅%'] < 25) & (df['量比'] <= 2.0)
    )

def report(mask, label):
    sub = df[mask]
    n = len(sub)
    if n < 80:
        print(f'{label:52s} 事件={n:4d}（样本不足）')
        return None
    daily = sub.groupby('入池日').size().mean()
    r5 = (sub['5日内启动'] == 1).mean() * 100
    r10 = (sub['10日内启动'] == 1).mean() * 100
    t5 = sub['入池直接T5%'].mean()
    st5 = sub['启动后T5%'].mean()
    print(f'{label:52s} 事件={n:5d} 每天={daily:4.1f} 5日启动={r5:5.1f}% 10日启动={r10:5.1f}% 入池T5={t5:+5.2f}% 启动后T5={st5:+5.2f}%')
    return dict(daily=daily, r5=r5, r10=r10, t5=t5, st5=st5)

print('=== 基线 ===')
report(v6(), 'V6\' 全条件（现状，每天3.5只）')

# 候选收紧因子（单因子研究中有效或数量收敛明显）
factors = {
    '振幅<15':  lambda m: m & (df['20日振幅%'] < 15),
    '振幅<20':  lambda m: m & (df['20日振幅%'] < 20),
    '额≤4亿':   lambda m: m & (df['成交额亿'] <= 4),
    '额≤3亿':   lambda m: m & (df['成交额亿'] <= 3),
    '乖离≥-2':  lambda m: m & (df['MA20乖离%'] >= -2),
    '乖离≥0':   lambda m: m & (df['MA20乖离%'] >= 0),
    '位置≤0.3': lambda m: m & (df['位置'] <= 0.30),
    '量比≤1.5': lambda m: m & (df['量比'] <= 1.5),
    '5日量比≤1.0': lambda m: m & (df['5日量比'] <= 1.0),
}
names = list(factors.keys())

print()
print('=== 两两组合（V6\' 基础上） ===')
results = []
for a, b in combinations(names, 2):
    m = v6()
    m = factors[a](m); m = factors[b](m)
    r = report(m, f'{a} + {b}')
    if r and 1.5 <= r['daily'] <= 4.5:
        results.append((a, b, r))

print()
print('=== 三因子组合（V6\' 基础上，控制在2-4只/天） ===')
for comb in combinations(names, 3):
    m = v6()
    for c in comb:
        m = factors[c](m)
    r = report(m, ' + '.join(comb))
    if r and 1.5 <= r['daily'] <= 4.5:
        results.append(comb + (r,))
