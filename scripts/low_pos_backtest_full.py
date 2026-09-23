# -*- coding: utf-8 -*-
"""低位埋伏全历史回测（本地 klines，2024-01 → 最新）。

需求（2026-09-21 用户）：低位埋伏策略统计用 2024-现在的样本回测。
实现：喂 data/klines/*.csv（腾讯前复权，约659个交易日）给 low_pos_entry.backtest()：
  - _load_hist 打补丁为本地读取，不走网络；
  - 成交额直接用缓存 amount 列（元），涨跌幅由收盘价计算；
  - 大盘温度闸门临时置 0，全部触发样本落 CSV（含大盘温度代理列），
    「闸门开/关」两套口径由统计端离线分层，避免全量扫描跑两遍。
用法：python low_pos_backtest_full.py
"""
import glob
import json
import os
import sys

import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import low_pos_entry as lpe  # noqa: E402


def _local_hist(code, days=180):
    """与 _load_hist 同列序的本地 K 线读取（腾讯前复权缓存）。"""
    sym = ("sh" if code.startswith(("6", "9")) else "sz") + code
    path = os.path.join(BASE, "data", "klines", f"{sym}.csv")
    if not os.path.exists(path):
        return code, None
    raw = pd.read_csv(path)
    if raw.empty:
        return code, None
    df = pd.DataFrame({
        "日期": pd.to_datetime(raw["date"]),
        "开盘": raw["open"].astype(float),
        "最高": raw["high"].astype(float),
        "最低": raw["low"].astype(float),
        "收盘": raw["last"].astype(float),
        "成交量": raw["volume"].astype(float),      # 手（量比为比值，单位无关）
    })
    df["成交额"] = raw["amount"].astype(float)       # 元，直接用缓存列
    df = df.drop_duplicates(subset=["日期"]).sort_values("日期").reset_index(drop=True)
    df = df.dropna(subset=["收盘"])
    df["涨跌幅"] = df["收盘"].pct_change() * 100     # 排序去重后再算，避免乱序错标
    if len(df) < 70:
        return code, None
    return code, df


def main():
    # 股票宇宙：本地 klines 缓存（约1092只活跃股，主板+创业板）
    codes = []
    for p in sorted(glob.glob(os.path.join(BASE, "data", "klines", "*.csv"))):
        sym = os.path.basename(p)[:-4]
        codes.append(sym[2:])
    print(f"[全历史回测] 本地宇宙 {len(codes)} 只", flush=True)

    # 名称表：尝试一次快照（供行业共振标记）；失败则降级为代码（共振标签失真，不影响样本入池）
    names_map = {}
    try:
        from fetch_data import get_market_snapshot
        snap = get_market_snapshot()
        if snap is not None and not snap.empty:
            snap = snap.copy()
            snap["代码"] = snap["代码"].astype(str).str.replace(r"\.0$", "", regex=True)
            names_map = dict(zip(snap["代码"], snap["名称"].astype(str)))
            print(f"[全历史回测] 名称表 {len(names_map)} 条（快照）", flush=True)
    except Exception as e:
        print(f"[全历史回测] 快照失败，名称降级为代码（共振标签失真）：{e}", flush=True)

    # 打补丁：本地读 K 线 + 关温度闸门（样本全保留，闸门口径离线分层）
    lpe._load_hist = _local_hist
    lpe.LOW_POS_ENTRY_TEMP_MIN = 0
    df = lpe.backtest(top=None, days=700, codes=codes, names_map=names_map)
    if df is None or df.empty:
        print("无触发样本")
        return
    print(f"\n[完成] 触发样本 {len(df)} 条（温度闸门=关，CSV含大盘温度列供离线分层）")

    # 统计 → data/低位埋伏_统计.json（notify 摘要卡读取）
    t5 = df["T5收益%"].dropna()
    t3 = df["T3收益%"].dropna()
    gate = df[df["大盘温度"] >= 55]          # 温度代理闸门（与 trend_gate 55 同阈值）
    g5 = gate["T5收益%"].dropna()
    stats = {
        "窗口": f"{df['触发日'].min()} ~ {df['触发日'].max()}",
        "样本数": int(len(df)),
        "票数": int(df["代码"].nunique()),
        "T5样本": int(t5.size),
        "T5均值": round(float(t5.mean()), 2) if t5.size else None,
        "T5胜率": round(float((t5 > 0).mean() * 100), 1) if t5.size else None,
        "T5大赚率": round(float((t5 >= 10).mean() * 100), 1) if t5.size else None,
        "T5大亏率": round(float((t5 <= -10).mean() * 100), 1) if t5.size else None,
        "T3均值": round(float(t3.mean()), 2) if t3.size else None,
        "T3胜率": round(float((t3 > 0).mean() * 100), 1) if t3.size else None,
        "止损触发率": round(float(df["8日破止损"].mean() * 100), 1),
        "闸门开样本": int(len(gate)),
        "闸门开T5均值": round(float(g5.mean()), 2) if g5.size else None,
        "闸门开T5胜率": round(float((g5 > 0).mean() * 100), 1) if g5.size else None,
        "路径分层": {
            path: {
                "样本": int(len(grp)),
                "T5均值": round(float(grp["T5收益%"].dropna().mean()), 2) if grp["T5收益%"].dropna().size else None,
                "T5胜率": round(float((grp["T5收益%"].dropna() > 0).mean() * 100), 1) if grp["T5收益%"].dropna().size else None,
            }
            for path, grp in df.groupby("蓄势路径")
        },
    }
    out_json = os.path.join(BASE, "data", "低位埋伏_统计.json")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=1)
    print(f"统计 JSON → {out_json}")
    print(json.dumps(stats, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
