# -*- coding: utf-8 -*-
"""一次性回补 K 线缓存历史缺口（09-19/09-21），任务 #12 第一步。
全量 1092 只 × ~0.8s/只 ≈ 15 分钟，后台跑。腾讯 K 线 80 天窗口内缺的行由 _merge_klines 自动补。"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from warm_start_entry import KLINE_DIR, update_klines

codes = sorted(p.stem for p in KLINE_DIR.glob("*.csv"))
print(f"[回补] 共 {len(codes)} 只，开始...", flush=True)
t0 = time.time()
ok, fail, added = update_klines(codes, days=80, quiet=False)
print(f"[回补] 完成：成功{ok} 失败{fail} 新增{added}行 耗时{time.time()-t0:.0f}s", flush=True)
