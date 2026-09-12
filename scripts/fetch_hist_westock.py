# -*- coding: utf-8 -*-
"""用 westockdata（westock-data-clawhub，腾讯自选股数据源）批量拉 A 股日线 K 线落盘。

用途：当新浪/腾讯 ifzq/东财等公网 K 线源被限流时，westockdata 的 kline 接口（独立数据源）
常仍可用。本脚本用它拉全量代码池的历史日线（前复权），落盘为 data/klines/*.csv，
供 backtest_engine 用 --cache 读取，绕开实时拉取。

数据字段：symbol,date,open,last(收盘),high,low,volume,amount,exchange
用法：
  python fetch_hist_westock.py --codes-file /tmp/codes_pool.txt --days 300 --batch 80
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

import pandas as pd

PKG = "westock-data-clawhub@1.0.4"
DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "klines"


def to_westock(codes):
    """6 位代码 → westock 格式（sh/sz 前缀）"""
    out = []
    for c in codes:
        c = c.strip()
        if not c:
            continue
        pre = "sh" if c.startswith(("6", "9")) else "sz"
        out.append(pre + c)
    return out


def parse_md_table(text):
    """解析 westockdata 输出的 markdown 表格为 DataFrame"""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    # 找表头行（含 '|' 且含 date 或 symbol）
    header_idx = None
    for i, ln in enumerate(lines):
        if ln.startswith("|") and ("date" in ln or "symbol" in ln):
            header_idx = i
            break
    if header_idx is None:
        return None
    header = [h.strip() for h in lines[header_idx].strip("|").split("|")]
    rows = []
    for ln in lines[header_idx + 1:]:
        if not ln.startswith("|"):
            continue
        if re.match(r"^\| *---", ln) or re.match(r"^\| *:?-", ln):
            continue
        cells = [c.strip() for c in ln.strip("|").split("|")]
        if len(cells) != len(header):
            continue
        rows.append(cells)
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=header)
    return df


def fetch_batch(wcodes, days):
    """一批代码拉 K 线，返回 DataFrame（解析后的）"""
    arg = ",".join(wcodes)
    cmd = ["npx", "-y", PKG, "kline", arg, "--period", "day", "--limit", str(days), "--fq", "qfq"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        txt = r.stdout
    except subprocess.TimeoutExpired:
        return None
    df = parse_md_table(txt)
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes-file", default="/tmp/codes_pool.txt")
    ap.add_argument("--days", type=int, default=300)
    ap.add_argument("--batch", type=int, default=80)
    args = ap.parse_args()

    # 读取代码池（文件第二行为逗号分隔代码串）
    text = Path(args.codes_file).read_text()
    lines = text.strip().splitlines()
    code_line = lines[-1] if len(lines) > 1 else lines[0]
    codes = [c for c in code_line.split(",") if c.strip()]
    wcodes = to_westock(codes)
    print(f"代码池 {len(wcodes)} 只，批次大小 {args.batch}，days={args.days}")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    frames = []
    ok = fail = 0
    for i in range(0, len(wcodes), args.batch):
        batch = wcodes[i:i + args.batch]
        df = fetch_batch(batch, args.days)
        if df is None:
            fail += len(batch)
            print(f"  批次 {i // args.batch + 1}: 失败({len(batch)})")
            continue
        # westockdata 批量返回含 symbol 列；单只时可能没有
        if "symbol" not in df.columns:
            df["symbol"] = batch[0]
        frames.append(df)
        ok += df["symbol"].nunique()
        print(f"  批次 {i // args.batch + 1}: 成功 {df['symbol'].nunique()}/{len(batch)} 只，累计成功 {ok}", flush=True)

    if not frames:
        print("无数据落盘")
        return 1
    all_df = pd.concat(frames, ignore_index=True)
    # 落盘：按 symbol 分文件，便于 backtest_engine 读取
    for sym, g in all_df.groupby("symbol"):
        g = g.sort_values("date").reset_index(drop=True)
        g.to_csv(DATA_DIR / f"{sym}.csv", index=False, encoding="utf-8")
    print(f"完成：成功 {ok} 只，落盘到 {DATA_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())