# -*- coding: utf-8 -*-
"""数据健康体检（K线 + 虚拟盘 + 台账），只读不改。

用途：周报/复盘前先跑一遍，把「K线缺日、假K线、成交价越界、台账票无K线」
这类会让统计口径失真的问题前置暴露，而不是事后靠人眼发现。

背景（2026-10-23）：10-22 全天 K 线在 1008/1419 只票里整行缺失，原因是
_ensure_klines_fresh 只校验基准日、不校验中间缺口，而 10-23 当日行已被 A3
追加 → 判为不新鲜。直接后果是初动池 T+5 重放把 10-21 当 10-22 用，同一形态
被登记两次、且 -8% 止损可能用错日的最低点。

用法：
    python scripts/check_data_health.py
    python scripts/check_data_health.py --days 60 --json out.json
退出码：0=无 P0；1=有 P0（缺日/越界/无K线）。
"""
import argparse
import glob
import json
import os
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
KLINE_DIR = ROOT / "data" / "klines"
DATA = ROOT / "data"

# 台账/交易文件 → 日期列 → 价格列
LEDGERS = [
    ("虚拟交易.csv", {"虚拟建仓日": "虚拟成本", "虚拟了结日": "了结价"}),
    ("推荐台账_短线激进.csv", {"虚拟建仓日": "虚拟成本"}),
    ("推荐台账_右侧趋势.csv", {"虚拟建仓日": "虚拟成本"}),
    ("推荐台账_低位触发.csv", {"虚拟建仓日": "虚拟成本", "触发日": "触发价"}),
]


def norm_code(code):
    """台账里代码前缀不统一（有的带 sz/sh，有的是纯6位），统一成纯6位。"""
    c = str(code).strip().lower()
    if c[:2] in ("sh", "sz", "bj"):
        c = c[2:]
    return c.zfill(6)


def load_kline(code):
    """code 可带前缀也可不带。"""
    raw = str(code).strip()
    cands = []
    if raw[:2].lower() in ("sh", "sz", "bj"):
        cands.append(KLINE_DIR / f"{raw.lower()}.csv")
    bare = norm_code(raw)
    cands += [KLINE_DIR / f"{p}{bare}.csv" for p in ("sh", "sz", "bj")]
    for p in cands:
        if p.exists():
            try:
                return pd.read_csv(p, encoding="utf-8-sig")
            except Exception:
                return None
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60, help="尾部窗口自然行数（默认60）")
    ap.add_argument("--json", default=None, help="把结果另存为 JSON")
    args = ap.parse_args()

    files = sorted(glob.glob(str(KLINE_DIR / "*.csv")))
    if not files:
        print("无 K 线缓存")
        return 1

    # ---------- 1. 结构体检 ----------
    dup, unsorted, badpx, rows = [], [], [], {}
    day_cnt = Counter()
    for f in files:
        sym = os.path.basename(f)[:-4]
        try:
            df = pd.read_csv(f, encoding="utf-8-sig")
        except Exception:
            badpx.append((sym, "READ_ERR"))
            continue
        if "date" not in df.columns or len(df) < 10:
            badpx.append((sym, "THIN"))
            continue
        d = df["date"].astype(str).tolist()
        if len(set(d)) != len(d):
            dup.append(sym)
        if d != sorted(d):
            unsorted.append(sym)
        rows[sym] = set(d[-args.days:])
        for x in d[-args.days:]:
            day_cnt[x] += 1
        for c in ("open", "last", "high", "low"):
            if c in df.columns and (df[c].tail(args.days) <= 0).any():
                badpx.append((sym, f"NONPOS_{c}"))
                break

    # ---------- 2. 交易日基准 = 覆盖 >90% 票的日期 ----------
    goal = sorted([d for d, c in day_cnt.items() if c > len(files) * 0.9])

    # ---------- 3. 缺日 ----------
    gaps = []
    for sym, s in rows.items():
        miss = [d for d in goal if d not in s]
        if miss:
            gaps.append({"代码": sym, "缺失数": len(miss), "缺失日": miss})

    # ---------- 4. 假K线：零成交量 ----------
    fake = []
    for sym in rows:
        df = load_kline(sym)
        if df is None:
            continue
        z = int((df["volume"].tail(20) == 0).sum())
        if z:
            fake.append({"代码": sym, "近20日零量行": z})

    # ---------- 5. 台账/虚拟盘 取价校验 ----------
    led_issues = []
    for fname, datecolmap in LEDGERS:
        p = DATA / fname
        if not p.exists():
            continue
        try:
            df = pd.read_csv(p, encoding="utf-8-sig", dtype={"代码": str})
        except Exception as e:
            led_issues.append({"文件": fname, "问题": f"READ_ERR {e}"})
            continue
        if "代码" not in df.columns:
            continue
        df["代码"] = df["代码"].astype(str).map(norm_code)
        for _, r in df.iterrows():
            code = r["代码"]
            k = load_kline(code)
            if k is None:
                led_issues.append({"文件": fname, "代码": code, "问题": "无K线文件"})
                continue
            for dcol, pcol in datecolmap.items():
                if dcol not in df.columns or pcol not in df.columns:
                    continue
                d = str(r.get(dcol, ""))[:10]
                if d in ("", "nan", "None"):
                    continue
                try:
                    px = float(r[pcol])
                except (TypeError, ValueError):
                    continue
                sub = k[k["date"].astype(str) == d]
                if len(sub) == 0:
                    led_issues.append({"文件": fname, "代码": code,
                                       "问题": f"{dcol}={d} 非交易日/无该日K线"})
                    continue
                lo, hi = float(sub["low"].iloc[0]), float(sub["high"].iloc[0])
                if not (lo - 0.02 <= px <= hi + 0.02):
                    led_issues.append({"文件": fname, "代码": code,
                                       "问题": f"{dcol}={d} 价{px}越界[{lo},{hi}]"})

    # ---------- 6. 重复登记 ----------
    dups = []
    p = DATA / "虚拟交易.csv"
    if p.exists():
        df = pd.read_csv(p, encoding="utf-8-sig", dtype={"代码": str})
        df["代码"] = df["代码"].astype(str).map(norm_code)
        dd = df[df.duplicated(subset=["代码", "虚拟建仓日"], keep=False)]
        for _, r in dd.iterrows():
            dups.append({"代码": r["代码"], "名称": r.get("名称", ""),
                         "建仓日": r["虚拟建仓日"]})

    # ---------- 输出 ----------
    p0 = len(gaps) + len(led_issues) + len(dup) + len(badpx) + len(dups)
    print(f"== K线体检 ==  文件{len(files)}  基准交易日{len(goal)}"
          f"  ({goal[0] if goal else '-'} ~ {goal[-1] if goal else '-'})")
    print(f"  重复日期 {len(dup)}  乱序 {len(unsorted)}  非正价 {len(badpx)}")
    print(f"  缺日票 {len(gaps)}")
    for g in gaps[:20]:
        print(f"    {g['代码']} 缺{g['缺失数']}日 {g['缺失日'][:5]}")
    print(f"  近20日含零量行 {len(fake)} 只（停牌或假K线，需人工判性质）")
    for x in fake[:10]:
        print(f"    {x['代码']} 零量{x['近20日零量行']}行")
    print(f"\n== 台账/虚拟盘体检 ==")
    print(f"  取价异常 {len(led_issues)} 条")
    for x in led_issues[:20]:
        print(f"    {x.get('文件')} {x.get('代码','')} {x['问题']}")
    print(f"  同代码同建仓日重复 {len(dups)} 条")
    for x in dups[:10]:
        print(f"    {x}")
    print(f"\n== 结论 == {'P0 存在，先修再统计' if p0 else 'OK'}")

    if args.json:
        Path(args.json).write_text(json.dumps({
            "files": len(files), "benchmark_days": goal,
            "gaps": gaps, "fake_volume": fake, "ledger_issues": led_issues,
            "dup_dates": dup, "unsorted": unsorted, "bad_px": badpx,
            "dup_trades": dups, "p0": p0,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    return 1 if p0 else 0


if __name__ == "__main__":
    sys.exit(main())
