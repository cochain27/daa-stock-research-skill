# -*- coding: utf-8 -*-
"""watch_history.csv 行业名升级：粗关键词名（计算机/电子/通信/军工…）→ 东财 f127 三级行业名。

背景：低位池早期归档用本地关键词兜底（_industry_by_name），行业名是"计算机""电子"
这类粗名，与 industry_heat.csv 的同花顺二级板块名对不上（行业热度列基本全是冷门）。
本脚本按代码逐个查东财 f127 三级行业（与 THS 板块名高度同构），只改"行业"列，
其余字段不动；查不到 f127 的保留原值。幂等，可重复跑。

用法：python backfill_watch_industry.py
"""
import sys, os, csv
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import requests
import fetch_data  # 加载直连 patch（trust_env=False）

CSV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "watch_history.csv")
_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
_CACHE = {}


def f127(code):
    """东财三级行业名（f127）。短超时 8s，进程内缓存。失败返回 None。
    用 push2delay host：push2/push2his/17.push2 当前被 IP 级风控返回空，
    push2delay（延迟行情）可用且 f127 字段同构（2026-09-09 实测）。"""
    if code in _CACHE:
        return _CACHE[code]
    market = "1" if code.startswith("6") else "0"
    try:
        r = requests.get(
            f"https://push2delay.eastmoney.com/api/qt/stock/get?secid={market}.{code}&fields=f57,f127",
            headers=_UA, timeout=8)
        d = r.json().get("data") or {}
        name = (d.get("f127") or "").strip() or None
    except Exception:
        name = None
    _CACHE[code] = name
    return name


def main():
    rows = list(csv.DictReader(open(CSV, encoding="utf-8")))
    if not rows:
        print("watch_history.csv 为空")
        return 1
    changed, failed = 0, 0
    for i, r in enumerate(rows, 1):
        code = str(r["代码"]).split(".")[0]
        old = (r.get("行业") or "").strip()
        new = f127(code)
        if new:
            r["行业"] = new
            if new != old:
                changed += 1
        else:
            failed += 1
        if i % 30 == 0:
            print(f"  进度 {i}/{len(rows)}  已更新{changed}  未取到{failed}", flush=True)
    fieldnames = list(rows[0].keys())
    with open(CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    print(f"完成：{len(rows)} 行，更新行业 {changed} 个，未取到 {failed} 个（保留原值）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
