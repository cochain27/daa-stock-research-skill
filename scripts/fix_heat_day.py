# -*- coding: utf-8 -*-
"""补全 industry_heat.csv 指定日期缺失的板块行（THS 板块指数接口部分失败后修复用）。

用法: python fix_heat_day.py 2026-10-23 [2026-10-21 ...]
逻辑: 逐板块拉最近 15 天板块指数K线，按「上一交易日收盘 → 当日收盘」算涨跌幅，
      只补 CSV 中该日缺失的板块，已有值不覆盖。
"""
import sys, os, csv, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pandas as pd
import akshare as ak
import fetch_data  # noqa: F401  加载直连 patch

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "industry_heat.csv")


def main():
    targets = sys.argv[1:]
    if not targets:
        print("usage: fix_heat_day.py YYYY-MM-DD [...]")
        return
    boards = ak.stock_board_industry_name_ths()
    print(f"THS 板块 {len(boards)} 个, 目标日期 {targets}")

    rows = []
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    have = {(r["日期"], r["板块"]) for r in rows}
    before = {d: sum(1 for r in rows if r["日期"] == d) for d in targets}

    need = {d: set() for d in targets}
    for _, b in boards.iterrows():
        for d in targets:
            if (d, str(b["name"]).strip()) not in have:
                need[d].add(str(b["name"]).strip())
    if not any(need.values()):
        print("无需补全")
        return

    start = (pd.Timestamp(min(targets)) - pd.Timedelta(days=20)).strftime("%Y%m%d")
    end = (pd.Timestamp(max(targets)) + pd.Timedelta(days=2)).strftime("%Y%m%d")

    added = {d: 0 for d in targets}
    for i, (name, code) in enumerate(zip(boards["name"], boards["code"]), 1):
        name = str(name).strip()
        want = [d for d in targets if name in need[d]]
        if not want:
            continue
        for attempt in range(3):
            try:
                df = ak.stock_board_industry_index_ths(symbol=name, start_date=start, end_date=end)
                break
            except Exception as e:
                if attempt == 2:
                    print(f"  {name} 失败: {type(e).__name__} {str(e)[:60]}")
                    df = None
                else:
                    time.sleep(1.5 * (attempt + 1))
        if df is None or df.empty:
            continue
        df = df.copy()
        df["日期"] = df["日期"].astype(str)
        closes = df["收盘价"].astype(float).tolist()
        pct = {}
        for k in range(1, len(df)):
            if closes[k - 1]:
                pct[df.iloc[k]["日期"]] = (closes[k] / closes[k - 1] - 1) * 100
        for d in want:
            if d in pct:
                rows.append({"日期": d, "板块": name, "涨跌幅": f"{pct[d]:.2f}"})
                added[d] += 1
        if i % 20 == 0:
            print(f"  进度 {i}/{len(boards)}")

    rows.sort(key=lambda x: (x["日期"], x["板块"]))
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["日期", "板块", "涨跌幅"])
        w.writeheader()
        w.writerows(rows)
    for d in targets:
        print(f"{d}: {before[d]} -> {before[d] + added[d]} 条")


if __name__ == "__main__":
    main()
