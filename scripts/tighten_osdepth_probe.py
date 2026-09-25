# -*- coding: utf-8 -*-
"""生产链路实测：超卖深度（os_dist 最极端值）作为新筛选因子
1) 基线 V6' 数量
2) + 超卖深度≥0.3（事件研究最优档：5日启动7.5%、入池T5+2.14%）
3) + 超卖深度≥0.4（5日启动10.4%）
monkeypatch 方式实跑，不改策略代码。
"""
import sys, os
sys.path.insert(0, '.')
os.environ.setdefault('NO_PROXY', '*')
import low_pos_entry as lpe

# 在 pick_low_pos_entry 筛选链里注入超卖深度检查：
# 生产 _recent_oversold 返回 (hit, os_date, os_age, os_dist, os_first_idx)
# 事件文件超卖深度 = -os_dist（dist60 最极端负值），≥0.3 即 os_dist <= -0.3

orig = lpe._蓄势通过

# monkeypatch _recent_oversold 在 low_pos_entry 内不可行（内部调用），改为包装 pick 前过滤
# 更稳：直接 monkeypatch pick_low_pos_entry 内部使用的 _recent_oversold（若存在）
# 检查 low_pos_entry 是否已有 _recent_oversold

print('low_pos_entry._recent_oversold:', hasattr(lpe, '_recent_oversold'))
