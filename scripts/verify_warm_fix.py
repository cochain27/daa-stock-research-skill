# -*- coding: utf-8 -*-
"""任务#12 验证脚本：K线缓存补齐后，复算深圳华强/北斗星通在 09-21（晨报T-1口径）
与 09-22（复盘当日口径）的初动池判定，与 09-22 真实数据重算结论对照：
  - 华强：09-21 应入选（真实形态 OK，晨报推它没错）；09-22 量比 1.22<1.3 应落选
  - 北斗：09-21 量比 1.25<1.3 应落选（晨报推它是脏缓存误推）；09-22 量比 3.20>2.5 应落选
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pandas as pd

from warm_start_entry import KLINE_DIR, _indicators, _is_pass

TARGETS = {"sz000062": "深圳华强", "sz002151": "北斗星通"}


def judge(sym, cutoff=None):
    df = pd.read_csv(KLINE_DIR / f"{sym}.csv").rename(columns={
        "date": "日期", "open": "开盘", "last": "收盘", "high": "最高",
        "low": "最低", "volume": "成交量", "amount": "成交额"})
    df["日期"] = pd.to_datetime(df["日期"])
    df = df.sort_values("日期").reset_index(drop=True)
    if cutoff:
        df = df[df["日期"] <= cutoff].reset_index(drop=True)
    df["涨跌幅"] = df["收盘"].pct_change() * 100
    row = _indicators(df).iloc[-1]
    ok, why = _is_pass(row)
    return str(row["日期"].date()), float(row["lb"]), ok, why


if __name__ == "__main__":
    for sym, name in TARGETS.items():
        d1, lb1, ok1, why1 = judge(sym, "2026-09-21")
        d2, lb2, ok2, why2 = judge(sym)
        tag1 = "✅入选" if ok1 else f"❌落选({why1})"
        tag2 = "✅入选" if ok2 else f"❌落选({why2})"
        print(f"{name}({sym})")
        print(f"  {d1} 判定 量比{lb1:.2f} → {tag1}")
        print(f"  {d2} 判定 量比{lb2:.2f} → {tag2}")
