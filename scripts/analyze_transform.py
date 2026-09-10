# -*- coding: utf-8 -*-
"""
低位池 → 右侧趋势池 转化研究（结果反推前兆）
==========================================
核心问题：低位启动池的票，哪些最终真正进入右侧趋势池（=成功启动）？
          这些"成功者"在进入趋势池之前（低位蓄势阶段）有什么共同形态/信号，
          可以用来反哺低位池的筛选，提高低位池选到"真启动"的概率。

方法（严格无未来函数）：
  1. 对每个交易日 T，用趋势 v3 规则找"当日进入右侧趋势池"的票（趋势信号日 = T）
  2. 对每只趋势票，回看它在 T 之前 D 个交易日的低位形态特征（只用到 T-1 及之前数据）
  3. 对照组：同一天"本可入选低位池但从未在后续 N 日内进入趋势池"的低位票
  4. 逐特征对比 成功组 vs 对照组，输出区分度（AUC/均值差/命中率），找出前兆因子

输出：
  data/转化研究_前兆因子.csv   —— 各特征区分度总表
  data/转化研究_成功组.csv     —— 成功转化票及其启动前形态
"""
import sys, os, json, time, argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import config as C
from fetch_data import _industry_by_name

SINA_KLINE = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"


# ============ 1. 数据加载（复用回测引擎口径） ============
def _load_hist(code):
    sym = ("sh" if code.startswith(("6", "9")) else "sz") + code
    try:
        r = requests.get(SINA_KLINE, params={"symbol": sym, "scale": "240", "ma": "no",
                                             "datalen": "300"}, timeout=20)
        if r.status_code != 200:
            return code, None
        arr = json.loads(r.text)
        if not arr:
            return code, None
        df = pd.DataFrame(arr).rename(columns={"day": "date", "open": "open", "high": "high",
                                               "low": "low", "close": "close", "volume": "vol"})
        df["date"] = pd.to_datetime(df["date"])
        for c in ["open", "high", "low", "close", "vol"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.sort_values("date").reset_index(drop=True)
        df["amount"] = df["vol"] * df["close"]
        if len(df) < 120:
            return code, None
        return code, df
    except Exception:
        return code, None


def _features(df):
    """向量化预计算全部特征，与 backtest_engine._features 一致"""
    f = df.copy()
    c = f["close"]
    for n in (5, 10, 20, 60):
        f[f"ma{n}"] = c.rolling(n).mean()
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    f["dif"] = ema12 - ema26
    f["dea"] = f["dif"].ewm(span=9, adjust=False).mean()
    f["hist"] = (f["dif"] - f["dea"]) * 2
    delta = c.diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    f["rsi14"] = 100 - 100 / (1 + gain / loss.replace(0, float("nan")))
    f["chg"] = c.pct_change()
    f["vol_ma20"] = f["vol"].rolling(20).mean()
    f["vol_ratio"] = f["vol"] / f["vol_ma20"]
    hi60 = f["high"].rolling(60).max()
    lo60 = f["low"].rolling(60).min()
    f["pos60"] = (c - lo60) / (hi60 - lo60).replace(0, float("nan"))
    c20max = c.rolling(20).max()
    c20min = c.rolling(20).min()
    f["amp20"] = (c20max - c20min) / c.rolling(20).mean().replace(0, float("nan"))
    f["std20"] = c.rolling(20).std() / c.rolling(20).mean()
    f["close_max20"] = c.rolling(20).max()
    f["close_max60"] = c.rolling(60).max()
    f["amount"] = f["amount"].fillna(0)
    # 额外：用于低位前兆分析
    c20 = c.rolling(20).mean()
    f["ma5_ma20"] = f["ma5"] / f["ma20"] - 1          # 5日线相对20日线乖离
    f["close_ma20"] = c / f["ma20"] - 1                # 收盘相对MA20乖离
    f["ma20_slope"] = f["ma20"].pct_change(3)          # MA20 三日前斜率
    f["vol_ratio5"] = f["vol"] / f["vol"].rolling(5).mean()  # 相对5日均量
    # 近5日累计涨幅、量能扩张
    f["chg5"] = c.pct_change(5)
    f["up_days5"] = (f["chg"] > 0).rolling(5).sum()     # 近5日上涨天数
    f["vol_expand5"] = (f["vol"] / f["vol"].rolling(5).mean()).rolling(5).mean()
    return f


# ============ 2. 趋势信号判定（v3 生产规则，只用 T-1 数据） ============
def is_trend_signal(f, idx, cfg=None):
    """在 f 的第 idx 行判定是否满足右侧趋势 v3 条件"""
    cfg = cfg or dict(pos_hi=C.TREND_MIN_60D_POS, pos_low=C.TREND_MIN_60D_POS_LOW)
    r = f.iloc[idx]
    try:
        if not (r["amount"] > C.TREND_MIN_AMOUNT):
            return False
        if not (r["pos60"] < cfg["pos_hi"]):
            return False
        if cfg.get("pos_low") and not (r["pos60"] >= cfg["pos_low"]):
            return False
        if not (r["amp20"] < C.TREND_MAX_20D_AMPLITUDE):
            return False
        if not (r["std20"] < C.TREND_MAX_20D_STD_RATIO):
            return False
        if not (r["vol_ratio"] >= C.TREND_BREAKOUT_VOL_RATIO):
            return False
        lo, hi = C.TREND_BREAKOUT_CHG_RANGE
        if not (lo <= r["chg"] <= hi):
            return False
        if not ((r["close"] > r["ma10"] > r["ma20"]) or (r["ma5"] > r["ma10"] > r["ma20"])):
            return False
        if not (r["dif"] > r["dea"]):
            return False
        if not (40 <= r["rsi14"] <= 70):
            return False
        if not (r["close"] > r["ma20"]):
            return False
        return True
    except Exception:
        return False


# 低位池条件（backfill_watch.screen_on_date 口径）
MIN_AMOUNT = 1.5e8
LOW_POS_MAX = 0.40

def is_low_pool(f, idx):
    """在第 idx 行判定是否满足低位启动观察池条件（信号数>=2）"""
    r = f.iloc[idx]
    try:
        if not (r["amount"] > MIN_AMOUNT):
            return False
        pct = r["chg"] * 100
        if not (-4 <= pct <= 4):
            return False
        if not (r["pos60"] < LOW_POS_MAX):
            return False
        signals = 0
        if r["close"] > r["ma20"]:
            signals += 1
        if r["dif"] > r["dea"]:
            signals += 1
        v_ratio = r["vol_ratio5"]
        if v_ratio > 1.2:
            signals += 1
        chg5 = r["chg5"] * 100
        if 2 <= chg5 <= 12:
            signals += 1
        return signals >= 2
    except Exception:
        return False


# ============ 3. 前兆特征提取（进入趋势池前 D 日的低位形态） ============
# 在趋势信号日 T 回看 T-K ~ T-1 区间，提取低位蓄势特征
LOOKBACK = 20   # 回看窗口（交易日）

def extract_precursor(f, t_idx):
    """t_idx 是趋势信号日。返回 T-K~T-1 区间（不含T）的低位形态特征"""
    k = LOOKBACK
    s = max(0, t_idx - k)
    win = f.iloc[s:t_idx]                     # 不含趋势信号日
    if len(win) < 10:
        return None
    last = win.iloc[-1]
    feat = {}
    # --- 位置 ---
    feat["pos60_prev"] = last["pos60"]
    feat["pos60_max20"] = win["pos60"].max()          # 蓄势期最高位置
    feat["pos60_mean20"] = win["pos60"].mean()
    # --- 均线结构 ---
    feat["ma5_ma20_prev"] = last["ma5_ma20"]
    feat["close_ma20_prev"] = last["close_ma20"]
    feat["ma20_slope_prev"] = last["ma20_slope"]
    feat["close_above_ma20_days"] = (win["close"] > win["ma20"]).sum()  # 站上MA20天数
    feat["ma5_above_ma20_days"] = (win["ma5"] > win["ma20"]).sum()
    # --- 量能 ---
    feat["vol_ratio_prev"] = last["vol_ratio"]
    feat["vol_expand5_prev"] = last["vol_expand5"]
    feat["vol_ratio_max20"] = win["vol_ratio"].max()   # 蓄势期最大量比
    feat["amount_mean"] = win["amount"].mean()
    # --- 动量 ---
    feat["chg5_prev"] = last["chg5"] * 100
    feat["up_days5_prev"] = last["up_days5"]
    feat["rsi14_prev"] = last["rsi14"]
    feat["chg20"] = (last["close"] / win["close"].iloc[0] - 1) * 100   # 20日累计
    # --- MACD ---
    feat["dif_dea_prev"] = (last["dif"] - last["dea"]) * 100           # MACD柱
    feat["macd_pos_days20"] = (win["dif"] > win["dea"]).sum()          # 蓄势期MACD多头天数
    # --- 波动率 ---
    feat["amp20_prev"] = last["amp20"]
    feat["std20_prev"] = last["std20"]
    # --- 相对20日高点距离 ---
    feat["dist_close_max20"] = (last["close"] / last["close_max20"] - 1) * 100
    feat["dist_close_max60"] = (last["close"] / last["close_max60"] - 1) * 100
    # --- 行业热度（近10日该行业进入低位池/趋势池次数）---
    return feat


# ============ 4. 主流程 ============
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-03-01")
    ap.add_argument("--end", default="2026-09-08")
    ap.add_argument("--top", type=int, default=1200)
    ap.add_argument("--workers", type=int, default=10)
    args = ap.parse_args()

    # 4.1 拉全A快照取候选池
    from fetch_data import get_market_snapshot
    print("[候选] 拉取全A快照 ...", flush=True)
    snap = get_market_snapshot()
    snap = snap.copy()
    snap["代码"] = snap["代码"].astype(str).str.replace(r"\.0$", "", regex=True)
    snap = snap[snap["代码"].str.match(r"^(?:" + "|".join(C.ALLOW_CODE_PREFIX) + ")", na=False)]
    snap = snap[~snap["名称"].astype(str).str.contains("ST|退", na=False)]
    snap["_amt"] = pd.to_numeric(snap.get("成交额"), errors="coerce").fillna(0)
    snap = snap.sort_values("_amt", ascending=False).head(args.top)
    codes = snap["代码"].tolist()
    names = dict(zip(snap["代码"], snap["名称"].astype(str)))
    print(f"[候选] {len(codes)} 只，并发拉历史K线 ...", flush=True)

    hists, t0 = {}, time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_load_hist, c): c for c in codes}
        for i, f in enumerate(as_completed(futs), 1):
            c, h = f.result()
            if h is not None:
                hists[c] = h
            if i % 300 == 0:
                print(f"  进度 {i}/{len(codes)} ok={len(hists)} {time.time()-t0:.0f}s", flush=True)
    print(f"[候选] 历史K线就绪 {len(hists)} 只，用时 {time.time()-t0:.0f}s\n", flush=True)

    feats = {c: _features(h) for c, h in hists.items()}

    # 4.2 交易日序列
    all_days = sorted(hists[codes[0]]["date"].tolist()) if codes else []
    # 用多个样本取共同交易日
    day_sets = [set(h["date"]) for c, h in list(hists.items())[:50]]
    common_days = sorted(set.intersection(*day_sets))
    ts_start, ts_end = pd.Timestamp(args.start), pd.Timestamp(args.end)
    days = [d for d in common_days if ts_start <= d <= ts_end]
    print(f"[日期] {len(days)} 个交易日: {days[0].date()} ~ {days[-1].date()}\n", flush=True)

    # 4.3 逐日扫描：找趋势信号 + 对照低位票
    #     trend_records: {date: [codes]}  进入趋势池
    #     low_pool_days: {code: [dates]}  进入低位池
    #     需先两遍：第一遍收集所有趋势信号日；第二遍对每只趋势票回看低位形态
    trend_signals = {}    # code -> [(t_idx, date, close, pos60)]
    low_pool_by_code = {} # code -> set(date)
    low_pool_by_day = {}  # date -> [codes]

    print("[扫描] 逐日判定趋势信号 & 低位池候选 ...", flush=True)
    for d in days:
        low_pool_by_day[d] = []
        for code in codes:
            f = feats.get(code)
            if f is None:
                continue
            sub = f[f["date"] <= d]
            if len(sub) < 70 or sub["date"].iloc[-1] != d:
                continue
            idx = len(sub) - 1
            # 趋势信号
            if is_trend_signal(f, idx):
                trend_signals.setdefault(code, []).append(idx)
            # 低位池
            if is_low_pool(f, idx):
                low_pool_by_day[d].append(code)
                low_pool_by_code.setdefault(code, set()).add(d)
    print(f"[扫描] 趋势信号：{sum(len(v) for v in trend_signals.values())} 次 / "
          f"{len(trend_signals)} 只；低位池：{sum(len(v) for v in low_pool_by_day.values())} 次\n", flush=True)

    # 4.4 构建成功组：趋势票，且其低位阶段形态
    success_rows = []
    for code, idxs in trend_signals.items():
        f = feats[code]
        for t_idx in idxs:
            prec = extract_precursor(f, t_idx)
            if prec is None:
                continue
            row = {"代码": code, "名称": names.get(code, code),
                   "趋势信号日": str(f["date"].iloc[t_idx].date()),
                   "收盘": float(f["close"].iloc[t_idx]),
                   "pos60_signal": float(f["pos60"].iloc[t_idx]),
                   "行业": _industry_by_name(names.get(code, "")) or "",
                   }
            row.update(prec)
            success_rows.append(row)

    # 4.5 对照组：同一信号日进入低位池、但后续 20 个交易日内从未进入趋势池的票
    #     为控制"同时期同市场环境"，从每个趋势信号日的低位池样本里抽对照
    control_rows = []
    trend_by_day = {}
    for r in success_rows:
        trend_by_day.setdefault(r["趋势信号日"], []).append(r)
    for d, rows in trend_by_day.items():
        low_codes = low_pool_by_day.get(pd.Timestamp(d), [])
        # 该日趋势票的代码集合
        trend_codes = {r["代码"] for r in rows}
        # 过滤：后续20交易日从未进趋势池
        f_start = pd.Timestamp(d)
        future = [dd for dd in days if dd > f_start]
        future_20 = future[:20]
        for code in low_codes:
            if code in trend_codes:
                continue
            f = feats.get(code)
            if f is None:
                continue
            # 该票在趋势信号日也处于低位池状态
            sub = f[f["date"] <= f_start]
            if len(sub) < 70 or sub["date"].iloc[-1] != f_start:
                continue
            # 检查后续20日是否进趋势池
            entered = False
            for dd in future_20:
                subf = f[f["date"] <= dd]
                if len(subf) >= 70 and subf["date"].iloc[-1] == dd:
                    if is_trend_signal(f, len(subf) - 1):
                        entered = True
                        break
            if entered:
                continue   # 实际上后来也进了，归到成功组逻辑（但不在同日 trend 里，跳过）
            prec = extract_precursor(f, len(sub) - 1)
            if prec is None:
                continue
            row = {"代码": code, "名称": names.get(code, code),
                   "趋势信号日": d, "收盘": float(f["close"].iloc[len(sub)-1]),
                   "pos60_signal": float(f["pos60"].iloc[len(sub)-1]),
                   "行业": _industry_by_name(names.get(code, "")) or ""}
            row.update(prec)
            control_rows.append(row)

    print(f"[样本] 成功组 {len(success_rows)} 条 / 对照组 {len(control_rows)} 条\n", flush=True)

    # 4.6 对比统计
    suc = pd.DataFrame(success_rows)
    ctl = pd.DataFrame(control_rows)
    suc.to_csv(ROOT / "data/转化研究_成功组.csv", index=False, encoding="utf-8-sig")
    if not ctl.empty:
        ctl.to_csv(ROOT / "data/转化研究_对照组.csv", index=False, encoding="utf-8-sig")

    if suc.empty or ctl.empty:
        print("样本不足，无法对比。")
        return

    feat_cols = [c for c in suc.columns if c not in
                 ("代码", "名称", "趋势信号日", "收盘", "pos60_signal", "行业")]
    print(f"\n{'='*70}")
    print("  前兆因子对比：成功组(低位→趋势) vs 对照组(低位→未趋势)")
    print(f"{'='*70}")
    print(f"{'因子':<18}{'成功组均值':>10}{'对照均值':>10}{'差值':>9}{'方向':>4}  解读")
    print("-" * 100)
    summary = []
    for col in feat_cols:
        a = suc[col].dropna()
        b = ctl[col].dropna()
        if len(a) < 5 or len(b) < 5:
            continue
        ma, mb = a.mean(), b.mean()
        diff = ma - mb
        direction = "高" if diff > 0 else "低"
        summary.append((col, ma, mb, diff, len(a), len(b)))
    # 按 |差值| 排序输出
    summary.sort(key=lambda x: abs(x[3]), reverse=True)
    for col, ma, mb, diff, na, nb in summary:
        print(f"{col:<18}{ma:>10.3f}{mb:>10.3f}{diff:>+9.3f}{'↑' if diff>0 else '↓':>4}  n={na}/{nb}")
    print("-" * 100)

    # 4.7 保存总表
    out = pd.DataFrame(summary, columns=["因子", "成功组均值", "对照组均值", "差值", "成功n", "对照n"])
    out.to_csv(ROOT / "data/转化研究_前兆因子.csv", index=False, encoding="utf-8-sig")
    print(f"\n[保存] 前兆因子 → data/转化研究_前兆因子.csv")
    print(f"[保存] 成功组明细 → data/转化研究_成功组.csv")


if __name__ == "__main__":
    main()
