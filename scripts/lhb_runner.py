#!/usr/bin/env python3
"""lhb_runner.py — 读取 lhb_raw.json，触发 lhb_drive 处理流程"""
import json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lhb_drive import (
    write_to_ledger, write_html_report,
    enrich_candidates, filter_lhb,
    DATA_DIR, LHB_THRESHOLD, TURNOVER_THRESHOLD
)
from datetime import date

def main():
    json_path = Path(__file__).resolve().parent / "lhb_raw.json"
    if not json_path.exists():
        print("❌ 未找到 lhb_raw.json，请先通过 MCP data_lhb 获取数据写入该文件")
        return

    with json_path.open(encoding="utf-8") as f:
        payload = json.load(f)

    trade_date = payload.get("trade_date", "2026-09-11")
    raw_items  = payload.get("raw_items", [])

    print(f"\n{'='*60}")
    print(f"  龙虎榜驱动处理  |  交易日:{trade_date}")
    print(f"{'='*60}")
    print(f"  原始记录: {len(raw_items)} 条")

    candidates = filter_lhb(raw_items, trade_date)
    candidates.sort(key=lambda x: x["inst_net_w"], reverse=True)
    print(f"  筛选后候选: {len(candidates)} 只")

    for c in candidates:
        print(f"  ✓ {c['code']} {c['name']}  机构净买入:{c['inst_net_w']:,.0f}万  "
              f"成交额:{c['total_buy']:.1f}亿  机构占比:{c['inst_rate']:.1f}%")

    if not candidates:
        print("  ⚠️ 无候选")
        return

    enriched = enrich_candidates(candidates, trade_date)
    enriched.sort(key=lambda x: x["score"], reverse=True)
    print(f"\n  综合评分排名：")
    for i, c in enumerate(enriched, 1):
        print(f"  {i}. {c['code']} {c['name']}  评分:{c['score']}  PE:{c.get('pe','?')}  "
              f"行业:{c.get('industry','?')}")

    to_ledger = [c for c in enriched if c["score"] >= 55]
    if to_ledger:
        n = write_to_ledger(to_ledger, trade_date)
        print(f"\n  📋 已写入推荐台账（评分≥55）: {n} 只")
    else:
        print("\n  📋 无评分≥55的标的，跳过台账写入")

    out_html = write_html_report(enriched, trade_date)
    print(f"\n  📊 HTML: {out_html}")

    json_out = DATA_DIR / "lhb_candidates.json"
    with json_out.open("w", encoding="utf-8") as f:
        json.dump({"trade_date": trade_date, "candidates": enriched}, f, ensure_ascii=False, indent=2)
    print(f"  📄 JSON: {json_out}")
    print(f"\n✅ 完成！候选 {len(enriched)} 只")

if __name__ == "__main__":
    main()
