# -*- coding: utf-8 -*-
"""一次性补数（东财源）：为 K 线缓存补齐指定日期缺口行。

背景：2026-10-23 腾讯双域名（proxy.finance.qq.com / web.ifzq.gtimg.cn）同时 501，
腾讯源不可用。改走东财 push2his 前复权日K（fqt=1），口径已验证与本地缓存一致：
  - 价格：前复权，与本地腾讯 qfq 行完全相同（sh600519/sz300773/sz002415/sh601318 抽验）
  - 成交量：东财单位为【手】，×100 转【股】，与本地行同单位
  - 成交额：东财为真实成交额，优于腾讯的 量×收盘 估算

用法：python backfill_em.py 2026-10-22 [--workers 6]
"""
import sys, time, io, csv
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from warm_start_entry import KLINE_DIR

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
EM_KLINE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
HEADER = ["symbol", "date", "open", "last", "high", "low", "volume", "amount", "exchange"]


def _session():
    s = requests.Session()
    s.trust_env = False
    s.proxies = {"http": None, "https": None}
    s.headers["User-Agent"] = UA
    return s


def fetch_em(sess, sym, beg, end="20301231"):
    """返回 {date: (open, close, high, low, volume股, amount元)}"""
    mkt = {"sh": "1", "sz": "0", "bj": "0"}.get(sym[:2], "0")
    p = {"secid": f"{mkt}.{sym[2:]}", "fields1": "f1,f2,f3",
         "fields2": "f51,f52,f53,f54,f55,f56,f57", "klt": "101", "fqt": "1",
         "beg": beg, "end": end}
    r = sess.get(EM_KLINE, params=p, timeout=15)
    r.raise_for_status()
    data = (r.json() or {}).get("data") or {}
    out = {}
    for line in data.get("klines") or []:
        f = line.split(",")
        out[f[0]] = (float(f[1]), float(f[2]), float(f[3]), float(f[4]),
                     int(float(f[5]) * 100), float(f[6]))
    return out


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "2026-10-22"
    workers = 6
    if "--workers" in sys.argv:
        workers = int(sys.argv[sys.argv.index("--workers") + 1])

    paths = sorted(KLINE_DIR.glob("*.csv"))
    missing = []
    for p in paths:
        try:
            if target not in p.read_text(encoding="utf-8-sig", errors="ignore"):
                missing.append(p)
        except Exception:
            missing.append(p)
    print(f"[补数-东财] 目标 {target}｜总 {len(paths)} 只｜缺 {len(missing)} 只｜并发 {workers}", flush=True)
    if not missing:
        return

    ok = fail = 0
    t0 = time.time()
    lock = []

    def work(p):
        nonlocal ok, fail
        sym = p.stem
        sess = _session()
        try:
            rows = fetch_em(sess, sym, "20260901")
            if target not in rows:
                fail += 1
                if fail <= 8:
                    print(f"  {sym} 无 {target}（停牌/退市?）", flush=True)
                return
            txt = p.read_text(encoding="utf-8-sig")
            lines = [ln for ln in txt.splitlines() if ln.strip()]
            exists = {ln.split(",")[1] for ln in lines[1:] if ln.count(",") >= 8}
            if target in exists:
                ok += 1
                return
            o, c, h, l, v, a = rows[target]
            exch = {"sh": "SH", "sz": "SZ", "bj": "BJ"}.get(sym[:2], "")
            new = (f"{sym},{target},{round(o,3)},{round(c,3)},{round(h,3)},{round(l,3)},"
                   f"{v},{round(a,2)},{exch}")
            # 按日期插入到正确位置
            body = lines[1:]
            body.append(new)
            body.sort(key=lambda x: x.split(",")[1])
            p.write_text("\ufeff" + "\n".join([",".join(HEADER)] + body) + "\n", encoding="utf-8")
            ok += 1
        except Exception as e:
            fail += 1
            if fail <= 8:
                print(f"  {sym} ERR {e}", flush=True)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(work, p) for p in missing]
        for i, f in enumerate(futures, 1):
            f.result()
            if i % 100 == 0:
                print(f"[进度] {i}/{len(missing)} ok={ok} fail={fail} {time.time()-t0:.0f}s", flush=True)

    print(f"[补数-东财] 完成 ok={ok} fail={fail} 耗时{time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
