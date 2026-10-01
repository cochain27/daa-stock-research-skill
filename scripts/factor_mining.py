# -*- coding: utf-8 -*-
"""回测明细 × 信号日特征 = 因子挖掘（训练/测试两段一致性验证）
用法: python factor_mining.py [--pools 初动,趋势,短线]
"""
import csv
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import train_test_backtest as tt

DETAIL = os.path.join(tt.BASE, "data", "回测_训练2022-23_测试2025-26_明细.csv")
DETAIL = os.path.abspath(DETAIL)

# 信号日口径特征清单
FACTORS = [
    # (列名, 显示名, 方向, 业务阈值或None)
    ("lb", "量比", "gt", 1.5),
    ("bias20", "MA20乖离", "gt", 0),
    ("pos60", "60日位(高低)", "gt", 0.3),
    ("pos60_c", "60日位(收盘)", "gt", 0.35),
    ("rsi14", "RSI14", "gt", 55),
    ("chg5", "5日涨幅%", "gt", 3),
    ("chg5", "5日涨幅%", "lt", 0),
    ("振幅", "当日振幅", "lt", 6),
    ("amp10_mean", "10日均振幅", "lt", 7),
    ("vol10_60", "量能比10/60", "gt", 1.2),
    ("ma20up", "MA20上行", "eq", True),
    ("week_up", "周线上扬", "eq", True),
    ("std20", "20日波动率", "lt", 0.035),
    ("成交额", "成交额亿元", "gt", 8e8),
    ("dist60", "距60日高%", "gt", -15),
]


def loc_idx(dates, buy_date, target):
    """返回特征日期 <= 买入日前一交易日（信号日）的行索引"""
    # dates: pd.Series datetime64; buy_date: 'YYYY-MM-DD'
    bd = pd.Timestamp(buy_date)
    pos = dates.searchsorted(bd) - 1          # 买入日或之前最后一行
    for k in range(pos, -1, -1):
        if dates.iloc[k] < bd:                # 必须严格在买入日之前（信号日）
            return k
    return -1


def main():
    pools = [p.strip() for p in sys.argv[sys.argv.index("--pools") + 1].split(",")] if "--pools" in sys.argv else ["初动", "趋势", "短线"]
    print("[1/3] 加载特征...", flush=True)
    feat = tt._load_all()
    rows = list(csv.reader(open(DETAIL, encoding="utf-8")))[1:]
    print(f"[2/3] 明细 {len(rows)} 笔，关联信号日特征...", flush=True)

    # 预构建 代码 -> (日期array, 特征DataFrame)
    idx_cache = {c: f["日期"] for c, f in feat.items()}

    # 收集：每笔信号的特征值
    recs = []  # (段, 池, 特征dict, 收益)
    for r in rows:
        seg, buy_date, code = r[0], r[1], r[3]  # 名称列放代码
        pool, ret = r[9].strip(), float(r[5])
        if pool not in pools:
            continue
        f = feat.get(code)
        if f is None:
            continue
        i = loc_idx(f["日期"], buy_date, None)
        if i < 0:
            continue
        row = f.iloc[i]
        recs.append((seg, pool, row, ret))

    print(f"    关联成功 {len(recs)} 笔", flush=True)

    # [3/3] 每因子 × 每池分桶统计（命中 vs 未命中，训练/测试分开）
    def _stat(sub):
        if not sub:
            return None
        n = len(sub)
        wins = sum(1 for x in sub if x > 0)
        rets = sub
        wr = [x for x in rets if x > 0]
        lr = [x for x in rets if x <= 0]
        pl = (np.mean(wr) / abs(np.mean(lr))) if wr and lr else float("inf")
        return n, wins / n * 100, np.mean(rets), pl

    out_lines = []
    for pool in pools:
        print(f"\n########## {pool} 因子两段一致性 ##########", flush=True)
        for col, name, op, thr in FACTORS:
            hit_t, miss_t = [], []
            hit_s, miss_s = [], []
            for seg, p, row, ret in recs:
                if p != pool:
                    continue
                v = row.get(col)
                if v is None or pd.isna(v):
                    continue
                try:
                    ok = (float(v) > thr) if op == "gt" else (float(v) < thr) if op == "lt" else bool(v) == thr
                except (ValueError, TypeError):
                    continue
                if seg == "训练":
                    (hit_t if ok else miss_t).append(ret)
                else:
                    (hit_s if ok else miss_s).append(ret)
            st_t, sm_t = _stat(hit_t), _stat(miss_t)
            st_s, sm_s = _stat(hit_s), _stat(miss_s)
            if not (st_t and st_s):
                continue

            def _fmt(s):
                if not s:
                    return "空"
                return f"{s[0]}笔/{s[1]:.0f}%/{s[2]:+.2f}%/{s[3]:.2f}"

            line = (f"{name}({op}{thr if thr is not None else ''})  "
                    f"训练命中:{_fmt(st_t)} 未中:{_fmt(sm_t)} | "
                    f"测试命中:{_fmt(st_s)} 未中:{_fmt(sm_s)}")
            # 两段同向优势标记（未中组空则不判）
            adv_t = (st_t[1] - sm_t[1]) if sm_t else 0
            adv_s = (st_s[1] - sm_s[1]) if sm_s else 0
            if sm_t and sm_s and adv_t * adv_s > 0 and min(abs(adv_t), abs(adv_s)) >= 5:
                line += "  <<< 两段同向"
            print(line, flush=True)
            out_lines.append((pool, line))


if __name__ == "__main__":
    main()