# -*- coding: utf-8 -*-
"""低位启动观察池回溯补录工具。

背景：low_pos_watch() 只在收盘复盘时按「当日实时快照」筛选，复盘 md 只有一份
（次日覆盖）且被 .gitignore 排除 —— 历史观察池无处可查。本脚本用同一套指标口径
对历史交易日重算，补写进 data/watch_history.csv。

口径与 stock_screener.low_pos_watch 保持一致：
  白名单 60/00/30、非ST、成交额>1.5亿、当日涨跌幅 -4%~4%、
  60日位置<0.40、启动信号>=2（站上MA20/MACD多头/放量1.2x/5日+2%~12%）、
  当日非涨停、同行业最多 MAX_SAME_INDUSTRY 只。
差异（无法回溯实时数据，属近似）：
  1) 候选池 = 当前全A快照按成交额取前 N 只（默认1200），而非当日换手率前80
  2) 涨停判定改用当日涨跌幅（主板/中小板 >=9.8%，创业板30开头 >=19.8%）
  3) 现价用当日收盘价

用法：
  python backfill_watch.py 2026-09-04 2026-09-07     # 指定日期
  python backfill_watch.py --days 5                   # 回溯最近5个交易日
  python backfill_watch.py --days 5 --top 2000        # 扩大候选池
"""
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import json

import pandas as pd
import requests

from fetch_data import get_market_snapshot, _industry_by_name  # 超时 patch + 快照 + 本地行业兜底
from industry_heat_tool import industry_heat_status_simple
from stock_screener import _tech_indicators, ALLOW_CODE_PREFIX, MAX_SAME_INDUSTRY
from config import (LOW_POS_MAX_20D_AMP, LOW_POS_MAX_20D_STD,
                    LOW_POS_MIN_DIST_60D_HIGH, LOW_POS_BEST_POS, LOW_POS_BEST_AMOUNT,
                    LOW_POS_HEAT_BONUS, LOW_POS_HEAT_MAIN_ONLY)
from close_review import _archive_watch

CAND_TOP = 1200        # 候选池大小（按当前成交额排序）
WORKERS = 10           # 新浪 JSON 接口无 JS 引擎，可放心并发
MIN_AMOUNT = 1.5e8     # 成交额门槛
MAX_POS = 0.40         # 60日位置上限


def _is_zt(code, pct):
    """回溯用涨停近似判定：主板/中小板 10%，创业板 20%"""
    if pct is None or pd.isna(pct):
        return False
    limit = 19.8 if code.startswith("30") else 9.8
    return pct >= limit


_SINA_KLINE = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"


def _load_hist(code):
    """拉个股历史K线（新浪公开 JSON 接口，纯 HTTP，并发安全）。

    为什么不用 akshare：
    - 首选的新浪源 ak.stock_zh_a_daily 内部走 py_mini_racer 执行 JS，非线程安全，
      并发下直接 FATAL 崩溃（2026-09-08 实测）；
    - 备选的东财源 ak.stock_zh_a_hist 在千次级批量请求后会整站封 IP（RemoteDisconnected）。
    差异：本接口返回**不复权**数据（low_pos_watch 用 qfq），近期有除权的票会略有偏差。
    """
    sym = ("sh" if code.startswith(("6", "9")) else "sz") + code
    try:
        r = requests.get(_SINA_KLINE, params={"symbol": sym, "scale": "240",
                                              "ma": "no", "datalen": "250"}, timeout=20)
        if r.status_code != 200:
            return code, None
        arr = json.loads(r.text)
        if not arr:
            return code, None
        df = pd.DataFrame(arr).rename(columns={"day": "日期", "open": "开盘", "high": "最高",
                                               "low": "最低", "close": "收盘", "volume": "成交量"})
        df["日期"] = pd.to_datetime(df["日期"])
        for c in ["开盘", "最高", "最低", "收盘", "成交量"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.sort_values("日期").reset_index(drop=True)
        df["成交额"] = df["成交量"] * df["收盘"]        # 近似：股数 × 收盘价
        df["涨跌幅"] = df["收盘"].pct_change() * 100
        if len(df) < 70:
            return code, None
        return code, df
    except Exception:
        return code, None


def screen_on_date(hists, codes, names, as_of):
    """对单个交易日切片计算，返回当日的低位启动观察列表"""
    ts = pd.Timestamp(as_of)
    picks = []
    for code in codes:
        h = hists.get(code)
        if h is None:
            continue
        sub = h[h["日期"] <= ts]
        if len(sub) < 70:
            continue
        if sub["日期"].iloc[-1] != ts:      # 当日停牌/无数据
            continue
        try:
            amount = float(sub["成交额"].iloc[-1])
            if amount < MIN_AMOUNT:
                continue
            pct = float(sub["涨跌幅"].iloc[-1])
            if not (-4 <= pct <= 4):
                continue
            if _is_zt(code, pct):
                continue
            ind = _tech_indicators(sub)
            if ind is None or len(ind) == 0:
                continue
            last = ind.iloc[-1]
            close = float(last["收盘"])
            hi60 = ind["最高"].tail(60).max()
            lo60 = ind["最低"].tail(60).min()
            if hi60 <= lo60:
                continue
            pos = (close - lo60) / (hi60 - lo60)
            if pos >= MAX_POS:
                continue
            # ===== 前兆筛选（2026-09-10 与线上 low_pos_watch 同步）=====
            amp20 = (ind["收盘"].tail(20).max() - ind["收盘"].tail(20).min()) / ind["收盘"].tail(20).mean()
            std20 = ind["收盘"].tail(20).std() / ind["收盘"].tail(20).mean()
            dist60 = (close / hi60 - 1) * 100
            if amp20 >= LOW_POS_MAX_20D_AMP:
                continue
            if std20 >= LOW_POS_MAX_20D_STD:
                continue
            if dist60 <= LOW_POS_MIN_DIST_60D_HIGH:
                continue
            pos_bonus = 1 if LOW_POS_BEST_POS[0] <= pos < LOW_POS_BEST_POS[1] else 0
            amt = float(sub["成交额"].iloc[-1])
            amt_bonus = 1 if LOW_POS_BEST_AMOUNT[0] <= amt < LOW_POS_BEST_AMOUNT[1] else 0
            # 行业热度加分（2026-09-10 与线上 low_pos_watch 同步）
            _h_tag, *_ = industry_heat_status_simple(_industry_by_name(names.get(code, "")), window=5, top_n=15)
            if LOW_POS_HEAT_BONUS:
                heat_bonus = 1 if _h_tag == "🔥主线" else (0 if LOW_POS_HEAT_MAIN_ONLY else (0.5 if _h_tag == "🌤升温" else 0))
            else:
                heat_bonus = 0
            ma5, ma20 = last["MA5"], last["MA20"]
            signals = []
            if close > ma20:
                signals.append("站上MA20")
            if last["DIF"] > last["DEA"]:
                signals.append("MACD多头")
            v_mean = ind["成交量"].tail(5).mean()
            v_ratio = float(last["成交量"]) / v_mean if v_mean else 0
            if v_ratio > 1.2:
                signals.append(f"放量{v_ratio:.1f}x")
            chg5 = (close / float(ind.iloc[-6]["收盘"]) - 1) * 100 if len(ind) >= 6 else 0
            if 2 <= chg5 <= 12:
                signals.append(f"5日+{chg5:.1f}%")
            if len(signals) < 2:
                continue
            buy_lo = round(min(close, float(ma5)) * 0.99, 2)
            buy_hi = round(close * 1.03, 2)
            picks.append({
                "代码": code, "名称": names.get(code, code), "现价": round(close, 2),
                "60日位置": round(pos, 2), "信号": "、".join(signals),
                "5日涨幅": round(chg5, 1), "行业": "",
                "买点区间": f"{buy_lo}-{buy_hi}",
                "止损价": round(close * 0.94, 2),
                "关注逻辑": f"60日低位({pos:.0%})，{'、'.join(signals)}，横盘蓄势紧凑、贴近60日高点，趋势启动初现可跟踪",
                "_pos_bonus": pos_bonus, "_amt_bonus": amt_bonus, "_heat_bonus": heat_bonus,
            })
        except Exception:
            continue

    picks.sort(key=lambda x: (-len(x["信号"].split("、")),
                              -(x.get("_pos_bonus", 0) + x.get("_amt_bonus", 0) + x.get("_heat_bonus", 0)),
                              x["60日位置"]))
    if MAX_SAME_INDUSTRY > 0:
        ind_count, dedup = {}, []
        for r in picks:
            # 行业只用本地名称关键词兜底：get_industry_of 会打东财个股信息接口，
            # 东财被封时每次请求都要等满超时（60s），回溯直接卡死（2026-09-08 实测）。
            ind = _industry_by_name(r["名称"]) or ""
            r["行业"] = ind or "未知"
            if ind and ind_count.get(ind, 0) >= MAX_SAME_INDUSTRY:
                continue          # 行业未知时不参与去重，避免误杀
            if ind:
                ind_count[ind] = ind_count.get(ind, 0) + 1
            dedup.append(r)
        picks = dedup
    return picks[:3]


def main():
    args = sys.argv[1:]
    top = CAND_TOP
    if "--top" in args:
        top = int(args[args.index("--top") + 1])
        args = args[:args.index("--top")] + args[args.index("--top") + 2:]

    dates = [a for a in args if not a.startswith("-")]
    if not dates:
        n = 5
        if "--days" in args:
            n = int(args[args.index("--days") + 1])
        # 最近 n 个自然日（非交易日会被 len(sub)<70 / 日期不匹配自动跳过）
        dates = [(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n, 0, -1)]

    print(f"[候选] 拉取全A快照，按成交额取前 {top} 只 ...", flush=True)
    snap = get_market_snapshot()
    if snap is None or snap.empty:
        print("快照获取失败，退出")
        return 1
    snap = snap.copy()
    snap["代码"] = snap["代码"].astype(str).str.replace(r"\.0$", "", regex=True)
    snap = snap[snap["代码"].str.match(r"^(?:" + "|".join(ALLOW_CODE_PREFIX) + ")", na=False)]
    snap = snap[~snap["名称"].astype(str).str.contains("ST|退", na=False)]
    snap["_amt"] = pd.to_numeric(snap.get("成交额"), errors="coerce").fillna(0)
    snap = snap.sort_values("_amt", ascending=False).head(top)
    codes = snap["代码"].tolist()
    names = dict(zip(snap["代码"], snap["名称"].astype(str)))
    print(f"[候选] {len(codes)} 只，开始并发拉历史K线（{WORKERS} 线程）...", flush=True)

    hists, t0 = {}, time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(_load_hist, c): c for c in codes}
        for i, f in enumerate(as_completed(futs), 1):
            c, h = f.result()
            if h is not None:
                hists[c] = h
            if i % 200 == 0:
                print(f"  进度 {i}/{len(codes)}  ok={len(hists)}  {time.time()-t0:.0f}s", flush=True)
    print(f"[候选] 历史K线就绪 {len(hists)} 只，用时 {time.time()-t0:.0f}s\n", flush=True)

    for d in dates:
        picks = screen_on_date(hists, codes, names, d)
        if not picks:
            print(f"=== {d}：无符合（或非交易日）===\n")
            continue
        print(f"=== {d}：{len(picks)} 只 ===")
        for p in picks:
            print(f"  {p['名称']}({p['代码']})  现价{p['现价']}  60日位置{p['60日位置']:.0%}  "
                  f"5日{p['5日涨幅']:+.1f}%  买点{p['买点区间']}  止损{p['止损价']}  [{p['信号']}]")
        _archive_watch(picks, d)
        print(f"  → 已写入 data/watch_history.csv\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
