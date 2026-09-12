#!/usr/bin/env python3
"""
merge_observation_pool.py — 事件驱动观察池汇流脚本
功能：将路径A（龙虎榜）、路径B（热点板块）的候选汇入统一的观察池
      生成 merge_observation_pool.csv，包含：
        - 候选来源（龙虎榜/热点板块）
        - 入池日期
        - 驱动逻辑（机构净买/综合评分）
        - 当前观察状态
用法：
    python merge_observation_pool.py --source lhb      # 只汇入龙虎榜
    python merge_observation_pool.py --source hotboard # 只汇入热点板块
    python merge_observation_pool.py --source both     # 两条路都汇入
"""
import csv, json, sys
from pathlib import Path
from datetime import date

BASE    = Path("/Users/chenyuting/Documents/workbuddy/workbuddy-daa/daa-stock-research-skill")
OBS_CSV = BASE / "data" / "merge_observation_pool.csv"
LHB_JSON = BASE / "data" / "lhb_candidates.json"
HOT_CSV  = BASE / "data" / "hot_board_dashboard.csv"

FIELDS = [
    "入池日期", "候选来源", "代码", "名称",
    "核心指标", "指标值", "驱动逻辑",
    "昨日收盘", "买区建议", "止损建议",
    "观察状态", "最后更新", "备注"
]

def _write_pool(rows, trade_date):
    """写入观察池（去重：同代码只保留最新记录）"""
    existing = {}
    if OBS_CSV.exists():
        with OBS_CSV.open(encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if r.get("代码"):
                    existing[r["代码"]] = r

    for r in rows:
        existing[r["代码"]] = r

    with OBS_CSV.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for code in sorted(existing, key=lambda c: (existing[c]["候选来源"], c)):
            writer.writerow(existing[code])

def _calc_zones(close):
    if not close or close <= 0:
        return "", ""
    return f"{close * 0.97:.2f}-{close * 1.02:.2f}", f"{close * 0.95:.2f}"

def _load_lhb(today):
    """从 lhb_candidates.json 读取龙虎榜候选"""
    rows = []
    if not LHB_JSON.exists():
        return rows
    with LHB_JSON.open(encoding="utf-8") as f:
        d = json.load(f)
    trade_date = d.get("trade_date", today)
    for c in d.get("candidates", []):
        close = c.get("close", 0)
        buy_zone, stop_loss = _calc_zones(close)
        rows.append({
            "入池日期": trade_date,
            "候选来源": "路径A-龙虎榜",
            "代码": c["code"],
            "名称": c["name"],
            "核心指标": "机构净买入",
            "指标值": f"{c.get('inst_net_w', 0):,.0f}万",
            "驱动逻辑": f"机构净买{c.get('inst_net_w', 0):,.0f}万/成交额{c.get('total_buy', 0):.1f}亿/机构占比{c.get('inst_rate', 0):.1f}%",
            "昨日收盘": close if close > 0 else "",
            "买区建议": buy_zone,
            "止损建议": stop_loss,
            "观察状态": "✅ 入池待观察",
            "最后更新": today,
            "备注": c.get("notes", "")
        })
    return rows

def _load_hotboard(today):
    """从 hot_board_dashboard.csv 读取热点板块候选（评分≥60）"""
    rows = []
    if not HOT_CSV.exists():
        return rows
    with HOT_CSV.open(encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            score = float(r.get("score", 0) or 0)
            if score < 60:
                continue
            close = float(r.get("close", 0) or 0)
            buy_zone, stop_loss = _calc_zones(close)
            rows.append({
                "入池日期": r.get("trade_date", today),
                "候选来源": "路径B-热点板块",
                "代码": r.get("code", ""),
                "名称": r.get("name", ""),
                "核心指标": "综合评分",
                "指标值": f"{score:.1f}",
                "驱动逻辑": f"{r.get('board','')} / {r.get('board_chg',0)}%" +
                            f" / 主力净流入{r.get('stock_net_in',0)}万",
                "昨日收盘": close if close > 0 else "",
                "买区建议": buy_zone,
                "止损建议": stop_loss,
                "观察状态": "✅ 入池待观察",
                "最后更新": today,
                "备注": f"板块:{r.get('board','')} / 评分:{score:.1f}"
            })
    return rows

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="both",
                        choices=["lhb", "hotboard", "both"])
    args = parser.parse_args()

    today = date.today().strftime("%Y-%m-%d")
    print(f"\n{'='*55}")
    print(f"  事件驱动观察池汇流  |  今日:{today}")
    print(f"{'='*55}")

    rows = []
    if args.source in ("lhb", "both"):
        r = _load_lhb(today)
        print(f"  龙虎榜候选: {len(r)} 只")
        rows += r
    if args.source in ("hotboard", "both"):
        r = _load_hotboard(today)
        print(f"  热点板块候选: {len(r)} 只")
        rows += r

    if not rows:
        print("  ⚠️ 无候选数据，跳过")
        return

    _write_pool(rows, today)
    print(f"\n  ✅ 观察池已更新: {OBS_CSV}")
    print(f"  总计: {len(rows)} 只")

    # 打印摘要
    print(f"\n  当前观察池内容：")
    print(f"  {'代码':<12} {'名称':<10} {'来源':<14} {'指标':<10} {'观察状态'}")
    print(f"  {'-'*65}")
    for r in rows:
        print(f"  {r['代码']:<12} {r['名称']:<10} {r['候选来源']:<14} "
              f"{r['指标值']:<10} {r['观察状态']}")

if __name__ == "__main__":
    main()
