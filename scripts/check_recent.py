#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""核实 7 只票最近 15 个交易日的真实涨跌幅，找近期爆发日"""
import json, re, time, urllib.request

CODES = {
    "600184 光电股份": "sh600184",
    "002201 九鼎新材": "sz002201",
    "002815 崇达技术": "sz002815",
    "300563 神宇股份": "sz300563",
    "603466 风语筑":   "sh603466",
    "300252 金信诺":   "sz300252",
    "603679 华体科技": "sh603679",
}

def fetch_kline(symbol, days=180):
    url = (f"https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_="
           f"/CN_MarketDataService.getKLineData?symbol={symbol}"
           f"&scale=240&ma=no&datalen={days}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=15) as r:
        t = r.read().decode("utf-8", "ignore")
    m = re.search(r"\((\[.*\])\)", t, re.S)
    d = json.loads(m.group(1))
    rows = []
    for x in d:
        rows.append({"date": x["day"], "close": float(x["close"]),
                     "vol": float(x["volume"])})
    rows.sort(key=lambda r: r["date"])
    return rows

for name, sym in CODES.items():
    df = fetch_kline(sym, 180)
    # 计算 chg
    for i in range(len(df)):
        df[i]["chg"] = (df[i]["close"]/df[i-1]["close"]-1)*100 if i > 0 else 0.0
    # 近15日
    print(f"\n=== {name} ({sym})  末个交易日 {df[-1]['date']} ===")
    for r in df[-15:]:
        bar = "█" if r["chg"] >= 9.5 else ("▍" if r["chg"] >= 5 else "")
        print(f"  {r['date']}  收{r['close']:>8.2f}  {r['chg']:+6.2f}%  {bar}")
    # 近20日最大涨幅日
    win = df[-20:]
    mx = max(win, key=lambda x: x["chg"])
    print(f"  >>> 近20日最大涨幅日: {mx['date']}  {mx['chg']:+.2f}%")
    time.sleep(0.3)
