# -*- coding: utf-8 -*-
"""生产链路实测：超卖深度（最极端 dist60）作为新筛选因子
monkeypatch low_pos_entry._recent_oversold 为「最极端 dist60」口径（与事件文件一致）
然后跑 pick_low_pos_entry，看加超卖深度阈值后的实际数量与名单。
只读研究，不改策略代码。
"""
import sys, os
sys.path.insert(0, '.')
os.environ.setdefault('NO_PROXY', '*')
import pandas as pd
import low_pos_entry as lpe

LOOKBACK = lpe.LOW_POS_ENTRY_RECENT_OVERSOLD_LOOKBACK
DIST_MAX = lpe.LOW_POS_ENTRY_RECENT_OVERSOLD_DIST_MAX

def _recent_oversold_detail(ind_df, cur_idx):
    """与事件文件同口径：窗口内最极端 dist60 / 最极端 pos60"""
    start = max(0, cur_idx - LOOKBACK)
    window = ind_df.iloc[start:cur_idx]
    best_d, best_p = None, None
    hit = False
    for idx, row in window.iterrows():
        p60 = row.get("pos60")
        d60 = row.get("dist60")
        if p60 is not None and p60 < 0.5:
            hit = True
        if d60 is not None and d60 < DIST_MAX:
            hit = True
            if best_d is None or d60 < best_d:
                best_d = d60
        if p60 is not None and (best_p is None or p60 < best_p):
            best_p = p60
    if not hit:
        return False, None, None, None, None
    return True, None, best_p, best_d, None

# 备份原函数
_orig = lpe._recent_oversold

def run(label, min_os_depth=None):
    # 注入超卖深度过滤：用 wrapper 包 _recent_oversold，让调用方拿到的 os_dist 是最极端值
    if min_os_depth is not None:
        def _wrapped(ind_df, cur_idx):
            hit, d, p, dist, idx = _recent_oversold_detail(ind_df, cur_idx)
            # 深度不足的视为未超卖（强制走标准蓄势路径或直接不过）
            if hit and dist is not None and abs(dist) < min_os_depth:
                return False, None, None, None, None
            return hit, d, p, dist, idx
        lpe._recent_oversold = _wrapped
    else:
        lpe._recent_oversold = _orig
    picks = lpe.pick_low_pos_entry(top=600, quiet=True)
    print(f'\n=== {label} ===')
    print(f'原始候选数: {len(picks)}')
    if picks:
        for p in picks:
            print(f"  {p['代码']} {p['名称']} pos60={p.get('60日位置',0):.2f} 量比={p.get('量比','')} 额={p.get('成交额亿','')}亿")
    return picks

run('基线 V6\'（现状）')
run('V6\' + 超卖深度≥0.3', min_os_depth=0.30)
run('V6\' + 超卖深度≥0.35', min_os_depth=0.35)
run('V6\' + 超卖深度≥0.4', min_os_depth=0.40)
