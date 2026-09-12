# -*- coding: utf-8 -*-
import sys
sys.path.insert(0, 'scripts')
from daily_report import LOW_POS_ENTRY_ENABLED_REPORT
print("低位埋伏集成:", "已启用" if LOW_POS_ENTRY_ENABLED_REPORT else "未启用")

import inspect
sig = inspect.signature(__import__('daily_report', fromlist=['generate_full_report']).generate_full_report)
params = list(sig.parameters.keys())
print("generate_full_report 参数:", params)
assert 'low_pos_picks' in params, "参数缺失!"

sig2 = inspect.signature(__import__('daily_report', fromlist=['generate_brief']).generate_brief)
params2 = list(sig2.parameters.keys())
print("generate_brief 参数:", params2)
assert 'low_pos_picks' in params2, "generate_brief参数缺失!"

# 模拟生成带低位埋伏的日报片段
from daily_report import generate_full_report
mock_picks = [{
    '代码': '600184', '名称': '光电股份', '日期': '2026-09-10',
    '现价': 18.50, '60日位置': 0.188, 'dist60%': -45.9,
    '量比': 0.96, '成交额亿': 2.68, '买点区间': '18.32-19.06',
    '止损价': 17.39, '止盈1': 19.63, '蓄势路径': '标准蓄势',
    '蓄势特征': '缩量0.96x/低位19%/距高-46%/横盘5日'
}]
report = generate_full_report(
    temp=50, details={}, pos_advice=('30%-50%', '弱势整理'),
    boards=None, picks=[], rps_rows=None, track_rows=None, track_overview=None,
    market_env='弱势', env_desc='成交低迷', env_reason='量能不足',
    low_pos_picks=mock_picks
)
# 检查章节是否存在
sections = ['三·低位埋伏候选池（观察）', '明日关注', '研究观察池', '四、推荐跟踪池']
for s in sections:
    assert s in report, f"章节缺失: {s}"
    print(f"章节存在: {s} ✅")
print()
print("=== 生成的低位埋伏章节片段预览 ===")
start = report.find('三、低位埋伏候选池')
end = report.find('四、推荐跟踪池')
print(report[start:end])
print("✅ 日报集成验证通过！")
