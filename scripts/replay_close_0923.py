"""2026-09-24 凌晨：补跑 09-23 收盘复盘（自动化 429 漏跑）.

要点：
1. patch close_review.datetime.now → 2026-09-23 00:40，使 today=2026-09-23
   （复盘文件名/标题/笔记本流水日期正确）。
2. 真实时间 00:40 < 15:05，全部判定走「缓存最后一行=09-23 收盘」分支，
   A3 当日K线追加不触发（数据安全）。
3. patch subprocess.run 跳过 daily_report --no-push 选股子进程：
   避免 09-23 收盘数据被登记成 09-24 台账（污染）；代价=09-23 趋势/短线
   推荐流水缺失（本就因 429 无法纯净重现）。低位池扫描由 close_review
   主流程自行执行，不受影响。
4. 真实推送微信 + 笔记本更新（这正是补跑目的）。
"""
import sys
from datetime import datetime

sys.path.insert(0, "/Users/chenyuting/Documents/workbuddy/workbuddy-daa/daa-stock-research-skill/scripts")

import close_review


class _FakeDT(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 9, 23, 0, 40)


def _fake_run(*args, **kwargs):
    class _R:
        returncode = 0
        stdout = "[补跑] 跳过 daily_report 选股子进程（防 09-24 台账污染）"
        stderr = ""

    return _R()


close_review.datetime = _FakeDT
# 2026-09-24 修复：run_close 内部 `import subprocess`（517 行）拿到的是全局模块对象，
# 必须 patch 全局 subprocess.run 才能拦住；此前 patch close_review.subprocess
# 因模块级无该属性而 AttributeError 秒退（第一次补跑失败根因）。
import subprocess as _sub
_sub.run = _fake_run

print("=== 补跑 09-23 收盘复盘 开始 ===", flush=True)
close_review.run_close()
print("=== 补跑结束 ===", flush=True)
