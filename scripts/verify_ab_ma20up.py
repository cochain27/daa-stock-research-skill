# -*- coding: utf-8 -*-
"""A+B 落地一次性验证（只读，不写文件不推送）：
① 生产口径（A=MA20上行硬过滤 + B=top3截断）→ 最终候选
② A 关 + topN 关 → A 前全量基线，对比 A 的筛除力度
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import low_pos_entry as lpe


def show(tag, picks):
    print(f"\n=== {tag}: {len(picks)} 只 ===")
    for p in picks:
        print(f"{p['名称']:　<6} pos60={p['60日位置']:.3f} 距高={p['dist60%']:+.1f}% "
              f"量比={p['量比']:.2f} 5日={p['5日涨幅%']:+.1f}% 额={p['成交额亿']:.2f}亿 "
              f"MA20上行={p['MA20上行']} 路径={p['蓄势路径']}")


# ① 生产口径（config 默认 A=True, TOPN=3）
p1 = lpe.filter_active_pool(lpe.pick_low_pos_entry(top=600, quiet=True))
show("A+B 生产口径", p1)

# ② 对照：关 A + 关 topN
lpe.LOW_POS_ENTRY_SCAN_MA20_UP = False
lpe.LOW_POS_ENTRY_TOPN = None
p2 = lpe.filter_active_pool(lpe.pick_low_pos_entry(top=600, quiet=True))
show("A关闭·topN关闭（对照基线）", p2)
