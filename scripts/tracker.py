# -*- coding: utf-8 -*-
"""tracker.py 兼容垫片（2026-09-05 双策略分立后保留）
旧代码（如 daily_report.py）通过 tracker.* 调用统一入口；
新代码直接 import trend_tracker / short_tracker。
"""
from trend_tracker import (
    log_picks as _trend_log,
    update_track as _trend_update,
    analyze_track_pool as _trend_analyze,
    stats as _trend_stats,
    load_open_picks as _trend_load,
    check_and_init as _trend_init,
)
from short_tracker import (
    log_picks as _short_log,
    update_track as _short_update,
    analyze_track_pool as _short_analyze,
    stats as _short_stats,
    roll_backtest,
    load_open_picks as _short_load,
    check_and_init as _short_init,
)
from tracker_common import virtual_nav_by_tag
from config import DATA_DIR, VIRTUAL_BASE

# 默认路由到趋势模块（旧版单策略行为）
def log_picks(picks, market_env=None):
    """兼容：默认写入趋势台账。推荐双策略时调用 trend_tracker.log_picks() 和 short_tracker.log_picks()"""
    return _trend_log(picks, market_env)

def update_track():
    """兼容：默认更新趋势台账。双策略时分别调用 trend_tracker.update_track() 和 short_tracker.update_track()"""
    return _trend_update()

def analyze_track_pool():
    """兼容：默认分析趋势跟踪池。双策略时分别调用 trend_tracker.analyze_track_pool() 和 short_tracker.analyze_track_pool()"""
    return _trend_analyze()

def stats():
    """兼容：默认返回趋势统计。双策略时需分别调用 trend_tracker.stats() 和 short_tracker.stats()"""
    return _trend_stats()

def load_open_picks():
    """兼容：默认读趋势台账。双策略时需分别调用 trend_tracker.load_open_picks() 和 short_tracker.load_open_picks()"""
    return _trend_load()

def virtual_nav():
    """返回趋势+短线合计虚拟净值"""
    trend_nav = virtual_nav_by_tag(str(DATA_DIR / "推荐台账_右侧趋势.csv"), "趋势")
    short_nav = virtual_nav_by_tag(str(DATA_DIR / "推荐台账_短线激进.csv"), "短线")
    total_nav = trend_nav.get("净值", 100000) + short_nav.get("净值", 100000) - 100000
    return {
        "净值": round(total_nav, 0),
        "累计收益": round(total_nav - VIRTUAL_BASE, 0),
        "累计收益率": round((total_nav / VIRTUAL_BASE - 1) * 100, 2),
        "已实现盈亏": round(trend_nav.get("已实现盈亏", 0) + short_nav.get("已实现盈亏", 0), 0),
        "浮动盈亏": round(trend_nav.get("浮动盈亏", 0) + short_nav.get("浮动盈亏", 0), 0),
        "已了结笔数": trend_nav.get("已了结笔数", 0) + short_nav.get("已了结笔数", 0),
        "持仓笔数": trend_nav.get("持仓笔数", 0) + short_nav.get("持仓笔数", 0),
        "基准": VIRTUAL_BASE,
        "趋势净值": trend_nav,
        "短线净值": short_nav,
    }

# 显式导出子模块供新代码使用
TREND = type("TREND", (), {
    "log_picks": _trend_log,
    "update_track": _trend_update,
    "analyze_track_pool": _trend_analyze,
    "stats": _trend_stats,
    "load_open_picks": _trend_load,
    "check_and_init": _trend_init,
})()

SHORT = type("SHORT", (), {
    "log_picks": _short_log,
    "update_track": _short_update,
    "analyze_track_pool": _short_analyze,
    "stats": _short_stats,
    "roll_backtest": roll_backtest,
    "load_open_picks": _short_load,
    "check_and_init": _short_init,
})()
