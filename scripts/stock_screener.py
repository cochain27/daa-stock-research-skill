# -*- coding: utf-8 -*-
"""双通道选股引擎（2026-09-05 架构升级）：
- 波段通道 pick_top_stocks(): 右侧趋势票，综合评分（价值/技术/资金/题材），ZT剔除+行业去重
- 短线通道 pick_shortline_stocks(): 情绪驱动票（涨停开板/冲板强势），情绪评分（封单/梯队/换手/炸板/量比）
两通道独立候选池、独立评分体系、独立风控参数，日报分板块展示
"""
import numpy as np
import pandas as pd
from fetch_data import (get_market_snapshot, get_industry_boards,
                        get_stock_hist, get_stock_info, get_stock_fund_flow,
                        get_realtime_quotes, get_industry_of, get_zt_pool_cached,
                        get_valuation_baidu, get_financial_indicators, _sina_symbol,
                        get_stock_zt_pool, get_stock_zb_pool, get_industry_em)
from config import (SCORE_WEIGHTS, ALLOW_CODE_PREFIX, MAX_SAME_INDUSTRY,
                    ZT_THRESHOLD, ZT_BAN, ZT_LIMIT_5DAY, ZT_MAX_LIANG,
                    VAL_W, VAL_PE_OK, VAL_PE_WARN, VAL_PB_OK, VAL_PB_WARN,
                    SHORTLINE_MIN_CHG, SHORTLINE_MAX_LIANBAN, SHORTLINE_MIN_SCORE,
                    SHORT_STOP_LOSS, SHORT_TAKE_PROFIT_1, SHORT_TAKE_PROFIT_2,
                    SWING_MAX_PICKS,
                    TREND_ENABLED, TREND_MAX_PICKS, TREND_MIN_MARKET_CAP,
                    TREND_MIN_60D_POS, TREND_MIN_60D_POS_LOW, TREND_MAX_20D_AMPLITUDE, TREND_MAX_20D_STD_RATIO,
                    TREND_BREAKOUT_VOL_RATIO, TREND_MIN_AMOUNT,
                    TREND_BREAKOUT_CHG_RANGE, TREND_HOLD_DAYS, TREND_EXTEND_MAX_DAYS,
                    TREND_STOP_LOSS, TREND_TAKE_PROFIT_1, TREND_TAKE_PROFIT_2,
                    SHORTLINE_MAX_UPPER_SHADOW,
                    BOTTOM_FISHING_ENABLED, BOTTOM_FISHING_TEMP_MAX,
                    BOTTOM_FISHING_ZT_MIN, BOTTOM_FISHING_MAX_PICKS,
                    LOW_POS_MAX_20D_AMP, LOW_POS_MAX_20D_STD,
                    LOW_POS_MIN_DIST_60D_HIGH, LOW_POS_BEST_POS, LOW_POS_BEST_AMOUNT,
                    LOW_POS_HARD_POS_AMOUNT, LOW_POS_HEAT_BONUS, LOW_POS_HEAT_MAIN_ONLY)
from industry_heat_tool import industry_heat_status_simple, load_heat


# ============ 第一步：价值初筛 ============

def value_screen(snapshot, top_n=60):
    """价值初筛，返回候选池DataFrame（仅沪深主板/创业板/科创板）"""
    df = snapshot.copy()
    # 代码列统一为干净6位字符串（新浪返回数值会带 .0）
    df["代码"] = df["代码"].astype(str).str.replace(r"\.0$", "", regex=True)
    # 排除 ST / 退市
    df = df[~df["名称"].str.contains("ST|退", na=False)]
    # 排除非沪深A股及科创板（68无权限）、北交所、B股等，见 config.ALLOW_CODE_PREFIX
    df = df[df["代码"].str.match(r"^(?:" + "|".join(ALLOW_CODE_PREFIX) + ")", na=False)]
    # 排除停牌/无价
    df = df[df["最新价"].notna() & (df["最新价"] > 0)]

    # 估值过滤（激进风格放宽，排除明显高估无支撑）
    if "市盈率-动态" in df.columns:
        df = df[df["市盈率-动态"].isna() | (df["市盈率-动态"] > 0) | (df["市盈率-动态"] < 100)]
    if "市净率" in df.columns:
        df = df[df["市净率"].isna() | ((df["市净率"] > 0.5) & (df["市净率"] < 15))]

    # 流动性过滤：成交额 > 1亿（盘前竞价阶段成交额为0，跳过该过滤）
    preopen = "成交额" in df.columns and pd.to_numeric(df["成交额"], errors="coerce").fillna(0).sum() == 0
    if "成交额" in df.columns and not preopen:
        df = df[pd.to_numeric(df["成交额"], errors="coerce").fillna(0) > 1e8]

    # 优先：相对活跃、当日不暴涨暴跌的票
    if "涨跌幅" in df.columns and not preopen:
        df = df[df["涨跌幅"].between(-5, 6)]

    # 打分排序：换手适中、有一定涨幅
    if "换手率" in df.columns:
        df["_liq"] = df["换手率"].fillna(0).clip(0, 20)
        df["_dyn"] = df["涨跌幅"].fillna(0).clip(-3, 6)
        df["_vscore"] = df["_liq"] * 0.5 + df["_dyn"] * 2.0
        df = df.sort_values("_vscore", ascending=False)
    return df.head(top_n)


def top_industry_boards(industry_df, top_n=5):
    """选出当日强势板块（涨幅+成交额）"""
    df = industry_df.copy()
    if df is None or df.empty:
        return df
    # 列名适配
    rename_map = {"板块": "板块名称", "涨跌幅": "涨跌幅", "总成交额": "总成交额",
                  "换手率": "换手率", "领涨股票": "领涨股票", "股票名称": "领涨股票",
                  "公司家数": "公司家数", "成交额": "总成交额"}
    df = df.rename(columns=rename_map)
    for col in ["涨跌幅", "总成交额", "换手率"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    # 综合分：涨幅70% + 成交额贡献30%（用分位数归一）
    if "涨跌幅" in df.columns:
        if "总成交额" in df.columns and df["总成交额"].notna().sum() > 3:
            amt_q = df["总成交额"].rank(pct=True)
            chg_q = df["涨跌幅"].rank(pct=True)
            df["_bscore"] = chg_q * 0.7 + amt_q * 0.3
        else:
            df["_bscore"] = df["涨跌幅"]
        df = df.sort_values("_bscore", ascending=False)
    return df.head(top_n)


def board_seed_stocks(boards, snapshot):
    """从强势板块收集种子股（领涨股代码），用于候选池"""
    seeds = []
    if boards is not None and not boards.empty:
        # 优先取"股票代码"列（sh600293 格式），回退"领涨股票"（名称）无法取代码时跳过
        for col in ["股票代码", "领涨股票"]:
            if col in boards.columns:
                for code in boards[col].dropna().astype(str):
                    code = code.replace("sh", "").replace("sz", "").replace("bj", "")
                    if code.isdigit() and len(code) == 6:
                        seeds.append(code)
                break
    return seeds


# ============ 第二步：技术+资金终筛 ============

def _tech_indicators(hist):
    """计算技术指标"""
    if hist is None or len(hist) < 40:
        return {}
    close = hist["收盘"].astype(float)
    df = hist.copy()
    for n in (5, 10, 20, 60):
        df[f"MA{n}"] = close.rolling(n).mean()
    # MACD
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    df["DIF"] = ema12 - ema26
    df["DEA"] = df["DIF"].ewm(span=9, adjust=False).mean()
    df["MACD"] = 2 * (df["DIF"] - df["DEA"])
    # RSI(14)
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    df["RSI14"] = 100 - 100 / (1 + rs)
    return df


def _zt_info(symbol, name=None, quote=None):
    """涨停/连板判定。返回 (近涨停bool, 连板数, 封板资金, 炸板次数, 备注str)
    数据源：腾讯盘口（涨停价）+ 涨停池（连板/封单）。失败返回 (False,0,0,0,'')"""
    zt_flag = False
    lianban = 0
    fengdan = 0.0
    zhaban = 0
    note = ""
    # 涨停池：连板数、封板资金、炸板次数、所属行业
    try:
        zt = get_zt_pool_cached()
        if zt is not None and not zt.empty and "代码" in zt.columns:
            row = zt[zt["代码"].astype(str).str.split(".").str[0] == str(symbol)]
            if not row.empty:
                r = row.iloc[0]
                lianban = int(r.get("连板数", 0) or 0)
                fengdan = float(r.get("封板资金", 0) or 0) / 1e8
                zhaban = int(r.get("炸板次数", 0) or 0)
    except Exception:
        pass
    # 盘口近涨停判定：现价 >= 涨停价 * 阈值
    if quote is not None:
        price = quote.get("现价")
        zt_price = quote.get("涨停价")
        if price and zt_price:
            ratio = price / zt_price
            if ratio >= ZT_THRESHOLD:
                zt_flag = True
                note = f"近涨停(现价/涨停价={ratio:.3f})"
    if lianban >= 2:
        zt_flag = True
        note = f"{note} 连板{lianban}".strip()
    return zt_flag, lianban, fengdan, zhaban, note


def tech_screen(symbol, days=120, latest_price=None, quote=None, name=None):
    """个股技术面评分(0-30)与买点参考。latest_price为盘中最新价（可作买点基准）
    新增维度（2026-09-01）：涨停/近涨停判定、60日价格位置、长上影、量比"""
    hist = get_stock_hist(symbol, days=days)
    if hist is None or len(hist) < 40:
        return 0, {}
    df = _tech_indicators(hist)
    last = df.iloc[-1]
    prev = df.iloc[-2]
    score = 0
    notes = []

    close = float(last["收盘"])
    base = float(latest_price) if latest_price else close
    ma5, ma10, ma20, ma60 = (last["MA5"], last["MA10"], last["MA20"], last["MA60"])

    # ===== 涨停/近涨停判定（最高优先级：直接削分 + 提示不可追） =====
    zt_flag, lianban, fengdan, zhaban, zt_note = _zt_info(symbol, name=name, quote=quote)
    if zt_flag:
        # 近涨停：追高买不进且次日溢价风险高，重罚
        score -= 12
        notes.append(f"⚠️近涨停/连板({zt_note})不可追")
        if lianban >= 2:
            score -= 5
            notes.append(f"{lianban}连板过热")
    else:
        # 未涨停：盘中大涨但未封板，说明有分歧，轻罚
        if quote is not None:
            chg = quote.get("涨跌幅")
            if chg is not None and chg >= 7:
                score -= 4
                notes.append(f"当日+{chg:.0f}%未封板(分歧)")

    # 趋势（1-2周波段：MA10>MA20 视为趋势确立，权重拉满）
    if close > ma20 and ma5 > ma10:
        score += 12
        notes.append("多头结构")
    elif close > ma20:
        score += 8
        notes.append("站上MA20")
    else:
        notes.append("MA20下方")
    # 波段趋势中继：MA10>MA20 且现价贴近MA10（回踩不破），吃趋势延续
    if pd.notna(ma10) and pd.notna(ma20) and ma10 > ma20 and close >= ma10:
        score += 4
        notes.append("趋势中继(MA10>MA20)")

    # MACD
    if last["DIF"] > last["DEA"] and last["MACD"] > 0:
        score += 8
        notes.append("MACD多头")
    if prev["MACD"] <= 0 < last["MACD"]:
        score += 3
        notes.append("MACD金叉")

    # RSI 强势区
    rsi = last["RSI14"]
    if 50 <= rsi <= 80:
        score += 4
        notes.append(f"RSI{rsi:.0f}")
    elif rsi > 80:
        notes.append("RSI超买")

    # 量价：昨日放量上涨
    if len(df) > 1:
        v_ratio = last["成交量"] / df["成交量"].tail(5).mean() if last["成交量"] else 0
        if v_ratio > 1.5 and last["涨跌幅"] > 0:
            score += 3
            notes.append("放量上攻")

    # ===== 60日价格位置（低位启动加分 / 高位滞涨减分） =====
    if len(df) >= 60:
        hi60 = df["最高"].tail(60).max()
        lo60 = df["最低"].tail(60).min()
        if hi60 > lo60:
            pos = (close - lo60) / (hi60 - lo60)
            if pos < 0.5:
                score += 2
                notes.append(f"低位启动({pos:.0%})")
            elif pos > 0.85:
                score -= 3
                notes.append(f"高位滞涨({pos:.0%})")
            else:
                notes.append(f"位置{pos:.0%}")

    # ===== 长上影/冲高回落（当日） =====
    if quote is not None and quote.get("最高") and quote.get("现价"):
        hi = quote["最高"]
        cur = quote["现价"]
        opn = quote.get("今开") or close
        if hi > opn and (hi - cur) / (hi - opn) > 0.6 and (hi - cur) > 0:
            score -= 3
            notes.append("长上影冲高回落")
        elif hi - cur > 0 and (hi - cur) / max(hi, 1e-9) > 0.06:
            score -= 2
            notes.append("上影线偏长")

    # ===== 量比（腾讯盘口：当日量能 vs 近期） =====
    if quote is not None:
        lb = quote.get("量比")
        if lb is not None:
            if lb >= 1.5:
                score += 2
                notes.append(f"量比{lb:.1f}放量")
            elif lb <= 0.6:
                score -= 1
                notes.append(f"量比{lb:.1f}缩量")

    # 近5日涨幅（1-2周波段：2%-10%为波段起涨加分，10-20%观望，>20%过热剔除）
    chg5 = (close / df.iloc[-6]["收盘"] - 1) * 100 if len(df) >= 6 else 0
    if 2 <= chg5 <= 10:
        score += 3
        notes.append(f"5日+{chg5:.1f}%波段起涨")
    elif 10 < chg5 <= 20:
        notes.append(f"5日+{chg5:.1f}%已加速(观望)")
    elif chg5 > 20:
        score -= 6
        notes.append("短线涨幅过大(>20%)")

    # ===== 右侧追启动：放量突破20日平台（2026-09-03 小火炉确认右侧风格） =====
    _break20 = False
    if len(df) >= 25:
        prev20_hi = df["收盘"].iloc[-21:-1].max()   # 不含当日的前20日最高收盘
        if close > prev20_hi:
            v_ratio2 = last["成交量"] / df["成交量"].iloc[-21:-1].mean() if last["成交量"] else 0
            if v_ratio2 > 1.3:
                score += 3
                _break20 = True
                notes.append("放量突破20日新高(右侧启动)")

    # 买点参考（基准：最新价 > 昨收）
    buy_ref = {}
    if latest_price and base > close:
        notes.append("当日已大涨")
        buy_ref["回踩买点"] = round(base * 0.97, 2)
        buy_ref["突破买点"] = round(base * 1.02, 2)
        buy_ref["建议买价区间"] = f"{round(base * 0.97, 2)}-{round(base * 1.02, 2)}"
    else:
        if pd.notna(ma10):
            buy_ref["回踩买点"] = round(float(ma10), 2)  # 波段：回踩MA10（原MA5）
        buy_ref["突破买点"] = round(base * 1.02, 2)
        lo = min(base, buy_ref.get("回踩买点", base))
        buy_ref["建议买价区间"] = f"{round(float(lo), 2)}-{round(base * 1.02, 2)}"
    buy_ref["基准价"] = round(float(base), 2)
    buy_ref["止损价"] = round(base * 0.94, 2)  # -6%（1-2周波段）
    buy_ref["止盈1"] = round(base * 1.06, 2)   # +6% 减半
    buy_ref["止盈2"] = round(base * 1.10, 2)   # +10% 清仓

    # 技术面衍生 meta（供可展期分复用，避免重复拉K线）
    pos60 = None
    if len(df) >= 60:
        hi60 = df["最高"].tail(60).max()
        lo60 = df["最低"].tail(60).min()
        if hi60 > lo60:
            pos60 = (close - lo60) / (hi60 - lo60)
    vol5_ratio = None
    if len(df) >= 11:
        recent5_v = df["成交量"].tail(5).mean()
        before5_v = df["成交量"].iloc[-10:-5].mean()
        if before5_v > 0:
            vol5_ratio = recent5_v / before5_v
    _meta = {
        "close": float(close), "ma5": float(ma5) if pd.notna(ma5) else None,
        "ma10": float(ma10) if pd.notna(ma10) else None,
        "ma20": float(ma20) if pd.notna(ma20) else None,
        "ma60": float(ma60) if pd.notna(ma60) else None,
        "pos60": float(pos60) if pos60 is not None else None,
        "vol5_ratio": float(vol5_ratio) if vol5_ratio is not None else None,
        "break20": _break20, "chg5": float(chg5),
    }

    return max(0, min(30, score)), {"分": score, "说明": "; ".join(notes), "买点": buy_ref, "_meta": _meta}


def capital_score(symbol, market="sh", turnover=None):
    """资金面评分(0-25)。接口失败时用换手率活跃度兜底"""
    score = 0
    notes = []
    try:
        flow = get_stock_fund_flow(symbol, market)
        if flow is not None and not flow.empty:
            recent = flow.tail(5)
            inflow = pd.to_numeric(recent["主力净流入-净额"], errors="coerce").sum() / 1e8
            if inflow > 0:
                score += 15
                notes.append(f"5日主力+{inflow:.2f}亿")
                if inflow > 2:
                    score += 5
                    notes.append("流入显著")
            else:
                notes.append(f"5日主力{inflow:.2f}亿")
        else:
            raise ValueError("资金流数据为空")
    except Exception:
        # 兜底：按换手率给活跃度分
        if turnover is not None:
            if 3 <= turnover <= 15:
                score += 10
                notes.append(f"换手{turnover:.1f}%活跃")
            elif 15 < turnover <= 25:
                score += 6
                notes.append(f"换手{turnover:.1f}%偏高")
            else:
                score += 3
                notes.append(f"换手{turnover:.1f}%")
        else:
            score += 6
            notes.append("资金数据缺省(中性)")
    return min(25, score), notes


def _value_score(symbol, quote=None, name=None, in_hot_board=False):
    """价值面评分(0-30)。真实估值/盈利质量，替代原来的'基础20+热门+5'
    数据源：腾讯盘口(市盈率) + 东财个股信息(行业/市值)。接口不稳定时自动降级"""
    notes = []
    score = 0.0
    base = VAL_W["base"]  # 基础分 8（存在性）
    score += base
    notes.append(f"基准{int(base)}")

    pe = quote.get("市盈率") if quote else None
    pb = None
    # PB：个股信息接口（东财，平时可用）→ 百度估值源兜底
    info = {}
    try:
        info = get_stock_info(symbol)
        if "行业" in info and info["行业"]:
            notes = [f"行业:{info['行业']}"] + notes
        for key, v in info.items():
            if "市净率" in str(key):
                try:
                    pb = float(v)
                    break
                except (TypeError, ValueError):
                    pass
    except Exception:
        pass
    # 东财信息缺失 → 百度估值源兜底（PB + 历史分位）
    bd_val = {}
    if pb is None:
        bd_val = get_valuation_baidu(symbol)
        if "pb" in bd_val:
            pb = bd_val["pb"]
        # 东财 PE 缺失 → 百度 PE 兜底
        if pe is None and "pe" in bd_val:
            pe = bd_val["pe"]

    # ---- 市盈率（动态，腾讯）----
    if pe is not None and pe > 0:
        if VAL_PE_OK[0] < pe < VAL_PE_OK[1]:
            score += VAL_W["pe"]
            notes.append(f"PE{pe:.0f}合理")
        elif pe < VAL_PE_WARN[1]:
            notes.append(f"PE{pe:.0f}偏高(不加分不扣分)")
        else:
            score -= VAL_W["pe"] * 0.75
            notes.append(f"PE{pe:.0f}高估")
    elif pe is not None and pe < 0:
        score -= VAL_W["pe"] * 0.5
        notes.append("PE为负(亏损)")
    else:
        notes.append("PE数据缺省")

    # ---- 市净率（东财）----
    if pb is not None and pb > 0:
        if VAL_PB_OK[0] < pb < VAL_PB_OK[1]:
            score += VAL_W["pb"]
            notes.append(f"PB{pb:.1f}合理")
        elif pb < VAL_PB_WARN[1]:
            notes.append(f"PB{pb:.1f}偏高(不加分不扣分)")
        else:
            score -= VAL_W["pb"] * 0.8
            notes.append(f"PB{pb:.1f}高估")
    else:
        notes.append("PB数据缺省")

    # ---- 盈利质量（东财，平时可用）----
    roe = margin = growth = None
    for key, v in info.items():
        k = str(key)
        if "净资产收益率" in k or "ROE" in k.upper():
            try:
                roe = float(v)
            except (TypeError, ValueError):
                pass
        elif "毛利率" in k:
            try:
                margin = float(v)
            except (TypeError, ValueError):
                pass
        elif "净利润" in k and "同比" in k:
            try:
                growth = float(v)
            except (TypeError, ValueError):
                pass
    # 东财盈利质量缺失 → 百度财务指标兜底（ROE/毛利率/净利同比）
    if roe is None or margin is None or growth is None:
        fin = get_financial_indicators(symbol)
        if fin:
            if roe is None and "roe" in fin:
                roe = fin["roe"]
            if margin is None and "margin" in fin:
                margin = fin["margin"]
            if growth is None and "growth" in fin:
                growth = fin["growth"]
    if roe is not None and roe > 0:
        score += VAL_W["roe"]
        notes.append(f"ROE{roe:.0f}%")
    elif roe is not None and roe <= 0:
        score -= VAL_W["roe"]
        notes.append("ROE非正")
    else:
        notes.append("ROE缺省")
    if margin is not None and margin > 15:
        score += VAL_W["margin"] * 0.5
        notes.append(f"毛利率{margin:.0f}%")
    if growth is not None and growth > 15:
        score += VAL_W["growth"]
        notes.append(f"净利+{growth:.0f}%")
    elif growth is not None and growth < 0:
        score -= VAL_W["growth"]
        notes.append(f"净利{growth:.0f}%降")

    # ---- 行业（强势板块加分）----
    industry = None
    if "行业" in info and info["行业"]:
        industry = str(info["行业"])
    else:
        industry = get_industry_of(symbol, name=name)
    if in_hot_board:
        score += 3
        notes.append("属强势板块")

    score = max(0, min(30, score))
    return round(score, 1), notes, industry


def _extendable_score(meta=None, quote=None, in_hot_board=False, value_notes=None):
    """可展期分 0-20（独立显示，不计入主评分排名）
    意义：满10天展期评估时的"底气分"——短线若未走完，该票是否有转中长期(≤30天)的支撑。
    评分项（共20分）：
    - 中期趋势 intact (5)：MA10>MA20>MA60 且现价>MA20
    - 行业持续性 (4)：属当日强势板块 / RPS 中期主线
    - 估值安全边际 (4)：PE合理 + PB合理（复用价值面判词）
    - 基本面托底 (3)：ROE为正 + 净利增长
    - 量能健康 (2)：近5日均量 >= 前5日均量 80%
    - 位置不高 (2)：60日价格位置 < 70%（留中期空间）
    返回 (分, [原因])
    """
    score = 0
    reasons = []
    meta = meta or {}
    vn = " ".join(value_notes or [])

    # 1. 中期趋势 intact
    ma10, ma20, ma60 = meta.get("ma10"), meta.get("ma20"), meta.get("ma60")
    close = meta.get("close")
    if all(x is not None for x in (ma10, ma20, ma60)) and ma10 > ma20 > ma60 and close > ma20:
        score += 5
        reasons.append("MA10>MA20>MA60 中期多头")
    elif ma10 is not None and ma20 is not None and ma10 > ma20 and close > ma20:
        score += 3
        reasons.append("MA10>MA20")

    # 2. 行业持续性（强势板块种子）
    if in_hot_board:
        score += 4
        reasons.append("属强势板块")

    # 3. 估值安全（从价值面判词提取）
    if any("PE" in n and "合理" in n for n in (value_notes or [])):
        score += 2
        reasons.append("PE合理")
    if any("PB" in n and "合理" in n for n in (value_notes or [])):
        score += 2
        reasons.append("PB合理")

    # 4. 基本面托底
    if any("ROE" in n and "%" in n and "-" not in n.split("ROE")[1][:4] for n in (value_notes or [])):
        score += 2
        reasons.append("ROE为正")
    if "净利+" in vn:
        score += 1
        reasons.append("净利增长")

    # 5. 量能健康
    vr5 = meta.get("vol5_ratio")
    if vr5 is not None and vr5 >= 0.8:
        score += 2
        reasons.append(f"量能{vr5*100:.0f}%")

    # 6. 位置不高
    pos = meta.get("pos60")
    if pos is not None and pos < 0.7:
        score += 2
        reasons.append(f"60日位置{pos*100:.0f}%")

    return min(20, score), reasons


def score_stock(symbol, theme_bonus=0.0, in_hot_board=False, latest_price=None, turnover=None,
                quote=None, name=None):
    """综合评分 0-100。停牌股（最高=0）直接返回 None，由调用方跳过。"""
    # ===== 停牌过滤：腾讯盘口最高=0 → 当日无成交，非可交易状态 =====
    if quote and quote.get("最高", -1) == 0:
        return None
    market = _sina_symbol(symbol)[:2]
    tech, tech_detail = tech_screen(symbol, latest_price=latest_price, quote=quote, name=name)
    if not tech_detail or "分" not in tech_detail:
        tech_detail = {"分": tech, "说明": "技术数据不足", "买点": {}}
    cap, cap_notes = capital_score(symbol, market, turnover=turnover)
    # 价值面：真实估值/盈利质量 + 行业
    value_score, value_notes, industry = _value_score(symbol, quote=quote, name=name,
                                                      in_hot_board=in_hot_board)
    # 题材催化分（由板块判断传入）
    theme_score = min(15, theme_bonus)

    # 可展期分（0-20，独立显示不计入 total；技术面 meta 复用，无新增网络请求）
    try:
        ext_score, ext_reasons = _extendable_score(
            meta=tech_detail.get("_meta"), quote=quote, in_hot_board=in_hot_board,
            value_notes=value_notes)
    except Exception:
        ext_score, ext_reasons = 0, ["可展期分计算失败"]

    total = value_score + tech + cap + theme_score
    return {
        "symbol": symbol, "value": value_score, "tech": tech_detail,
        "capital": min(25, cap), "theme": theme_score, "total": round(total, 0),
        "buy": tech_detail.get("买点", {}), "行业": industry,
        "说明": "; ".join(value_notes),
        "可展期分": ext_score, "可展期理由": ext_reasons,
    }


def pick_top_stocks(snapshot, boards=None, n=3):
    """主流程：强势板块种子 + 快照候选 → 综合评分 → 涨停剔除/行业去重 → Top N"""
    pool = value_screen(snapshot, top_n=40)
    seeds = board_seed_stocks(boards, snapshot) if boards is not None else []
    hot_codes = set(seeds)

    # 候选池：种子股优先（属强势板块），再补充快照候选
    candidates = []
    seen = set()
    if "代码" in snapshot.columns:
        code_map = dict(zip(snapshot["代码"].astype(str), snapshot["名称"]))
        price_map = dict(zip(snapshot["代码"].astype(str), snapshot["最新价"]))
        chg_map = dict(zip(snapshot["代码"].astype(str), snapshot.get("涨跌幅")))
        hs_map = dict(zip(snapshot["代码"].astype(str), snapshot.get("换手率")))
    else:
        code_map = price_map = chg_map = hs_map = {}

    for code in seeds:
        if code not in seen and str(code).startswith(ALLOW_CODE_PREFIX):
            candidates.append({"代码": str(code), "来源": "板块种子", "名称": code_map.get(str(code), "")})
            seen.add(code)
    for _, row in pool.head(18).iterrows():
        code = str(row["代码"])
        if code not in seen:
            candidates.append({"代码": code, "来源": "快照候选", "名称": row.get("名称", "")})
            seen.add(code)

    # 批量拉腾讯盘口（涨停价/量比/PE/封单，一次请求）
    # 扩池到24只（强势市场中ZT剔除可达60%+，需保证剔除后仍有足够候选）
    cand_codes = [c["代码"] for c in candidates[:24]]
    quotes = get_realtime_quotes(cand_codes)

    results = []
    for c in candidates[:24]:
        symbol = c["代码"]
        try:
            price = price_map.get(symbol)
            hs = hs_map.get(symbol)
            q = quotes.get(symbol, {})
            # 最新价优先用腾讯盘口（更实时），缺省回退快照
            if q.get("现价"):
                price = q["现价"]
            r = score_stock(symbol, theme_bonus=8 if c["来源"] == "板块种子" else 4,
                            in_hot_board=c["来源"] == "板块种子",
                            latest_price=price if pd.notna(price) else None,
                            turnover=hs if pd.notna(hs) else None,
                            quote=q, name=c["名称"])
            if r is None:
                continue  # 停牌股跳过
            r["名称"] = c["名称"] or symbol
            r["现价"] = round(float(price), 2) if pd.notna(price) else None
            r["涨跌幅"] = round(float(q.get("涨跌幅", chg_map.get(symbol))), 2) if q.get("涨跌幅") is not None or pd.notna(chg_map.get(symbol)) else None
            # 涨停/连板状态透出（供去重与日报提示）
            zt_flag, lianban, fengdan, zhaban, zt_note = _zt_info(symbol, name=c["名称"], quote=q)
            r["近涨停"] = zt_flag
            r["连板数"] = lianban
            r["封板资金亿"] = round(fengdan, 2) if fengdan else 0
            r["炸板次数"] = zhaban
            results.append(r)
        except Exception:
            continue
        # 不在这里截断：必须让全部候选都评完，ZT剔除后再统一截断

    # ===== 涨停/连板剔除（好看不好买）——波段通道纪律 =====
    # 波段的"不追高"纪律：近涨停/连板≥2 的票留给短线通道处理
    if ZT_BAN:
        kept = []
        for r in results:
            if r.get("近涨停"):
                continue  # 近涨停直接剔除
            if ZT_LIMIT_5DAY and r.get("连板数", 0) >= 2:
                continue  # 连板≥2 高位过热
            kept.append(r)
        results = kept

    # ===== 行业去重：同行业最多 MAX_SAME_INDUSTRY 只 =====
    if MAX_SAME_INDUSTRY > 0:
        results.sort(key=lambda x: x["total"], reverse=True)
        ind_count = {}
        dedup = []
        for r in results:
            ind = r.get("行业")
            if ind:
                cnt = ind_count.get(ind, 0)
                if cnt >= MAX_SAME_INDUSTRY:
                    continue
                ind_count[ind] = cnt + 1
            dedup.append(r)
        results = dedup

    results.sort(key=lambda x: x["total"], reverse=True)
    return results[:n]


# ============ 短线通道（2026-09-05 双通道架构：情绪驱动选股） ============

def _lianban_from_stat(stat):
    """从涨停统计字段解析连板数（如 '2/1' = 2天1板 → 连板1）"""
    try:
        return int(str(stat).split("/")[1])
    except (IndexError, ValueError, TypeError):
        return 1


def _shortline_emotion_score(code, name, chg, hs, zhaban, lianban, quote,
                             industry, board_zt_count):
    """短线情绪评分（0-100），维度与波段综合评分完全独立
    - 基准 40
    - 涨幅动能（0-10）：涨幅占涨停幅度比例 0.7-0.95 最佳（冲板未封）
    - 换手率（0-10）：5-15% 最佳（活跃且未过热）
    - 炸板次数（0-8）：0次/1次最佳（首板刚或博弈充分）
    - 板块梯队（0-15）：同行业涨停家数（板块效应）
    - 量比（0-7）：≥2 放量承接
    - 回封动能（0-15）：现价距涨停价越近回封概率越高
    """
    score = 40.0
    notes = []

    # ① 涨幅动能
    zt_price = quote.get("涨停价") or 0
    prev_close = quote.get("昨收") or 0
    limit_pct = (zt_price / prev_close - 1) if (zt_price and prev_close) else 0.10
    ratio = (chg / 100) / limit_pct if limit_pct > 0 else 0
    if 0.7 <= ratio < 0.95:
        score += 10
        notes.append(f"冲板动能强({chg:.1f}%)")
    elif ratio >= 0.95:
        score += 8
        notes.append(f"贴板未封({chg:.1f}%)")
    elif 0.5 <= ratio < 0.7:
        score += 5
        notes.append(f"强势上攻({chg:.1f}%)")
    else:
        notes.append(f"开板回落({chg:.1f}%)")

    # ② 换手率
    if hs is not None and hs > 0:
        if 5 <= hs <= 15:
            score += 10
            notes.append(f"换手{hs:.1f}%活跃")
        elif 15 < hs <= 25:
            score += 6
            notes.append(f"换手{hs:.1f}%偏热")
        elif hs > 25:
            score += 2
            notes.append(f"换手{hs:.1f}%过热")
        elif hs >= 3:
            score += 4

    # ③ 炸板次数
    if zhaban == 0:
        score += 8
        notes.append("未炸板")
    elif zhaban == 1:
        score += 6
        notes.append("1炸博弈充分")
    elif zhaban == 2:
        score += 2
        notes.append("2炸")
    else:
        notes.append(f"{zhaban}炸筹码松")

    # ④ 板块梯队（同行业涨停家数）
    if board_zt_count >= 5:
        score += 15
        notes.append(f"{industry}板块涨停{board_zt_count}家效应强")
    elif board_zt_count >= 3:
        score += 10
        notes.append(f"{industry}板块涨停{board_zt_count}家")
    elif board_zt_count >= 1:
        score += 5
        notes.append(f"{industry}板块涨停{board_zt_count}家")
    else:
        notes.append("无板块梯队")

    # ⑤ 量比
    lb = quote.get("量比")
    if lb:
        if lb >= 2:
            score += 7
            notes.append(f"量比{lb:.1f}放量")
        elif lb >= 1.2:
            score += 4
        elif lb < 0.8:
            notes.append("缩量")
        else:
            score += 2

    # ⑥ 回封动能（距涨停价空间）
    price = quote.get("现价") or 0
    if price and zt_price:
        dist = 1 - price / zt_price
        if dist < 0.02:
            score += 12
            notes.append("贴板回封概率高")
        elif dist < 0.05:
            score += 8
            notes.append("近板")
        else:
            score += 3

    return min(100.0, score), "；".join(notes)


def pick_shortline_stocks(snapshot, n=2, exclude_codes=None):
    """短线通道：情绪驱动选股（2026-09-05 双通道架构）
    候选池（只收未封死的票，推荐即虚拟成交）：
    a. 炸板池：曾涨停后开板的票（回封博弈主战场）
    b. 涨停池炸板过的票（开板后回封，复查是否真封死）
    c. 快照涨幅榜：涨幅≥SHORTLINE_MIN_CHG 的冲高强势票（近涨停不剔，2026-09-10 回测：
       剔除≥9% 反而砍掉最赚钱的创业近涨停梯队，胜×盈亏 79.95→72.17）
    排除：ST/退、非白名单、停牌（盘口最高=0）、封死（现价≥涨停价×0.997）、
          连板>SHORTLINE_MAX_LIANBAN、波段通道已推荐（exclude_codes）
    评分：情绪评分 0-100（涨幅动能/换手/炸板/板块梯队/量比/回封动能）
    返回 list[dict]，字段与波段通道兼容（total=情绪分）
    """
    exclude = set(exclude_codes or [])
    cands = {}  # code -> {代码,名称,涨跌幅,换手率,炸板次数,连板数,行业,来源}

    # 涨停池（行业梯队统计 + 炸板回封票）
    zt_df = get_stock_zt_pool()
    board_zt_count = {}
    if zt_df is not None and not zt_df.empty:
        zt_df["代码"] = zt_df["代码"].astype(str).str.split(".").str[0]
        for ind, cnt in zt_df["所属行业"].value_counts().items():
            board_zt_count[str(ind)] = int(cnt)
        # 涨停池中炸板过的票（开板后回封，但需盘口复查是否真封死）
        for _, row in zt_df.iterrows():
            code = str(row["代码"])
            if (int(row.get("炸板次数", 0) or 0) >= 1 and code not in cands
                    and code.startswith(ALLOW_CODE_PREFIX)):
                cands[code] = {
                    "代码": code, "名称": row.get("名称", ""),
                    "涨跌幅": float(row.get("涨跌幅", 0) or 0),
                    "换手率": float(row.get("换手率", 0) or 0),
                    "炸板次数": int(row.get("炸板次数", 0) or 0),
                    "连板数": int(row.get("连板数", 1) or 1),
                    "行业": str(row.get("所属行业", "") or ""),
                    "来源": "涨停池(炸板回封)",
                }

    # 炸板池（曾涨停后开板——回封博弈主战场）
    zb_df = get_stock_zb_pool()
    if zb_df is not None and not zb_df.empty:
        zb_df["代码"] = zb_df["代码"].astype(str).str.split(".").str[0]
        for _, row in zb_df.iterrows():
            code = str(row["代码"])
            if code not in cands and code.startswith(ALLOW_CODE_PREFIX):
                cands[code] = {
                    "代码": code, "名称": row.get("名称", ""),
                    "涨跌幅": float(row.get("涨跌幅", 0) or 0),
                    "换手率": float(row.get("换手率", 0) or 0),
                    "炸板次数": int(row.get("炸板次数", 0) or 0),
                    "连板数": _lianban_from_stat(row.get("涨停统计", "1/1")),
                    "行业": str(row.get("所属行业", "") or ""),
                    "来源": "炸板池(开板)",
                }

    # 快照冲高强势票（涨幅≥SHORTLINE_MIN_CHG；近涨停不剔——2026-09-10 回测：创业近涨停梯队是利润核心）
    if snapshot is not None and not snapshot.empty:
        snap = snapshot.copy()
        snap["代码"] = snap["代码"].astype(str).str.replace(r"\.0$", "", regex=True)
        snap = snap[~snap["名称"].str.contains("ST|退", na=False)]
        snap = snap[snap["代码"].str.startswith(ALLOW_CODE_PREFIX)]
        snap = snap[pd.to_numeric(snap["涨跌幅"], errors="coerce").fillna(0) >= SHORTLINE_MIN_CHG]
        snap = snap.sort_values("成交额", ascending=False)
        for _, row in snap.head(15).iterrows():
            code = str(row["代码"])
            if code not in cands:
                cands[code] = {
                    "代码": code, "名称": row.get("名称", ""),
                    "涨跌幅": float(row.get("涨跌幅", 0) or 0),
                    "换手率": float(row.get("换手率", 0) or 0),
                    "炸板次数": 0, "连板数": 0,
                    "行业": "",
                    "来源": "快照(冲高强势)",
                }

    if not cands:
        return []

    # 批量拉腾讯盘口复查（现价/停牌/封死判定）
    codes = list(cands.keys())
    quotes = get_realtime_quotes(codes)

    results = []
    for code, c in cands.items():
        try:
            if code in exclude:
                continue
            q = quotes.get(code, {})
            if not q:
                continue
            price = q.get("现价") or 0
            zt_price = q.get("涨停价") or 0
            if price <= 0 or q.get("最高", -1) == 0:
                continue  # 停牌/无行情
            # 连板上限：3板以上高位不追
            if c["连板数"] > SHORTLINE_MAX_LIANBAN:
                continue
            # 封死判定：只推未封死的票
            if zt_price and price >= zt_price * 0.997:
                continue
            # 快照来源的票需复查：涨幅够但已回落到近涨停以下的才留（避免收进慢涨票）
            if c["来源"].startswith("快照") and q.get("涨跌幅", 0) < SHORTLINE_MIN_CHG:
                continue
            # 上影过滤（2026-09-09 回测：冲高回落>3% 剔除，胜率 45.0→47.2%、盈亏比 1.59→1.69）
            hi_q = float(q.get("最高", 0) or 0)
            if hi_q > 0 and (hi_q - price) / hi_q > SHORTLINE_MAX_UPPER_SHADOW:
                continue
            # 量比门槛（2026-09-09：缩量冲高不参与）
            if SHORTLINE_MIN_VOL_RATIO > 0 and float(q.get("量比", 0) or 0) < SHORTLINE_MIN_VOL_RATIO:
                continue
            # 剔除昨日涨停票（2026-09-09：避免高位接力，昨日涨停今日溢价风险大）
            if SHORTLINE_EXCLUDE_YESTERDAY_ZT:
                h = get_stock_hist(code, days=10)
                if h is not None and len(h) >= 3:
                    h = h.sort_values("日期").reset_index(drop=True)
                    c1, c2 = float(h["收盘"].iloc[-2]), float(h["收盘"].iloc[-3])
                    zt_pct = 19.5 if code.startswith("30") else 9.5
                    if c2 > 0 and (c1 / c2 - 1) * 100 >= zt_pct:
                        continue

            chg = q.get("涨跌幅", c["涨跌幅"])
            hs = q.get("换手率", c["换手率"])
            em_score, em_notes = _shortline_emotion_score(
                code, c["名称"], chg, hs, c["炸板次数"], c["连板数"], q,
                c["行业"], board_zt_count.get(c["行业"], 0))

            # 短线买点：推荐即成交（实时价），风控按短线参数
            base = round(price, 2)
            r = {
                "symbol": code, "名称": c["名称"], "现价": base, "涨跌幅": round(chg, 2),
                "total": round(em_score, 0),  # 情绪分
                "value": None, "tech": {"分": em_score, "说明": em_notes},
                "capital": None, "theme": None,
                "buy": {
                    "基准价": base,
                    "止损价": round(base * (1 + SHORT_STOP_LOSS), 2),
                    "止盈1": round(base * (1 + SHORT_TAKE_PROFIT_1), 2),
                    "止盈2": round(base * (1 + SHORT_TAKE_PROFIT_2), 2),
                    "突破买点": base, "回踩买点": "-",
                    "建议买价区间": f"{base}",
                },
                "行业": c["行业"], "说明": em_notes,
                "strategy_tag": "短线",
                "连板数": c["连板数"], "炸板次数": c["炸板次数"],
                "近涨停": False, "来源": c["来源"],
            }
            results.append(r)
        except Exception:
            continue

    # 及格线过滤
    results = [r for r in results if r["total"] >= SHORTLINE_MIN_SCORE]

    # 行业去重（与波段一致）
    if MAX_SAME_INDUSTRY > 0:
        ind_count = {}
        dedup = []
        for r in sorted(results, key=lambda x: x["total"], reverse=True):
            ind = r.get("行业")
            if ind:
                cnt = ind_count.get(ind, 0)
                if cnt >= MAX_SAME_INDUSTRY:
                    continue
                ind_count[ind] = cnt + 1
            dedup.append(r)
        results = dedup

    results.sort(key=lambda x: x["total"], reverse=True)
    return results[:n]


def pick_quality(picks, min_score=60):
    """推荐质量评估：返回 (是否健康, 原因str)
    当涨停潮/候选质量不足导致推荐评分普遍偏低时提示，避免误导实盘"""
    if not picks:
        return False, "今日无合格候选（涨停剔除+行业去重后为空）"
    low = [p for p in picks if p["total"] < min_score]
    if len(low) == len(picks):
        return False, f"全部推荐评分<{min_score}（今日强势板块普涨涨停，优质候选被涨停剔除，可操作性不足）"
    if low:
        names = ", ".join(f"{p['名称']}{p['total']:.0f}" for p in low)
        return False, f"{len(low)}/3 只评分<{min_score}（{names}），建议观望或仅轻仓参与最高分"
    return True, "推荐质量健康"


# ============ 低位启动观察池（收盘复盘"本周关注"） ============

def low_pos_watch(snapshot=None, top_n=3, exclude_codes=None):
    """选出"低位 + 趋势启动初现"的股票（本周关注观察池）。
    定位：已从高位回落充分、60日价格位置处于低位，且出现底部放量/站上MA20/MACD金叉等启动信号，
    但尚未大涨（5日涨幅温和）——属于"有开始上涨趋势"的左侧/右侧临界股，供用户本周跟踪。

    条件（全部满足才入选）：
    - 白名单 60/00/30（无科创板/北交所权限）、非ST、非停牌
    - 60日价格位置 < 0.40（低位）
    - 启动信号 ≥2 个：站上MA20 / MACD金叉 / 近5日温和放量 / 5日涨幅2%-12%
    - 当日非涨停/近涨停（现价 < 涨停价*0.95）
    - 成交额 > 1.5亿（流动性），当日涨跌幅 -4%~+4%（不追当日大涨）
    - 行业去重（同行业最多1只）
    返回 list[dict]：代码/名称/现价/60日位置/启动信号/5日涨幅/买点参考/止损价/行业/关注逻辑
    """
    if snapshot is None:
        snapshot = get_market_snapshot()
    if snapshot is None or snapshot.empty:
        return []
    exclude = set(exclude_codes or [])
    df = snapshot.copy()
    df["代码"] = df["代码"].astype(str).str.replace(r"\.0$", "", regex=True)
    # 基础过滤：白名单 / 非ST / 有价
    df = df[df["代码"].str.match(r"^(?:" + "|".join(ALLOW_CODE_PREFIX) + ")", na=False)]
    df = df[~df["名称"].str.contains("ST|退", na=False)]
    df = df[df["最新价"].notna() & (df["最新价"] > 0)]
    # 流动性 + 当日温和
    df = df[pd.to_numeric(df["成交额"], errors="coerce").fillna(0) > 1.5e8]
    if "涨跌幅" in df.columns:
        # 2026-09-11 修复：放开涨停——低位+首板涨停正是最强启动信号，
        # 原-4%~+4%过滤把"涨停+放量启动"当过热误杀（光电股份9-11 10%涨停3倍量未入选）。
        # 保留下跌端过滤（-4%以下仍剔除，深跌非蓄势），上涨端只挡一字板（-1%~+1%开盘即封）。
        df = df[df["涨跌幅"] >= -4]
    # 活跃度排序取前 80 只做技术扫描（控制耗时）
    if "换手率" in df.columns:
        df["_liq"] = df["换手率"].fillna(0).clip(0, 15)
        df = df.sort_values("_liq", ascending=False)
    cand = df.head(80)

    picks = []
    # 批量拉腾讯盘口（涨停价判定用）
    codes = cand["代码"].tolist()
    quotes = get_realtime_quotes(codes) if codes else {}
    for _, row in cand.iterrows():
        code = str(row["代码"])
        if code in exclude:
            continue
        name = str(row.get("名称", code))
        try:
            hist = get_stock_hist(code, days=120)
            if hist is None or len(hist) < 60:
                continue
            ind = _tech_indicators(hist)
            last = ind.iloc[-1]
            close = float(last["收盘"])
            # 60日价格位置
            hi60 = ind["最高"].tail(60).max()
            lo60 = ind["最低"].tail(60).min()
            if hi60 <= lo60:
                continue
            pos = (close - lo60) / (hi60 - lo60)
            if pos >= 0.40:  # 必须低位
                continue
            # ===== 前兆筛选（2026-09-10 转化研究落地：剔劣三件套）=====
            # 研究：低位池→趋势池转化率 10.5%。成功转化票启动前形态：
            #   横盘紧凑（振幅<15%/波动<4%）+ 贴近60日高点（距>-22%）+ 未提前上涨
            # 剔劣三件套 → 转化率 16.1%（样本 16743→3988，足量）
            amp20 = (ind["收盘"].tail(20).max() - ind["收盘"].tail(20).min()) / ind["收盘"].tail(20).mean()
            std20 = ind["收盘"].tail(20).std() / ind["收盘"].tail(20).mean()
            dist60 = (close / hi60 - 1) * 100
            # 当日涨幅（涨停/大涨判定）
            try:
                _dchg_now = float(last["涨跌幅"]) if last.get("涨跌幅") is not None else (
                    (close / float(ind.iloc[-2]["收盘"]) - 1) * 100 if len(ind) >= 2 else 0)
            except (ValueError, TypeError, KeyError):
                _dchg_now = 0.0
            _is_burst = _dchg_now >= 7   # 涨停/放量大涨 = 启动确认，非蓄势期
            if amp20 >= LOW_POS_MAX_20D_AMP:
                continue          # 波动过大：蓄势期混乱，启动概率低
            if std20 >= LOW_POS_MAX_20D_STD:
                continue          # 收盘波动率过高：不稳
            # 2026-09-11 修复：当日涨停/大涨（≥7%）跳过 dist60 深跌剔除。
            # 原逻辑 dist60<=-22% 一刀切剔除"深跌远离前高" → 光电股份9-11 涨停突破近10日新高
            # （60日高33.55是2个月前的深跌顶）被误判"下行通道非蓄势"。
            # 但涨停放量=启动确认，不再属于"蓄势前兆"路径 → 不受此约束。
            if not _is_burst and dist60 <= LOW_POS_MIN_DIST_60D_HIGH:
                continue          # 非启动日：深跌远离前高仍在下行通道，非蓄势
            # 位置/成交额最优区间（2026-09-12 升级为硬过滤：研究 17.9%/18.2% 即硬过滤口径）
            amt = float(row.get("成交额", 0)) if not pd.isna(row.get("成交额", 0)) else 0
            pos_bonus = 1 if LOW_POS_BEST_POS[0] <= pos < LOW_POS_BEST_POS[1] else 0
            amt_bonus = 1 if LOW_POS_BEST_AMOUNT[0] <= amt < LOW_POS_BEST_AMOUNT[1] else 0
            if LOW_POS_HARD_POS_AMOUNT and not _is_burst:
                # 蓄势前兆路径：位置/成交额必须落在最优区间（硬过滤）
                if not (LOW_POS_BEST_POS[0] <= pos < LOW_POS_BEST_POS[1]):
                    continue   # 位置偏离最优区间（<0.20 深跌 / 0.40 已不在低位）
                if not (LOW_POS_BEST_AMOUNT[0] <= amt < LOW_POS_BEST_AMOUNT[1]):
                    continue   # 成交额偏离 2-8 亿（太小流动性差 / 太大已非中小盘弹性票）
            # 启动确认路径（≥7%）：跳过位置/成交额约束，仅保留加分用于排序
            # 启动信号计数
            signals = []
            ma20 = last["MA20"]
            ma5 = last["MA5"]
            if close > ma20:
                signals.append("站上MA20")
            if last["DIF"] > last["DEA"]:
                signals.append("MACD多头")
            v_ratio = float(last["成交量"]) / ind["成交量"].tail(5).mean() if last["成交量"] else 0
            if v_ratio > 1.2:
                signals.append(f"放量{v_ratio:.1f}x")
            chg5 = (close / float(ind.iloc[-6]["收盘"]) - 1) * 100 if len(ind) >= 6 else 0
            if 2 <= chg5 <= 12:
                signals.append(f"5日+{chg5:.1f}%")
            # 2026-09-11 修复：当日涨停/大涨作为强启动信号直接计入（chg5 上限 12 会卡掉涨停当天）
            if last.get("涨跌幅") is not None:
                _dchg = float(last["涨跌幅"])
            else:
                _dchg = (close / float(ind.iloc[-2]["收盘"]) - 1) * 100 if len(ind) >= 2 else 0
            if _dchg >= 7:
                signals.append(f"当日+{_dchg:.1f}%")
            if len(signals) < 2:
                continue
            # 当日近涨停剔除
            # 2026-09-11 修复：涨停≠剔。低位+首板涨停放量 = 最强启动信号（光电股份9-11）。
            # 原 price/zt>=0.95 一刀切剔除 → 换手板被误杀。
            # 新口径：只剔"一字板/秒板"（今开即涨停价且封单>0，无换手空间无法介入），
            #       换手板（开盘低于涨停、盘中拉起封板）保留入选。
            q = quotes.get(code, {})
            price = q.get("现价") or close
            zt_price = q.get("涨停价")
            is_zt = price and zt_price and price / zt_price >= ZT_THRESHOLD
            if is_zt:
                open_px = q.get("今开") or float(last.get("开盘", 0)) if last is not None else 0
                try:
                    open_px = float(open_px)
                except (ValueError, TypeError):
                    open_px = 0
                is_yizi = open_px > 0 and open_px >= zt_price * 0.995   # 开盘即涨停
                if is_yizi:
                    continue                   # 一字板：无法参与，剔除
                if "当日+" not in "、".join(signals):
                    signals.append(f"涨停{_dchg:+.1f}%")   # 换手板：标注启动属性
            # 行业：优先东财三级名（与板块热度精确匹配），兜底原逻辑
            industry = get_industry_em(code) or get_industry_of(code, name=name) or "未知"
            # 行业热度排序加分（2026-09-10 归因落地）：近5日板块涨幅前15上榜天数
            # 归因结论：热度对转化率无增益（10.5→11.2%），但热度>=3 组的7日胜率显著更高
            # （48.3% vs 46.3%），且"前兆+热度"叠加后7日胜率54.2%（基线45.5%）。
            # 低位池的定位是"埋伏真启动"（转化率优先）→ 热度仅做排序加分不硬过滤。
            _h_tag, _h_rank, _h_tot, _h_last = industry_heat_status_simple(industry, window=5, top_n=15)
            if LOW_POS_HEAT_BONUS:
                heat_bonus = 1 if _h_tag == "🔥主线" else (0 if LOW_POS_HEAT_MAIN_ONLY else (0.5 if _h_tag == "🌤升温" else 0))
            else:
                heat_bonus = 0
            # 买点参考（收盘价基准，1-2周波段）
            buy_lo = round(min(close, float(ma5)) * 0.99, 2)
            buy_hi = round(close * 1.03, 2)
            picks.append({
                "代码": code, "名称": name, "现价": round(float(price), 2),
                "60日位置": round(pos, 2), "信号": "、".join(signals),
                "5日涨幅": round(chg5, 1), "行业": industry,
                "买点区间": f"{buy_lo}-{buy_hi}",
                "止损价": round(close * 0.94, 2),
                "关注逻辑": f"60日低位({pos:.0%})，{'、'.join(signals)}，横盘蓄势紧凑、贴近60日高点，趋势启动初现可跟踪",
                "_pos_bonus": pos_bonus, "_amt_bonus": amt_bonus, "_heat_bonus": heat_bonus,
            })
        except Exception:
            continue

    # 排序：信号数优先，其次位置/成交额/行业热度加分（前兆+热度研究：最优区间加分）
    picks.sort(key=lambda x: (-len(x["信号"].split("、")),
                              -(x.get("_pos_bonus", 0) + x.get("_amt_bonus", 0) + x.get("_heat_bonus", 0)),
                              x["60日位置"]))
    if MAX_SAME_INDUSTRY > 0:
        ind_count = {}
        dedup = []
        for r in picks:
            ind = r["行业"]
            cnt = ind_count.get(ind, 0)
            if cnt >= MAX_SAME_INDUSTRY:
                continue
            ind_count[ind] = cnt + 1
            dedup.append(r)
        picks = dedup
    return picks[:top_n]


# ============ 右侧趋势通道：pick_trend_stocks()（2026-09-05 双策略分立） ============

def pick_trend_stocks(snapshot=None, boards=None, n=2, exclude_codes=None):
    """右侧趋势选股：专门识别横盘很久底部放量、突破平台的右侧确认票。
    定位：中等市值/大盘股，60日低位，横盘整理后放量突破，MA/MACD/RSI共振确认。
    每天最多 n 只（默认2只）。

    选股标准（需同时满足）：
    1. 基础：白名单60/00/30，非ST，非停牌，成交额>1.5亿
    2. 60日价格位置 < TREND_MIN_60D_POS（<45%，低位）
    3. 横盘特征：近20日振幅<TREND_MAX_20D_AMPLITUDE，收盘价标准差/均价<TREND_MAX_20D_STD_RATIO
    4. 放量突破：今日成交量≥20日均量×TREND_BREAKOUT_VOL_RATIO(1.5倍)
    5. 突破阳线：收盘突破近20日/60日最高收盘，涨幅在TREND_BREAKOUT_CHG_RANGE(3-7%)
    6. MA共振：close > MA10 > MA20 或 MA5>MA10>MA20（多头排列）
    7. MACD共振：DIF>DEA 且 MACD柱>0 或 近3日内金叉
    8. RSI健康：RSI(14) 在 40-70 区间（不过热）
    9. 流通市值≥TREND_MIN_MARKET_CAP（30亿）

    风控：止损-6%，止盈+6%/+10%，趋势完好可展期最长60天，目标2-4周。
    """
    if not TREND_ENABLED:
        return []
    if snapshot is None:
        snapshot = get_market_snapshot()
    if snapshot is None or snapshot.empty:
        return []
    exclude = set(exclude_codes or [])

    df = snapshot.copy()
    df["代码"] = df["代码"].astype(str).str.replace(r"\.0$", "", regex=True)
    # 白名单/非ST/有价
    df = df[df["代码"].str.match(r"^(?:" + "|".join(ALLOW_CODE_PREFIX) + ")", na=False)]
    df = df[~df["名称"].str.contains("ST|退", na=False)]
    df = df[df["最新价"].notna() & (df["最新价"] > 0)]
    # 成交额过滤
    df = df[pd.to_numeric(df["成交额"], errors="coerce").fillna(0) > TREND_MIN_AMOUNT]
    # 活跃度排序取前100只做技术扫描
    if "换手率" in df.columns:
        df["_liq"] = df["换手率"].fillna(0).clip(0, 15)
        df = df.sort_values("_liq", ascending=False)
    cand = df.head(100)

    picks = []
    codes = cand["代码"].tolist()
    quotes = get_realtime_quotes(codes) if codes else {}

    for _, row in cand.iterrows():
        code = str(row["代码"])
        if code in exclude:
            continue
        try:
            hist = get_stock_hist(code, days=90)
            if hist is None or len(hist) < 60:
                continue
            ind = _tech_indicators(hist)
            last = ind.iloc[-1]
            close = float(last["收盘"])
            ma5 = float(last["MA5"])
            ma10 = float(last["MA10"])
            ma20 = float(last["MA20"])
            vol = float(last["成交量"])
            avg_vol20 = float(last.get("AVG_VOL20", 0))
            if avg_vol20 <= 0:
                continue
            q = quotes.get(code, {})
            if not q:
                continue
            price = q.get("现价") or close
            zt_price = q.get("涨停价") or 0
            if price <= 0 or q.get("最高", -1) == 0:
                continue  # 停牌
            # 近涨停剔除（避免追高）
            if zt_price and price >= zt_price * ZT_THRESHOLD:
                continue

            chg_today = q.get("涨跌幅", 0)
            # 条件2：60日价格位置<70%（2026-09-09 v3 规则。
            # 原值0.45与"突破20/60日新高"逻辑互斥——收盘创60日新高则位置必为100%。
            # 修复 2026-09-08 bug：原代码 last.get("位置60D", 0.5) 读的是从未生产的字段，恒为 0.5）
            h60 = hist.tail(60)
            lo60, hi60 = float(h60["收盘"].min()), float(h60["收盘"].max())
            pos60 = (close - lo60) / (hi60 - lo60) if hi60 > lo60 else 0.5
            if pos60 >= TREND_MIN_60D_POS or pos60 < TREND_MIN_60D_POS_LOW:
                # 2026-09-09：位置≥70% 追高；<30% 深跌弱势、趋势未确认（回测证伪率高）
                continue
            # 条件3：横盘特征
            recent20 = hist.tail(20)
            amp20 = (recent20["收盘"].max() - recent20["收盘"].min()) / recent20["收盘"].mean()
            std_ratio = recent20["收盘"].std() / recent20["收盘"].mean()
            if amp20 >= TREND_MAX_20D_AMPLITUDE or std_ratio >= TREND_MAX_20D_STD_RATIO:
                continue
            # 条件4：放量突破
            if vol < avg_vol20 * TREND_BREAKOUT_VOL_RATIO:
                continue
            # 条件5：站上MA20（2026-09-09 v3 规则：删除原"突破20/60日最高收盘"——
            # 与60日低位条件互斥，是趋势策略长期0出票的根因）
            if close <= ma20:
                continue
            chg_min, chg_max = TREND_BREAKOUT_CHG_RANGE
            if not (chg_min * 100 <= chg_today <= chg_max * 100):
                continue
            # 条件6：MA多头排列
            ma_ok = (close > ma10 > ma20) or (ma5 > ma10 > ma20)
            if not ma_ok:
                continue
            # 条件7：MACD
            dif = float(last.get("DIF", 0))
            dea = float(last.get("DEA", 0))
            macd_hist = float(last.get("MACD", 0))
            macd_ok = (dif > dea and macd_hist > 0)  # MACD在水上
            # 近3日金叉备用
            if not macd_ok and len(ind) >= 4:
                prev3 = ind.iloc[-4]
                if float(prev3.get("DIF", 0)) <= float(prev3.get("DEA", 0)) and dif > dea:
                    macd_ok = True  # 近3日内金叉
            if not macd_ok:
                continue
            # 条件8：RSI健康区间（修复 2026-09-08 bug：字段名是 RSI14，原读 "RSI" 恒为默认 50）
            rsi = float(last.get("RSI14", 50))
            if not (40 <= rsi <= 70):
                continue
            # 条件9：流通市值（用成交额/换手率粗估，或跳过）
            # 行业
            industry = get_industry_of(code, name=str(row.get("名称", code))) or "未知"
            # 买点：突破阳线低点~收盘价
            low20 = recent20["最低"].min()
            buy_lo = round(max(low20, close * 0.98), 2)
            buy_hi = round(close * 1.01, 2)
            # 趋势独立风控（修复 2026-09-08 bug：此前错误复用短线 -4%/+5%/+8%）
            stop_loss = round(close * (1 + TREND_STOP_LOSS), 2)
            tp1 = round(close * (1 + TREND_TAKE_PROFIT_1), 2)
            tp2 = round(close * (1 + TREND_TAKE_PROFIT_2), 2)
            hold_min, hold_max = TREND_HOLD_DAYS
            signals = []
            if close > ma10 > ma20:
                signals.append("MA多头")
            elif ma5 > ma10 > ma20:
                signals.append("均线多头")
            if dif > dea and macd_hist > 0:
                signals.append("MACD水上")
            elif macd_ok:
                signals.append("MACD金叉")
            signals.append(f"放量{vol/avg_vol20:.1f}倍")
            signals.append(f"RSI{rsi:.0f}")
            picks.append({
                "symbol": code, "名称": str(row.get("名称", code)),
                "现价": round(price, 2), "涨跌幅": round(chg_today, 2),
                "total": 75,  # 趋势票固定基准分
                "value": None, "tech": {"分": 75, "说明": "、".join(signals)},
                "capital": None, "theme": None,
                "buy": {
                    "基准价": round(close, 2),
                    "止损价": stop_loss, "止盈1": tp1, "止盈2": tp2,
                    "突破买点": buy_lo, "回踩买点": buy_hi,
                    "建议买价区间": f"{buy_lo}-{buy_hi}",
                },
                "行业": industry,
                "说明": f"横盘突破，{'、'.join(signals)}，目标持仓{hold_min}-{hold_max}天",
                "strategy_tag": "趋势",
                "近涨停": False, "来源": "右侧趋势放量突破",
                "信号": "、".join(signals),
                "横盘幅度": f"{amp20:.1%}",
                "放量倍数": round(vol / avg_vol20, 1),
                "量比": round(vol / avg_vol20, 1),
                "60日位置": round(pos60, 2),
            })
        except Exception:
            continue

    # 行业去重
    if MAX_SAME_INDUSTRY > 0:
        ind_count = {}
        dedup = []
        for r in picks:
            ind = r.get("行业", "未知")
            cnt = ind_count.get(ind, 0)
            if cnt >= MAX_SAME_INDUSTRY:
                continue
            ind_count[ind] = cnt + 1
            dedup.append(r)
        picks = dedup

    # 排序优选（2026-09-09）：信号冲突时按「60日位置低优先 + 量比大优先」
    picks.sort(key=lambda x: (x.get("60日位置", 1), -x.get("量比", 0)))
    return picks[:n]


# ============ 冰点抄底子模块：bottom_fishing_picks()（2026-09-05 双策略分立） ============

def bottom_fishing_picks(snapshot=None, temp=30, board_zt_count=None, n=2, exclude_codes=None, temp_min=None):
    """冰点抄底选股：温度≤35且涨停家数极少时，启动短线激进冰点抄底子模块。
    定位：短线激进子模块，市场情绪冰点期选被错杀的强势票（并非选超跌垃圾票）。
    选的是"质地好但被大盘拖累错杀"的票——近期强势（有净流入/高换手）但随大盘补跌，
    等待情绪修复时弹性最大。

    条件（需同时满足）：
    1. 白名单60/00/30，非ST，非停牌，成交额>1亿
    2. 近5日涨幅 > 0（近期相对强势，未随大盘全面崩盘）
    3. 近10日有资金净流入迹象（净流入>0 或 换手率>3%）
    4. 今日跌幅 > 大盘跌幅（跑输大盘，属于被错杀）
    5. 当日非涨停（避免选到反弹涨停的短线客）
    6. RSI(14) 在 30-50 区间（超卖但未极度超卖，有反弹空间）
    7. 行业去重，最多同行业1只

    风控：止损-5%，止盈+5%/+8%，持股不超过5天（短线本质）。
    返回 list[dict]，字段格式同 pick_shortline_stocks()。

    2026-09-11 修复：新增 temp_min 参数（默认 SHORTLINE_TEMP_MIN=50）。
    冰点抄底同属短线通道，温度<50 不得出票（弱势期短线期望为负），
    与仓位档位（温度<35→0%避险）联动，杜绝「避险档还推新票」的矛盾。
    """
    if not BOTTOM_FISHING_ENABLED:
        return []
    if temp > BOTTOM_FISHING_TEMP_MAX:
        return []
    # 2026-09-11 修复：冰点抄底也受短线温度闸门约束（默认与常规短线一致 ≥50）
    if temp_min is None:
        from config import SHORTLINE_TEMP_MIN
        temp_min = SHORTLINE_TEMP_MIN
    if temp < temp_min:
        return []
    if snapshot is None:
        snapshot = get_market_snapshot()
    if snapshot is None or snapshot.empty:
        return []
    exclude = set(exclude_codes or [])
    bzt = board_zt_count or {}

    df = snapshot.copy()
    df["代码"] = df["代码"].astype(str).str.replace(r"\.0$", "", regex=True)
    df = df[df["代码"].str.match(r"^(?:" + "|".join(ALLOW_CODE_PREFIX) + ")", na=False)]
    df = df[~df["名称"].str.contains("ST|退", na=False)]
    df = df[df["最新价"].notna() & (df["最新价"] > 0)]
    # 流动性
    df = df[pd.to_numeric(df["成交额"], errors="coerce").fillna(0) > 1e8]
    # 今日下跌（非上涨非涨停）
    df = df[df["涨跌幅"].astype(str).apply(lambda x: float(x or 0) < 0)]
    # 活跃度排序取前80只
    if "换手率" in df.columns:
        df["_liq"] = df["换手率"].fillna(0).clip(0, 15)
        df = df.sort_values("_liq", ascending=False)
    cand = df.head(80)

    picks = []
    codes = cand["代码"].tolist()
    quotes = get_realtime_quotes(codes) if codes else {}

    for _, row in cand.iterrows():
        code = str(row["代码"])
        if code in exclude:
            continue
        try:
            hist = get_stock_hist(code, days=40)
            if hist is None or len(hist) < 20:
                continue
            ind = _tech_indicators(hist)
            last = ind.iloc[-1]
            close = float(last["收盘"])
            chg5 = (close / float(ind.iloc[-6]["收盘"]) - 1) * 100 if len(ind) >= 6 else 0
            chg10 = (close / float(ind.iloc[-11]["收盘"]) - 1) * 100 if len(ind) >= 11 else 0
            hs = float(row.get("换手率", 0))
            q = quotes.get(code, {})
            if not q:
                continue
            price = q.get("现价") or close
            zt_price = q.get("涨停价") or 0
            if price <= 0 or q.get("最高", -1) == 0:
                continue
            # 近涨停剔除
            if zt_price and price >= zt_price * ZT_THRESHOLD:
                continue
            chg_today = q.get("涨跌幅", float(row.get("涨跌幅", 0)))
            # 条件2：近5日强势（相对大盘补跌，非主动下跌）
            if chg5 <= 0:
                continue
            # 条件3：资金面（换手率）
            if hs < 2.0:
                continue
            # 条件5：当日非涨停
            if zt_price and price >= zt_price * 0.97:
                continue
            # 条件6：RSI区间（修复 2026-09-08 bug：同趋势策略，字段应为 RSI14）
            rsi = float(last.get("RSI14", 50))
            if not (25 <= rsi <= 50):
                continue
            # 条件4：跑输大盘（今日跌幅大于指数跌幅，简化用平均市场跌幅-2%）
            # 冰点期大盘弱势，个股只要相对强势且超卖即可
            industry = get_industry_of(code, name=str(row.get("名称", code))) or "未知"
            base = round(price, 2)
            stop_loss = round(base * 0.95, 2)
            tp1 = round(base * 1.05, 2)
            tp2 = round(base * 1.08, 2)
            em_notes = f"冰点抄底 RSI={rsi:.0f} 近5日+{chg5:.1f}%被大盘拖累错杀"
            picks.append({
                "symbol": code, "名称": str(row.get("名称", code)),
                "现价": base, "涨跌幅": round(chg_today, 2),
                "total": 65,
                "value": None, "tech": {"分": 65, "说明": em_notes},
                "capital": None, "theme": None,
                "buy": {
                    "基准价": base,
                    "止损价": stop_loss, "止盈1": tp1, "止盈2": tp2,
                    "突破买点": base, "回踩买点": "-",
                    "建议买价区间": f"{base}",
                },
                "行业": industry,
                "说明": em_notes,
                "strategy_tag": "短线",
                "来源": "冰点抄底",
                "近涨停": False,
            })
        except Exception:
            continue

    # 行业去重 + 评分排序
    if MAX_SAME_INDUSTRY > 0:
        ind_count = {}
        dedup = []
        for r in sorted(picks, key=lambda x: x["total"], reverse=True):
            ind = r.get("行业", "未知")
            cnt = ind_count.get(ind, 0)
            if cnt >= MAX_SAME_INDUSTRY:
                continue
            ind_count[ind] = cnt + 1
            dedup.append(r)
        picks = dedup
    return picks[:n]


def _is_short(r):
    """判断是否短线票（strategy_tag == 短线 或 冰点抄底）"""
    return r.get("strategy_tag") in ("短线",)


def _is_trend(r):
    """判断是否右侧趋势票（strategy_tag == 趋势）"""
    return r.get("strategy_tag") == "趋势"

