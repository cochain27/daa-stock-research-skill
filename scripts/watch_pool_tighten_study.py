# -*- coding: utf-8 -*-
"""观察池收紧研究：每天 2-4 只 + 转化率最高
输入：data/低位池_入池事件_20260923.csv（18279 事件，含未来标签）
口径：V6' 全条件为基线（成交额≤6亿 + pos60≤0.40 + 距高≤-25% + MA20乖离≥-5% + 振幅<25 + 量比≤2.0）
方法：单因子逐步收紧 + 组合搜索，评估 每天数量 / 5日启动率 / 10日启动率 / 入池T5均值 / 启动后T5均值
只读研究，不改任何策略代码。
"""
import pandas as pd

df = pd.read_csv('data/低位池_入池事件_20260923.csv')
df['入池日'] = pd.to_datetime(df['入池日'])
N_DAYS = df['入池日'].nunique()

# ---- 基线：V6' 全条件（09-23 生产扫描端） ----
def v6_mask():
    return (
        (df['成交额亿'] <= 6) &
        (df['位置'] <= 0.40) &
        (df['距高%'] <= -25) &
        (df['MA20乖离%'] >= -5) &
        (df['20日振幅%'] < 25) &
        (df['量比'] <= 2.0)
    )

def report(mask, label):
    sub = df[mask]
    n = len(sub)
    if n == 0:
        print(f'{label:44s} 事件=0')
        return
    daily = sub.groupby('入池日').size().mean()
    r5 = (sub['5日内启动'] == 1).mean() * 100
    r10 = (sub['10日内启动'] == 1).mean() * 100
    r60 = (sub['60日内启动'] == 1).mean() * 100
    t5 = sub['入池直接T5%'].mean()
    t5_pos = (sub['入池直接T5%'] > 0).mean() * 100
    st5 = sub['启动后T5%'].mean()
    print(f'{label:44s} 事件={n:5d} 每天={daily:4.1f} 5日启动={r5:5.1f}% 10日启动={r10:5.1f}% 60日启动={r60:5.1f}% 入池T5={t5:+5.2f}% 胜率={t5_pos:4.1f}% 启动后T5={st5:+5.2f}%')

base = v6_mask()
print(f'=== 基线：V6\' 全条件（交易日 {N_DAYS} 天） ===')
report(base, 'V6\' 全条件（现状）')

# ---- 单因子收紧 ----
print()
print('=== 单因子收紧（每次只在 V6\' 基础上加严一个因子） ===')
for val in (20, 15, 12):
    report(base & (df['20日振幅%'] < val), f'振幅<{val}')
for val in (1.8, 1.5, 1.2):
    report(base & (df['量比'] <= val), f'量比≤{val}')
for val in (0.35, 0.30, 0.25, 0.20):
    report(base & (df['位置'] <= val), f'位置≤{val}')
for val in (-2, 0):
    report(base & (df['MA20乖离%'] >= val), f'MA20乖离≥{val}（{"贴线/站上" if val==0 else "近线"}）')
for val in (4, 3):
    report(base & (df['成交额亿'] <= val), f'成交额≤{val}亿')
for val in (1.0, 0.8):
    report(base & (df['5日量比'] <= val), f'5日量比≤{val}')
# 5日涨幅：温和上扬段
report(base & (df['5日涨幅%'] >= 0) & (df['5日涨幅%'] <= 8), '5日涨幅0~8%')
report(base & (df['5日涨幅%'] >= 0) & (df['5日涨幅%'] <= 5), '5日涨幅0~5%')
report(base & (df['5日涨幅%'] >= -2) & (df['5日涨幅%'] <= 5), '5日涨幅-2~5%')
# 量能收缩画像
if '量能收缩' in df.columns:
    report(base & (df['量能收缩'] == 1), '+ 量能收缩')
if '连续蓄势' in df.columns:
    report(base & (df['连续蓄势'] == 1), '+ 连续蓄势')
