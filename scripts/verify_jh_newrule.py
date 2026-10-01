# -*- coding: utf-8 -*-
"""江淮汽车 9/22 启动 —— 新策略口径能否抓到（全市场离线复现）。

数据源：本地 data/klines/*.csv（已到 2026-09-30），不走东财快照（避免限流）。
口径：完全复用 low_pos_entry 的 _indicators / _蓄势通过 与 config 当前参数。

两段式验证：
  ① 扫描端：9/17 收盘（T-1）全市场按新口径扫描 → 江淮是否在候选池 / 排第几
  ② 触发端：9/22 盘口（量比/涨幅/成交额）是否命中触发条件

注意：成交额用「成交量(股) × 收盘价」估算（本地K线口径），与腾讯实时「成交额」略有差异。
"""
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
from low_pos_entry import _indicators, _蓄势通过

BASE = Path(__file__).resolve().parent.parent
KLINES = BASE / "data" / "klines"

SCAN_DATE = "2026-09-21"   # T-1 扫描日（9/22 的前一交易日 = 9/18）
TARGET = "600418"

TENCENT = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"


def load_local(code_sym):
    p = KLINES / f"{code_sym}.csv"
    if not p.exists():
        return None
    try:
        df = pd.read_csv(p)
    except Exception:
        return None
    if df.empty or "date" not in df.columns:
        return None
    df = df.rename(columns={"date": "日期", "open": "开盘", "last": "收盘",
                            "high": "最高", "low": "最低", "volume": "成交量",
                            "amount": "成交额"})
    df["日期"] = pd.to_datetime(df["日期"])
    df = df[["日期", "开盘", "最高", "最低", "收盘", "成交量", "成交额"]]
    df = df.sort_values("日期").reset_index(drop=True)
    if "涨跌幅" not in df.columns:
        df["涨跌幅"] = df["收盘"].pct_change() * 100
    df = df.dropna(subset=["收盘"])
    return df


def main():
    files = sorted(KLINES.glob("*.csv"))
    print(f"本地K线 {len(files)} 只，扫描日 T-1 = {SCAN_DATE}")
    # 目标先算
    tgt = load_local("sh600418")
    if tgt is None:
        print("!! 找不到 600418")
        return
    tgt = tgt[tgt["日期"] <= SCAN_DATE]
    ind = _indicators(tgt)
    row = ind.iloc[-1]
    ok, why = _蓄势通过(row)
    print(f"\n=== 江淮汽车 600418 @ {SCAN_DATE} 收盘 ===")
    print(f"收盘={row['收盘']:.2f}  pos60={row['pos60']:.3f}  dist60={row['dist60']:.1f}%  "
          f"MA20={row['MA20']:.2f}  MA20上行={row['ma20up']}  量比={row['lb']:.2f}  "
          f"5日均量比={row['lb5mean']:.2f}  5日涨幅={row['chg5']:+.1f}%  "
          f"20日振幅={row['amp20']:.1f}%  当日涨幅={row['涨跌幅']:+.1f}%  "
          f"成交额={row['成交额']/1e8:.2f}亿")
    bias20 = (row['收盘'] / row['MA20'] - 1) * 100
    print(f"MA20乖离={bias20:.2f}%")
    print(f"判定：{'✅ 通过' if ok else '❌ 不通过'}  ({why})")

    # 全市场扫描，统计 9/17 通过名单（含江淮排名）
    hits = []
    t0 = time.time()
    for i, f in enumerate(files, 1):
        sym = f.stem
        code = sym[2:]
        if not code[:1] in ("6", "0", "3"):
            continue
        df = load_local(sym)
        if df is None:
            continue
        df = df[df["日期"] <= SCAN_DATE]
        if len(df) < 70:
            continue
        try:
            ind = _indicators(df)
        except Exception:
            continue
        row = ind.iloc[-1]
        if pd.isna(row.get("pos60")) or pd.isna(row.get("dist60")):
            continue
        _ok, why = _蓄势通过(row)
        if not _ok:
            continue
        hits.append({
            "代码": code, "sym": sym,
            "收盘": float(row["收盘"]),
            "pos60": float(row["pos60"]),
            "dist60": float(row["dist60"]),
            "量比": float(row["lb"]),
            "MA20上行": bool(row["ma20up"]),
            "成交额亿": float(row["成交额"]) / 1e8,
        })
        if i % 300 == 0:
            print(f"  进度 {i}/{len(files)} 命中={len(hits)} {time.time()-t0:.0f}s", flush=True)

    hits.sort(key=lambda x: (not x["MA20上行"], x["pos60"], x["量比"]))
    print(f"\n=== {SCAN_DATE} 全市场通过 _蓄势通过 共 {len(hits)} 只 ===")
    jh_rank = None
    for i, h in enumerate(hits, 1):
        mark = "  <<< 江淮" if h["代码"] == TARGET else ""
        if i <= 15 or h["代码"] == TARGET:
            print(f"{i:>3}. {h['代码']} 收{h['收盘']:.2f} pos{h['pos60']:.2f} "
                  f"距高{h['dist60']:.0f}% 量比{h['量比']:.2f} MA20up={h['MA20上行']} "
                  f"额{h['成交额亿']:.1f}亿{mark}")
        if h["代码"] == TARGET:
            jh_rank = i
    print(f"\n江淮全市场排序第 {jh_rank} 名（共 {len(hits)} 只）")
    if jh_rank:
        print(f"topN={config.LOW_POS_ENTRY_TOPN} 截断 → "
              f"{'✅ 进入候选池' if jh_rank <= config.LOW_POS_ENTRY_TOPN else '❌ 被截断'}")


if __name__ == "__main__":
    main()
