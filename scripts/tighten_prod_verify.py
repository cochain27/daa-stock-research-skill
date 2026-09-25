# -*- coding: utf-8 -*-
"""生产链路收紧档位实测：monkeypatch low_pos_entry 模块级常量后实跑 pick_low_pos_entry
只读验证，不改任何策略文件。
"""
import sys, os
sys.path.insert(0, '.')
os.environ.setdefault('NO_PROXY', '*')
import low_pos_entry as lpe

def run(label, **patches):
    for k, v in patches.items():
        setattr(lpe, k, v)
    print(f'\n=== {label} ===')
    picks = lpe.pick_low_pos_entry(top=600, quiet=True)
    print(f'原始候选数: {len(picks)}')
    if picks:
        for p in picks[:6]:
            print(f"  {p['代码']} {p['名称']} pos60={p.get('60日位置',0):.2f} 量比={p.get('量比','')} 额={p.get('成交额亿','')}亿")
    filt = lpe.filter_active_pool(picks)
    print(f'filter_active_pool 后: {len(filt)}')

# 基线：现状 V6'（振幅<25 量比≤2.0）
run('基线 V6\'（振幅<25, 乖离≥-5）')

# 档位A：振幅<15 + 乖离≥-2
run('档位A 振幅<15 + 乖离≥-2',
    LOW_POS_ENTRY_MAX_AMP20=15,
    LOW_POS_ENTRY_SCAN_BIAS20_MIN=-2,
    LOW_POS_ENTRY_OS_SCAN_AMP_MAX=15)  # 超卖路径若也用振幅

# 档位B：振幅<15 + 乖离≥-2 + 成交额≤4亿
run('档位B 振幅<15 + 乖离≥-2 + 额≤4亿',
    LOW_POS_ENTRY_MAX_AMP20=15,
    LOW_POS_ENTRY_SCAN_BIAS20_MIN=-2,
    LOW_POS_ENTRY_OS_SCAN_AMP_MAX=15,
    LOW_POS_ENTRY_MIN_AMOUNT=2e8,
    LOW_POS_ENTRY_MAX_AMOUNT=4e8,
    LOW_POS_ENTRY_OS_SCAN_AMOUNT_RANGE=(2e8, 4e8))
