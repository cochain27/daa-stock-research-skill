# -*- coding: utf-8 -*-
"""用腾讯源补齐 klines 缓存更早历史（增量合并，2024-01 ~ 缓存起点前）。

背景：东财直连被限流，腾讯可用。腾讯日K字段 [date,open,close,high,low,volume(手)]，
无成交额 → amount 用 volume×100×close 估算（误差约±5%，16亿阈值边界有±5%误判，
对全市场统计影响极小）。

安全：每只票先读原文件，拉取失败跳过；合并后整文件写回，不覆盖式清空。
用法：python fetch_kline_tencent_hist.py [--start 2024-01-01] [--sleep 0.3] [--limit 20]
"""
import csv
import os
import sys
import time
import glob
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent.parent / "data" / "klines"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
HIST_START = "2024-01-01"
EXCH = {"sz": "SZ", "sh": "SH", "bj": "BJ"}


def _tencent_klines(symbol: str, start: str, end: str, count: int = 800):
    """拉腾讯前复权日K，返回 [(date, open, close, high, low, volume手)]"""
    s = requests.Session()
    s.trust_env = False  # 绕过系统代理，直连国内源
    r = s.get(
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
        params={"param": f"{symbol},day,{start},{end},{count},qfq"},
        headers=HEADERS, timeout=15,
    )
    r.raise_for_status()
    j = r.json()
    d = j.get("data", {}).get(symbol, {})
    kl = d.get("qfqday") or d.get("day") or []
    out = []
    for row in kl:
        if len(row) < 6:
            continue
        try:
            out.append((row[0], float(row[1]), float(row[2]), float(row[3]),
                        float(row[4]), float(row[5])))
        except (ValueError, TypeError):
            continue
    return out


def _merge_and_write(path: Path, symbol: str, new_rows, dry: bool = False):
    """读原缓存 → 合并（按日期去重，现有行优先）→ 写回。返回 (原行数, 新增行数)"""
    # 读原缓存
    old = {}
    header = "symbol,date,open,last,high,low,volume,amount,exchange"
    if path.exists():
        with path.open(encoding="utf-8") as f:
            rdr = csv.reader(f)
            rows = list(rdr)
        if rows and rows[0] and rows[0][0] == "symbol":
            header = ",".join(rows[0])
            rows = rows[1:]
        else:
            rows = rows
        for r in rows:
            if len(r) >= 9:
                old[r[1]] = r
    # 新数据 → amount 估算
    exch = EXCH.get(symbol[:2], "")
    added = 0
    for dt, op, cl, hi, lo, vol in new_rows:
        if dt in old:  # 缓存已有该日，保留旧值（东财口径更准）
            continue
        if cl <= 0:
            continue
        amt = round(vol * 100 * cl, 2)  # 估算成交额（元）
        old[dt] = [symbol, dt, f"{op:.3f}", f"{cl:.3f}", f"{hi:.3f}",
                   f"{lo:.3f}", f"{vol:.0f}", f"{amt:.2f}", exch]
        added += 1
    if not added or dry:
        return len(old) - (len(new_rows) - added), added
    # 按日期排序写回
    sorted_rows = sorted(old.values(), key=lambda r: r[1])
    tmp = path.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header.split(","))
        w.writerows(sorted_rows)
    tmp.replace(path)
    return len(sorted_rows), added


def main():
    args = sys.argv[1:]
    start = HIST_START
    sleep_s = 0.3
    limit = None
    for a in args:
        if a.startswith("--start="):
            start = a.split("=", 1)[1]
        elif a.startswith("--sleep="):
            sleep_s = float(a.split("=", 1)[1])
        elif a.startswith("--limit="):
            limit = int(a.split("=", 1)[1])
    files = sorted(glob.glob(str(BASE / "*.csv")))
    if limit:
        files = files[:limit]
    print(f"补齐 {len(files)} 只票 {start} 起历史（腾讯源，估算成交额），sleep={sleep_s}s", flush=True)
    ok = fail = skip = total_added = 0
    t0 = time.time()
    for idx, fp in enumerate(files, 1):
        path = Path(fp)
        symbol = path.stem
        # 原缓存起点
        first_date = None
        try:
            with path.open(encoding="utf-8") as f:
                rdr = csv.reader(f)
                next(rdr, None)
                row = next(rdr, None)
                if row and len(row) >= 2:
                    first_date = row[1]
        except Exception:
            pass
        if first_date is None:
            fail += 1
            continue
        if first_date <= start:
            skip += 1  # 起点已早于目标，无需补
            continue
        end = first_date  # 补到缓存起点前（接口含end日，脚本内去重）
        try:
            new_rows = _tencent_klines(symbol, start, end, 800)
        except Exception as e:
            fail += 1
            print(f"[{idx}/{len(files)}] {symbol} 拉取失败: {e}", flush=True)
            time.sleep(sleep_s)
            continue
        if not new_rows:
            skip += 1
            continue
        n, added = _merge_and_write(path, symbol, new_rows)
        total_added += added
        ok += 1
        if idx % 50 == 0 or added:
            print(f"[{idx}/{len(files)}] {symbol} 原{n}行 +新增{added}（起点{first_date}→{start}）", flush=True)
        time.sleep(sleep_s)
    dt = time.time() - t0
    print(f"\n完成: 成功{ok} 失败{fail} 跳过{skip} 累计新增{total_added}行 耗时{dt:.0f}s", flush=True)


if __name__ == "__main__":
    main()
