# -*- coding: utf-8 -*-
"""
低位观察池新因子归因研究（第三轮：均线结构/涨停基因/波动压缩/MACD/K线形态）
- 数据：低位池_入池事件_20260923.csv（V6' 基线 1327 事件）+ 本地 K 线补算新因子
- 口径：并集转化 = 触发（LB=1.5）∪ T+5涨幅>15%，10日并集为主指标
- 全部因子在入池日左侧计算（防未来函数）
- 只读研究，不动存量代码
"""
import pandas as pd
import numpy as np
import glob, os, sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(BASE)

# ---------- 1. 加载事件文件 ----------
ev = pd.read_csv('data/低位池_入池事件_20260923.csv', dtype={'代码': str})
tg = pd.read_csv('data/trigger_lb_sensitivity.csv', dtype={'代码': str})
ev['key'] = ev['代码'] + '_' + ev['入池日']
ev['入池日'] = pd.to_datetime(ev['入池日'])

# V6' 基线
B = ev[(ev['成交额亿']<=6)&(ev['位置']<=0.40)&(ev['距高%']<=-25)&
       (ev['MA20乖离%']>=-5)&(ev['20日振幅%']<25)&(ev['量比']<=2.0)].copy()
print(f'V6基线事件: {len(B)}')

# LB=1.5 触发列
t15 = tg[tg['LB下界']==1.5][['代码','入池日','5日内启动','10日内启动','60日内启动']]
t15['key'] = t15['代码'] + '_' + t15['入池日']
t15 = t15.rename(columns={'5日内启动':'tg5','10日内启动':'tg10','60日内启动':'tg60'})
B = B.merge(t15[['key','tg5','tg10','tg60']], on='key', how='left')
B['T5大涨15'] = B['入池直接T5%'] > 15
B['并集5'] = B['tg5'].fillna(False) | B['T5大涨15']
B['并集10'] = B['tg10'].fillna(False) | B['T5大涨15']
B['并集60'] = B['tg60'].fillna(False) | B['T5大涨15']
print(f"并集基线: 5日 {B['并集5'].mean()*100:.2f}% / 10日 {B['并集10'].mean()*100:.2f}% / 60日 {B['并集60'].mean()*100:.2f}%")
print()

# ---------- 2. 加载本地K线 ----------
kl = {}
for p in glob.glob('data/klines/*.csv'):
    sym = os.path.basename(p)[:-4]; code = sym[2:]
    if not code.startswith(('60','00','30')): continue
    raw = pd.read_csv(p)
    if raw.empty: continue
    df = pd.DataFrame({'日期': pd.to_datetime(raw['date']), '开盘': raw['open'].astype(float),
        '最高': raw['high'].astype(float), '最低': raw['low'].astype(float),
        '收盘': raw['last'].astype(float), '成交量': raw['volume'].astype(float)})
    df = df.drop_duplicates(subset=['日期']).sort_values('日期').reset_index(drop=True)
    df = df.dropna(subset=['收盘'])
    if len(df) >= 80: kl[code] = df
print(f'本地K线宇宙: {len(kl)} 只')

# ---------- 3. 对每个事件计算新因子 ----------
def calc_factors(df, i):
    """在入池日 i 计算全部左侧因子"""
    if i < 65: return None
    c = df.loc[i]
    close = df['收盘']; open_ = df['开盘']; high = df['最高']; low = df['最低']
    ma5 = close.iloc[i-4:i+1].mean()
    ma10 = close.iloc[i-9:i+1].mean()
    ma20 = close.iloc[i-19:i+1].mean()
    ma60 = close.iloc[i-59:i+1].mean()
    p = c['收盘']
    f = {}
    # 均线结构
    f['多头排列'] = 1 if (ma5 > ma20 > ma60) else 0
    f['站上MA5'] = 1 if p > ma5 else 0
    f['站上MA20'] = 1 if p > ma20 else 0
    f['站上MA60'] = 1 if p > ma60 else 0
    f['MA5上穿MA20'] = 1 if (close.iloc[i-1] <= close.iloc[i-6:i-1].mean() and p > ma20) else 0
    # MA5 斜率（5日变化）
    ma5_prev = close.iloc[i-9:i-5].mean()
    f['MA5上行'] = 1 if ma5 > ma5_prev else 0
    # MA20 斜率
    ma20_prev = close.iloc[i-24:i-20].mean()
    f['MA20上行'] = 1 if ma20 > ma20_prev else 0
    # MACD
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    f['DIF>0'] = 1 if dif.iloc[i] > 0 else 0
    f['MACD金叉'] = 1 if (dif.iloc[i] > dea.iloc[i] and dif.iloc[i-1] <= dea.iloc[i-1]) else 0
    f['MACD多头'] = 1 if dif.iloc[i] > dea.iloc[i] else 0
    # 涨停基因
    chg = close.pct_change() * 100
    f['20日涨停数'] = int((chg.iloc[i-19:i+1] >= 9.5).sum())
    f['60日涨停数'] = int((chg.iloc[i-59:i+1] >= 9.5).sum())
    f['60日最大单日涨幅'] = chg.iloc[i-59:i+1].max()
    f['近5日最大涨幅'] = chg.iloc[i-4:i+1].max()
    # 波动率压缩
    amp20 = (high.iloc[i-19:i+1].max() - low.iloc[i-19:i+1].min()) / close.iloc[i-20] * 100 if close.iloc[i-20] > 0 else 999
    amp60 = (high.iloc[i-59:i+1].max() - low.iloc[i-59:i+1].min()) / close.iloc[i-60] * 100 if close.iloc[i-60] > 0 else 999
    f['波动压缩(20/60)'] = amp20 / amp60 if amp60 > 0 else 999
    amp5 = (high.iloc[i-4:i+1].max() - low.iloc[i-4:i+1].min()) / close.iloc[i-5] * 100 if close.iloc[i-5] > 0 else 999
    amp15 = (high.iloc[i-19:i-4].max() - low.iloc[i-19:i-4].min()) / close.iloc[i-20] * 100 if close.iloc[i-20] > 0 else 999
    f['近5日振幅'] = amp5
    f['波动收缩(5/15)'] = amp5 / amp15 if amp15 > 0 else 999
    # 20日趋势
    f['20日涨幅'] = (p / close.iloc[i-20] - 1) * 100
    # 位置变化（vs 10日前）
    win_now = close.iloc[i-59:i+1]
    win_prev = close.iloc[i-69:i-9]
    pos_now = (p - win_now.min()) / max(win_now.max() - win_now.min(), 1e-9)
    pos_prev = (close.iloc[i-10] - win_prev.min()) / max(win_prev.max() - win_prev.min(), 1e-9)
    f['位置抬升'] = 1 if pos_now - pos_prev >= 0.05 else 0
    # RSI14
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    f['RSI14'] = rsi.iloc[i] if np.isfinite(rsi.iloc[i]) else 50.0
    f['RSI超卖<30'] = 1 if f['RSI14'] < 30 else 0
    # 距60日低点
    f['距60日低'] = (p / win_now.min() - 1) * 100
    f['贴地(<8%)'] = 1 if f['距60日低'] < 8 else 0
    # K线形态
    f['当日收阳'] = 1 if c['收盘'] > c['开盘'] else 0
    yin = (close.iloc[i-4:i+1] < open_.iloc[i-4:i+1]).sum()
    f['近5日阴线数'] = int(yin)
    # 量能
    v20 = df['成交量'].iloc[i-19:i].mean()
    f['量比20日'] = c['成交量'] / v20 if v20 > 0 else 999
    v3 = df['成交量'].iloc[i-3:i].mean()
    f['连续缩量3日'] = 1 if (df['成交量'].iloc[i] < v3 and df['成交量'].iloc[i-1] < df['成交量'].iloc[i-2]) else 0
    f['温和放量'] = 1 if 0.8 <= f['量比20日'] <= 1.5 else 0
    return f

rows = []
miss = 0
for _, r in B.iterrows():
    code, d = r['代码'], r['入池日']
    df = kl.get(code)
    if df is None:
        miss += 1; continue
    idx = df.index[df['日期'] == d]
    if len(idx) == 0:
        miss += 1; continue
    i = idx[0]
    f = calc_factors(df, i)
    if f is None:
        miss += 1; continue
    f.update({'代码': code, '入池日': d, '路径': r['路径'],
              '并集5': r['并集5'], '并集10': r['并集10'], '并集60': r['并集60'],
              '入池T5': r['入池直接T5%'], '启动后T5': r.get('启动后T5%', np.nan)})
    rows.append(f)

R = pd.DataFrame(rows)
print(f'成功计算因子: {len(R)} / {len(B)}（缺失 {miss}）')
R.to_csv('data/watch_newfactor_attribution.csv', index=False)
print()

# ---------- 4. 分桶归因 ----------
base5 = R['并集5'].mean()*100
base10 = R['并集10'].mean()*100
base60 = R['并集60'].mean()*100
print(f'基线（V6\'+LB1.5并集）: n={len(R)} 5日={base5:.2f}% 10日={base10:.2f}% 60日={base60:.2f}% 入池T5={R["入池T5"].mean():+.2f}%')
print()

def bucket(col, edges, labels=None, min_n=40, desc=''):
    print(f'=== {col} {desc} ===')
    tmp = R.copy()
    try:
        tmp['_b'] = pd.cut(tmp[col], bins=edges, labels=labels, right=False)
    except Exception:
        return
    for b, g in tmp.groupby('_b', observed=True):
        n = len(g)
        mark = '' if n >= min_n else ' ⚠️小样本'
        print(f'  {str(b):14s} n={n:4d} 每天{n/g["入池日"].nunique():4.2f} | 并集5日={g["并集5"].mean()*100:5.2f}% 并集10日={g["并集10"].mean()*100:5.2f}% 并集60日={g["并集60"].mean()*100:5.2f}% | 入池T5={g["入池T5"].mean():+5.2f}%{mark}')
    print()

def bucket_bin(col, desc=''):
    print(f'=== {col} {desc} ===')
    for v, g in R.groupby(col):
        n = len(g)
        mark = '' if n >= min_n else ' ⚠️小样本'
        print(f'  {col}={v}: n={n:4d} 每天{n/g["入池日"].nunique():4.2f} | 并集5日={g["并集5"].mean()*100:5.2f}% 并集10日={g["并集10"].mean()*100:5.2f}% 并集60日={g["并集60"].mean()*100:5.2f}% | 入池T5={g["入池T5"].mean():+5.2f}%{mark}')
    print()

min_n = 40
# 0/1 因子
for c in ['多头排列','站上MA5','站上MA20','站上MA60','MA5上行','MA20上行','DIF>0','MACD金叉','MACD多头',
          '位置抬升','RSI超卖<30','贴地(<8%)','当日收阳','连续缩量3日','温和放量','MA5上穿MA20']:
    if c in R.columns:
        bucket_bin(c)

# 连续因子
bucket('RSI14', [0,20,30,40,50,100], ['<20','20-30','30-40','40-50','50+'])
bucket('20日涨停数', [-1,0,1,2,99], ['0次','1次','2次+'], desc='(过去20日)')
bucket('60日涨停数', [-1,0,1,2,3,99], ['0次','1次','2次','3次+'], desc='(过去60日)')
bucket('60日最大单日涨幅', [0,5,8,10,15,99], ['<5%','5-8%','8-10%','10-15%','15%+'])
bucket('近5日最大涨幅', [-99,2,4,6,9,99], ['<2%','2-4%','4-6%','6-9%','9%+'])
bucket('波动压缩(20/60)', [0,0.5,0.6,0.7,0.8,0.9,99], ['<0.5','0.5-0.6','0.6-0.7','0.7-0.8','0.8-0.9','0.9+'])
bucket('近5日振幅', [0,5,8,10,15,99], ['<5%','5-8%','8-10%','10-15%','15%+'])
bucket('波动收缩(5/15)', [0,0.5,0.7,1.0,1.5,99], ['<0.5','0.5-0.7','0.7-1.0','1.0-1.5','1.5+'])
bucket('20日涨幅', [-99,-15,-10,-5,0,5,99], ['<-15%','-15~-10','-10~-5','-5~0','0~5','5%+'])
bucket('距60日低', [0,5,8,12,20,99], ['<5%','5-8%','8-12%','12-20%','20%+'])
bucket('量比20日', [0,0.5,0.8,1.0,1.5,2.0,99], ['<0.5','0.5-0.8','0.8-1.0','1.0-1.5','1.5-2.0','2.0+'])
bucket('近5日阴线数', [-1,0,1,2,3,5], ['0','1','2','3','4-5'])
print('done')
