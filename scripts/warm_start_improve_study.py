# -*- coding: utf-8 -*-
"""温和放量初动池 · 改进点实验（2026-09-20）。

基于 warm_start_backtest.py 落盘的明细（data/warm_start_backtest.csv，≤16亿档），
用本地 klines（腾讯前复权）重放逐日持仓路径，验证 4 个改进点（均保留 -8% 硬止损）：

  V1 大盘环境过滤：信号日上证收 > MA20 且 MA20 上行才开仓（指数数据腾讯接口拉取，缓存 data/index_klines/）
  V2 破MA5离场：持仓 T+2 收盘起收盘 < MA5 → 次日开盘离场，否则 T+5 收盘
  V3 移动止盈：T+2 收盘起记录最高浮盈，peak ≥ +5% 后收盘浮盈 ≤ peak-2% → 次日开盘离场，否则 T+5 收盘
  V4 = V1 × V2 组合
  B0 基线 = T+5 收盘出场（与 warm_start_backtest.py 一致口径）

入场口径与基线一致：信号日 T 收盘确认 → T+1 开盘买入；明细中 r5 为空的（一字板买不进）已剔除。
防未来函数：所有出场判定用 T+k 收盘 → T+k+1 开盘成交；大盘状态用 T 日收盘数据。

用法：python warm_start_improve_study.py
"""
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

BASE = Path(__file__).resolve().parent.parent
KLINE_DIR = BASE / "data" / "klines"
IDX_DIR = BASE / "data" / "index_klines"
DETAIL = BASE / "data" / "warm_start_backtest.csv"

TENCENT_KLINE = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

HARD_STOP = 0.92          # -8% 硬止损（收盘判定，按 0.92×入场 出场）
HOLD_MAX = 5              # T+5 收盘出场


def _sess():
    s = requests.Session()
    s.trust_env = False   # 国内源绕代理（用户环境约定）
    s.headers.update({"User-Agent": UA})
    return s


def fetch_index(symbol="sh000001", days=700):
    """拉上证指数日线（腾讯 fqkline），缓存到 data/index_klines/。"""
    IDX_DIR.mkdir(parents=True, exist_ok=True)
    f = IDX_DIR / f"{symbol}.csv"
    if f.exists():
        df = pd.read_csv(f)
        if len(df) > 300:
            return df
    s = _sess()
    url = f"{TENCENT_KLINE}?param={symbol},day,,,{days},"
    r = s.get(url, timeout=15)
    data = r.json()
    node = data["data"][symbol]
    rows = node.get("qfqday") or node.get("day")
    df = pd.DataFrame(rows, columns=["date", "open", "close", "high", "low", "volume"])
    df = df[["date", "open", "close", "high", "low"]].astype(
        {"open": float, "close": float, "high": float, "low": float})
    df.to_csv(f, index=False)
    print(f"指数缓存 {f}（{len(df)} 行）")
    return df


def index_regime(df_idx):
    """信号日大盘状态：收>MA20 且 MA20 上行 → 强市。返回 date→bool 字典。"""
    df = df_idx.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma20_up"] = df["ma20"] > df["ma20"].shift(5)
    df["bull"] = (df["close"] > df["ma20"]) & df["ma20_up"]
    return dict(zip(df["date"].dt.strftime("%Y-%m-%d"), df["bull"]))


_stock_cache = {}


def load_stock(code):
    if code in _stock_cache:
        return _stock_cache[code]
    f = KLINE_DIR / f"{code}.csv"
    if not f.exists():
        _stock_cache[code] = None
        return None
    df = pd.read_csv(f, encoding="utf-8-sig")
    if len(df) < 30:
        _stock_cache[code] = None
        return None
    df["dt"] = pd.to_datetime(df["date"])
    df = df.sort_values("dt").reset_index(drop=True)
    df["ma5"] = df["last"].rolling(5).mean()
    _stock_cache[code] = df
    return df


def replay(code, sig_date, regime, variant):
    """重放一笔信号，返回 (收益%, 持仓交易日数, 出场原因)。出场原因: stop/ma5/trail/maturity/na"""
    df = load_stock(code)
    if df is None:
        return None
    idx = df.index[df["date"] == sig_date]
    if len(idx) == 0:
        return None
    i = idx[0]
    if i + 1 >= len(df):          # 信号日是最后一根，无次日
        return None
    entry_row = df.iloc[i + 1]
    entry = float(entry_row["open"])
    # 一字板买不进（与基线口径一致）
    if entry_row["open"] == entry_row["high"] == entry_row["low"] == entry_row["close"]:
        return None
    if entry <= 0:
        return None

    peak = 0.0
    for k in range(1, HOLD_MAX + 1):
        j = i + 1 + k
        if j >= len(df):
            # 数据尽头，按最后可得收盘出场
            j = len(df) - 1
            r = (float(df.iloc[j]["last"]) / entry - 1) * 100
            return r, k, "maturity"
        row = df.iloc[j]
        c, o = float(row["last"]), float(row["open"])
        # 1) 硬止损：前一日收盘 < 0.92×entry → 当日按止损价成交（保守）
        prev_c = float(df.iloc[j - 1]["last"])
        if prev_c < HARD_STOP * entry:
            return -8.0, k, "stop"
        # 2) V3 移动止盈（收盘判定 → 次日开盘出场）
        if "V3" in variant and k >= 2:
            ret = (c / entry - 1) * 100
            peak = max(peak, ret)
            if peak >= 5.0 and ret <= peak - 2.0:
                if j + 1 < len(df):
                    r = (float(df.iloc[j + 1]["open"]) / entry - 1) * 100
                    return r, k + 1, "trail"
                return ret, k, "trail"
        # 3) V2/V5 破MA5（收盘判定 → 次日开盘出场）
        if ("V2" in variant or variant == "V5") and k >= 2:
            ma5 = float(row["ma5"]) if not np.isnan(row["ma5"]) else None
            if ma5 and c < ma5:
                if j + 1 < len(df):
                    r = (float(df.iloc[j + 1]["open"]) / entry - 1) * 100
                    return r, k + 1, "ma5"
                return (c / entry - 1) * 100, k, "ma5"
    # T+5 收盘出场
    r = (float(df.iloc[i + 1 + HOLD_MAX]["last"]) / entry - 1) * 100
    return r, HOLD_MAX, "maturity"


def summarize(name, rows):
    if not rows:
        print(f"{name:<28} 无样本")
        return
    r = pd.Series([x[0] for x in rows])
    days = pd.Series([x[1] for x in rows])
    wins, losses = r[r > 0], r[r < 0]
    pl = round(wins.mean() / abs(losses.mean()), 2) if len(wins) and len(losses) else None
    print(f"{name:<28} n={len(r):<5} 胜率 {(r>0).mean()*100:>5.1f}%  均值 {r.mean():>+6.2f}%  "
          f"盈亏比 {pl}  最大亏 {r.min():>6.1f}%  均持仓 {days.mean():.1f}天")


def main():
    t0 = time.time()
    df = pd.read_csv(DETAIL)
    sub = df[(df["成交额档"] == "≤16亿") & df["r5"].notna()].copy()
    print(f"样本：≤16亿档可执行信号 {len(sub)} 条（来自 warm_start_backtest.csv）\n")

    # V1 大盘状态
    idx_df = fetch_index("sh000001")
    regime = index_regime(idx_df)
    sub["bull"] = sub["date"].map(regime)
    n_bull = int(sub["bull"].fillna(False).sum())
    print(f"大盘强市信号 {n_bull} 条 / 弱市 {len(sub) - n_bull} 条\n")

    variants = ["B0", "V1", "V2", "V3", "V4", "V5"]
    results = {v: [] for v in variants}
    bull_only = []   # 强市·基线出场
    bear_only = []   # 弱市·基线出场
    miss = 0
    for _, row in sub.iterrows():
        code, sig_date = row["code"], row["date"]
        bull = bool(row["bull"]) if pd.notna(row["bull"]) else False
        for v in variants:
            if v in ("V1", "V4", "V5") and not bull:
                continue
            out = replay(code, sig_date, regime, v)
            if out is None:
                miss += 1
                continue
            results[v].append(out)
            if v == "B0":
                (bull_only if bull else bear_only).append(out)

    print("=" * 86)
    print(f"{'变体':<30}{'样本':<8}{'指标':>40}")
    print("-" * 86)
    labels = {
        "B0": "基线 T+5收盘",
        "V1": "V1 大盘过滤(只做强市)",
        "V2": "V2 +破MA5离场",
        "V3": "V3 +移动止盈(5%回撤2%)",
        "V4": "V4 大盘过滤+破MA5",
        "V5": "V5 大盘过滤+破MA5+移动止盈",
    }
    for v in variants:
        summarize(labels[v], results[v])
    summarize("拆解·强市基线出场", bull_only)
    summarize("拆解·弱市基线出场", bear_only)
    print("=" * 86)
    print(f"缺失样本 {miss} 次（个股无K线/日期缺失）｜ 耗时 {time.time()-t0:.0f}s")
    print("\n判读指引：胜率/均值/盈亏比三项同时不劣于基线、且最大亏收窄的变体才有采纳价值；")
    print("V1 样本减少=空仓期放弃的机会成本，需对照弱市基线收益判断规避是否值得。")


if __name__ == "__main__":
    main()
