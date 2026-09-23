# -*- coding: utf-8 -*-
"""「温和放量初动」右侧路径回测（读本地 data/klines 前复权缓存，无网络）。

画像来源：2026-09-09 露笑科技/通鼎互联（入选后 4-5 个交易日涨停）——
  「60日低位 + 站上MA20 + MACD多头 + 放量1.3-2.5x + 5日涨幅+3~8%，趋势启动初现」

筛选画像（T 日收盘确认）：
  1) 量比(当日量/前5日均量) ∈ [1.3, 2.5]
  2) 5日涨幅(收/5日前收-1) ∈ [+3%, +8%]
  3) 60日位置 < 80%
  4) 收盘站上 MA20
  5) MACD 多头：DIF > DEA
  6) 当日涨幅 ≤ 9.5%（排除已涨停，抓"涨停前初动"）
  7) 成交额上限为变量：≤16亿（防大亏档） / ≤50亿（宽容档，通鼎43亿能否进入）

入场口径（真实可执行）：候选日 T 的【次日开盘价】买入（收盘确认信号，次日才可挂单）。
  - 一字板跳过：次日 open==high==low==close 视为买不进。

收益统计（两个口径）：
  A. 固定持有：T+5 收盘出场 → 均值/胜率/盈亏比
  B. 带止损：任意收盘 < 入场×0.92 → 按 0.92×入场 出场(-8%)，否则 T+5 收盘出场
        → 胜率/盈亏比(平均盈/平均亏)/止损触发率

信号冷却：同一只票 10 个交易日内最多记 1 个信号，减少重叠样本。
"""
import sys
from pathlib import Path
from glob import glob

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import numpy as np

BASE = Path(__file__).resolve().parent.parent / "data" / "klines"

# 画像参数
LB_LO, LB_HI = 1.3, 2.5          # 量比 1.3-2.5x
CHG5_LO, CHG5_HI = 3.0, 8.0      # 5日涨幅 +3%~+8%
POS60_MAX = 0.80                 # 位置 <80%
CHG_TODAY_MAX = 9.5              # 当日涨幅上限（未涨停）
AMT_CAPS = {"≤16亿": 16e8, "≤50亿": 50e8, "不限": None}  # 成交额档位
STOP_PCT = 0.92                  # 8% 止损

# ===== 蓄势前置条件（2026-09-16 新增维度，用户提出）=====
# 用户假设：大涨前应是「窄幅缩量震荡」蓄势。验证发现露笑符合(前10振幅2.5%/量能萎缩43%)、
# 通鼎不符合(前10振幅6.7%/量能不缩/前15日+26.8%)。故做两档变量回测，用数据判断该条件价值。
CONSOL_TIERS = {
    "无蓄势": None,                                     # 不设前置（基线）
    "宽松蓄势": {"amp10_max": 4.5, "vol10_60_max": 0.80},  # 前10振幅≤4.5% 且 前10均量/前60均量≤0.8
    "保守蓄势": {"amp10_max": 3.5, "vol10_60_max": 0.70},  # 前10振幅≤3.5% 且 前10均量/前60均量≤0.7
}
ACTIVE_CONSOL = "无蓄势"   # 命令行 --consol 覆盖

# ===== 2026-09-16 第三轮改进：周线共振 + 回踩不破 + 振幅主画像 =====
# 口诀「周线上扬，日线放量，回踩不破，果断上车」拆解：
#   ① 周线上扬 = 周线趋势过滤（周收盘>5周均线 且 5周均线上行）→ 只在上升趋势中买
#   ② 日线放量 = 原画像（量比1.3-2.5x）已覆盖
#   ③ 回踩不破 = 放量后回调不破 MA20 → 回调日买入（降追高成本）而非次日开盘追
#   ④ 振幅因素 = 主画像新增「信号日振幅」上限（避免追高振幅过大的票）
WEEK_FILTER = False        # --week 启用周线共振过滤
PULLBACK_ENTRY = False     # --pullback 启用回踩不破入场（回调日收盘买，3日内无回调→次日开盘保底）
AMP_TODAY_MAX = 7.5        # 信号日振幅上限%（防过度波动，振幅=(high-low)/prev_close）
PULLBACK_WINDOW = 3        # 回踩观察窗口（信号后 N 个交易日内）

# ===== 两段式模式（2026-09-16 用户新想法：从低位观察池内筛温和放量初动）=====
# 阶段1 = 低位池入池标准（缩量蓄势）：位置<55%、量比≤1.3、5日涨幅-10~+5%
# 阶段2 = 温和放量初动触发（原画像）：量比1.3-2.5、5日+3~8%、站上MA20、MACD多头、当日≤9.5%
# 信号 = 蓄势确认后 WINDOW 个交易日内首次出现阶段2的日子
MODE_TWOSTAGE = False   # --twostage 覆盖
STAGE1 = {
    "pos60_max": 0.55,      # 低位池位置上限（config LOW_POS_ENTRY_MAX_POS60）
    "lb_max": 1.30,         # 缩量（config LOW_POS_ENTRY_MAX_LB）
    "lb_min": 0.30,         # 防极低量噪音
    "chg5_lo": -10.0,       # 5日涨幅下沿（config 横盘区间）
    "chg5_hi": 5.0,         # 5日涨幅上沿
    "window": 20,           # 蓄势确认后等待触发的交易日窗口
}


def _load(code):
    f = BASE / f"{code}.csv"
    if not f.exists():
        return None
    df = pd.read_csv(f)
    if len(df) < 70:
        return None
    df = df.copy()
    df["日期"] = pd.to_datetime(df["date"])
    df = df.sort_values("日期").reset_index(drop=True)
    df["涨跌幅"] = df["last"].pct_change() * 100
    df["chg5"] = df["last"].pct_change(5) * 100
    df["v5"] = df["volume"].shift(1).rolling(5).mean()
    df["lb"] = df["volume"] / df["v5"]
    df["hi60"] = df["high"].rolling(60, min_periods=40).max()
    df["lo60"] = df["low"].rolling(60, min_periods=40).min()
    df["pos60"] = (df["last"] - df["lo60"]) / (df["hi60"] - df["lo60"])
    df["ma20"] = df["last"].rolling(20).mean()
    ema12 = df["last"].ewm(span=12, adjust=False).mean()
    ema26 = df["last"].ewm(span=26, adjust=False).mean()
    df["dif"] = ema12 - ema26
    df["dea"] = df["dif"].ewm(span=9, adjust=False).mean()
    # 蓄势指标（2026-09-16 新增）：前 N 日振幅均值（不含 T 日）、前10均量/前60均量
    df["prev_close"] = df["last"].shift(1)
    df["振幅"] = (df["high"] - df["low"]) / df["prev_close"] * 100
    df["amp10_mean"] = df["振幅"].shift(1).rolling(10).mean()   # 前10日平均振幅
    df["v10"] = df["volume"].shift(1).rolling(10).mean()
    df["v60"] = df["volume"].shift(1).rolling(60).mean()
    df["vol10_60"] = df["v10"] / df["v60"]                       # 前10均量 / 前60均量（缩量程度）
    # 周线指标（2026-09-16 第三轮新增）：按自然周聚合，映射回日线
    df["week"] = df["日期"].dt.to_period("W").astype(str)
    wk = df.groupby("week").agg(wclose=("last", "last"), wopen=("open", "first"),
                                whigh=("high", "max"), wlow=("low", "min"),
                                wvol=("volume", "sum")).reset_index()
    wk["wma5"] = wk["wclose"].rolling(5).mean()
    wk["wma5_up"] = wk["wma5"] > wk["wma5"].shift(1)             # 5周均线上行
    wk = wk.ffill()
    df = df.merge(wk[["week", "wclose", "wma5", "wma5_up"]], on="week", how="left")
    df["week_up"] = (df["wclose"] > df["wma5"]) & df["wma5_up"]  # 周线上扬：收>5周线 且 5周线走多
    return df


def _signal_mask(df):
    """T 日收盘后满足画像的布尔掩码。蓄势前置由 ACTIVE_CONSOL、周线共振由 WEEK_FILTER 控制。"""
    m = (
        df["lb"].between(LB_LO, LB_HI)
        & df["chg5"].between(CHG5_LO, CHG5_HI)
        & (df["pos60"] < POS60_MAX)
        & (df["last"] > df["ma20"])
        & (df["dif"] > df["dea"])
        & (df["涨跌幅"] <= CHG_TODAY_MAX)
        & (df["涨跌幅"] > -9.5)          # 排除跌停等极端
        & df["ma20"].notna() & df["lb"].notna()
    )
    # 2026-09-17 第三轮：信号日振幅上限进主画像（口诀"日线放量"防过度波动追高）
    if AMP_TODAY_MAX:
        m &= df["振幅"].notna() & (df["振幅"] <= AMP_TODAY_MAX)
    # 2026-09-17 第三轮：周线共振过滤（周线上扬：周收>5周线 且 5周线走多）
    if WEEK_FILTER:
        m &= df["week_up"].notna() & df["week_up"]
    consol = CONSOL_TIERS.get(ACTIVE_CONSOL)
    if consol:
        m &= (
            df["amp10_mean"].notna()
            & (df["amp10_mean"] <= consol["amp10_max"])
            & df["vol10_60"].notna()
            & (df["vol10_60"] <= consol["vol10_60_max"])
        )
    return m


def _is_limit_up(chg, code):
    th = 19.0 if code.startswith(("3", "68")) else 9.5
    return chg >= th


def _stage1_mask(df):
    """两段式·阶段1：低位观察池入池标准（缩量蓄势低位）。"""
    s1 = STAGE1
    return (
        df["lb"].between(s1["lb_min"], s1["lb_max"])
        & df["chg5"].between(s1["chg5_lo"], s1["chg5_hi"])
        & (df["pos60"] < s1["pos60_max"])
        & df["ma20"].notna()
    )


def _collect_signals(df, code):
    """按票扫描所有满足画像的日子，10 交易日冷却。

    MODE_TWOSTAGE=True 时两段式：
      阶段1 = 缩量蓄势低位（STAGE1 条件）确认入池
      阶段2 = 之后 WINDOW 个交易日内首次满足温和放量初动画像 → 记为信号
    单段式：直接按 _signal_mask 找信号（10 日冷却）。
    """
    if not MODE_TWOSTAGE:
        m = _signal_mask(df)
        idx = df.index[m].tolist()
        out = []
        last_i = -99
        for i in idx:
            if i - last_i < 10:      # 冷却期 10 个交易日
                continue
            last_i = i
            out.append(i)
        return out

    # 两段式：蓄势确认日 → 窗口内首个阶段2触发
    s1 = _stage1_mask(df)
    stage1_days = df.index[s1].tolist()
    s2 = _signal_mask(df)            # 阶段2 = 温和放量初动画像
    out = []
    pending_until = -1               # 已确认蓄势，等待触发的窗口截止日（index）
    for i in range(len(df)):
        if s1.iloc[i]:
            pending_until = i + STAGE1["window"]   # 蓄势确认，开启窗口
        elif pending_until > 0 and s2.iloc[i] and i <= pending_until:
            # 窗口内首次触发阶段2
            out.append(i)
            pending_until = -1                     # 触发后重置，等下一次蓄势确认
    return out


def _trade_result(df, i):
    """候选日 i，次日开盘入场，返回 (ok, entry, r5, r5_stop, stop_hit, max_r5, reason)。
    ok=False 表示次日买不进（一字板）。
    PULLBACK_ENTRY=True 时改为「回踩不破」入场：
      信号后 PULLBACK_WINDOW 个交易日内，首个「收盘不破 MA20 且当日回调(收盘<信号日收盘)」的日 → 该日收盘买入；
      窗口内无回调 → 次日开盘买入保底。"""
    if i + 1 >= len(df):
        return None
    if PULLBACK_ENTRY:
        # 回踩不破入场：窗口内找「收盘≥MA20 且 回调」的日，收盘买入
        sig_close = df.iloc[i]["last"]
        sig_ma20 = df.iloc[i]["ma20"]
        entry_i = None
        for j in range(i + 1, min(i + 1 + PULLBACK_WINDOW, len(df))):
            rj = df.iloc[j]
            if rj["last"] <= 0:
                continue
            # 回调判定：当日收盘 < 信号日收盘（回踩）；不破 MA20（用当日MA20，容忍微破用信号日MA20*0.99）
            if rj["last"] >= sig_ma20 * 0.99 and rj["last"] < sig_close:
                entry_i = j
                break
        if entry_i is None:
            entry_i = i + 1  # 窗口内无回调 → 次日开盘保底
        entry = df.iloc[entry_i]["last"]
        end_i = min(entry_i + 6, len(df) - 1)     # 自入场日 T+5
        r5 = df.iloc[end_i]["last"] / entry - 1
        seg = df.iloc[entry_i + 1:end_i + 1]
        min_low = seg["low"].min()
        stop_hit = min_low <= entry * STOP_PCT
        r5_stop = r5
        if stop_hit:
            r5_stop = STOP_PCT - 1
        max_r5 = seg["high"].max() / entry - 1
        return {
            "entry": entry, "r5": r5 * 100, "r5_stop": r5_stop * 100,
            "stop_hit": stop_hit, "max_r5": max_r5 * 100,
        }
    nxt = df.iloc[i + 1]
    # 次日一字板（open==high==low==close）→ 买不进
    if nxt["open"] == nxt["high"] == nxt["low"] == nxt["last"]:
        return None
    entry = nxt["open"]
    if entry <= 0:
        return None
    end_i = min(i + 6, len(df) - 1)     # T+5
    r5 = df.iloc[end_i]["last"] / entry - 1
    seg = df.iloc[i + 1:end_i + 1]
    min_low = seg["low"].min()
    stop_hit = min_low <= entry * STOP_PCT
    r5_stop = r5
    if stop_hit:
        r5_stop = STOP_PCT - 1          # -8% 出场
    max_r5 = seg["high"].max() / entry - 1
    return {
        "entry": entry, "r5": r5 * 100, "r5_stop": r5_stop * 100,
        "stop_hit": stop_hit, "max_r5": max_r5 * 100,
    }


def main():
    import sys
    global ACTIVE_CONSOL, MODE_TWOSTAGE, WEEK_FILTER, PULLBACK_ENTRY
    args = sys.argv[1:]
    for a in args:
        if a.startswith("--consol="):
            v = a.split("=", 1)[1]
            if v in CONSOL_TIERS:
                ACTIVE_CONSOL = v
        if a == "--twostage":
            MODE_TWOSTAGE = True
        if a == "--week":
            WEEK_FILTER = True
        if a == "--pullback":
            PULLBACK_ENTRY = True
    mode_desc = "两段式(蓄势→初动)" if MODE_TWOSTAGE else f"单段式(蓄势前置:{ACTIVE_CONSOL})"
    if WEEK_FILTER:
        mode_desc += " +周线共振"
    if PULLBACK_ENTRY:
        mode_desc += " +回踩入场"
    files = glob(str(BASE / "*.csv"))
    codes = sorted(Path(f).stem for f in files)
    print(f"扫描 {len(codes)} 只票，温和放量初动回测 [{mode_desc}] ...", flush=True)

    stats = {}
    detail = {k: [] for k in AMT_CAPS}
    for code in codes:
        df = _load(code)
        if df is None:
            continue
        for i in _collect_signals(df, code):
            row = df.iloc[i]
            amt = row["amount"]
            rec = {
                "code": code, "date": str(pd.Timestamp(row["日期"]).date()),
                "lb": round(row["lb"], 2), "chg5": round(row["chg5"], 1),
                "pos60": round(row["pos60"], 3), "amt亿": round(amt / 1e8, 1),
                "close": round(row["last"], 2), "chg": round(row["涨跌幅"], 1),
            }
            r = _trade_result(df, i)
            if r is None:
                rec.update({"entry": None, "r5": None, "r5_stop": None,
                            "stop_hit": None, "max_r5": None})
            else:
                rec.update(r)
            for cap_name, cap in AMT_CAPS.items():
                if cap is None or amt <= cap:
                    detail[cap_name].append(rec)

    for cap_name, recs in detail.items():
        if not recs:
            stats[cap_name] = {"n": 0}
            continue
        rr = [x for x in recs if x["r5"] is not None]
        s = {"n": len(recs), "可执行": len(rr)}
        if rr:
            r5 = pd.Series([x["r5"] for x in rr])
            r5s = pd.Series([x["r5_stop"] for x in rr])
            wins = r5s[r5s > 0]; losses = r5s[r5s < 0]
            s.update({
                "T5均值%": round(r5.mean(), 2),
                "T5胜率%": round((r5 > 0).mean() * 100, 1),
                "T5止损版均值%": round(r5s.mean(), 2),
                "止损版胜率%": round((r5s > 0).mean() * 100, 1),
                "盈亏比": round(wins.mean() / abs(losses.mean()), 2) if len(wins) and len(losses) else None,
                "止损触发率%": round(pd.Series([x["stop_hit"] for x in rr]).mean() * 100, 1),
                "最大单笔盈%": round(r5s.max(), 1),
                "最大单笔亏%": round(r5s.min(), 1),
                "T5≥10%占比%": round((r5 >= 10).mean() * 100, 1),
            })
        stats[cap_name] = s

    print("\n========== 温和放量初动 · 全市场回测结果 ==========\n")
    for cap_name, s in stats.items():
        if s["n"] == 0:
            print(f"[{cap_name}] 无信号")
            continue
        print(f"【成交额{cap_name}】信号 {s['n']} 条，可执行(次日非一字) {s['可执行']} 条")
        for k in ("T5均值%", "T5胜率%", "T5止损版均值%", "止损版胜率%", "盈亏比",
                  "止损触发率%", "T5≥10%占比%", "最大单笔盈%", "最大单笔亏%"):
            print(f"  {k}: {s.get(k)}")
        print()

    # ===== 9-09 露笑/通鼎 命中验证 =====
    print("========== 9-09 露笑/通鼎 命中验证 ==========\n")
    targets = {"sz002617": "露笑科技", "sz002491": "通鼎互联"}
    for code, name in targets.items():
        df = _load(code)
        if df is None:
            print(f"{name}({code}) 无K线缓存")
            continue
        m = _signal_mask(df)
        hit_days = df.loc[m, "日期"]
        print(f"{name}({code}):")
        # 9-09 当天画像明细
        t909 = df[df["日期"] == "2026-09-09"]
        if len(t909):
            r = t909.iloc[0]
            checks = {
                "量比1.3-2.5": LB_LO <= r["lb"] <= LB_HI,
                "5日+3~8%": CHG5_LO <= r["chg5"] <= CHG5_HI,
                "位置<80%": r["pos60"] < POS60_MAX,
                "站上MA20": r["last"] > r["ma20"],
                "MACD多头": r["dif"] > r["dea"],
                "当日≤9.5%": r["涨跌幅"] <= CHG_TODAY_MAX,
            }
            for k, v in checks.items():
                print(f"  {k}: {'✅' if v else '❌'} (量比{r['lb']:.2f} 5日{r['chg5']:.1f}% 位置{r['pos60']:.0%} 收{r['last']:.2f} 涨{r['涨跌幅']:.1f}%)")
            full_hit = all(checks.values())
            print(f"  → 9-09 画像全中: {'✅ 是' if full_hit else '❌ 否'}  成交额 {r['amount']/1e8:.1f}亿")
        print(f"  近60日命中画像的日子: {[str(pd.Timestamp(d).date()) for d in hit_days.tail(5)]}")
        # 9-09 后走势
        t = t909
        if len(t):
            i = t.index[0]
            r = _trade_result(df, i)
            if r:
                print(f"  9-09 信号 → 次日开盘 {r['entry']:.2f} 入场，T+5 {r['r5']:+.1f}%"
                      f"（止损版 {r['r5_stop']:+.1f}%），区间最大 {r['max_r5']:+.1f}%")
            else:
                print("  9-09 信号 → 次日一字板买不进")
        print()

    # ===== 保存明细 =====
    out = Path(__file__).resolve().parent.parent / "data" / "warm_start_backtest.csv"
    all_recs = []
    for cap_name, recs in detail.items():
        for r in recs:
            all_recs.append({**r, "成交额档": cap_name})
    if all_recs:
        pd.DataFrame(all_recs).to_csv(out, index=False, encoding="utf-8-sig")
        print(f"明细已存 {out}（{len(all_recs)} 条）")


if __name__ == "__main__":
    main()
