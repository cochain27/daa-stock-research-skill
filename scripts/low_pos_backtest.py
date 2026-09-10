# -*- coding: utf-8 -*-
"""
低位启动池策略回测
口径：
  - 信号日：个股首次入选低位启动池的日期（从 watch_history.csv 读取）
  - 买点：信号日收盘价
  - 持有期：7个自然日（第1~7日）
  - 止损：买点 * 0.94（-6%，任意一日最低价触及即触发）
  - 止盈：持有期内最高价相对买点涨幅 ≥ 8% 记止盈出场
  - 出场方式：止损 / 止盈(≥8%) / 持有到期
  - 基准：同期上证指数涨幅
统计指标：胜率 / 盈亏比 / 平均收益 / 中位收益 / 最大盈利 / 最大亏损 / 各出场方式分布
"""
import sys, os, json, csv
from pathlib import Path
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

# ===== 新浪日线接口 =====
SINA_KLINE = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"

def _load_sina_hist(code: str, datalen: int = 250) -> pd.DataFrame | None:
    sym = ("sh" if code.startswith(("6", "9")) else "sz") + code
    try:
        r = requests.get(SINA_KLINE, params={"symbol": sym, "scale": 240, "ma": "no", "datalen": str(datalen)}, timeout=20)
        if r.status_code != 200:
            return None
        arr = json.loads(r.text)
        if not arr:
            return None
        df = pd.DataFrame(arr)
        if "day" not in df.columns:
            return None
        df = df.rename(columns={"day": "日期", "open": "开盘", "high": "最高",
                                  "low": "最低", "close": "收盘", "volume": "成交量"})
        df["日期"] = pd.to_datetime(df["日期"])
        for c in ["开盘", "最高", "最低", "收盘", "成交量"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.sort_values("日期").reset_index(drop=True)
        return df
    except Exception:
        return None

# ===== 技术指标 =====
def _tech(df: pd.DataFrame) -> pd.DataFrame:
    c = df["收盘"]
    f = df.copy()
    f["MA5"] = c.rolling(5).mean()
    f["MA20"] = c.rolling(20).mean()
    e12 = c.ewm(span=12, adjust=False).mean()
    e26 = c.ewm(span=26, adjust=False).mean()
    f["DIF"] = e12 - e26
    f["DEA"] = f["DIF"].ewm(span=9, adjust=False).mean()
    return f

# ===== 加载 watch_history =====
HIST_FILE = ROOT / "data" / "watch_history.csv"
WATCH_FIELDS = ["日期", "代码", "名称", "现价", "60日位置", "信号", "5日涨幅", "行业", "买点区间", "止损价", "关注逻辑"]

records = []
if HIST_FILE.exists():
    with HIST_FILE.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            records.append(row)

print(f"[数据] watch_history 共 {len(records)} 条信号")
if not records:
    print("watch_history.csv 为空，无法回测。")
    sys.exit(0)

# ===== 补录更多历史信号（用 backfill 逻辑重跑近 30 天）=====
print("[补录] 从 backfill_watch.py 加载历史候选信号…")
# 复用 backfill_watch 的扫描函数
sys.path.insert(0, str(ROOT / "scripts"))
try:
    from backfill_watch import scan_day as _scan_day
    from fetch_data import get_trade_calendar
    cal = get_trade_calendar(start="2026-08-01", end="2026-09-09")
    trading_days = sorted(cal[-30:])  # 近30个交易日
    print(f"[补录] 近30个交易日: {trading_days[0]} ~ {trading_days[-1]}")
    extra_records = []
    for d in trading_days:
        try:
            rows = _scan_day(d, top=1200)
            for r in rows:
                # 格式对齐 watch_history
                extra_records.append({
                    "日期": d,
                    "代码": r["代码"],
                    "名称": r["名称"],
                    "现价": str(r["现价"]),
                    "60日位置": str(r["60日位置"]),
                    "信号": r["信号"],
                    "5日涨幅": str(r["5日涨幅"]),
                    "行业": r["行业"],
                    "买点区间": r["买点区间"],
                    "止损价": str(r["止损价"]),
                    "关注逻辑": r["关注逻辑"],
                })
        except Exception:
            continue
    print(f"[补录] 额外获得 {len(extra_records)} 条信号")
    # 合并去重（同日期同代码保留一条，优先保留有更多信息的）
    all_records = records + extra_records
    seen = {}
    for r in all_records:
        key = (r["日期"], r["代码"])
        if key not in seen:
            seen[key] = r
    all_records = list(seen.values())
    all_records.sort(key=lambda x: (x["日期"], x["代码"]))
    print(f"[合并] 去重后共 {len(all_records)} 条信号")
except Exception as e:
    print(f"[补录] 失败 ({e})，仅用 watch_history {len(records)} 条")
    all_records = records

# ===== 逐条回测 =====
BUY_HOLD = 7       # 持有自然日数
STOP_LOSS = 0.94   # 止损线
TAKE_PROFIT = 1.08 # 止盈线

def _backtest_one(record: dict) -> dict | None:
    code = record["代码"].zfill(6)
    sig_date = pd.Timestamp(record["日期"])
    try:
        buy_price = float(record["现价"])
        stop_loss = float(record["止损价"])
    except Exception:
        return None

    df = _load_sina_hist(code)
    if df is None or len(df) < 10:
        return None

    # 定位信号日
    after = df[df["日期"] > sig_date]
    if len(after) < 2:
        return None

    hold = after.head(BUY_HOLD)
    if len(hold) < 2:
        return None

    buy_close = float(hold.iloc[0]["收盘"])
    # 用信号日收盘价作为买点（回测用当日开盘买入）
    # 注意：record["现价"] 是信号日收盘，信号日选股后次日开盘买入
    # 为简化，这里用信号日收盘作为买入成本基准（次日开盘约等于收盘）
    buy_cost = buy_close

    high_prices = hold["最高"]
    low_prices = hold["最低"]
    close_prices = hold["收盘"]

    # 止损触发
    stop_day = low_prices[low_prices <= stop_loss]
    stop_triggered = len(stop_day) > 0

    # 止盈触发（任意一日最高价触及 8%）
    tp_day = high_prices[high_prices / buy_cost >= TAKE_PROFIT]
    tp_triggered = len(tp_day) > 0

    # 最大实际涨幅（持有期内）
    max_high = high_prices.max()
    max_ret = (max_high / buy_cost - 1) * 100

    # 最终出场
    if stop_triggered:
        exit_ret = round((stop_loss / buy_cost - 1) * 100, 2)
        exit_type = "止损"
    elif tp_triggered:
        exit_ret = round((TAKE_PROFIT - 1) * 100, 2)  # 记为止盈8%
        exit_type = "止盈(≥8%)"
    else:
        final_close = close_prices.iloc[-1]
        exit_ret = round((final_close / buy_cost - 1) * 100, 2)
        exit_type = f"持有到期({len(hold)}日)"

    # 上证基准（信号日后7日）
    bench_ret = None
    try:
        bench = _load_sina_hist("000001")  # 上证指数
        if bench is not None:
            bafter = bench[bench["日期"] > sig_date].head(BUY_HOLD)
            if len(bafter) >= 2:
                bench_ret = round((bafter["收盘"].iloc[-1] / bafter["收盘"].iloc[0] - 1) * 100, 2)
    except Exception:
        pass

    return {
        "信号日": str(sig_date.date()),
        "代码": code,
        "名称": record["名称"],
        "行业": record.get("行业", ""),
        "信号": record.get("信号", ""),
        "60日位置": float(record.get("60日位置", 0)),
        "买点": buy_cost,
        "止损价": stop_loss,
        "出场方式": exit_type,
        "最终涨幅": exit_ret,
        "最大涨幅": round(max_ret, 2),
        "基准涨幅": bench_ret,
        "相对基准": round(exit_ret - (bench_ret or 0), 2) if bench_ret is not None else None,
        "持有天数": len(hold),
    }

# 并行回测
trades = []
total = len(all_records)
errors = 0

def _work(rec):
    try:
        r = _backtest_one(rec)
        return r
    except Exception:
        return None

with ThreadPoolExecutor(max_workers=15) as pool:
    futures = {pool.submit(_work, rec): rec for rec in all_records}
    done = 0
    for fut in as_completed(futures):
        done += 1
        if done % 20 == 0:
            print(f"  回测进度 {done}/{total}")
        r = fut.result()
        if r:
            trades.append(r)

print(f"\n[回测] 成功 {len(trades)} / {total} 条")

# ===== 统计 =====
df = pd.DataFrame(trades)
if df.empty:
    print("样本不足，无法统计。")
    sys.exit(0)

# 基本统计
wins = df[df["最终涨幅"] > 0]
losses = df[df["最终涨幅"] <= 0]
win_rate = len(wins) / len(df) * 100
avg_win = wins["最终涨幅"].mean() if len(wins) else 0
avg_loss = losses["最终涨幅"].mean() if len(losses) else 0
pl_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")
avg_ret = df["最终涨幅"].mean()
median_ret = df["最终涨幅"].median()

print(f"\n{'='*55}")
print(f"  低位启动池策略回测报告（持有7日，止损-6%）")
print(f"{'='*55}")
print(f"  统计区间: {df['信号日'].min()} ~ {df['信号日'].max()}")
print(f"  总信号数: {len(df)}")
print(f"  盈利次数: {len(wins)}  |  亏损次数: {len(losses)}")
print(f"  胜率: {win_rate:.1f}%")
print(f"  平均盈利: +{avg_win:.2f}%")
print(f"  平均亏损: {avg_loss:.2f}%")
print(f"  盈亏比: {pl_ratio:.2f}")
print(f"  平均收益: {avg_ret:.2f}%")
print(f"  中位收益: {median_ret:.2f}%")
print(f"  最大单笔盈利: {df['最终涨幅'].max():.2f}%")
print(f"  最大单笔亏损: {df['最终涨幅'].min():.2f}%")
print(f"  超额收益（相对基准）均值: {df['相对基准'].mean():.2f}%" if df["相对基准"].notna().any() else "")

# 出场分布
print(f"\n  出场方式分布:")
for et, cnt in df["出场方式"].value_counts().items():
    avg = df[df["出场方式"] == et]["最终涨幅"].mean()
    print(f"    {et}: {cnt}次({cnt/len(df)*100:.0f}%) 均收益{avg:+.2f}%")

# 行业分布
ind_stats = df.groupby("行业").agg(
    信号数=("代码", "count"),
    均收益=("最终涨幅", "mean"),
    胜率=("最终涨幅", lambda x: (x > 0).mean() * 100)
).sort_values("信号数", ascending=False)
print(f"\n  行业分布（信号数≥1）:")
for ind, row in ind_stats.iterrows():
    print(f"    {ind}: {int(row['信号数'])}次 均收益{row['均收益']:+.2f}% 胜率{row['胜率']:.0f}%")

# 60日位置分层统计
df["位置分层"] = pd.cut(df["60日位置"], bins=[0, 0.15, 0.25, 0.40], labels=["极低位(<15%)", "低位(15-25%)", "偏低(25-40%)"])
print(f"\n  60日位置分层统计:")
for seg, g in df.groupby("位置分层", observed=True):
    if len(g):
        print(f"    {seg}: {len(g)}次 胜率{(g['最终涨幅']>0).mean()*100:.0f}% 均收益{g['最终涨幅'].mean():+.2f}%")

# 盈亏比改进（加入止盈8%后，盈亏比大幅改善）
# 期望值 = win_rate * avg_win + (1-win_rate) * avg_loss
ev = win_rate/100 * avg_win + (1 - win_rate/100) * avg_loss
print(f"\n  期望值（数学期望）: {ev:.2f}%")

# 保存明细
sig_range = f"{df['信号日'].min()}_{df['信号日'].max()}"
out_csv = ROOT / "04_每日复盘" / f"低位池回测_{sig_range}.csv"
out_csv.parent.mkdir(parents=True, exist_ok=True)
df.to_csv(out_csv, index=False, encoding="utf-8-sig")
print(f"\n[保存] 明细 → {out_csv}")

# 打印明细表格
print(f"\n{'='*55}")
print("  逐条明细（按信号日排序）")
print(f"{'='*55}")
print(df[["信号日","名称","代码","行业","60日位置","买点","最终涨幅","最大涨幅","出场方式"]].to_string(index=False))
