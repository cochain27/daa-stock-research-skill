"""一次性补数：为 K 线缓存补齐 2026-10-22 缺口行。

背景：10-22 凌晨补跑时缓存末行停在 10-21，10-23 15:10 复盘又把当日行直接追加在
10-21 之后，导致 1008/1419 只票永久缺 10-22 一整行（中间缺口，末行日期检测发现不了）。
本脚本按缺失清单逐只拉腾讯全量日K，交给 _merge_klines 合并（已存在的日期保留原值）。
"""
import sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from warm_start_entry import KLINE_DIR, _fetch_tencent, _merge_klines

TARGET = "2026-10-22"
LIST = Path("/tmp/missing_1022.txt")

codes = [ln.strip() for ln in LIST.read_text().splitlines() if ln.strip()]
print(f"[补数] 待补 {len(codes)} 只 → {TARGET}", flush=True)

ok = fail = 0
t0 = time.time()
for i, sym in enumerate(codes, 1):
    path = KLINE_DIR / f"{sym}.csv"
    try:
        df = _fetch_tencent(sym, days=80)
        if df is None or len(df) < 70:
            fail += 1
            continue
        added = _merge_klines(path, sym, df)
        txt = path.read_text(encoding="utf-8-sig", errors="ignore")
        if TARGET in txt:
            ok += 1
        else:
            fail += 1
    except Exception as e:
        fail += 1
        if fail <= 5:
            print(f"  [{i}] {sym} ERR {e}", flush=True)
    if i % 50 == 0:
        print(f"[进度] {i}/{len(codes)} ok={ok} fail={fail} {time.time()-t0:.0f}s", flush=True)
    time.sleep(0.25)

print(f"[补数] 完成 ok={ok} fail={fail} 耗时{time.time()-t0:.0f}s", flush=True)
