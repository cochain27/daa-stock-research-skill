#!/Users/chenyuting/.workbuddy/binaries/python/versions/3.13.12/bin/python3
"""run_backtest.py"""
import os, sys
for k in list(os.environ):
    if "proxy" in k.lower():
        del os.environ[k]

VENV = "/Users/chenyuting/.workbuddy/binaries/python/envs/default/lib/python3.13/site-packages"
sys.path.insert(0, VENV)

import requests
import pandas as pd

# 新浪全A快照直调（无需 akshare）
print("=== 直调新浪快照 ===", flush=True)
try:
    r = requests.get(
        "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData",
        params={"page": 1, "num": 5, "sort": "amount", "asc": 0, "node": "hs_a", "symbol": "", "_s_r_a": "page"},
        timeout=10
    )
    print(f"状态: {r.status_code}, 内容长度: {len(r.text)}, 前200字: {r.text[:200]}", flush=True)
except Exception as e:
    print(f"失败: {e}", flush=True)

# 东财快照
print("=== 东财快照 ===", flush=True)
try:
    r2 = requests.get(
        "https://push2.eastmoney.com/api/qt/clist/get",
        params={"pn": 1, "pz": 5, "po": 1, "np": 1, "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "fltt": 2, "invt": 2, "fid": "f3", "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
                "fields": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f14,f15,f16,f17,f18,f20,f21,f23,f24,f25,f22,f11,f62,f128,f136,f115,f152"},
        timeout=10
    )
    print(f"状态: {r2.status_code}, 内容长度: {len(r2.text)}, 前200字: {r2.text[:200]}", flush=True)
except Exception as e:
    print(f"失败: {e}", flush=True)
