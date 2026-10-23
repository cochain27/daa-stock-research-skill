# -*- coding: utf-8 -*-
"""一次性补数（新浪源）：为 K 线缓存补齐指定日期缺口行。

2026-10-23 现状：腾讯双域名（proxy.finance.qq.com / web.ifzq.gtimg.cn）同时 501，
东财 push2his 高并发下大面积 RemoteDisconnected（部分票已补上）。改走新浪日K兜底。

口径校验（缺一不写，防止复权错位）：
  - 新浪为不复权价；写入前必须校验「目标日前一交易日收盘」与本地缓存一致（容差 0.3%），
    一致视为该区间无除权除息，可直接按原价追行。
  - volume 新浪单位为【股】，与本地同单位；amount 无字段，按 量×收盘 估算（与腾讯口径相同）。

用法：python backfill_sina.py 2026-10-22 [--workers 4]
"""
import sys, time, json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from warm_start_entry import KLINE_DIR

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
SINA_KLINE = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
HEADER = ["symbol", "date", "open", "last", "high", "low", "volume", "amount", "exchange"]


def _session():
    s = requests.Session()
    s.trust_env = False
    s.proxies = {"http": None, "https": None}
    s.headers["User-Agent"] = UA
    s.headers["Referer"] = "https://finance.sina.com.cn/"
    return s


def fetch_sina(sess, sym, n=12):
    r = sess.get(SINA_KLINE, params={"symbol": sym, "scale": "240", "ma": "no",
                                    "datalen": str(n)}, timeout=15)
    r.raise_for_status()
    return {x["day"]: x for x in (r.json() or [])}


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "2026-10-22"
    workers = 4
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
    print(f"[补数-新浪] 目标 {target}｜缺 {len(missing)} 只｜并发 {workers}", flush=True)
    if not missing:
        return

    ok = fail = skip_mismatch = nos = 0
    t0 = time.time()

    def work(p):
        nonlocal ok, fail, skip_mismatch, nos
        sym = p.stem
        sess = _session()
        try:
            rows = fetch_sina(sess, sym)
            if target not in rows:
                nos += 1
                if nos <= 8:
                    print(f"  {sym} 新浪无 {target}（停牌/退市?）", flush=True)
                return
            txt = p.read_text(encoding="utf-8-sig")
            lines = [ln for ln in txt.splitlines() if ln.strip()]
            body = lines[1:]
            local = {ln.split(",")[1]: ln.split(",") for ln in body if ln.count(",") >= 8}
            if target in local:
                ok += 1
                return
            # 复权校验：目标日前一交易日收盘须与本地一致
            prev_dates = sorted(d for d in local if d < target)
            if prev_dates:
                prev = prev_dates[-1]
                if prev in rows:
                    lp = float(local[prev][3])
                    sp = float(rows[prev]["close"])
                    if lp and abs(sp - lp) / lp > 0.003:
                        skip_mismatch += 1
                        print(f"  {sym} 复权不一致 {prev}: 本地{lp} 新浪{sp}，跳过", flush=True)
                        return
            x = rows[target]
            o, c, h, l = float(x["open"]), float(x["close"]), float(x["high"]), float(x["low"])
            v = int(float(x["volume"]))
            exch = {"sh": "SH", "sz": "SZ", "bj": "BJ"}.get(sym[:2], "")
            body.append(f"{sym},{target},{round(o,3)},{round(c,3)},{round(h,3)},{round(l,3)},"
                        f"{v},{round(v*c,2)},{exch}")
            body.sort(key=lambda y: y.split(",")[1])
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
            if i % 50 == 0:
                print(f"[进度] {i}/{len(missing)} ok={ok} fail={fail} "
                      f"skip={skip_mismatch} 无数据={nos} {time.time()-t0:.0f}s", flush=True)

    print(f"[补数-新浪] 完成 ok={ok} fail={fail} 复权不一致={skip_mismatch} "
          f"无数据={nos} 耗时{time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
