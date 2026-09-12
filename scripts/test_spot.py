#!/Users/chenyuting/.workbuddy/binaries/python/versions/3.13.12/bin/python3
"""run_backtest.py"""
import os, sys
for k in list(os.environ):
    if "proxy" in k.lower():
        del os.environ[k]

VENV = "/Users/chenyuting/.workbuddy/binaries/python/envs/default/lib/python3.13/site-packages"
sys.path.insert(0, VENV)

import akshare as ak
import pandas as pd

# 直接测试新浪快照
print("=== 新浪快照 ===", flush=True)
try:
    df = ak.stock_zh_a_spot()
    print(f"返回 {len(df)} 行, 列: {list(df.columns[:8])}", flush=True)
    if df is not None and not df.empty:
        print(df.head(3), flush=True)
except Exception as e:
    print(f"失败: {type(e).__name__} {e}", flush=True)

print("=== 东财快照 ===", flush=True)
try:
    df2 = ak.stock_zh_a_spot_em()
    print(f"返回 {len(df2)} 行, 列: {list(df2.columns[:8])}", flush=True)
    if df2 is not None and not df2.empty:
        print(df2.head(3), flush=True)
except Exception as e:
    print(f"失败: {type(e).__name__} {e}", flush=True)
