#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""低位启动观察池 · 命中率定期点检（2026-09-16 新增）

背景：用户问"9-09 推荐露笑/通鼎后 2/2 涨停，成功率 100% 能否借鉴"。
系统此前只有逐日跟踪（watch_history + 复盘连续追踪），没有「入池 → N日涨停率/上涨率/破止损率」
的统计指标。本脚本补齐：把 watch_history.csv 里每次入池当做一个信号样本，
回看该票入池日之后 K 线，计算 T+1/T+3/T+5 收盘收益、7 日内最大涨幅、N 日内是否涨停、
是否破止损，并按「信号日温度档 / 蓄势路径 / 行业」分桶汇总。

用法：
    python watch_pool_stats.py                # 全量点检（读本地 klines + 在线拉取补缺）
    python watch_pool_stats.py --since 2026-09-01   # 只看某日之后的入池信号
    python watch_pool_stats.py --json          # 同时输出 data/watch_pool_stats.json

数据源：
- 信号清单：data/watch_history.csv（日期/代码/名称/现价/止损价/买点区间/蓄势路径/状态）
- K线：优先 data/klines/{code}.csv 本地缓存（前复权，14个月），缺失的在线拉取。
  注：本地缓存只到 2026-09-11，之后的 T+N 收益会拉不到 → 样本自动标记"数据不足"。
- 涨停判定：当日收盘涨幅 ≥ 9.5%（主板口径，粗略；ST 5% 不做区分）。

输出：控制台报告 + 可选 data/watch_pool_stats.json
"""
import csv, sys, json
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
HIST_CSV = BASE / "data" / "watch_history.csv"
OUT_JSON = BASE / "data" / "watch_pool_stats.json"
KLINE_DIR = BASE / "data" / "klines"

# 涨停阈值：主板 9.5%（粗略，创业板/科创板 19.5% 另行判定）
LIMIT_UP = {"main": 9.5, "gem": 19.5, "star": 19.5}


def _market_of(code):
    """粗略判断板块：30/68 开头=创业板/科创板，其余主板"""
    c = str(code).split(".")[0]
    if c.startswith("30"):
        return "gem"
    if c.startswith(("68", "689")):
        return "star"
    return "main"


def _load_klines(code):
    """本地 klines 目录加载，返回 {date: close} 升序 dict；失败返回 {}。"""
    cands = [f"{code}.csv", f"sz{code}.csv", f"sh{code}.csv",
             f"{code}.csv"] if not code.startswith(("6", "0", "3")) else []
    # 优先找 6/0/3 前缀文件
    for prefix in ("", "sh" if code.startswith("6") else "sz"):
        p = KLINE_DIR / f"{prefix}{code}.csv"
        if p.exists():
            try:
                out = {}
                with p.open(encoding="utf-8") as f:
                    for r in csv.DictReader(f):
                        d = r.get("日期") or r.get("date")
                        c = r.get("收盘") or r.get("close")
                        if d and c:
                            try:
                                out[str(d)[:10]] = float(c)
                            except ValueError:
                                pass
                return out
            except Exception:
                return {}
    return {}


def _online_klines(code):
    """在线拉取（复用 fetch_data.get_stock_hist），失败返回 {}。"""
    try:
        import sys as _s
        _s.path.insert(0, str(BASE / "scripts"))
        from fetch_data import get_stock_hist
        df = get_stock_hist(code, days=120)
        if df is None or df.empty:
            return {}
        return {str(d)[:10]: float(c) for d, c in zip(df["日期"], df["收盘"])}
    except Exception:
        return {}


def _signals(since=None):
    rows = []
    with HIST_CSV.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            d = r.get("日期", "")
            if since and d < since:
                continue
            rows.append(r)
    return rows


def _summary(results, limit_days=7):
    """由 _analyze 明细计算汇总统计。返回 dict（键与 main 打印口径一致）。"""
    n = len(results)
    out = {"样本数": n}
    if not n:
        return out

    def _pct(a, b):
        return f"{a / b * 100:.0f}%" if b else "-"

    t1s = [r["T1"] for r in results if r["T1"] is not None]
    t3s = [r["T3"] for r in results if r["T3"] is not None]
    t5s = [r["T5"] for r in results if r["T5"] is not None]
    mr = [r["7日最大涨幅%"] for r in results if r["7日最大涨幅%"] is not None]
    lu = [r for r in results if r["N日涨停天数"] > 0]
    sl = [r for r in results if r["破止损"]]
    out.update({
        "T1均值": round(sum(t1s) / len(t1s), 2) if t1s else None,
        "T1胜率": _pct(len([x for x in t1s if x > 0]), len(t1s)),
        "T3均值": round(sum(t3s) / len(t3s), 2) if t3s else None,
        "T3胜率": _pct(len([x for x in t3s if x > 0]), len(t3s)),
        "T5均值": round(sum(t5s) / len(t5s), 2) if t5s else None,
        "T5胜率": _pct(len([x for x in t5s if x > 0]), len(t5s)),
        "7日最大涨幅均值": round(sum(mr) / len(mr), 2) if mr else None,
        "7日最大涨幅>=5%": _pct(len([x for x in mr if x >= 5]), len(mr)),
        "涨停率": _pct(len(lu), n),
        "破止损率": _pct(len(sl), n),
        "涨停数": len(lu),
        "破止损数": len(sl),
    })
    return out


def _analyze(sig, klines):
    """对单个入池信号计算 T+N 统计。返回 dict 或 None（数据不足）。"""
    entry_date = sig["日期"]
    # 找到 entry_date 当天及之后的交易日序列
    dates = sorted(d for d in klines if d >= entry_date)
    if len(dates) < 2:
        return None  # 没有 T+1 数据
    entry_close = klines[entry_date] if entry_date in klines else klines[dates[0]]
    closes = [klines[d] for d in dates]

    def ret_at(n):
        """T+n 收盘收益（n=1..7），超出范围返回 None"""
        if n < len(closes):
            return (closes[n] / entry_close - 1) * 100
        return None

    max_ret = max((c / entry_close - 1) * 100 for c in closes)
    min_ret = min((c / entry_close - 1) * 100 for c in closes)

    # N 日内是否涨停（任一交易日涨幅≥阈值；粗略用当日收盘相对前收）
    limit_th = LIMIT_UP.get(_market_of(sig["代码"]), 9.5)
    limit_days = 0
    prev = None
    for d in dates:
        c = klines[d]
        if prev:
            pct = (c / prev - 1) * 100
            if pct >= limit_th:
                limit_days += 1
        prev = c

    # 破止损：入池后任意收盘 ≤ 止损价（用收盘口径，保守）
    sl_raw = sig.get("止损价", "")
    try:
        sl = float(sl_raw)
    except (ValueError, TypeError):
        sl = None
    hit_sl = any(c <= sl for c in closes[1:]) if sl else None

    return {
        "日期": entry_date,
        "代码": sig["代码"],
        "名称": sig.get("名称", ""),
        "路径": sig.get("蓄势路径", ""),
        "行业": sig.get("行业", ""),
        "入池价": round(entry_close, 2),
        "止损价": sl,
        "状态": sig.get("状态", ""),
        "T1": round(ret_at(1), 2) if ret_at(1) is not None else None,
        "T3": round(ret_at(3), 2) if ret_at(3) is not None else None,
        "T5": round(ret_at(5), 2) if ret_at(5) is not None else None,
        "7日最大涨幅%": round(max_ret, 2),
        "7日最大回撤%": round(min_ret, 2),
        "N日涨停天数": limit_days,
        "破止损": hit_sl,
        "样本天数": len(dates),
    }


def main():
    since = None
    want_json = False
    for a in sys.argv[1:]:
        if a.startswith("--since"):
            since = a.split("=")[-1]
        if a == "--json":
            want_json = True

    sigs = _signals(since)
    print(f"=== 低位观察池命中率点检 {datetime.now():%Y-%m-%d %H:%M} ===")
    print(f"信号总数（watch_history 全部入池记录）：{len(sigs)}\n")

    results = []
    for sig in sigs:
        code = sig["代码"]
        kl = _load_klines(code)
        if not kl:
            kl = _online_klines(code)
        r = _analyze(sig, kl)
        if r:
            results.append(r)
        else:
            print(f"  [数据不足] {sig['日期']} {code} {sig.get('名称','')}（无 T+1 数据，可能刚入池）")

    if not results:
        print("无可统计样本。")
        return

    def pct(a, b):
        return f"{a/b*100:.0f}%" if b else "-"

    n = len(results)
    t1s = [r["T1"] for r in results if r["T1"] is not None]
    t3s = [r["T3"] for r in results if r["T3"] is not None]
    t5s = [r["T5"] for r in results if r["T5"] is not None]
    mr = [r["7日最大涨幅%"] for r in results if r["7日最大涨幅%"] is not None]
    lu = [r for r in results if r["N日涨停天数"] > 0]
    sl = [r for r in results if r["破止损"]]

    print("--- 总体 ---")
    print(f"样本：{n} 条（含当日及之后≥2个交易日）")
    if t1s:
        print(f"T+1 收盘收益：均值 {sum(t1s)/len(t1s):+.2f}% ｜ 胜率 {pct(len([x for x in t1s if x>0]), len(t1s))}（{len([x for x in t1s if x>0])}/{len(t1s)}）")
    if t3s:
        print(f"T+3 收盘收益：均值 {sum(t3s)/len(t3s):+.2f}% ｜ 胜率 {pct(len([x for x in t3s if x>0]), len(t3s))}（{len([x for x in t3s if x>0])}/{len(t3s)}）")
    if t5s:
        print(f"T+5 收盘收益：均值 {sum(t5s)/len(t5s):+.2f}% ｜ 胜率 {pct(len([x for x in t5s if x>0]), len(t5s))}（{len([x for x in t5s if x>0])}/{len(t5s)}）")
    if mr:
        print(f"7日内最大涨幅：均值 {sum(mr)/len(mr):+.2f}% ｜ ≥5% {pct(len([x for x in mr if x>=5]), len(mr))} ｜ ≥10% {pct(len([x for x in mr if x>=10]), len(mr))}")
    print(f"N日内涨停：{pct(len(lu), n)}（{len(lu)}/{n}）")
    print(f"破止损（收盘≤止损价）：{pct(len(sl), n)}（{len(sl)}/{n}）")

    # 涨停样本明细
    if lu:
        print("\n--- 涨停样本明细 ---")
        for r in sorted(lu, key=lambda x: -x["N日涨停天数"]):
            print(f"  {r['日期']} {r['代码']} {r['名称']} 入池价{r['入池价']} 涨停{r['N日涨停天数']}天 T5={r['T5']}% 7日最大+{r['7日最大涨幅%']}% 状态[{r['状态']}]")

    # 破止损样本明细
    if sl:
        print("\n--- 破止损样本明细 ---")
        for r in sorted(sl, key=lambda x: x.get("T5") or 999):
            print(f"  {r['日期']} {r['代码']} {r['名称']} 入池价{r['入池价']} 止损{r['止损价']} T5={r['T5']}% 7日最大+{r['7日最大涨幅%']}% 状态[{r['状态']}]")

    # 路径分桶
    from collections import defaultdict
    by_path = defaultdict(list)
    for r in results:
        by_path[r["路径"] or "未知"].append(r)
    print("\n--- 按蓄势路径分桶 ---")
    for path, rs in sorted(by_path.items()):
        m5 = [r["T5"] for r in rs if r["T5"] is not None]
        lu_n = len([r for r in rs if r["N日涨停天数"] > 0])
        sl_n = len([r for r in rs if r["破止损"]])
        line = f"  {path:<6} 样本{len(rs)}"
        if m5:
            line += f" ｜ T5均值{sum(m5)/len(m5):+.2f}% 胜率{pct(len([x for x in m5 if x>0]), len(m5))}"
        line += f" ｜ 涨停{pct(lu_n, len(rs))} ｜ 破止损{pct(sl_n, len(rs))}"
        print(line)

    if want_json:
        OUT_JSON.write_text(json.dumps({
            "生成时间": datetime.now().isoformat(timespec="seconds"),
            "样本数": n,
            "总体": {
                "T1均值": round(sum(t1s)/len(t1s), 2) if t1s else None,
                "T3均值": round(sum(t3s)/len(t3s), 2) if t3s else None,
                "T5均值": round(sum(t5s)/len(t5s), 2) if t5s else None,
                "涨停数": len(lu),
                "破止损数": len(sl),
            },
            "明细": results,
        }, ensure_ascii=False, indent=1))
        print(f"\n已输出 {OUT_JSON.name}")


if __name__ == "__main__":
    main()
