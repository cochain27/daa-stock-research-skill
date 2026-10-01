# -*- coding: utf-8 -*-
"""A股交易日历（法定休市日判断）。

数据源：上交所《关于2026年部分节假日休市安排的通知》（上证公告〔2025〕45号）
        + 《关于2026年中秋节、国庆节休市安排的公告》（上证公告〔2026〕22号）。

用途：休市日跳过晨报/盘中/收盘自动化，避免用 T-1 静态数据重复出报告、误导推送。
设计原则：**宁可多跑，不可漏跑** —— 年份无数据或判断异常时一律返回 True（按交易日处理）。
"""
from datetime import date, timedelta

# 2026 年法定休市区间（含端点），周末已单独排除，此处照录公告口径
HOLIDAY_RANGES = {
    2026: [
        ("2026-01-01", "2026-01-03"),  # 元旦
        ("2026-02-15", "2026-02-23"),  # 春节
        ("2026-04-04", "2026-04-06"),  # 清明
        ("2026-05-01", "2026-05-05"),  # 劳动节
        ("2026-06-19", "2026-06-21"),  # 端午
        ("2026-09-25", "2026-09-27"),  # 中秋
        ("2026-10-01", "2026-10-07"),  # 国庆
    ],
}


def _parse(s):
    y, m, d = (int(x) for x in s.split("-"))
    return date(y, m, d)


def holiday_set(year):
    """返回该年度全部休市日（含周末）。"""
    out = set()
    d = date(year, 1, 1)
    while d.year == year:
        if d.weekday() >= 5:
            out.add(d)
        d += timedelta(days=1)
    for a, b in HOLIDAY_RANGES.get(year, []):
        d = _parse(a)
        end = _parse(b)
        while d <= end:
            out.add(d)
            d += timedelta(days=1)
    return out


def is_trading_day(d=None):
    """是否交易日。无日历数据的年份或异常时返回 True。"""
    try:
        d = d or date.today()
        return d not in holiday_set(d.year)
    except Exception:
        return True


def next_trading_day(d=None):
    """下一个交易日（含当日若为交易日）。"""
    d = d or date.today()
    for _ in range(15):
        if is_trading_day(d):
            return d
        d += timedelta(days=1)
    return d


if __name__ == "__main__":
    import sys
    t = date.today()
    print(f"today={t} trading={is_trading_day(t)} next={next_trading_day(t)}")
    if len(sys.argv) > 1:
        for arg in sys.argv[1:]:
            d = _parse(arg)
            print(f"{d} trading={is_trading_day(d)}")
