# -*- coding: utf-8 -*-
"""补录行业板块历史涨幅 → data/industry_heat.csv
数据源：同花顺二级行业板块（90个，stock_board_industry_name_ths）
历史K线：stock_board_industry_index_ths（板块指数收盘价序列 → 涨跌幅）
板块名与东财个股 f127 三级名高度匹配（归一化处理后精确对应），
供 close_review 主线判定（连续N日板块涨幅居前 = 主线）。
"""
import sys, os, csv, time, re
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pandas as pd
import akshare as ak
import fetch_data  # 加载直连 patch（trust_env=False，东财/同花顺直连）

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "industry_heat.csv")


def get_board_daily(name, code, start="20260201", end="20260910"):
    """同花顺板块指数日K → [(date, pct)]。涨跌幅=收盘价环比
    2026-09-10 扩展：起始回测区间 20260201（原 20260801 只有 27 天，不足做归因）"""
    df = ak.stock_board_industry_index_ths(symbol=name, start_date=start, end_date=end)
    if df is None or df.empty:
        return []
    df["日期"] = df["日期"].astype(str)
    closes = df["收盘价"].astype(float).tolist()
    res = []
    for i in range(1, len(df)):
        if closes[i - 1] == 0:
            continue
        pct = (closes[i] / closes[i - 1] - 1) * 100
        res.append((df.iloc[i]["日期"], round(pct, 2)))
    return res


def main():
    boards = ak.stock_board_industry_name_ths()
    print(f"同花顺板块数: {len(boards)}")
    records = {}
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                records[(row["日期"], row["板块"])] = row["涨跌幅"]
    print(f"已有 {len(records)} 条")

    new = 0
    for i, (name, code) in enumerate(zip(boards["name"], boards["code"]), 1):
        try:
            daily = get_board_daily(name, code)
        except Exception as e:
            print(f"  {name} 失败: {type(e).__name__} {str(e)[:80]}")
            continue
        for d, pct in daily:
            key = (d, name)
            if key not in records:
                records[key] = f"{pct:.2f}"
                new += 1
        if i % 15 == 0:
            print(f"  进度 {i}/{len(boards)} 新增{new}")

    rows = sorted(records.items(), key=lambda x: (x[0][0], x[0][1]))
    with open(OUT, "w", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["日期", "板块", "涨跌幅"])
        for (d, n), pct in rows:
            w.writerow([d, n, pct])
    print(f"完成: {OUT} 共{len(rows)}条(新增{new})")


if __name__ == "__main__":
    main()