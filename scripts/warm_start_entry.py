# -*- coding: utf-8 -*-
"""温和放量初动池 —— 第3正式策略选股（warm_start_entry）。

定位：短线激进（3天）+ 右侧趋势（波段2-4周）之外的第3路正式策略：
「低位蓄势后温和放量初动」。2026-09-20 由观察池升级为正式策略。

出场规则（V2 已固化，1366 样本逐日重放，warm_start_improve_study.py）：
  - 入场：信号日 T 收盘确认 → T+1 开盘介入
  - 硬止损：-8%（止损价 = 现价 × 0.92）
  - 破MA5离场：持有 T+2 收盘起，收盘 < MA5 → 次日开盘离场
  - 无固定止盈（让利润奔跑）；最长持有 T+5 收盘强制离场
  - 大盘过滤已证伪（弱市信号胜率 69.2% 反超强市 50.3%），不设开闸条件
  - V2 效果：盈亏比 3.07→4.46、最大亏 -12.3%→-9.3%，代价胜率 -4.7pp

回测结论（warm_start_backtest.py，660 天扩样本 2024-04~2026-09，腾讯源 1092 只）：
  最优档 = 量比1.3-2.5 + 5日+3~8% + 位置<80% + 站上MA20 + MACD多头
          + 当日≤9.5% + 振幅≤7.5% + 周线共振(周收>25日周均且周均上行)
          + 保守蓄势(前10振幅≤3.5% 且 前10均量/前60均量≤0.70)
          + 成交额≤16亿 → 1366 信号：T5 胜率 59.0%、均值 +4.20%、盈亏比 3.07
  「周线共振」最大增益来源（盈亏比+38%）；「回踩不破」弱化不启用。

数据流：
  - K线：data/klines/*.csv（腾讯前复权，含 amount），先增量补到最新交易日
  - 快照：get_market_snapshot()（腾讯源）→ 取成交额前 top 只 → 逐票判定
  - 输出：data/warm_start_pool_<date>.csv + 微信推送（--push）

用法：
  python warm_start_entry.py [--top 600] [--push] [--update-klines]
"""
import glob
import json
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fetch_data import get_market_snapshot

BASE = Path(__file__).resolve().parent.parent
KLINE_DIR = BASE / "data" / "klines"
DATA_DIR = BASE / "data"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
TENCENT_KLINE = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get"  # 2026-09-19：ifzq/web被WAF拦，此域名稳定
WORKERS = 1  # 2026-09-19：腾讯WAF/proxy限流严格，并发必被拦，改纯顺序

# ===== 最优档参数（回测结论固化）=====
LB_LO, LB_HI = 1.3, 2.5            # 量比 1.3-2.5x
CHG5_LO, CHG5_HI = 3.0, 8.0        # 5日涨幅 +3~+8%
POS60_MAX = 0.80                   # 60日位置 <80%
CHG_TODAY_MAX = 9.5                # 当日涨幅上限（防涨停追高）
CHG_TODAY_MIN = -9.5               # 当日跌幅下限（防跌停）
AMP_TODAY_MAX = 7.5                # 信号日振幅上限%
AMT_MAX = 16e8                     # 成交额 ≤16亿
CONSOL_AMP10_MAX = 3.5             # 保守蓄势：前10日平均振幅 ≤3.5%
CONSOL_VOL10_60_MAX = 0.70         # 保守蓄势：前10均量/前60均量 ≤0.70
STOP_PCT = 0.92                    # 止损 8%（回测口径）


def _new_session():
    s = requests.Session()
    s.trust_env = False          # 绕过系统代理（trojan 全局下境外出口被国内源限流）
    s.proxies = {"http": None, "https": None}
    s.headers["User-Agent"] = UA
    s.headers["Referer"] = "https://gu.qq.com/"
    return s


def _to_symbol(code):
    """纯6位数字 → 带交易所前缀（缓存文件名 & 腾讯接口均需前缀）。"""
    code = str(code).strip()
    if code[:2] in ("sh", "sz", "bj"):
        return code
    if code.startswith(("60", "68", "90")):
        return "sh" + code
    if code.startswith(("00", "30")):
        return "sz" + code
    if code.startswith(("92", "8", "4")):
        return "bj" + code
    return "sz" + code


# ---------------- K线增量更新（腾讯源） ----------------

def _fetch_tencent(symbol, days=80):
    """拉腾讯前复权日K，返回正序 DataFrame。字段: 日期,开盘,收盘,最高,最低,成交量(股),成交额(元)

    腾讯 fqkline 的 volume 列单位是【手】，×100 转成【股】。
    实测 sh600363：原始 141605 手 → 14160500 股(1416万股)，amount=成交量×收盘≈2.15亿 吻合。
    注意：东财源的旧缓存行 volume 也是【手】，与腾讯行同单位，勿混。"""
    # 2026-09-19：腾讯WAF对高频请求拦501，单线程+重试+间隔仍可稳定通过
    s = _new_session()
    # 参数格式: symbol,day,start,end,count,qfq —— start/end 留空会 501，必须给足 count
    last_err = None
    for _ in range(3):
        try:
            r = s.get(TENCENT_KLINE, params={"param": f"{symbol},day,2024-01-01,2030-01-01,{days},qfq"}, timeout=15)
            r.raise_for_status()
            j = r.json()
            d = (j.get("data") or {}).get(symbol) or {}
            k = d.get("qfqday") or d.get("day") or []
            if not k:
                last_err = RuntimeError("empty")
                time.sleep(1.0)
                continue
            # 腾讯字段序: date, open, close, high, low, volume(手)
            rows = [[x[0], float(x[1]), float(x[2]), float(x[3]),
                     float(x[4]), float(x[5]) * 100] for x in k]
            df = pd.DataFrame(rows, columns=["日期", "开盘", "收盘", "最高", "最低", "成交量"])
            df["日期"] = pd.to_datetime(df["日期"])
            df = df.sort_values("日期").reset_index(drop=True)
            df["成交额"] = df["成交量"] * df["收盘"]
            return df
        except Exception as e:
            last_err = e
            time.sleep(1.2)
    raise last_err


def _merge_klines(path, symbol, new_df):
    """增量合并：新数据有则更新/追加，原缓存行保留（金额口径优先）。"""
    old = {}
    header = ["symbol", "date", "open", "last", "high", "low", "volume", "amount", "exchange"]
    if path.exists():
        try:
            old_df = pd.read_csv(path)
            for _, r in old_df.iterrows():
                # 统一转 dict（避免 Series 与 dict 混用导致 DataFrame 构造崩溃）
                old[str(r["date"])] = {c: r[c] for c in old_df.columns}
        except Exception:
            old = {}
    exch = {"sh": "SH", "sz": "SZ", "bj": "BJ"}.get(symbol[:2], "")
    added = 0
    for _, r in new_df.iterrows():
        dt = str(pd.Timestamp(r["日期"]).date())
        if dt in old:          # 已有该日：保留原值（腾讯估算额不覆盖东财真实额）
            continue
        old[dt] = {"symbol": symbol, "date": dt,
                   "open": round(float(r["开盘"]), 3), "last": round(float(r["收盘"]), 3),
                   "high": round(float(r["最高"]), 3), "low": round(float(r["最低"]), 3),
                   "volume": int(float(r["成交量"])), "amount": round(float(r["成交额"]), 2),
                   "exchange": exch}
        added += 1
    if added:
        merged = pd.DataFrame([old[k] for k in sorted(old)])
        merged = merged[header]
        merged.to_csv(path, index=False, encoding="utf-8-sig")
    return added


def update_klines(codes, days=80, quiet=True, time_budget=None):
    """增量更新指定代码的K线缓存到最新。返回 (更新数, 失败数, 累计新增行)。

    2026-09-19：腾讯K线源对并发敏感（proxy.finance 同IP并发必被拒），
    必须纯顺序 + 单飞（每只间隔），不能用线程池。
    time_budget：可选秒数预算，用尽即停（供晨报等时限敏感场景定向补数），
    剩余票下次调用继续补——补一半也比全脏好。"""
    t0 = time.time()
    ok = fail = total_added = 0
    for c in codes:
        if time_budget is not None and time.time() - t0 > time_budget:
            if not quiet:
                print(f"[K线更新] 预算{time_budget}s用尽，余{len(codes) - ok - fail}只未补（下次续补）")
            break
        try:
            df = _fetch_tencent(c, days)
        except Exception:
            fail += 1
            continue
        if df is None or len(df) < 70:
            fail += 1
            continue
        path = KLINE_DIR / f"{c}.csv"
        try:
            added = _merge_klines(path, c, df)
            total_added += added
            ok += 1
        except Exception:
            fail += 1
        time.sleep(0.5)  # 限速：proxy源高频必被拒
    if not quiet:
        print(f"[K线更新] 成功{ok} 失败{fail} 新增{total_added}行 耗时{time.time()-t0:.0f}s")
    return ok, fail, total_added


def _last_cache_date(path):
    """读K线CSV末行的date字段（纯文本，比 read_csv 快两个量级）。失败返回空串。"""
    try:
        with open(path, encoding="utf-8-sig", errors="ignore") as f:
            lines = [ln for ln in f.readlines() if ln.strip()]
        if len(lines) < 2:
            return ""
        return lines[-1].split(",")[1][:10]
    except Exception:
        return ""


def _ensure_klines_fresh(codes, quiet=True, time_budget=420):
    """轻量增量补数（2026-09-22 方案A 任务#11）。

    背景：全量 update_klines 1092只≈15分钟，晨报09:25/复盘15:10/笔记本链路
    都不可行；而它们全走 pick_warm_start(update=False) 只读缓存，缓存一旦有
    历史缺口，判定基准整体错位（09-22 晨报北斗误推的根因）。

    方案：以上证指数K线最后日期为「市场最新已收盘交易日」基准——腾讯K线源
    T日行隔日才有，天然=T-1，恰与晨报盘中判定口径一致。本地文本扫描1092个
    CSV末行日期（秒级、零网络），仅对落后票定向补齐：正常日落后0只秒过；
    缓存有缺口/昨日复盘漏跑时自动定向补。复盘链路另有 A3 当日行追加自愈，
    本函数兜底历史缺口。

    返回 (落后票数, 实际补齐票数, 基准日期)。
    指数拉取失败时退化为局部模式：以缓存众数日期为基准，只补局部落后票。"""
    dates = {sym: _last_cache_date(KLINE_DIR / f"{sym}.csv") for sym in codes}
    if not any(dates.values()):
        return 0, 0, None
    expected = None
    try:
        ref = _fetch_tencent("sh000001", days=10)
        if len(ref):
            expected = str(pd.Timestamp(ref["日期"].iloc[-1]).date())
    except Exception:
        pass
    if not expected:
        expected = Counter(d for d in dates.values() if d).most_common(1)[0][0]
    stale = [s for s, d in dates.items() if not d or d < expected]
    if not stale:
        return 0, 0, expected
    if not quiet:
        print(f"[K线增量检测] 基准{expected}，落后{len(stale)}只，定向补齐（预算{time_budget}s）")
    ok, _fail, _added = update_klines(stale, days=80, quiet=True, time_budget=time_budget)
    return len(stale), ok, expected


# ---------------- 指标计算（与 warm_start_backtest._load 口径一致） ----------------

TENCENT_QUOTE = "https://qt.gtimg.cn/q="


def _fetch_quote_batch(symbols, batch=60):
    """腾讯批量实时行情（≤60只/批）。返回 {symbol: (名称, 最新价, 涨跌幅%, 成交额元)}。

    2026-09-22 方案A：追加当日完整 OHLCV 字段，供 pick_warm_start 把当日追加为独立 K 线行。
    兼容旧消费方：前 4 个元素（名称/最新价/涨跌幅%/成交额）不变，只追加字段。
    实时行情字段（腾讯 ~ 分隔）：3最新价 4昨收 5今开 6成交量(手) 31涨跌 32涨跌% 33最高 34最低 37成交额(万)
    """
    out = {}
    s = _new_session()
    for i in range(0, len(symbols), batch):
        chunk = symbols[i:i + batch]
        try:
            r = s.get(TENCENT_QUOTE + ",".join(chunk), timeout=15)
            r.raise_for_status()
            text = r.text
        except Exception:
            continue
        for line in text.split(";"):
            line = line.strip()
            if not line.startswith("v_"):
                continue
            try:
                payload = line.split("=", 1)[1].strip().strip('"')
                f = payload.split("~")
                if len(f) < 40:
                    continue
                sym = line[2:line.index("=")]
                name = f[1]
                price = float(f[3] or 0)
                chg_pct = float(f[32] or 0)
                amt = float(f[37] or 0) * 1e4   # 万元 → 元
                # 方案A：当日完整 OHLCV（开盘/最高/最低/成交量(手×100→股)）
                ohlcv = {
                    "open": float(f[5] or 0),
                    "high": float(f[33] or 0),
                    "low": float(f[34] or 0),
                    "volume": float(f[6] or 0) * 100,   # 手 → 股（与 K 线缓存口径一致）
                    "amount": amt,
                    "prev_close": float(f[4] or 0),
                }
                out[sym] = (name, price, chg_pct, amt, ohlcv)
            except (ValueError, IndexError):
                continue
    return out

def _append_today_row(df, q, today):
    """用腾讯实时行情把当日追加为独立 K 线行（收盘后调用，实时 OHLCV = 当日收盘）。

    替代旧的「覆盖最后一行收盘价」hack（2026-09-22 修复）：
    旧法只覆盖收盘价，成交量/最高/最低仍是旧日数据 → 量比/振幅/位置全用脏值，
    是晨报 2 只 vs 复盘 0 只随机分裂的根因。
    新法：当日完整 OHLCV（量手×100→股、额万元→元）追加为独立行，指标算得准。

    盘中调用方不应追加当日行（成交量只走了一部分，量比严重低估）——
    晨报判定基准应为缓存最后一行（T-1 收盘形态）。"""
    name, price, chg_pct, amt, ohlcv = q
    if price <= 0:
        return df, False
    have = set(df["日期"].dt.strftime("%Y-%m-%d"))
    if today.strftime("%Y-%m-%d") in have:
        return df, False
    row = pd.DataFrame([{
        "日期": today,
        "开盘": ohlcv.get("open") or price,
        "收盘": price,
        "最高": ohlcv.get("high") or price,
        "最低": ohlcv.get("low") or price,
        "成交量": ohlcv.get("volume") or 0,
        "成交额": ohlcv.get("amount") or amt,
    }])
    df = pd.concat([df, row], ignore_index=True)
    return df.sort_values("日期").reset_index(drop=True), True


def _indicators(df):
    d = df.copy()
    d["ma20"] = d["收盘"].rolling(20).mean()
    d["v5"] = d["成交量"].shift(1).rolling(5).mean()
    d["lb"] = d["成交量"] / d["v5"]
    d["chg5"] = (d["收盘"] / d["收盘"].shift(5) - 1) * 100
    d["hi60"] = d["最高"].rolling(60, min_periods=40).max()
    d["lo60"] = d["最低"].rolling(60, min_periods=40).min()
    d["pos60"] = (d["收盘"] - d["lo60"]) / (d["hi60"] - d["lo60"])
    ema12 = d["收盘"].ewm(span=12, adjust=False).mean()
    ema26 = d["收盘"].ewm(span=26, adjust=False).mean()
    d["dif"] = ema12 - ema26
    d["dea"] = d["dif"].ewm(span=9, adjust=False).mean()
    d["prev_close"] = d["收盘"].shift(1)
    d["振幅"] = (d["最高"] - d["最低"]) / d["prev_close"] * 100
    d["amp10_mean"] = d["振幅"].shift(1).rolling(10).mean()   # 前10日平均振幅
    d["v10"] = d["成交量"].shift(1).rolling(10).mean()
    d["v60"] = d["成交量"].shift(1).rolling(60).mean()
    d["vol10_60"] = d["v10"] / d["v60"]                       # 前10均量/前60均量
    # 周线（自然周聚合 → 映射回日线）
    d["week"] = d["日期"].dt.to_period("W").astype(str)
    wk = d.groupby("week").agg(wclose=("收盘", "last"),
                               wvol=("成交量", "sum")).reset_index()
    wk["wma5"] = wk["wclose"].rolling(5).mean()
    wk["wma5_up"] = wk["wma5"] > wk["wma5"].shift(1)
    wk = wk.ffill()
    d = d.merge(wk[["week", "wclose", "wma5", "wma5_up"]], on="week", how="left")
    d["week_up"] = (d["wclose"] > d["wma5"]) & d["wma5_up"]
    return d


# ---------------- 画像判定 ----------------

def _is_pass(row):
    """T 日收盘后是否满足最优档画像。返回 (ok, 原因)。"""
    if pd.isna(row.get("lb")) or pd.isna(row.get("chg5")) or pd.isna(row.get("pos60")):
        return False, "指标不足"
    if not (LB_LO <= row["lb"] <= LB_HI):
        return False, f"量比{row['lb']:.2f}∉[1.3,2.5]"
    if not (CHG5_LO <= row["chg5"] <= CHG5_HI):
        return False, f"5日{row['chg5']:+.1f}%∉[3,8]"
    if row["pos60"] >= POS60_MAX:
        return False, f"位置{row['pos60']:.0%}≥80%"
    if row["收盘"] <= row["ma20"]:
        return False, f"收{row['收盘']:.2f}≤MA20{row['ma20']:.2f}"
    if row["dif"] <= row["dea"]:
        return False, "MACD未多头"
    chg = float(row.get("涨跌幅") or 0)
    if not (CHG_TODAY_MIN <= chg <= CHG_TODAY_MAX):
        return False, f"当日{chg:+.1f}%越界"
    if pd.notna(row.get("振幅")) and row["振幅"] > AMP_TODAY_MAX:
        return False, f"振幅{row['振幅']:.1f}%>7.5%"
    # 周线上扬
    if pd.isna(row.get("week_up")) or not row["week_up"]:
        return False, "周线未上扬"
    # 保守蓄势
    if pd.isna(row.get("amp10_mean")) or row["amp10_mean"] > CONSOL_AMP10_MAX:
        return False, f"前10振幅{row.get('amp10_mean', float('nan')):.1f}%>3.5%"
    if pd.isna(row.get("vol10_60")) or row["vol10_60"] > CONSOL_VOL10_60_MAX:
        return False, f"量能{row.get('vol10_60', float('nan')):.2f}>0.70"
    amt = float(row.get("成交额") or 0)
    if amt <= 0 or amt > AMT_MAX:
        return False, f"成交额{amt/1e8:.1f}亿>16亿"
    return True, "ok"


def pick_warm_start(top=600, update=True, quiet=False):
    """扫描候选池（klines 缓存宇宙，缺快照时用腾讯批量行情），返回今日满足最优档画像的初动票列表。

    2026-09-19 改造：新浪全A分页被反爬(返回HTML)、东财子域不可达 → 不再依赖 get_market_snapshot。
    候选宇宙 = data/klines/*.csv（1092只，覆盖成交额活跃股），
    成交额/名称 = 腾讯批量行情实时拉取，按成交额降序取 top 只。

    返回 list[dict]: 代码/名称/现价/量比/5日涨幅/位置/成交额亿/MA20/振幅/周线/蓄势/止损。
    """
    # 候选宇宙：klines 缓存（带前缀 symbol）
    symbols = sorted(p.stem for p in KLINE_DIR.glob("*.csv"))
    if not symbols:
        print("无K线缓存，无法选股")
        return []
    # ST/退市过滤（从缓存文件名无从判断，靠行情名称过滤）
    codes = symbols

    # 先更新缓存到最新
    if update:
        update_klines(codes, days=80, quiet=quiet)
    else:
        # 2026-09-22 方案A 任务#11：晨报/复盘/笔记本三条 update=False 链路的轻量增量补数。
        # 正常日落后0只秒过；缓存有缺口/昨日复盘漏跑时定向补（7分钟预算兜底，下次续补）。
        _ensure_klines_fresh(codes, quiet=quiet, time_budget=420)

    # 腾讯批量实时行情 → 名称/最新价/涨跌幅/成交额
    quotes = _fetch_quote_batch(codes)
    # 成交额降序取 top（缺行情票按缓存最新成交额兜底）
    amt_map = {}
    for sym in codes:
        path = KLINE_DIR / f"{sym}.csv"
        if path.exists():
            try:
                df = pd.read_csv(path)
                amt_map[sym] = float(df["amount"].iloc[-1]) if "amount" in df.columns and len(df) else 0.0
            except Exception:
                amt_map[sym] = 0.0
    # 2026-09-19 修复：按成交额降序取 top 会把候选卡死在最大票（>16亿）上，与 ≤16亿 条件自相矛盾。
    # 改为全量扫描，成交额由 _is_pass 自然过滤；top 只做输出截断（取成交量最小的活跃票优先不适用，
    # 直接按缓存顺序全扫，最后按位置/量比排序输出）。
    ranked = sorted(codes, key=lambda s: (quotes.get(s, (None, None, None, 0))[3] or amt_map.get(s, 0)), reverse=True)
    if not quiet:
        print(f"[候选池] 全量扫描{len(ranked)}只（缓存{len(symbols)}只，腾讯行情命中{len(quotes)}只）")

    now = datetime.now()
    post_close = (now.hour > 15) or (now.hour == 15 and now.minute >= 5)
    today = pd.Timestamp(now.date())

    picks = []
    for sym in ranked:
        path = KLINE_DIR / f"{sym}.csv"
        if not path.exists():
            continue
        try:
            df = pd.read_csv(path)
        except Exception:
            continue
        if len(df) < 70:
            continue
        # 缓存列: symbol,date,open,last,high,low,volume,amount,exchange → 中文列（与回测口径一致）
        df = df.rename(columns={
            "date": "日期", "open": "开盘", "last": "收盘", "high": "最高",
            "low": "最低", "volume": "成交量", "amount": "成交额",
        })
        df["日期"] = pd.to_datetime(df["日期"])
        df = df.sort_values("日期").reset_index(drop=True)
        q = quotes.get(sym)
        # 收盘后（≥15:05）：实时行情=当日收盘 → 追加当日完整 OHLCV 行，指标用真实完整数据
        # 盘中（晨报 09:25）：不追加当日行（成交量未走完，量比失真），判定基准 = 缓存最后一行（最近收盘形态，V2 信号日口径）
        if q and post_close:
            new_df, added = _append_today_row(df, q, today)
            if added:
                df = new_df
                # 持久化当日行到缓存：close_review 收盘后推进缓存到当日，明早晨报判定昨日收盘
                try:
                    _merge_klines(path, sym, df.tail(1)[["日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额"]].reset_index(drop=True))
                except Exception:
                    pass
        df["涨跌幅"] = df["收盘"].pct_change() * 100
        ind = _indicators(df)
        row = ind.iloc[-1]  # 最新交易日收盘
        if pd.isna(row["收盘"]):
            continue
        ok, why = _is_pass(row)
        if not ok:
            continue
        close = float(row["收盘"])
        stop = round(close * STOP_PCT, 2)          # 回测口径 8% 止损
        amt = float(row.get("成交额") or 0)
        picks.append({
            "代码": sym[2:], "名称": q[0] if q else sym,
            "日期": str(pd.Timestamp(row["日期"]).date()),
            "现价": round(close, 2),
            "量比": round(float(row["lb"]), 2),
            "5日涨幅%": round(float(row["chg5"]), 1),
            "60日位置": round(float(row["pos60"]), 3),
            "成交额亿": round(amt / 1e8, 1),
            "MA20": round(float(row["ma20"]), 2),
            "振幅%": round(float(row.get("振幅") or 0), 1),
            "前10振幅%": round(float(row.get("amp10_mean") or 0), 1),
            "量能比": round(float(row.get("vol10_60") or 0), 2),
            "周线上扬": bool(row.get("week_up")),
            "止损价": stop,
            "买入逻辑": f"低位蓄势后温和放量初动：量比{row['lb']:.2f}x 5日{row['chg5']:+.1f}% "
                       f"位置{row['pos60']:.0%} 站上MA20 MACD多头 周线上扬 前10振幅{row.get('amp10_mean', 0):.1f}%",
        })
    # 排序：位置低优先（越低位启动，空间越大）
    picks.sort(key=lambda x: (x["60日位置"], -x["量比"]))
    return picks


# ---------------- 主入口 ----------------

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=600)
    ap.add_argument("--push", action="store_true", help="推送微信")
    ap.add_argument("--no-update", action="store_true", help="跳过K线增量更新")
    ap.add_argument("--save", action="store_true", help="落盘 CSV")
    args = ap.parse_args()

    t0 = time.time()
    picks = pick_warm_start(top=args.top, update=not args.no_update, quiet=False)
    print(f"\n=== 温和放量初动池 {len(picks)} 只（耗时 {time.time()-t0:.0f}s）===")
    if not picks:
        print("今日无满足最优档画像的初动票（可能整体高位/无放量蓄势）")
        return
    for p in picks:
        print(f"· {p['名称']}({p['代码']}) 现价{p['现价']} 量比{p['量比']:.2f}x "
              f"5日{p['5日涨幅%']:+.1f}% 位置{p['60日位置']:.0%} 额{p['成交额亿']}亿 "
              f"止损{p['止损价']} 前10振幅{p['前10振幅%']:.1f}% 量能{p['量能比']:.2f}")

    if args.save:
        today = datetime.now().strftime("%Y-%m-%d")
        out = DATA_DIR / f"warm_start_pool_{today}.csv"
        pd.DataFrame(picks).to_csv(out, index=False, encoding="utf-8-sig")
        print(f"已落盘 → {out}")

    if args.push:
        try:
            from notify import push_alert
            lines = [f"【温和放量初动池】{len(picks)}只（第三策略）"]
            for p in picks[:8]:
                lines.append(f"· {p['名称']}({p['代码']}) {p['现价']} 量比{p['量比']:.2f}x "
                             f"5日{p['5日涨幅%']:+.1f}% 位置{p['60日位置']:.0%} 额{p['成交额亿']}亿 "
                             f"止损{p['止损价']}")
            lines.append("参数=量比1.3-2.5+5日3-8%+位置<80%+MA20+MACD+周线上扬+保守蓄势+≤16亿；回测盈亏比2.45胜率55.6%。参考，非投资建议。")
            push_alert(f"温和放量初动池 {len(picks)}只", "\n".join(lines))
        except Exception as e:
            print(f"推送失败: {e}")


if __name__ == "__main__":
    main()
