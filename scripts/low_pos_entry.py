# -*- coding: utf-8 -*-
"""低位埋伏启动策略 pick_low_pos_entry —— 观察/研究工具（⚠️ 非唯一买入信号）。

⚠️⚠️⚠️ 策略定位（2026-09-12 晚，基于 60 只真实触发票分层分析）⚠️⚠️⚠️
  两条路径并存、互不矛盾，核心目标 = 提高"所选股票的转化率"（7日内真拉升概率），而非池子大：
  · 标准蓄势路径 = 低估价值票调整后启动（位置中等偏低 + 缩量横盘后放量启动）
  · 近期超卖路径 = 超跌反弹修复（近期曾极度超卖，反弹途中温和放量）

2026-09-23 入池标准温和版收紧（用户拍板；依据 18279 去重事件全历史回放，low_pos_tighten_study2.py）：
  · 扫描端成交额上限 15亿→6亿（双路径同步）：12-15亿档 60日启动率仅 0.2%，大票被触发线
    「启动日成交 4-16亿」结构性卡死；2-4亿档最优 4.6%
  · 扫描端 pos60 上限 0.55→0.40（新增 SCAN_POS60_MAX，双路径同步；触发端 MAX_POS60=0.55 不动）：
    >0.40 档 5日启动仅 0.1%/中位 31 天
  · 效果：60日启动率 3.15%→5.20%，5日启动 0.61%→1.34%，启动中位 16→12 交易日，
    启动后 T5 +3.1%→+3.4%（胜率 51.5%→52.8%），日均入池约 32→13 只
  · 激进版（再加 dist60≤-30：60日启动 10.35%）未采纳，池子日均仅 2 只、胜率 -3pct，暂缓

2026-09-23 晚 两连拍板（A → A+宽），依据 low_pos_factor_sweep.py 地毯式验证 + 18279 事件回放：
  · A：超卖路径扫描端加「蓄势日距高≤-25%」（LOW_POS_ENTRY_OS_SCAN_DIST60_MAX）。
    温和版内距高>-25 的 4386 事件（全部来自超卖反弹段）10日启动仅 1.44%，是最大拖累项；
    标准蓄势路径本就有 MIN_DIST60=-25，故仅补齐超卖路径。10日 2.46%→4.01%
  · A+宽：双路径扫描端再加「MA20乖离≥-5%」（LOW_POS_ENTRY_SCAN_BIAS20_MIN，标准路径
    在 _蓄势通过、超卖路径在 OS_SCAN 段）。企稳确认逻辑：深跌25%+且收复MA20=距发动最近，
    深埋MA20下方=下跌中继。四指标全面改善：10日 4.01%→5.32%（基线2.2倍）、60日 9.55%、
    启动后 T5 +4.29%、胜率 54.2%（反超基线 52.8%）；最近15日实测日均 7.2→6.3只（-12%）
  · A+紧（MA20乖离(-5,3]）10日更高 6.07% 但 T5 降至 3.36%，未采纳
  · 分年稳健：A+宽 10日启动 2024/2025/2026 = 3.77/5.55/5.76% 单调走强

  关键数据结论（隔离"大票"混杂因子后）：
  · 大亏票（T5≤-10%）成交额全部 ≥20.9亿 → 成交额是最强区分因子，16亿以下 0 大亏
  · 大赚票（T5≥+10%）量比 2.04~3.05，3/4 落在 2.0~2.5 → 量比下界 2.5 误杀，应放宽到 2.0
  · 涨幅 7-9% 是陷阱区（0大赚），9-12% 是甜区（27%大赚），>15% 过热
  · 蓄势位置对大赚无区分度（0.05~0.66）→ 位置不卡太死，两类路径都能进
  · 优化参数（量比2-7x + 成交额4-16亿 + 涨幅9-15% + 位置<55%）：11只命中，大赚27%、大亏0%、T5均值+8.4%

2026-09-12 傍晚 7日窗口优化（基于60样本全量分层分析）：
  · 触发涨幅下限：5%→9%（5-7%区间7d最大涨幅均值仅0.9%；7-9%区间0大赚为陷阱区）
  · 触发涨幅上限：新增≤15%（>15%过热，样本少胜率低）
  · 触发量比上限：7x（>7x动能衰竭，大亏）
  · 触发成交额上限：16亿（大亏票全部≥20.9亿，16亿是干净分界）

今日改进（2026-09-12 光电股份研究）：
  - 候选日涨幅过滤：候选日本身涨幅>5% → 跳过（防追已启动票）
  - 入场跳空过滤：入场价相对候选日跳空>3% → 跳过（防追爆发次日）
  - 近期超卖路径 chg_today 补过滤：超卖路径绕过了候选日涨幅检查，已补上
  - 参数放宽：成交额下限 8亿→4亿 / 量比均值上限 1.2x→1.5x / dist60 -12%→-8%
  因此策略 = 两段式：T-1 收盘蓄势候选（埋伏池）→ T 日盘口触发（买入推荐）。

防未来函数：
  - 蓄势扫描只用截至 T-1 的 K 线（不含当日），当日收盘后运行
  - 触发判断用 T 日实时盘口（腾讯行情量比/涨幅），触发即当日买入

三种运行模式：
  python low_pos_entry.py --scan              # T-1 蓄势候选（全A扫描，收盘后跑）
  python low_pos_entry.py --trigger           # T 日触发检查（对最近一次候选池查实时盘口）
  python low_pos_entry.py --backtest --days N # 历史回测：蓄势→次日触发→T+1..T+5收益
"""
import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import (LOW_POS_ENTRY_ENABLED, LOW_POS_ENTRY_MAX_POS60,
                    LOW_POS_ENTRY_SCAN_POS60_MAX,
                    LOW_POS_ENTRY_SCAN_BIAS20_MIN,
                    LOW_POS_ENTRY_MIN_DIST60, LOW_POS_ENTRY_MAX_LB5,
                    LOW_POS_ENTRY_MAX_LB5_MEAN, LOW_POS_ENTRY_CHG5_RANGE,
                    LOW_POS_ENTRY_MAX_CHG_TODAY,
                    LOW_POS_ENTRY_MAX_AMP20, LOW_POS_ENTRY_MIN_AMOUNT,
                    LOW_POS_ENTRY_MAX_AMOUNT, LOW_POS_ENTRY_TRIGGER_LB,
                    LOW_POS_ENTRY_TRIGGER_LB_MAX,
                    LOW_POS_ENTRY_TRIGGER_CHG_MIN, LOW_POS_ENTRY_TRIGGER_CHG_MAX, LOW_POS_ENTRY_TRIGGER_BREAK_MA20,
                    LOW_POS_ENTRY_STOP_LOSS, LOW_POS_ENTRY_TP1, LOW_POS_ENTRY_TP2,
                    LOW_POS_ENTRY_HOLD_DAYS,
                    LOW_POS_ENTRY_TRIGGER_MIN_AMOUNT, LOW_POS_ENTRY_TRIGGER_MAX_AMOUNT,
                    LOW_POS_ENTRY_TEMP_MIN, LOW_POS_ENTRY_BOARD_RESONANCE,
                    LOW_POS_ENTRY_RECENT_OVERSOLD_LOOKBACK,
                    LOW_POS_ENTRY_RECENT_OVERSOLD_POS60_MAX,
                    LOW_POS_ENTRY_RECENT_OVERSOLD_DIST_MAX,
                    LOW_POS_ENTRY_OS_SCAN_AMOUNT_RANGE,
                    LOW_POS_ENTRY_OS_SCAN_POS60_MAX,
                    LOW_POS_ENTRY_OS_SCAN_DIST60_MAX,
                    LOW_POS_ENTRY_OS_SCAN_OS_AGE_MIN,
                    LOW_POS_ENTRY_OS_SCAN_LB_MAX,
                    LOW_POS_ENTRY_OS_SCAN_LB5MEAN_MAX,
                    LOW_POS_ENTRY_OS_SCAN_CHG5_MAX,
                    LOW_POS_ENTRY_OS_TRIGGER_LB, LOW_POS_ENTRY_OS_TRIGGER_LB_MAX,
                    LOW_POS_ENTRY_OS_TRIGGER_CHG_MIN, LOW_POS_ENTRY_OS_TRIGGER_CHG_MAX,
                    LOW_POS_ENTRY_OS_TRIGGER_MIN_AMOUNT, LOW_POS_ENTRY_OS_TRIGGER_MAX_AMOUNT,
                    LOW_POS_ENTRY_OS_TRIGGER_BREAK_MA20,
                    LOW_POS_ENTRY_OS_STOP_LOSS, LOW_POS_ENTRY_OS_TP1, LOW_POS_ENTRY_OS_TP2,
                    LOW_POS_ENTRY_OS_HOLD_DAYS)
from fetch_data import get_market_snapshot, get_realtime_quotes
from stock_screener import ALLOW_CODE_PREFIX, MAX_SAME_INDUSTRY

SINA_KLINE = "https://quotes.sina.cn/cn/api_json_v2.php/CN_MarketDataService.getKLineData"
TENCENT_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
WORKERS = 10
DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _new_session():
    s = requests.Session()
    s.trust_env = False          # 绕过系统代理（trojan 全局模式下境外出口会被限流）
    s.proxies = {"http": None, "https": None}
    s.headers["User-Agent"] = UA
    return s


# ---------------- 数据层（腾讯前复权日K主源 + 新浪备选，纯HTTP并发安全） ----------------

def _load_hist(code, days=180):
    """日K（腾讯 qfq 前复权主源，新浪不复权备选）。
    返回正序 DataFrame[日期,开盘,最高,最低,收盘,成交量,成交额,涨跌幅]。
    腾讯字段序：date,open,close,high,low,volume(手)；成交量×100=股，成交额=股×收盘。
    前复权对 60日位置/dist60 更准（避免除权跳变更改位置语义），优于新浪不复权数据。"""
    sym = ("sh" if code.startswith(("6", "9")) else "sz") + code
    s = _new_session()
    rows = None
    try:
        # 主源：腾讯 fqkline（qfq）
        r = s.get(TENCENT_KLINE, params={"param": f"{sym},day,,,{days},qfq"}, timeout=15)
        if r.status_code == 200:
            j = r.json()
            d = (j.get("data") or {}).get(sym) or {}
            k = d.get("qfqday") or d.get("day") or []
            if k:
                # 腾讯字段序: date, open, close, high, low, volume(手)
                rows = [[x[0], float(x[1]), float(x[2]), float(x[3]),
                         float(x[4]), float(x[5]) * 100] for x in k]  # 手→股
    except Exception:
        pass
    if rows is None:
        try:
            # 备选：新浪（不复权）
            r = s.get(SINA_KLINE, params={"symbol": sym, "scale": "240",
                                          "ma": "no", "datalen": str(days)}, timeout=15)
            if r.status_code == 200:
                arr = r.json()
                if arr:
                    rows = [[x["day"], float(x["open"]), float(x["close"]),
                             float(x["high"]), float(x["low"]), float(x["volume"])] for x in arr]
        except Exception:
            pass
    if not rows:
        return code, None
    # rows 元组顺序统一为 [日期, 开盘, 收盘, 最高, 最低, 成交量]，再重排为标准列序
    df = pd.DataFrame(rows, columns=["日期", "开盘", "收盘", "最高", "最低", "成交量"])
    df = df[["日期", "开盘", "最高", "最低", "收盘", "成交量"]]
    df["日期"] = pd.to_datetime(df["日期"])
    df = df.sort_values("日期").reset_index(drop=True)
    df["成交额"] = df["成交量"] * df["收盘"]          # 股数×收盘价（元）
    df["涨跌幅"] = df["收盘"].pct_change() * 100
    df = df.dropna(subset=["收盘"])
    if len(df) < 70:
        return code, None
    return code, df


def _indicators(df):
    """对完整K线计算指标，返回按日期升序的 DataFrame（末尾行=T-1 收盘状态）。"""
    d = df.copy()
    for w in (5, 10, 20):
        d[f"MA{w}"] = d["收盘"].rolling(w).mean()
    # 量比：当日量 / 前5日均量（不含当日）
    d["v5"] = d["成交量"].shift(1).rolling(5).mean()
    d["lb"] = d["成交量"] / d["v5"]
    d["lb5mean"] = d["lb"].rolling(5).mean()
    # 60日位置 / 距60日高点
    d["hi60"] = d["最高"].rolling(60, min_periods=40).max()
    d["lo60"] = d["最低"].rolling(60, min_periods=40).min()
    d["pos60"] = (d["收盘"] - d["lo60"]) / (d["hi60"] - d["lo60"])
    d["dist60"] = (d["收盘"] / d["hi60"] - 1) * 100
    # 20日振幅
    d["amp20"] = (d["收盘"].rolling(20).max() - d["收盘"].rolling(20).min()) / d["收盘"].rolling(20).mean() * 100
    # 5日涨幅
    d["chg5"] = (d["收盘"] / d["收盘"].shift(5) - 1) * 100
    return d


# ---------------- 近期超卖路径（2026-09-12 新增：兼容光电股份模式） ----------------
# 光电股份路径：股票在 T-n 日（n≤25）曾出现极度超卖（pos60<0.5x 或 dist60<-30%），
# 之后反弹筑底；当反弹途中量比爆发触发时，pos60 可能已升至 0.55x~0.65x，
# 超出标准蓄势上限（0.55x），但只要"近期超卖"存在，仍值得参与。
# 返回 (是否近期超卖, 超卖日, 超卖时pos60, 超卖时dist60)

def _recent_oversold(ind_df, cur_idx):
    """检查 ind_df[0:cur_idx) 区间内是否有超卖信号（pos60<0.5 or dist60<-30%）。
    返回 (是否近期超卖, 首次超卖日, 超卖时pos60, 超卖时dist60, 首次超卖idx)。"""
    lookback = LOW_POS_ENTRY_RECENT_OVERSOLD_LOOKBACK
    start = max(0, cur_idx - lookback)
    window = ind_df.iloc[start:cur_idx]
    for idx, row in window.iterrows():
        p60 = row.get("pos60")
        d60 = row.get("dist60")
        if p60 is not None and p60 < 0.5:
            return True, str(pd.Timestamp(row["日期"]).date()), p60, d60, idx
        if d60 is not None and d60 < LOW_POS_ENTRY_RECENT_OVERSOLD_DIST_MAX:
            return True, str(pd.Timestamp(row["日期"]).date()), p60, d60, idx
    return False, None, None, None, None


# ---------------- 蓄势候选（T-1 扫描，全A） ----------------

def _蓄势通过(row):
    """T-1 收盘状态的八项蓄势判据（v3 共性研究 + 09-23 A+宽/V6' 演进）
    pos60 / 距高 / MA20乖离 / 量比 / 5日涨幅 / 候选日涨幅 / 振幅 / 成交额"""
    if pd.isna(row.get("pos60")) or pd.isna(row.get("dist60")):
        return False, "指标不足"
    if row["pos60"] >= LOW_POS_ENTRY_SCAN_POS60_MAX:
        return False, f"位置{row['pos60']:.0%}≥{LOW_POS_ENTRY_SCAN_POS60_MAX:.0%}"
    if row["dist60"] > LOW_POS_ENTRY_MIN_DIST60:
        return False, f"距高点{row['dist60']:.0f}%>-{abs(LOW_POS_ENTRY_MIN_DIST60):.0f}%"
    # 2026-09-23 晚 A+宽：MA20乖离 ≥-5%（用户拍板）。企稳确认逻辑：深跌25%+且股价
    #   收复到 MA20 附近 = 止跌企稳，距发动最近（10日启动5.32%/胜率54.2%，双超基线）；
    #   深埋 MA20 下方 = 下跌中继（10日仅2.32%）。双路径扫描端统一，与回测口径一致。
    _ma20 = row.get("MA20")
    if _ma20 is not None and not pd.isna(_ma20):
        _bias20 = (float(row["收盘"]) / float(_ma20) - 1) * 100
        if _bias20 < LOW_POS_ENTRY_SCAN_BIAS20_MIN:
            return False, f"MA20乖离{_bias20:.1f}%<-5%（未企稳，下跌中继）"
    lb = row.get("lb")
    lbm = row.get("lb5mean")
    if pd.isna(lb) or pd.isna(lbm):
        return False, "量比不足"
    if lb > LOW_POS_ENTRY_MAX_LB5 or lbm > LOW_POS_ENTRY_MAX_LB5_MEAN:
        return False, f"量比{lb:.2f}x/5日均{lbm:.2f}x未缩量"
    c5 = row.get("chg5", 0)
    lo5, hi5 = LOW_POS_ENTRY_CHG5_RANGE
    if not (lo5 <= c5 <= hi5):
        return False, f"5日{c5:+.1f}%非横盘"
    # 候选日本身涨幅（防追已启动票：8/26光电股份候选日已涨6.81%，8/27追高亏损）
    chg_today = row.get("涨跌幅", 0) or 0
    if chg_today > LOW_POS_ENTRY_MAX_CHG_TODAY:
        return False, f"候选日涨幅{chg_today:+.1f}%>{LOW_POS_ENTRY_MAX_CHG_TODAY}%已启动"
    if row.get("amp20", 0) >= LOW_POS_ENTRY_MAX_AMP20:
        return False, f"振幅{row['amp20']:.0f}%≥{LOW_POS_ENTRY_MAX_AMP20}%"
    amt = row.get("成交额", 0)
    if not (LOW_POS_ENTRY_MIN_AMOUNT <= amt <= LOW_POS_ENTRY_MAX_AMOUNT):
        return False, f"成交额{amt/1e8:.1f}亿出界"
    return True, "ok"


def _layered_candidates(snap, top):
    """2026-09-23 V6 分层候选池（修复 top 截断漏抓小票，康强电子根因）。

    旧逻辑：快照按成交额降序 head(top) —— 晨报/复盘调用 top=600，收盘后 600 名门槛
    约 6-8 亿，判据主战场 2-6 亿小票被结构性排除（低位首板票中位额 4.1 亿、康强
    3.39 亿被挡）。新规则：
      ① 主战场 2-6 亿段全保（超限按额升序截断——最小票是旧逻辑最先牺牲的）；
      ② <2 亿段按额降序补一半剩余名额（盘前时点实时额不可靠，留通道）；
      ③ ≥6 亿段按额降序补足剩余（判据会淘汰，纯占位兜底）。
    总量 cap = max(top, 1200)，K线请求成本较 top=600 最多翻倍、较 1500 不增。
    """
    cap = max(top, 1200)
    inband = snap[snap["_amt"].between(LOW_POS_ENTRY_MIN_AMOUNT, LOW_POS_ENTRY_MAX_AMOUNT - 1e-9)]
    below_all = snap[snap["_amt"] < LOW_POS_ENTRY_MIN_AMOUNT].sort_values("_amt", ascending=False)
    big_all = snap[snap["_amt"] >= LOW_POS_ENTRY_MAX_AMOUNT].sort_values("_amt", ascending=False)
    if len(inband) > cap:
        inband = inband.sort_values("_amt").head(cap)
    rest = cap - len(inband)
    below = below_all.head(rest // 2)
    big = big_all.head(rest - len(below))
    if len(big) < rest - len(below):  # big 段不满额（如盘前 below 独大），名额回落给 below
        below = below_all.head(rest - len(big))
    out = pd.concat([inband, below, big], ignore_index=True)
    print(f"[候选池] 分层：2-6亿 {len(inband)} / <2亿 {len(below)} / ≥6亿 {len(big)}，共 {len(out)}（cap={cap}）")
    return out


def pick_low_pos_entry(as_of=None, top=1500, quiet=False):
    """主策略函数：T-1 收盘扫描全A，返回蓄势候选列表（含指标）。

    as_of: 截止日期（str %Y-%m-%d）；None=最新交易日。
    返回 list[dict]，字段含 代码/名称/现价/60日位置/dist60/量比/5日量比/5日涨幅/振幅/成交额/MA20/买点/止损。
    """
    if not LOW_POS_ENTRY_ENABLED:
        print("低位埋伏策略未启用 (LOW_POS_ENTRY_ENABLED=False)")
        return []
    snap = get_market_snapshot()
    if snap is None or snap.empty:
        print("快照获取失败，退出")
        return []
    snap = snap.copy()
    snap["代码"] = snap["代码"].astype(str).str.replace(r"\.0$", "", regex=True)
    snap = snap[snap["代码"].str.match(r"^(?:" + "|".join(ALLOW_CODE_PREFIX) + ")", na=False)]
    snap = snap[~snap["名称"].astype(str).str.contains("ST|退", na=False)]
    snap["_amt"] = pd.to_numeric(snap.get("成交额"), errors="coerce").fillna(0)
    snap = _layered_candidates(snap, top)  # 2026-09-23 V6 分层候选池，替代 head(top) 截断
    codes = snap["代码"].tolist()
    names = dict(zip(snap["代码"], snap["名称"].astype(str)))

    ts = pd.Timestamp(as_of) if as_of else None
    hists, t0 = {}, time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(_load_hist, c): c for c in codes}
        for i, f in enumerate(as_completed(futs), 1):
            c, h = f.result()
            if h is not None:
                hists[c] = h
            if not quiet and i % 300 == 0:
                print(f"  K线进度 {i}/{len(codes)} ok={len(hists)} {time.time()-t0:.0f}s", flush=True)
    if not quiet:
        print(f"[蓄势] K线就绪 {len(hists)} 只，用时 {time.time()-t0:.0f}s", flush=True)

    picks = []
    for code in codes:
        h = hists.get(code)
        if h is None:
            continue
        if ts is not None:
            h = h[h["日期"] <= ts]
        if len(h) < 70:
            continue
        last = h.iloc[-1]
        if pd.isna(last["收盘"]):
            continue
        ind = _indicators(h)
        row = ind.iloc[-1]  # T-1 收盘行
        ok, why = _蓄势通过(row)
        # 如果标准蓄势失败，检查是否属于"近期超卖"路径
        recent_os = False
        os_date, os_pos60, os_dist60, os_first_idx = None, None, None, None
        if not ok:
            # 2026-09-21 对齐：窗口改为含 T-1 行本身（原 len(ind)-1 漏掉蓄势日，
            # 与 backtest() 的 _recent_oversold(ind, i+1) 口径不一致 → 回测统计失真）
            recent_os, os_date, os_pos60, os_dist60, os_first_idx = _recent_oversold(ind, len(ind))
        if not ok and not recent_os:
            continue
        # 候选日自身涨幅过滤（双路径统一，防追已启动票）
        chg_today = float(row.get("涨跌幅") or 0)
        if chg_today > LOW_POS_ENTRY_MAX_CHG_TODAY:
            continue  # 候选日已大涨，不追
        # 标准蓄势通过：扫描端 pos60 上限 0.40（09-23 温和版）；近期超卖路径：扫描端同样 0.40（OS_SCAN）
        pos60_max = (LOW_POS_ENTRY_SCAN_POS60_MAX if ok else LOW_POS_ENTRY_RECENT_OVERSOLD_POS60_MAX)
        pos60_val = float(row["pos60"])
        if pos60_val >= pos60_max:
            continue  # 双路径都不满足上限
        # ---- 2026-09-21 超卖路径扫描段收紧（防池子膨胀，研究定案见 config）----
        #   只作用于超卖路径候选入池；标准蓄势路径不受影响。
        if recent_os:
            amt_lo, amt_hi = LOW_POS_ENTRY_OS_SCAN_AMOUNT_RANGE
            amt = float(row.get("成交额") or 0)
            if not (amt_lo <= amt <= amt_hi):
                continue  # 蓄势日成交额出界（巨头票纯占池）
            if pos60_val >= LOW_POS_ENTRY_OS_SCAN_POS60_MAX:
                continue  # 蓄势日位置过高（0.55-0.65 区间触发样本=0）
            # 2026-09-23 A方案提速：蓄势日距60日高点须 ≤-25%（用户拍板）。
            #   依据：温和版内距高>-25 的 4386 事件全部来自超卖反弹段，10日启动仅1.44%
            #   拖累整体（2.46%）；加此后 10日 4.01%、日均 16.2→6.5只、胜率-3pct。
            #   标准蓄势路径已有 LOW_POS_ENTRY_MIN_DIST60=-25，此处仅补齐超卖路径。
            if float(row.get("dist60") or 0) > LOW_POS_ENTRY_OS_SCAN_DIST60_MAX:
                continue  # 反弹已收复超25%跌幅，距发动远（10日启动率仅1.44%）
            # 2026-09-23 A+宽：超卖路径同样要求 MA20乖离 ≥-5%（与 _蓄势通过 口径一致）
            _os_ma20 = row.get("MA20")
            if _os_ma20 is not None and not pd.isna(_os_ma20):
                _os_bias = (float(row["收盘"]) / float(_os_ma20) - 1) * 100
                if _os_bias < LOW_POS_ENTRY_SCAN_BIAS20_MIN:
                    continue  # 深埋MA20下方=下跌中继（10日启动2.32%且T5弱）
            os_age = len(ind) - 1 - os_first_idx  # 距首次超卖交易日数
            if os_first_idx is not None and os_age < LOW_POS_ENTRY_OS_SCAN_OS_AGE_MIN:
                continue  # 新进超卖（≤9日）反弹前夜，样本胜率33%均值-1.96%
            lb_val = float(row.get("lb") or 0)
            if lb_val > LOW_POS_ENTRY_OS_SCAN_LB_MAX:
                continue  # 蓄势日量比过大（反弹前夜已爆量=不追）
            lbm_val = float(row.get("lb5mean") or 0)
            if lbm_val > LOW_POS_ENTRY_OS_SCAN_LB5MEAN_MAX:
                continue  # 5日量比均值过大（大亏50%）
            c5_val = float(row.get("chg5") or 0)
            if c5_val > LOW_POS_ENTRY_OS_SCAN_CHG5_MAX:
                continue  # 已反弹段（5日>0），样本胜率48%均值+2.21%；底部段更优
        close = float(row["收盘"])
        ma20 = float(row["MA20"]) if not pd.isna(row["MA20"]) else close
        buy_lo = round(close * 0.99, 2)
        buy_hi = round(close * 1.03, 2)
        if recent_os:
            path_label = "近期超卖"
            feat = (f"超卖修复{row['pos60']:.0%}/{os_date} pos60 {os_pos60:.0%}"
                    if os_pos60 else f"超卖修复{row['pos60']:.0%}/{os_date}")
        else:
            path_label = "标准蓄势"
            feat = f"缩量{row['lb']:.2f}x/低位{row['pos60']:.0%}/距高{row['dist60']:.0f}%/横盘5日"
        # 止盈止损按路径分流：近期超卖用固化 OS_* 参数，标准蓄势用 TRIGGER 同族参数
        if recent_os:
            _stop_loss, _tp1, _tp2, _hold_days = (LOW_POS_ENTRY_OS_STOP_LOSS,
                                      LOW_POS_ENTRY_OS_TP1, LOW_POS_ENTRY_OS_TP2,
                                      LOW_POS_ENTRY_OS_HOLD_DAYS)
        else:
            _stop_loss, _tp1, _tp2, _hold_days = (LOW_POS_ENTRY_STOP_LOSS,
                                      LOW_POS_ENTRY_TP1, LOW_POS_ENTRY_TP2,
                                      LOW_POS_ENTRY_HOLD_DAYS)
        # 2026-09-16 修复：止损锚定从「候选日收盘价」改为「买区上沿」。
        #   旧逻辑止损=收盘×(1-8%)，而买区下限=收盘×0.99，区间内买入后
        #   价格仅回落 ~1% 就触及止损（通鼎：买区21.03-22.89、止损20.89，
        #   21.03买入→-0.7%即止损），低位横盘票单日振幅±4-6%，静态止损
        #   完全给不出洗盘空间。新逻辑止损=买区上沿×(1-8%)，在区间内任意
        #   位置买入，止损距离都≥8%，给足正常震荡空间。
        _stop_price = round(buy_hi * (1 + _stop_loss), 2)
        # 启动信号触发线（观察池用）：放量突破 买区上沿 → 触发介入提示
        #   仅用于观察池跟踪提示（close_review 连续追踪段），不参与 T-1 选股。
        _trigger_line = round(buy_hi, 2)
        picks.append({
            "代码": code, "名称": names.get(code, code),
            "日期": str(pd.Timestamp(row["日期"]).date()),
            "现价": round(close, 2),
            "60日位置": round(float(row["pos60"]), 3),
            "dist60%": round(float(row["dist60"]), 1),
            "量比": round(float(row["lb"]), 2),
            "5日量比均值": round(float(row["lb5mean"]), 2),
            "5日涨幅%": round(float(row.get("chg5") or 0), 1),
            "20日振幅%": round(float(row.get("amp20") or 0), 1),
            "成交额亿": round(float(row.get("成交额") or 0) / 1e8, 2),
            "MA20": round(ma20, 2),
            "买点区间": f"{buy_lo}-{buy_hi}",
            "止损价": _stop_price,
            "触发线": _trigger_line,
            "止盈1": round(close * (1 + _tp1), 2),
            "止盈2": round(close * (1 + _tp2), 2),
            "操作计划": f"触发后买入，T+{_hold_days}止盈（均价法）",
            "蓄势路径": path_label,        # "标准蓄势" 或 "近期超卖"
            "超卖日": os_date,             # 若近期超卖，记录超卖发生日
            "超卖时位置": round(float(os_pos60), 3) if os_pos60 else None,
            "蓄势特征": feat,
        })
    # 行业去重
    if MAX_SAME_INDUSTRY > 0:
        from fetch_data import _industry_by_name
        ind_count, dedup = {}, []
        for r in picks:
            ind = _industry_by_name(r["名称"]) or ""
            r["行业"] = ind or "未知"
            if ind and ind_count.get(ind, 0) >= MAX_SAME_INDUSTRY:
                continue
            if ind:
                ind_count[ind] = ind_count.get(ind, 0) + 1
            dedup.append(r)
        picks = dedup
    # 排序：位置低优先 + 量比低优先（极致缩量优先）
    picks.sort(key=lambda x: (x["60日位置"], x["量比"]))
    return picks


# ---------------- 观察池去重（2026-09-21 修低位池膨胀） ----------------

def filter_active_pool(picks):
    """剔除已在观察池在册（最新状态=观察中）的票。

    背景（2026-09-21）：09-21 单日入池 22 只，其中宁德/兆易/东财/烽火等 4 只
    早在 09-15~09-17 已在池，仍每日重刷「今日候选」——此前 close_review 只
    排除了短线/趋势跟踪池（track_rows），从未排除低位池自己在册的票。
    规则：最新状态=「观察中」→ 剔除（老票只进连续追踪段）；
          「已失效」（T+5 超时/止损移出）→ 允许重新入池（新信号）。
    """
    import csv as _csv
    path = DATA_DIR / "watch_history.csv"
    if not path.exists() or not picks:
        return picks
    latest = {}
    try:
        with path.open(encoding="utf-8") as f:
            for r in _csv.DictReader(f):
                c = (r.get("代码") or "").strip()
                if c:
                    latest[c] = (r.get("状态") or "").strip()
    except Exception:
        return picks
    active = {c for c, st in latest.items() if st == "观察中"}
    if not active:
        return picks
    return [p for p in picks if str(p.get("代码", "")) not in active]


# ---------------- T 日触发（实时盘口） ----------------

def check_launch_trigger(cands, quiet=False):
    """对蓄势候选池查腾讯实时盘口，量比≥LB 且 涨幅≥CHG（且现价>MA20）→ 买入推荐。

    cands: pick_low_pos_entry 返回的候选列表。返回触发列表（含实时价/触发原因）。"""
    if not cands:
        return []
    codes = [c["代码"] for c in cands]
    q = get_realtime_quotes(codes)
    hits = []
    for c in cands:
        r = q.get(c["代码"])
        if r is None or r.get("现价", 0) <= 0:
            continue
        price = r["现价"]; chg = r.get("涨跌幅", 0); lb = r.get("量比", 0)
        amt = r.get("成交额", 0)
        ma20 = c["MA20"]
        # 触发参数按路径分流：近期超卖用固化 OS_*，标准蓄势用 TRIGGER 同族参数
        if c.get("蓄势路径") == "近期超卖":
            _lb_lo = LOW_POS_ENTRY_OS_TRIGGER_LB
            _chg_lo = LOW_POS_ENTRY_OS_TRIGGER_CHG_MIN
            _chg_hi = LOW_POS_ENTRY_OS_TRIGGER_CHG_MAX
            _amt_lo = LOW_POS_ENTRY_OS_TRIGGER_MIN_AMOUNT
            _amt_hi = LOW_POS_ENTRY_OS_TRIGGER_MAX_AMOUNT
            _break_ma20 = LOW_POS_ENTRY_OS_TRIGGER_BREAK_MA20
            _hold_days = LOW_POS_ENTRY_OS_HOLD_DAYS
        else:
            _lb_lo = LOW_POS_ENTRY_TRIGGER_LB
            _chg_lo = LOW_POS_ENTRY_TRIGGER_CHG_MIN
            _chg_hi = LOW_POS_ENTRY_TRIGGER_CHG_MAX
            _amt_lo = LOW_POS_ENTRY_TRIGGER_MIN_AMOUNT
            _amt_hi = LOW_POS_ENTRY_TRIGGER_MAX_AMOUNT
            _break_ma20 = LOW_POS_ENTRY_TRIGGER_BREAK_MA20
            _hold_days = LOW_POS_ENTRY_HOLD_DAYS
        reasons = []
        if lb >= _lb_lo:
            reasons.append(f"量比{lb:.1f}x")
        if _chg_lo <= chg <= _chg_hi:
            reasons.append(f"涨幅{chg:+.1f}%✓")
        elif chg > 0:
            reasons.append(f"涨幅{chg:+.1f}%(不足)")
        if amt >= _amt_lo:
            reasons.append(f"成交额{amt/1e8:.1f}亿")
        if _break_ma20 and price > ma20:
            reasons.append("破MA20")
        if not reasons:
            continue
        # 需同时满足：量比 + 涨幅(甜区) + 成交额三重确认
        if not (lb >= _lb_lo
                and _chg_lo <= chg <= _chg_hi
                and _amt_lo <= amt <= _amt_hi):
            continue
        hits.append({
            "代码": c["代码"], "名称": c["名称"],
            "现价": price, "涨跌幅%": round(chg, 2), "量比": round(lb, 2),
            "成交额亿": round(amt / 1e8, 2),
            "MA20": ma20, "触发": "+".join(reasons),
            "买点区间": c["买点区间"], "止损价": c["止损价"],
            "止盈1": c["止盈1"], "止盈2": c["止盈2"],
            "操作计划": f"T+{_hold_days} 快速止盈（均价法）",
            "蓄势日": c["日期"], "蓄势位置": c["60日位置"],
            "买入逻辑": f"{c['名称']}低位蓄势({c['日期']}入选)后放量启动：{'+'.join(reasons)}，"
                        f"距60日高{c['dist60%']:.0f}%，买点{c['买点区间']}，止损{c['止损价']}",
        })
    return hits


# ---------------- 历史回测（蓄势→次日触发→T+N 收益） ----------------

def _next_trading_days(ind, anchor_date, n=5):
    """anchor_date 之后的 n 个交易日（含 anchor 当日之后的），返回 (触发日行, 后续行列表)"""
    dates = ind["日期"]
    mask = dates > pd.Timestamp(anchor_date)
    after = ind[mask]
    if after.empty:
        return None, []
    return after.iloc[0], after.iloc[1:].head(n).to_dict("records")


def backtest(top=600, days=120, min_trigger=1, codes=None, names_map=None):
    """对最近 days 天内的每个蓄势日，检查次日是否触发，统计 T+N 收益。
    codes: 显式代码列表（绕开快照限流）；None 时走全市场快照 top 只。
    names_map: {code: 名称}（显式 codes 时补名称，供行业共振判断）"""
    if codes is None:
        snap = get_market_snapshot()
        if snap is None or snap.empty:
            print("快照失败，可改用 --codes 显式传股票池")
            return None
        snap = snap.copy()
        snap["代码"] = snap["代码"].astype(str).str.replace(r"\.0$", "", regex=True)
        snap = snap[snap["代码"].str.match(r"^(?:" + "|".join(ALLOW_CODE_PREFIX) + ")", na=False)]
        snap = snap[~snap["名称"].astype(str).str.contains("ST|退", na=False)]
        snap["_amt"] = pd.to_numeric(snap.get("成交额"), errors="coerce").fillna(0)
        snap = _layered_candidates(snap, top)  # 2026-09-23 V6 与生产口径一致，修复回测缺小票
        codes = snap["代码"].tolist()
        names = dict(zip(snap["代码"], snap["名称"].astype(str)))
    else:
        names = dict(names_map) if names_map else {c: c for c in codes}
    print(f"[回测] 拉取 {len(codes)} 只K线（{days}天）...", flush=True)
    hists = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(_load_hist, c, days): c for c in codes}
        for i, f in enumerate(as_completed(futs), 1):
            c, h = f.result()
            if h is not None:
                hists[c] = h
    print(f"[回测] K线就绪 {len(hists)} 只", flush=True)

    # 触发参数按路径分流（标准蓄势用 TRIGGER 同族，近期超卖用固化 OS_*；两路径彻底解耦）
    PARAMS = {
        "标准蓄势": {
            "lb_lo": LOW_POS_ENTRY_TRIGGER_LB,
            "lb_hi": LOW_POS_ENTRY_TRIGGER_LB_MAX,
            "chg_lo": LOW_POS_ENTRY_TRIGGER_CHG_MIN,
            "chg_hi": LOW_POS_ENTRY_TRIGGER_CHG_MAX,
            "amt_lo": LOW_POS_ENTRY_TRIGGER_MIN_AMOUNT,
            "amt_hi": LOW_POS_ENTRY_TRIGGER_MAX_AMOUNT,
            "break_ma20": LOW_POS_ENTRY_TRIGGER_BREAK_MA20,
            "pos60_max": LOW_POS_ENTRY_MAX_POS60,
            "stop_loss": LOW_POS_ENTRY_STOP_LOSS,
        },
        "近期超卖": {
            "lb_lo": LOW_POS_ENTRY_OS_TRIGGER_LB,
            "lb_hi": LOW_POS_ENTRY_OS_TRIGGER_LB_MAX,
            "chg_lo": LOW_POS_ENTRY_OS_TRIGGER_CHG_MIN,
            "chg_hi": LOW_POS_ENTRY_OS_TRIGGER_CHG_MAX,
            "amt_lo": LOW_POS_ENTRY_OS_TRIGGER_MIN_AMOUNT,
            "amt_hi": LOW_POS_ENTRY_OS_TRIGGER_MAX_AMOUNT,
            "break_ma20": LOW_POS_ENTRY_OS_TRIGGER_BREAK_MA20,
            "pos60_max": LOW_POS_ENTRY_RECENT_OVERSOLD_POS60_MAX,
            "stop_loss": LOW_POS_ENTRY_OS_STOP_LOSS,
        },
    }

    rows, n_trigger = [], 0
    # 第一步：全量扫描所有蓄势日→触发候选（含触发日行业，用于共振判断）
    from fetch_data import _industry_by_name
    all_triggers = {}   # {(code, 触发日): row_data}
    for code, h in hists.items():
        ind = _indicators(h)
        for i in range(70, len(ind) - 1):
            row = ind.iloc[i]
            ok, _ = _蓄势通过(row)
            # 检查是否属于近期超卖路径（标准蓄势失败才检查；标准蓄势通过时也计算用于回测分类）
            recent_os, os_date, os_pos60, os_dist60, os_first_idx = _recent_oversold(ind, i + 1)
            if not ok and not recent_os:
                continue
            # 候选日自身涨幅过滤（双路径统一，防追已启动票）
            chg_today = float(row.get("涨跌幅") or 0)
            if chg_today > LOW_POS_ENTRY_MAX_CHG_TODAY:
                continue  # 候选日已大涨，不追
            # ---- 2026-09-21 超卖路径扫描段收紧（与 pick_low_pos_entry 口径一致）----
            if recent_os:
                amt_lo, amt_hi = LOW_POS_ENTRY_OS_SCAN_AMOUNT_RANGE
                amt_i = float(row.get("成交额") or 0)
                if not (amt_lo <= amt_i <= amt_hi):
                    continue
                if float(row["pos60"]) >= LOW_POS_ENTRY_OS_SCAN_POS60_MAX:
                    continue
                # 2026-09-23 A方案提速：与 pick_low_pos_entry 超卖段口径一致（距高≤-25%）
                if float(row.get("dist60") or 0) > LOW_POS_ENTRY_OS_SCAN_DIST60_MAX:
                    continue
                # 2026-09-23 A+宽：与 pick 超卖段口径一致（MA20乖离≥-5%）
                _bt_ma20 = row.get("MA20")
                if _bt_ma20 is not None and not pd.isna(_bt_ma20):
                    if (float(row["收盘"]) / float(_bt_ma20) - 1) * 100 < LOW_POS_ENTRY_SCAN_BIAS20_MIN:
                        continue
                if os_first_idx is not None and (i - os_first_idx) < LOW_POS_ENTRY_OS_SCAN_OS_AGE_MIN:
                    continue
                if float(row.get("lb") or 0) > LOW_POS_ENTRY_OS_SCAN_LB_MAX:
                    continue
                if float(row.get("lb5mean") or 0) > LOW_POS_ENTRY_OS_SCAN_LB5MEAN_MAX:
                    continue
                if float(row.get("chg5") or 0) > LOW_POS_ENTRY_OS_SCAN_CHG5_MAX:
                    continue
            # 触发日（次日）的各项指标
            nxt = ind.iloc[i + 1]
            # 注：2026-09-12 深夜移除「入场跳空 entry_gap>3% 过滤」。
            # 该过滤用 (触发日收盘 - 蓄势日收盘)/蓄势日收盘 计算，而蓄势日=触发日前一交易日，
            # 故 entry_gap 恒等于触发日涨幅；触发条件又要求涨幅 9-15%，二者互斥 → 恒 0 触发。
            # 实时 check_launch_trigger 本无此过滤，此处删除使回测与实时触发口径一致。
            lb = float(nxt["lb"])
            chg = float(nxt["涨跌幅"])
            nxt_amt = float(nxt.get("成交额", 0))
            ma20 = float(row["MA20"])
            nxt_pos60 = float(nxt.get("pos60", 0))
            # 双路径判断：
            #   标准蓄势路径：ok=True → pos60_max=0.55, lb_threshold=2.0
            #   近期超卖路径：ok=False, recent_os=True → pos60_max=0.65, lb_threshold=2.0
            if ok:
                path = "标准蓄势"
            else:
                path = "近期超卖"
            p = PARAMS[path]
            triggered = (lb >= p["lb_lo"]
                         and lb <= p["lb_hi"]          # 过滤量比过大票（>7x=动能衰竭，大亏）
                         and p["chg_lo"] <= chg <= p["chg_hi"]  # 涨幅甜区
                         and p["amt_lo"] <= nxt_amt <= p["amt_hi"]
                         and (not p["break_ma20"] or float(nxt["收盘"]) > ma20)
                         and nxt_pos60 < p["pos60_max"])
            if not triggered:
                continue
            trigger_date = str(pd.Timestamp(nxt["日期"]).date())
            industry = _industry_by_name(names.get(code, code)) or "未知"
            entry = float(nxt["收盘"])
            all_triggers[(code, trigger_date)] = {
                "code": code, "name": names.get(code, code),
                "trigger_date": trigger_date, "industry": industry,
                "lb": round(lb, 2), "chg": round(chg, 2),
                "nxt_amt": nxt_amt, "entry": entry,
                "ind": ind, "i": i,
                "path": path,                           # "标准蓄势" 或 "近期超卖"
                "os_date": os_date,                     # 超卖发生日
                "os_pos60": os_pos60,                   # 超卖时 pos60
                "trigger_pos60": nxt_pos60,             # 触发日 pos60
            }

    # 第二步：计算每个触发日的市场温度（用 hists 里所有股票当日上涨比例）
    date_up_ratio = {}  # {触发日: 上涨比例 0-100}
    for code, h in hists.items():
        ind = _indicators(h)
        for i in range(70, len(ind) - 1):
            nxt = ind.iloc[i + 1]
            td = str(pd.Timestamp(nxt["日期"]).date())
            if td not in date_up_ratio:
                date_up_ratio[td] = {"up": 0, "total": 0}
            date_up_ratio[td]["total"] += 1
            if float(nxt["涨跌幅"]) > 0:
                date_up_ratio[td]["up"] += 1
    for td, v in date_up_ratio.items():
        date_up_ratio[td] = v["up"] / v["total"] * 100 if v["total"] > 0 else 0

    # 第三步：计算每个触发日的同行业触发数量（板块共振）
    date_industry_count = {}  # {触发日: {行业: count}}
    for (code, td), info in all_triggers.items():
        ind_name = info["industry"]
        if td not in date_industry_count:
            date_industry_count[td] = {}
        date_industry_count[td][ind_name] = date_industry_count[td].get(ind_name, 0) + 1

    # 第四步：逐条过滤并计算收益
    for (code, trigger_date), info in all_triggers.items():
        # 过滤①：大盘温度
        temp = date_up_ratio.get(trigger_date, 0)
        if LOW_POS_ENTRY_TEMP_MIN > 0 and temp < LOW_POS_ENTRY_TEMP_MIN:
            continue  # 跳过温度不达标的触发
        # 过滤②：板块共振（放宽：有1只就保留但打标记；≥2只共振更强）
        board_count = date_industry_count.get(trigger_date, {}).get(info["industry"], 0)
        resonance = "强共振" if board_count >= 2 else "单兵"
        # 收益计算
        ind, i = info["ind"], info["i"]
        futs_rows = ind.iloc[i + 2:i + 7]
        entry = info["entry"]
        r1 = float(futs_rows.iloc[0]["收盘"]) / entry - 1 if len(futs_rows) >= 1 else None
        r3 = float(futs_rows.iloc[2]["收盘"]) / entry - 1 if len(futs_rows) >= 3 else None
        r5 = float(futs_rows.iloc[4]["收盘"]) / entry - 1 if len(futs_rows) >= 5 else None
        sl_pct = PARAMS[info["path"]]["stop_loss"]
        hit_sl = bool((futs_rows["收盘"] < entry * (1 + sl_pct)).any())
        rows.append({
            "代码": info["code"], "名称": info["name"],
            "蓄势日": str(pd.Timestamp(ind.iloc[i]["日期"]).date()),
            "触发日": trigger_date,
            "触发量比": info["lb"], "触发涨幅%": info["chg"],
            "触发成交额亿": round(info["nxt_amt"] / 1e8, 1),
            "蓄势位置": round(float(ind.iloc[i]["pos60"]), 2),
            "触发位置": round(float(info["trigger_pos60"]), 2),
            "蓄势路径": info["path"],
            "超卖日": info["os_date"],
            "行业": info["industry"],
            "板块共振": resonance, "同板块触发数": board_count,
            "大盘温度": round(temp, 1),
            "入场价": round(entry, 2),
            "T1收益%": round(r1 * 100, 1) if r1 is not None else None,
            "T3收益%": round(r3 * 100, 1) if r3 is not None else None,
            "T5收益%": round(r5 * 100, 1) if r5 is not None else None,
            "8日破止损": hit_sl,
        })
    if not rows:
        print("无触发样本")
        return None
    df = pd.DataFrame(rows)
    print(f"\n=== 回测结果：{len(df)} 次触发（{len(df['代码'].unique())} 只票）===")
    for w, col in (("T1", "T1收益%"), ("T3", "T3收益%"), ("T5", "T5收益%")):
        s = df[col].dropna()
        if s.empty:
            continue
        win = (s > 0).mean() * 100
        print(f"{w}：样本{s.size} 均值{s.mean():+.1f}% 中位{s.median():+.1f}% 胜率{win:.0f}% "
              f"最差{s.min():+.1f}% 最好{s.max():+.1f}%")
    sl = df["8日破止损"].mean() * 100
    print(f"8%止损触发比例：{sl:.0f}%")
    # 分路径统计
    if "蓄势路径" in df.columns:
        print("\n--- 按蓄势路径分层 ---")
        for path, grp in df.groupby("蓄势路径"):
            s3 = grp["T3收益%"].dropna()
            if s3.empty:
                continue
            win3 = (s3 > 0).mean() * 100
            print(f"  {path}：{len(grp)}笔 T3均值{s3.mean():+.1f}% 胜率{win3:.0f}%")
    out = DATA_DIR / f"低位埋伏回测_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    df.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"明细 → {out}")
    return df


# ---------------- 主入口 ----------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true", help="T-1 蓄势候选（收盘后）")
    ap.add_argument("--trigger", action="store_true", help="T 日触发检查（实时盘口）")
    ap.add_argument("--backtest", action="store_true", help="历史回测")
    ap.add_argument("--top", type=int, default=1500, help="扫描候选数（默认1500）")
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--as-of", default=None, help="截止日期 %Y-%m-%d（scan/回测用）")
    ap.add_argument("--codes", default=None, help="回测用代码列表，逗号分隔（绕开快照限流）")
    ap.add_argument("--no-push", action="store_true", help="不推送微信")
    args = ap.parse_args()

    if args.backtest:
        codes = None
        if args.codes:
            codes = [c.strip() for c in args.codes.split(",") if c.strip()]
        backtest(top=args.top, days=args.days, codes=codes)
        return

    if args.scan:
        picks = filter_active_pool(pick_low_pos_entry(as_of=args.as_of, top=args.top))
        if not picks:
            print("今日无蓄势候选")
            return
        today = datetime.now().strftime("%Y-%m-%d")
        fname = DATA_DIR / f"低位埋伏候选_{today}.csv"
        df = pd.DataFrame(picks)
        df.to_csv(fname, index=False, encoding="utf-8-sig")
        print(f"\n=== 蓄势候选 {len(picks)} 只 → {fname} ===")
        show = df[["代码", "名称", "现价", "60日位置", "dist60%", "量比", "5日量比均值",
                   "5日涨幅%", "成交额亿", "蓄势路径", "超卖日", "买点区间", "止损价", "操作计划"]]
        with pd.option_context("display.max_rows", None, "display.width", 220):
            print(show.to_string(index=False))
        if not args.no_push:
            try:
                from notify import push_alert
                lines = ["【低位埋伏蓄势候选】"]
                for r in picks[:10]:
                    lines.append(f"· {r['名称']}({r['代码']}) 现价{r['现价']} "
                                 f"位置{r['60日位置']:.0%} 距高{r['dist60%']:.0f}% 量比{r['量比']:.2f}x "
                                 f"买点{r['买点区间']} 止损{r['止损价']}")
                lines.append("触发条件：量比≥2.0 + 涨幅9-15% + 成交额4-16亿 → 买入，T+3/T+5止盈（标准蓄势T+3、近期超卖T+5）。仅供参考，非投资建议。")
                push_alert("低位埋伏蓄势池", "\n".join(lines))
            except Exception as e:
                print(f"推送失败: {e}")
        return

    if args.trigger:
        fname = sorted(DATA_DIR.glob("低位埋伏候选_*.csv"))[-1] if DATA_DIR.exists() else None
        if fname is None:
            print("无候选池，先跑 --scan")
            return
        cands = pd.read_csv(fname).to_dict("records")
        hits = check_launch_trigger(cands)
        today = datetime.now().strftime("%Y-%m-%d")
        if hits:
            f2 = DATA_DIR / f"低位埋伏触发_{today}.csv"
            pd.DataFrame(hits).to_csv(f2, index=False, encoding="utf-8-sig")
            print(f"\n=== 触发买入推荐 {len(hits)} 只 → {f2} ===")
            for h in hits:
                print(f"· {h['名称']}({h['代码']}) {h['现价']} {h['触发']} "
                      f"买点{h['买点区间']} 止损{h['止损价']} 止盈{h['止盈1']}/{h['止盈2']}")
            if not args.no_push:
                try:
                    from notify import push_alert
                    lines = [f"【低位埋伏启动】{h['名称']}({h['代码']}) 现价{h['现价']} "
                             f"{h['触发']} 买点{h['买点区间']} 止损{h['止损价']}" for h in hits[:10]]
                    lines.append("参考，非投资建议。")
                    push_alert(f"低位埋伏触发 {len(hits)}只", "\n".join(lines))
                except Exception as e:
                    print(f"推送失败: {e}")
        else:
            print(f"候选池 {len(cands)} 只，今日暂未触发（量比<2.0 或 涨幅未达9-15%）")
        return

    ap.print_help()


if __name__ == "__main__":
    main()
