#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
前兆埋伏票共性分析 v2 —— 正确口径版
数据源: 新浪日K (urllib 直连, 零依赖)
修正: 真实涨跌幅(close环比) / 量比(vol/前5均量) / 60日位置 / dist60 / 20日振幅 / 成交额
样本: 6只首涨停票 + 1只准爆发对照(金信诺9/11 +7.1%)
"""
import json, re, time, urllib.request
from datetime import datetime, timedelta

CODES = {
    "600184 光电股份": "sh600184",
    "002201 九鼎新材": "sz002201",
    "002815 崇达技术": "sz002815",
    "300563 神宇股份": "sz300563",
    "603466 风语筑":   "sh603466",
    "300252 金信诺":   "sz300252",
    "603679 华体科技": "sh603679",
}
N_DAYS = 180  # 约9个月，够60日位置

def fetch_kline(symbol, days=180):
    url = (f"https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_="
           f"/CN_MarketDataService.getKLineData?symbol={symbol}"
           f"&scale=240&ma=no&datalen={days}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    # 绕过系统代理，国内直连
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=15) as r:
        t = r.read().decode("utf-8", "ignore")
    m = re.search(r"\((\[.*\])\)", t, re.S)
    d = json.loads(m.group(1))
    rows = []
    for x in d:
        rows.append({
            "date": x["day"],
            "open": float(x["open"]), "high": float(x["high"]),
            "low": float(x["low"]), "close": float(x["close"]),
            "vol": float(x["volume"]),  # 股
        })
    rows.sort(key=lambda r: r["date"])  # 正序
    return rows

def enrich(df):
    """计算全部指标。df: list[dict] 已按日期正序"""
    n = len(df)
    for i in range(n):
        r = df[i]
        r["chg"] = (r["close"] / df[i-1]["close"] - 1) * 100 if i > 0 else 0.0
        r["amount_yi"] = r["vol"] * r["close"] / 1e8  # 成交额≈量×价(亿)
    # 量比: 当日vol / 前5日均量
    for i in range(n):
        if i >= 5:
            avg5 = sum(df[j]["vol"] for j in range(i-5, i)) / 5
            df[i]["lb"] = df[i]["vol"] / avg5 if avg5 else 0.0
        else:
            df[i]["lb"] = 0.0
    # 20日振幅、60日位置、dist60、近10日低点差
    for i in range(n):
        r = df[i]
        # 20日振幅 = (近20日最高 - 近20日最低)/近20日最低
        lo = max(0, i-19)
        win20 = df[lo:i+1]
        hi20 = max(x["high"] for x in win20)
        lo20 = min(x["low"] for x in win20)
        r["amp20"] = (hi20 - lo20) / lo20 * 100 if lo20 else 0.0
        # 60日位置
        lo60 = max(0, i-59)
        win60 = df[lo60:i+1]
        hi60 = max(x["high"] for x in win60)
        lo60v = min(x["low"] for x in win60)
        r["pos60"] = (r["close"] - lo60v) / (hi60 - lo60v) if hi60 != lo60v else 0.5
        r["dist60"] = (r["close"] / hi60 - 1) * 100 if hi60 else 0.0
        # 近10日低点差
        lo10 = max(0, i-9)
        low10 = min(x["low"] for x in df[lo10:i+1])
        r["d_low10"] = (r["close"] / low10 - 1) * 100
    # 均线
    for i in range(n):
        for w in (5, 10, 20):
            if i >= w-1:
                df[i][f"ma{w}"] = sum(df[j]["close"] for j in range(i-w+1, i+1)) / w
            else:
                df[i][f"ma{w}"] = None
    # MACD(12,26,9)
    ema12, ema26 = None, None
    difs = []
    for i in range(n):
        c = df[i]["close"]
        ema12 = c if ema12 is None else ema12 * 11/13 + c * 2/13
        ema26 = c if ema26 is None else ema26 * 25/27 + c * 2/27
        difs.append(ema12 - ema26)
    dea = None
    for i in range(n):
        dea = difs[i] if dea is None else dea * 8/10 + difs[i] * 2/10
        df[i]["macd_bull"] = 1 if difs[i] > dea else 0
        df[i]["macd_bar"] = (difs[i] - dea) * 2
    return df

def find_first_burst(df, is_chinext=False):
    """首次涨停日。主板≥9.5%，创业板(300/301)≥19.5%。返回(index, date)"""
    limit = 19.5 if is_chinext else 9.5
    for i, r in enumerate(df):
        if r["chg"] >= limit:
            return i, r["date"]
    return None, None

def main():
    results = {}
    for name, sym in CODES.items():
        is_chinext = sym.startswith(("sz300", "sz301"))
        df = fetch_kline(sym, N_DAYS)
        enrich(df)
        idx, bdate = find_first_burst(df, is_chinext)
        results[name] = {"df": df, "burst_idx": idx, "burst_date": bdate, "sym": sym, "cn": is_chinext}
        print(f"[{name}] 数据{len(df)}行 {df[0]['date']}~{df[-1]['date']} "
              f"末次涨停日={bdate or '无'}"
              f"{'  (创业板20%)' if is_chinext else ''}")
        time.sleep(0.3)

    print("\n" + "="*80)
    print("每只票：首涨停日前 1 日特征（前兆）")
    print("="*80)
    rows = []
    for name, info in results.items():
        df, idx = info["df"], info["burst_idx"]
        if idx is None:
            # 准爆发: 用最近最大阳线日
            cands = sorted(range(len(df)), key=lambda i: df[i]["chg"], reverse=True)
            idx = cands[0]
            info["burst_idx"] = idx
            info["burst_date"] = df[idx]["date"]
            info["approx"] = True
        else:
            info["approx"] = False
        i1 = idx - 1
        if i1 < 0:
            print(f"{name}: 爆发日为首日，无前兆可查")
            continue
        r = df[i1]
        row = {
            "name": name, "burst": info["burst_date"], "approx": info["approx"],
            "d1_chg": r["chg"], "d1_lb": r["lb"], "d1_chg5": df[i1]["close"]/df[max(0,i1-4)]["close"]-1,
            "d1_amp20": r["amp20"], "d1_pos60": r["pos60"], "d1_dist60": r["dist60"],
            "d1_amount": r["amount_yi"], "d1_dlow10": r["d_low10"],
            "d1_macd": r["macd_bull"], "d1_ma5gt10": (r.get("ma5") and r.get("ma10") and r["ma5"] > r["ma10"]),
            "d3_lb": sum(df[j]["lb"] for j in range(max(0,idx-3), idx))/min(3, idx),
            "burst_lb": df[idx]["lb"], "burst_amount": df[idx]["amount_yi"],
            "burst_ratio": df[idx]["vol"] / (sum(df[j]["vol"] for j in range(max(0,idx-5), idx))/min(5,idx)) if idx>=5 else 0,
        }
        # 5日均值
        row["d5_lb"] = sum(df[j]["lb"] for j in range(max(0,idx-5), idx))/min(5, idx)
        row["d5_chg"] = sum(df[j]["chg"] for j in range(max(0,idx-5), idx))/min(5, idx)
        rows.append(row)
        tag = "【对照-未涨停】" if info["approx"] else ""
        print(f"\n{tag}{name}  首爆发:{info['burst_date']}  前1日特征:")
        print(f"  收盘涨幅={r['chg']:.2f}% | 量比={r['lb']:.2f}x | 5日涨幅={row['d1_chg5']*100:.1f}%")
        print(f"  20日振幅={r['amp20']:.1f}% | 60日位置={r['pos60']:.2f} | dist60={r['dist60']:.1f}%")
        print(f"  成交额={r['amount_yi']:.2f}亿 | 近10日低点差={r['d_low10']:.1f}% | MACD多头={r['d1_macd']} MA5>MA10={int(r['d1_ma5gt10'])}")
        print(f"  前3日量比均值={row['d3_lb']:.2f}x | 前5日量比均值={row['d5_lb']:.2f}x | 前5日涨幅均值={row['d5_chg']:.2f}%")
        print(f"  爆发日: 量比={df[idx]['lb']:.2f}x 成交额={df[idx]['amount_yi']:.2f}亿 量/前5均量={row['burst_ratio']:.1f}x")

    # ===== 共性统计（仅主样本，不含对照） =====
    mains = [r for r in rows if not r["approx"]]
    n = len(mains)
    print("\n" + "="*80)
    print(f"共性统计（{n}只首涨停主样本） + 对照({len(rows)-n}只)")
    print("="*80)
    if n:
        import statistics as st
        def agg(key, fmt=".2f", pct=False):
            vals = [r[key] for r in mains]
            vals5 = [r[key] for r in rows]  # 含对照
            m, med, lo, hi = st.mean(vals), st.median(vals), min(vals), max(vals)
            m5 = st.mean(vals5)
            suf = "%" if pct else ""
            return f"{m:{fmt}}{suf} | 中位{med:{fmt}}{suf} | {lo:{fmt}}~{hi:{fmt}}{suf} | 含对照均值{m5:{fmt}}{suf}"
        print(f"{'指标':<16} {'均值|中位|范围 (含对照均值)'}")
        print("-"*70)
        print(f"{'前1日涨幅%':<16} {agg('d1_chg', pct=True)}")
        print(f"{'前1日量比x':<16} {agg('d1_lb')}")
        print(f"{'前1日5日涨幅%':<16} {agg('d1_chg5', pct=True)}")
        print(f"{'前1日20日振幅%':<16} {agg('d1_amp20', pct=True)}")
        print(f"{'前1日60日位置':<16} {agg('d1_pos60')}")
        print(f"{'前1日dist60%':<16} {agg('d1_dist60', pct=True)}")
        print(f"{'前1日成交额亿':<16} {agg('d1_amount')}")
        print(f"{'前1日近10日低点差%':<16} {agg('d1_dlow10', pct=True)}")
        print(f"{'前3日量比均值':<16} {agg('d3_lb')}")
        print(f"{'前5日量比均值':<16} {agg('d5_lb')}")
        print(f"{'前5日涨幅均值%':<16} {agg('d5_chg', pct=True)}")
        print(f"{'爆发日量比':<16} {agg('burst_lb')}")
        print(f"{'爆发日成交额亿':<16} {agg('burst_amount')}")
        print(f"{'爆发/前5均量x':<16} {agg('burst_ratio')}")

        # 命中率
        def hit(key, cond, label):
            m = sum(1 for r in mains if cond(r[key]))
            c = sum(1 for r in rows if cond(r[key]))
            print(f"  {label}: 主样本 {m}/{n} ({m/n*100:.0f}%) | 含对照 {c}/{len(rows)}")
        print("\n【各条件命中率】")
        hit("d1_lb", lambda v: v >= 1.3, "量比≥1.3x")
        hit("d1_lb", lambda v: 1.1 <= v <= 3.0, "量比1.1~3.0x(温和)")
        hit("d1_chg", lambda v: 0 <= v <= 8, "前1日涨幅0~8%(温和启动)")
        hit("d1_chg5", lambda v: 0 <= v <= 0.12, "5日涨幅0~12%(刚启动)")
        hit("d1_pos60", lambda v: v < 0.5, "60日位置<0.50")
        hit("d1_pos60", lambda v: v < 0.7, "60日位置<0.70")
        hit("d1_dist60", lambda v: v < 0, "dist60<0(高点下方)")
        hit("d1_amp20", lambda v: v < 15, "20日振幅<15%")
        hit("d1_amount", lambda v: 2 <= v <= 20, "成交额2~20亿")
        hit("d1_dlow10", lambda v: v < 15, "近10日低点差<15%")
        hit("d1_macd", lambda v: v == 1, "MACD多头")

        # 综合命中: 全部满足几条
        print("\n【每只票满足核心条件数(共6条: 量比≥1.3 / 5日涨幅0~12% / 60日位置<0.7 / 振幅<15% / 成交额2~20亿 / dist60<0)】")
        for r in rows:
            s = sum([
                r["d1_lb"] >= 1.3,
                0 <= r["d1_chg5"] <= 0.12,
                r["d1_pos60"] < 0.7,
                r["d1_amp20"] < 15,
                2 <= r["d1_amount"] <= 20,
                r["d1_dist60"] < 0,
            ])
            print(f"  {r['name']}: {s}/6 {'(对照)' if r['approx'] else ''}")

if __name__ == "__main__":
    main()
