# -*- coding: utf-8 -*-
"""大盘环境分析：市场温度计 + 仓位建议"""
import numpy as np
import pandas as pd
from fetch_data import get_index_daily, get_market_snapshot, get_stock_zt_pool, get_stock_dt_pool, get_stock_zb_pool, get_market_fund_flow
from config import (WEIGHTS, TOTAL_CAPITAL, MARKET_ENV_THRESHOLDS, SHORTLINE_ZT_MIN,
                    SHORTLINE_ZB_RATE_MAX, SHORTLINE_TEMP_MIN, TREND_TEMP_MIN)


def _ma(series, n):
    return series.rolling(n).mean()


def score_trend(idx_df):
    """趋势打分（0-100）"""
    if idx_df is None or len(idx_df) < 60:
        return 50, "数据不足"
    close = idx_df["close"].astype(float)
    ma20 = _ma(close, 20)
    ma60 = _ma(close, 60)
    last = close.iloc[-1]
    m20 = ma20.iloc[-1]
    m60 = ma60.iloc[-1]
    prev = close.iloc[-2]

    # 近期新高判断
    high_60 = close.tail(60).max()
    is_high = last >= high_60 * 0.995  # 接近60日新高

    score = 50
    notes = []
    if last > m20 > m60:
        score += 25
        notes.append("多头排列")
    elif last > m20:
        score += 15
        notes.append("站上MA20")
    elif last < m20 and m20 > m60:
        score += 0
        notes.append("跌破MA20")
    else:
        score -= 10
        notes.append("空头/破位")

    if is_high:
        score += 15
        notes.append("创阶段新高")
    if last > prev and prev > close.iloc[-3]:
        score += 10
        notes.append("连续上涨")

    # 近5日累计涨跌
    chg5 = (last / close.iloc[-6] - 1) * 100 if len(close) > 6 else 0
    if chg5 > 3:
        score += 5
        notes.append(f"5日+{chg5:.1f}%")
    elif chg5 < -3:
        score -= 10
        notes.append(f"5日{chg5:.1f}%")

    return max(0, min(100, score)), "; ".join(notes)


def _prev_day_change(idx_df):
    """取指数最近一个有效交易日的涨跌幅%（剔除当日无成交的盘前行）"""
    if idx_df is None or len(idx_df) < 2:
        return None
    df = idx_df.copy()
    if "volume" in df.columns:
        df = df[pd.to_numeric(df["volume"], errors="coerce").fillna(0) > 0]
    if len(df) < 2:
        df = idx_df
    close = df["close"].astype(float)
    return (close.iloc[-1] / close.iloc[-2] - 1) * 100


def _prev_day_amount():
    """东财指数日线取最近有效交易日两市成交额合计（亿元），失败返回0"""
    import akshare as ak
    total = 0.0
    for sym in ("sh000001", "sz399001"):
        try:
            df = ak.stock_zh_index_daily_em(symbol=sym)
            if df is None or df.empty or "amount" not in df.columns:
                continue
            d = df[pd.to_numeric(df["volume"], errors="coerce").fillna(0) > 0]
            if d.empty:
                continue
            total += float(pd.to_numeric(d["amount"], errors="coerce").fillna(0).iloc[-1])
        except Exception:
            continue
    return total / 1e8


def score_sentiment(snapshot, idx_sh=None):
    """情绪打分（0-100），用涨停/跌停池精确统计；盘前自动回退昨日数据"""
    if snapshot is None or snapshot.empty:
        return 50, "数据不足"
    up = (snapshot["涨跌幅"] > 0).sum()
    down = (snapshot["涨跌幅"] < 0).sum()

    # 盘前模式：竞价阶段无涨跌家数，回退用昨日指数涨跌幅估情绪
    if up + down == 0:
        chg = _prev_day_change(idx_sh)
        if chg is not None:
            score = 50 + max(-30, min(25, chg * 8))
            return max(0, min(100, score)), f"盘前模式：昨日沪指{chg:+.2f}%估情绪"
        return 50, "盘前数据不足(中性)"

    total = max(up + down, 1)
    up_ratio = up / total

    zt_df = get_stock_zt_pool()
    dt_df = get_stock_dt_pool()
    zt = len(zt_df) if zt_df is not None else 0
    dt = len(dt_df) if dt_df is not None else 0

    score = 50
    notes = [f"涨跌家数 {up}/{down}"]
    if up_ratio > 0.6:
        score += 25
        notes.append("普涨")
    elif up_ratio > 0.5:
        score += 10
    elif up_ratio < 0.4:
        score -= 15
        notes.append("普跌")
    elif up_ratio < 0.3:
        score -= 30
        notes.append("恐慌")

    if zt >= 60:
        score += 10
        notes.append(f"涨停{zt}")
    elif zt < 20:
        score -= 10
        notes.append(f"涨停仅{zt}")
    if dt >= 30:
        score -= 15
        notes.append(f"跌停{dt}")

    return max(0, min(100, score)), "; ".join(notes)


def score_volume(snapshot):
    """量能打分（0-100）；盘前回退昨日成交额"""
    if snapshot is None or snapshot.empty:
        return 50, "数据不足"
    amount = snapshot["成交额"].sum() / 1e8  # 亿元
    if amount < 100:  # 盘前/竞价：快照成交额近零
        prev_amt = _prev_day_amount()
        if prev_amt > 100:
            amount = prev_amt
        else:
            return 50, "盘前模式：量能待开盘(中性)"
    score = 50
    notes = [f"两市成交额 {amount:.0f}亿"]
    if amount >= 12000:
        score += 25
        notes.append("显著放量")
    elif amount >= 10000:
        score += 15
        notes.append("万亿之上")
    elif amount >= 8000:
        score += 0
    elif amount >= 6000:
        score -= 15
        notes.append("缩量")
    else:
        score -= 30
        notes.append("地量/低迷")
    return max(0, min(100, score)), "; ".join(notes)


def score_capital():
    """资金打分（0-100）——akshare北向数据不稳定时给中性分"""
    try:
        df = get_market_fund_flow()
        if df is None or df.empty:
            return 50, "资金数据不可得(中性)"
        # df列可能为 日期/上证-涨跌幅/主力净流入-净额 等
        if "主力净流入-净额" in df.columns:
            latest = df["主力净流入-净额"].astype(float).iloc[-1] / 1e8
            score = 50 + max(-30, min(30, latest))  # 净流入1亿≈+1分，封顶±30
            return max(0, min(100, score)), f"主力净流入 {latest:.0f}亿"
        return 50, "字段缺失(中性)"
    except Exception:
        return 50, "资金数据不可得(中性)"


def score_regime():
    """体制分（0-100）：stock-researcher 牛熊震荡判断；失败给中性"""
    try:
        from skill_bridge import get_market_regime
        reg = get_market_regime()
        if not reg:
            return 50, "体制数据不可得(中性)"
        label = reg["体制"]
        if label == "牛市":
            score = 85
        elif label == "震荡":
            score = 60
        else:  # 熊市
            score = 25
        note = f"{label}(置信{reg['置信']}%, 体制分{reg['体制分']})"
        return score, note
    except Exception:
        return 50, "体制数据不可得(中性)"


def calc_market_temperature():
    """综合温度计（4维加权 + 体制修正）"""
    idx_sh = get_index_daily("sh000001")
    idx_sz = get_index_daily("sz399001")
    idx_cy = get_index_daily("sz399006")
    snapshot = get_market_snapshot()

    t_score, t_note = score_trend(idx_sh)
    s_score, s_note = score_sentiment(snapshot, idx_sh)
    v_score, v_note = score_volume(snapshot)
    c_score, c_note = score_capital()
    r_score, r_note = score_regime()

    temp = (t_score * WEIGHTS["trend"] + s_score * WEIGHTS["sentiment"] +
            v_score * WEIGHTS["volume"] + c_score * WEIGHTS["capital"]) / 100.0
    # 体制修正：牛市+5 / 震荡不变 / 熊市-10
    if "牛市" in r_note:
        temp = min(100, temp + 5)
    elif "熊市" in r_note:
        temp = max(0, temp - 10)

    details = {
        "趋势": {"分": t_score, "说明": t_note},
        "情绪": {"分": s_score, "说明": s_note},
        "量能": {"分": v_score, "说明": v_note},
        "资金": {"分": c_score, "说明": c_note},
        "体制": {"分": r_score, "说明": r_note},
    }
    return round(temp, 0), details


def safe_market_temperature():
    """温度计安全包装：任何数据源失败都不抛异常，返回 (temp, details, ok)。
    ok=False 表示走中性兜底（正文应标注"数据降级"）。供 daily_report / noon_review /
    close_review 统一使用，避免全A快照/指数失败时主流程整体崩溃导致推送静默丢失。"""
    try:
        temp, details = calc_market_temperature()
        return temp, details, True
    except Exception as e:
        print(f"[温度计] 计算失败，降级中性温度: {e}")
        neutral = {"趋势": {"分": 50, "说明": f"数据降级: {str(e)[:60]}"},
                   "情绪": {"分": 50, "说明": "数据降级"},
                   "量能": {"分": 50, "说明": "数据降级"},
                   "资金": {"分": 50, "说明": "数据降级"},
                   "体制": {"分": 50, "说明": "数据降级"}}
        return 50.0, neutral, False


def decide_position(temp):
    """温度→仓位建议"""
    if temp >= 75:
        return "70%-90%", "进攻：可满仓运作，优先加仓强势主线"
    if temp >= 55:
        return "40%-60%", "标准：正常建仓，控制单票风险"
    if temp >= 35:
        return "10%-30%", "防守：轻仓试探，只做最强龙头"
    return "0%", "避险：空仓等待，不参与"


def single_stock_position(score):
    """评分→单票仓位"""
    if score >= 80:
        return TOTAL_CAPITAL * 0.30
    if score >= 70:
        return TOTAL_CAPITAL * 0.20
    return 0


def shortline_gate():
    """短线通道情绪门槛（2026-09-05 双通道架构）
    情绪达标 = 涨停家数≥SHORTLINE_ZT_MIN 且 炸板率<SHORTLINE_ZB_RATE_MAX
    炸板率 = 炸板家数 / (涨停家数 + 炸板家数)
    返回 (gate_open: bool, reason: str)
    """
    try:
        zt_df = get_stock_zt_pool()
        zb_df = get_stock_zb_pool()
        zt_n = len(zt_df) if zt_df is not None else 0
        zb_n = len(zb_df) if zb_df is not None else 0
        total_attempts = zt_n + zb_n
        zb_rate = (zb_n / total_attempts) if total_attempts > 0 else 1.0

        reasons = []
        gate = True
        if zt_n < SHORTLINE_ZT_MIN:
            gate = False
            reasons.append(f"涨停{zt_n}家<{SHORTLINE_ZT_MIN}（情绪不够热）")
        else:
            reasons.append(f"涨停{zt_n}家")
        if zb_rate >= SHORTLINE_ZB_RATE_MAX:
            gate = False
            reasons.append(f"炸板率{zb_rate*100:.0f}%≥{SHORTLINE_ZB_RATE_MAX*100:.0f}%（封板质量差，接力易被埋）")
        else:
            reasons.append(f"炸板率{zb_rate*100:.0f}%")

        # 温度门控（2026-09-09：回测8个月4个月为负，低温期不开仓）
        temp, _ = calc_market_temperature()
        if temp < SHORTLINE_TEMP_MIN:
            gate = False
            reasons.append(f"温度{temp:.0f}<{SHORTLINE_TEMP_MIN}（弱势期短线期望为负）")
        else:
            reasons.append(f"温度{temp:.0f}")

        if gate:
            return True, "情绪达标：" + "，".join(reasons)
        return False, "短线通道休战：" + "，".join(reasons)
    except Exception as e:
        # 数据不可得时保守处理：不开闸
        return False, f"短线通道休战：情绪数据不可得({e})"


def trend_gate():
    """趋势通道温度门控（2026-09-09：回测2月/9月低温期月度收益为负，温度≥55 才开仓）"""
    try:
        temp, _ = calc_market_temperature()
        if temp >= TREND_TEMP_MIN:
            return True, f"温度{temp:.0f}≥{TREND_TEMP_MIN}"
        return False, f"温度{temp:.0f}<{TREND_TEMP_MIN}（低温期趋势期望为负）"
    except Exception as e:
        return False, f"趋势通道休战：温度数据不可得({e})"


def judge_market_env(idx_df=None, temp=None, snapshot=None):
    """判定市场环境，返回 (env_label, env_desc, reason)
    env_label: "强势市" | "震荡市" | "弱势市"
    env_desc:  简短描述
    reason:    判定理由（供日报展示）
    """
    import akshare as ak

    # 取指数数据（若未传入则自己拉）
    if idx_df is None:
        idx_df = get_index_daily("sh000001")
    if idx_df is None or len(idx_df) < 60:
        return "震荡市", "数据不足(中性)", "指数数据不足，无法判断，默认为震荡市"

    close = idx_df["close"].astype(float)
    ma20 = _ma(close, 20)
    ma60 = _ma(close, 60)
    last = close.iloc[-1]
    m20 = ma20.iloc[-1]
    m60 = ma60.iloc[-1]

    # 取量能
    vol_ok = False
    amount = 0
    if snapshot is not None and not snapshot.empty:
        amount = snapshot["成交额"].sum() / 1e8
    if amount < 100:
        prev = _prev_day_amount()
        if prev > 100:
            amount = prev
    if amount >= 100:
        vol_ok = True

    # 取温度（若未传入则算）
    if temp is None:
        temp, _ = calc_market_temperature()

    # 取涨停池健康度（近5日连板率）
    zt_df = get_stock_zt_pool()
    zt_count = len(zt_df) if zt_df is not None else 0
    zt_healthy = zt_count >= 30  # 涨停30家以上算情绪活跃

    notes = []
    env = "震荡市"
    env_desc = "中性市况"

    # 强势市条件：温度≥65 + MA多头排列 + 量能活跃
    if temp >= MARKET_ENV_THRESHOLDS["strong_temp"]:
        ma_ok = (last > m20 > m60) or (last > m20 and m20 > m60 * 0.98)
        if ma_ok and vol_ok:
            env = "强势市"
            env_desc = "右侧行情"
            notes.append(f"温度{temp:.0f}，MA多头，量能{amount:.0f}亿，涨停{zt_count}家")
        elif ma_ok:
            env = "强势市"
            env_desc = "右侧行情（量能待确认）"
            notes.append(f"温度{temp:.0f}，MA多头，涨停{zt_count}家")
        else:
            env = "震荡市"
            env_desc = "温度偏高但趋势不强"
            notes.append(f"温度{temp:.0f}，MA未多头，震荡市对待")

    # 弱势市条件：温度≤35 或 指数跌破MA60 + 量能萎缩 + 涨停稀少
    elif temp <= MARKET_ENV_THRESHOLDS["weak_temp"]:
        if last < m60:
            env = "弱势市"
            env_desc = "控仓观望"
            notes.append(f"温度{temp:.0f}，沪指破MA60，弱势")
        elif not vol_ok or zt_count < 15:
            env = "弱势市"
            env_desc = "控仓观望"
            notes.append(f"温度{temp:.0f}，量能萎缩/涨停稀少，弱势")
        else:
            env = "震荡市"
            env_desc = "温度偏低但未破位"
            notes.append(f"温度{temp:.0f}，未破位，震荡对待")

    # 中间地带：震荡市（默认）
    else:
        if last > m20 > m60 and vol_ok and zt_healthy:
            env = "震荡偏强"
            env_desc = "局部做多"
            notes.append(f"温度{temp:.0f}，MA多头但温度未达强势阈值")
        elif last < m20 and m20 < m60:
            env = "震荡偏弱"
            env_desc = "谨慎做多"
            notes.append(f"温度{temp:.0f}，MA空头排列")
        else:
            env = "震荡市"
            env_desc = "中性市况"
            notes.append(f"温度{temp:.0f}，趋势不明")

    return env, env_desc, "; ".join(notes) if notes else f"温度{temp:.0f}"
