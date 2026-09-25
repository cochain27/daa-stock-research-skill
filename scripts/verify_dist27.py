"""2026-09-24 距高-27 收紧后生产实跑验证.

跑 pick_low_pos_entry(top=600)，输出最终候选与排序，
确认：①距高 -25→-27 双路径生效 ②排序=标准蓄势优先→pos60→量比 ③top3 截断。
只打印，不写文件不推送。
"""
import sys

sys.path.insert(0, "/Users/chenyuting/Documents/workbuddy/workbuddy-daa/daa-stock-research-skill/scripts")

import low_pos_entry as lpe

picks = lpe.pick_low_pos_entry(top=600)
print(f"\n===== 距高-27 后生产候选（top3 截断后）：{len(picks)} 只 =====")
for p in picks:
    print(
        f"{p.get('股票', p.get('名称', '?')):<8} 路径={p.get('蓄势路径', '?'):<4} "
        f"pos60={p.get('60日位置', '?')} 距高={p.get('距60日高', p.get('距高', '?'))}% "
        f"量比={p.get('量比', '?')} 5日涨幅={p.get('5日涨幅', '?')}% "
        f"成交额={p.get('成交额', '?')}亿"
    )
