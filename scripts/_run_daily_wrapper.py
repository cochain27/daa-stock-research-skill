# -*- coding: utf-8 -*-
"""daily_report.py 运行包装器（不修改 skill 本体）。

背景：
1. akshare 大量接口未设置超时，行情源/代理链路间歇性无响应时，日报流程会永久挂起；
2. 本机直连东财会被 reset，必须走系统代理（requests 默认会读取 macOS 系统代理，不要关闭）；
3. 东财拒绝 python-requests 默认 UA；
4. 全 A 快照默认逐页串行拉取（约 59 页），在链路抖动时单页可达 60s+，是主要耗时项。

本包装提供：
- requests：25s 超时 + 浏览器 UA + 退避重试；
- curl_cffi：禁止 timeout=None；
- 全 A 快照：并发分页拉取（8 线程）+ 结果缓存（脚本内会调用两次）；
- 禁用已被限流的新浪源，强制走东财兜底；
- faulthandler 看门狗，每 300s 打印全线程堆栈。
"""
import faulthandler
import math
import socket
import sys
import time
import runpy
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests

REQ_TIMEOUT = 25
DUMP_INTERVAL = 300
PAGE_WORKERS = 8
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

socket.setdefaulttimeout(REQ_TIMEOUT)

# ---- 1. requests：超时 + UA + 退避重试 ----
_orig_req = requests.Session.request


def _req(self, *args, **kwargs):
    kwargs.setdefault("timeout", REQ_TIMEOUT)
    hdrs = kwargs.get("headers")
    hdrs = dict(hdrs) if hdrs else {}
    if not any(k.lower() == "user-agent" for k in hdrs):
        hdrs["User-Agent"] = UA
    kwargs["headers"] = hdrs
    last_err = None
    for attempt in range(4):
        try:
            return _orig_req(self, *args, **kwargs)
        except requests.exceptions.RequestException as e:
            last_err = e
            if attempt < 3:
                time.sleep(2 + 2 * attempt)
    raise last_err


requests.Session.request = _req

# ---- 2. curl_cffi：禁止 timeout=None ----
try:
    from curl_cffi.requests import Session as CffiSession

    _cffi_req = CffiSession.request

    def _cffi_req(self, *args, **kwargs):
        if kwargs.get("timeout", "__unset__") is None:
            kwargs["timeout"] = REQ_TIMEOUT
        return _cffi_req(self, *args, **kwargs)

    CffiSession.request = _cffi_req
except Exception as e:  # pragma: no cover
    print(f"[wrapper] curl_cffi patch failed: {e}")

# ---- 3. 全 A 快照并发分页（替换 akshare 的串行实现）----
try:
    import akshare.stock_feature.stock_hist_em as _em_mod

    def _fetch_page(args):
        url, base_params, page = args
        p = dict(base_params)
        p["pn"] = str(page)
        last_err = None
        for i in range(3):
            try:
                r = requests.get(url, params=p, timeout=REQ_TIMEOUT,
                                 headers={"User-Agent": UA})
                return pd.DataFrame(r.json()["data"]["diff"])
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(1 + i)
        raise last_err

    def _fast_fetch_paginated_data(url, base_params, timeout=15):
        params = dict(base_params)
        params["pn"] = "1"
        last_err = None
        for i in range(3):
            try:
                r = requests.get(url, params=params, timeout=REQ_TIMEOUT,
                                 headers={"User-Agent": UA})
                data_json = r.json()
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(1 + i)
        else:
            raise last_err
        diff = data_json["data"]["diff"]
        per_page = len(diff)
        total_page = math.ceil(data_json["data"]["total"] / per_page)
        frames = [pd.DataFrame(diff)]
        if total_page > 1:
            jobs = [(url, base_params, p) for p in range(2, total_page + 1)]
            with ThreadPoolExecutor(max_workers=PAGE_WORKERS) as pool:
                frames.extend(pool.map(_fetch_page, jobs))
        temp_df = pd.concat(frames, ignore_index=True)
        temp_df["f3"] = pd.to_numeric(temp_df["f3"], errors="coerce")
        temp_df.sort_values(by=["f3"], ascending=False, inplace=True)
        temp_df.reset_index(inplace=True)
        temp_df["index"] = temp_df["index"].astype(int) + 1
        return temp_df

    _em_mod.fetch_paginated_data = _fast_fetch_paginated_data
    print("[wrapper] eastmoney spot fetcher -> parallel")
except Exception as e:  # pragma: no cover
    print(f"[wrapper] parallel patch failed: {e}")

# ---- 4. 全 A 快照：新浪并发分页（东财当前对本机 IP 全站超时）----
try:
    import akshare as ak
    import akshare.stock.stock_zh_a_sina as _sina_mod
    from akshare.stock.cons import (zh_sina_a_stock_url, zh_sina_a_stock_payload,
                                    zh_sina_a_stock_count_url)
    from akshare.utils import demjson
    import re as _re

    def _sina_page(page):
        p = dict(zh_sina_a_stock_payload)
        p.update({"page": page})
        last_err = None
        for i in range(3):
            try:
                r = requests.get(zh_sina_a_stock_url, params=p, timeout=REQ_TIMEOUT,
                                 headers={"User-Agent": UA})
                return pd.DataFrame(demjson.decode(r.text))
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(1 + i)
        raise last_err

    def _fast_sina_spot():
        res = requests.get(zh_sina_a_stock_count_url, timeout=REQ_TIMEOUT,
                           headers={"User-Agent": UA})
        page_count = int(_re.findall(r"\d+", res.text)[0]) / 80
        page_count = page_count if isinstance(page_count, int) else int(page_count) + 1
        with ThreadPoolExecutor(max_workers=4) as pool:
            frames = list(pool.map(_sina_page, range(1, page_count + 1)))
        big_df = pd.concat(frames, ignore_index=True)
        big_df = big_df.astype({
            "trade": "float", "pricechange": "float", "changepercent": "float",
            "buy": "float", "sell": "float", "settlement": "float", "open": "float",
            "high": "float", "low": "float", "volume": "float", "amount": "float",
            "per": "float", "pb": "float", "mktcap": "float", "nmc": "float",
            "turnoverratio": "float",
        })
        big_df.columns = [
            "代码", "_", "名称", "最新价", "涨跌额", "涨跌幅", "买入", "卖出", "昨收",
            "今开", "最高", "最低", "成交量", "成交额", "时间戳", "_", "_", "_", "_", "_",
        ]
        return big_df[[
            "代码", "名称", "最新价", "涨跌额", "涨跌幅", "买入", "卖出", "昨收", "今开",
            "最高", "最低", "成交量", "成交额", "时间戳",
        ]]

    ak.stock_zh_a_spot = _fast_sina_spot
    print("[wrapper] sina spot fetcher -> parallel")
except Exception as e:  # pragma: no cover
    print(f"[wrapper] sina parallel patch failed: {e}")

# ---- 5. 快照缓存（脚本内会调用两次）----
try:
    import fetch_data as _fd

    _orig_snapshot = _fd.get_market_snapshot
    _cache = {}

    def _cached_snapshot():
        if "v" not in _cache:
            _cache["v"] = _orig_snapshot()
        return _cache["v"]

    _fd.get_market_snapshot = _cached_snapshot
    print("[wrapper] market snapshot cached")
except Exception as e:  # pragma: no cover
    print(f"[wrapper] snapshot cache failed: {e}")

# ---- 6. 看门狗 ----
faulthandler.dump_traceback_later(DUMP_INTERVAL, exit=False, repeat=True)

SCRIPT_DIR = "/Users/chenyuting/Documents/workbuddy/workbuddy-daa/daa-stock-research-skill/scripts"
SCRIPT = f"{SCRIPT_DIR}/daily_report.py"
sys.path.insert(0, SCRIPT_DIR)
sys.argv = [SCRIPT]
runpy.run_path(SCRIPT, run_name="__main__")
