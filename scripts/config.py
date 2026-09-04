# -*- coding: utf-8 -*-
"""全局配置：风格参数、路径、风控规则"""
import os
from pathlib import Path

# ============ 路径 ============
PROJECT_ROOT = Path(r"E:\workbuddy——大A")
DATA_DIR = PROJECT_ROOT / "data"
PUSH_DIR = PROJECT_ROOT / "05_每日推送"
REVIEW_DIR = PROJECT_ROOT / "04_每日复盘"

for _d in [DATA_DIR, PUSH_DIR, REVIEW_DIR]:
    _d.mkdir(parents=True, exist_ok=True)

# ============ 风格参数（用户设定） ============
TOTAL_CAPITAL = 100_000      # 总资金 10万以下（按10万估）
RISK_STYLE = "双策略并行"       # 2026-09-05 起：短线激进 + 右侧趋势 分通道独立运作
MAX_POSITION_PER_STOCK = 0.30  # 单票最大仓位 30%

# ============ 虚拟盘参数（2026-09-05 小火炉拍板：推荐即虚拟成交，实时价入账） ============
VIRTUAL_ENABLED = True                   # 虚拟盘开关
VIRTUAL_BASE = 100_000                   # 虚拟净值基准 10万
VIRTUAL_TRADE_AMOUNT = 10_000            # 每笔虚拟成交金额（等权记账，不受仓位约束）
VIRTUAL_BUY_PRICE_MODE = "realtime"      # 虚拟建仓价：推荐时实时价（推荐=成交）
VIRTUAL_BUY_ZONE_CHECK = False           # 已废弃：不再校验次日触及买区（历史保留字段）
VIRTUAL_BUY_ZONE_DAYS = 2                # 已废弃（历史保留字段）

# ============ 双通道选股（2026-09-05 小火炉拍板：短线激进 + 右侧波段分通道） ============
# 短线通道（情绪驱动：涨停梯队/冲板票，推荐即虚拟成交）
SHORTLINE_ENABLED = True            # 短线通道总开关
SHORTLINE_ZT_MIN = 50               # 情绪门槛：涨停家数≥50 才开闸
SHORTLINE_ZB_RATE_MAX = 0.40        # 情绪门槛：炸板率<40%（炸板/(涨停+炸板)）
SHORTLINE_MAX_PICKS = 2             # 短线通道每日最多 2 只
SHORTLINE_MIN_SCORE = 60            # 短线情绪评分及格线（低于不出票）
SHORTLINE_MIN_CHG = 5.0             # 短线候选最低涨幅%（冲板/强势票）
SHORTLINE_MAX_LIANBAN = 2           # 短线候选连板数上限（≤2，3板以上高位不追）
# 波段通道（右侧趋势：现有综合评分体系）
SWING_MAX_PICKS = 2                 # 波段通道每日最多 2 只
DAILY_PICKS = 4                     # 双通道合计上限（短线0-2 + 波段0-2）

# ============ 短线策略参数（弱势市/短线票专用） ============
SHORT_STOP_LOSS = -0.04            # 短线止损 -4%（比波段-6%更紧）
SHORT_TAKE_PROFIT_1 = 0.05         # 短线止盈1 +5%（减半）
SHORT_TAKE_PROFIT_2 = 0.08         # 短线止盈2 +8%（清仓）
SHORT_HOLD_DAYS_MAX = 5            # 短线目标3-5天，极限5天
SHORT_TP_MOVE_UP = 0.02            # 短线盈利>2%上移止损至成本线

# 候选股代码前缀白名单：主板(60/00) + 创业板(30)
# 科创板(68)无交易权限，北交所(92/43/83)排除 —— 2026-09-01 小火炉反馈
ALLOW_CODE_PREFIX = ("60", "00", "30")

# ============ 风控规则（2026-09-02 校准为 1-2 周波段） ============
STOP_LOSS = -0.06            # 止损 -6%
TAKE_PROFIT_1 = 0.06         # 第一档止盈 +6%（减半仓）
TAKE_PROFIT_2 = 0.10         # 第二档止盈 +10%（清仓）
MOVE_STOP = "MA10"           # 盈利>3%上移成本线后跌破MA10离场
TP_MOVE_UP = 0.03            # 盈利 >3% 即上移止损至成本线
HOLD_DAYS_MAX = 10           # 目标持有 10 天（7-10 天波段；满10天触发展期评估，非机械离场）
HOLD_DAYS_QUIT = 5           # 横盘 5 天强制离场

# ============ 展期规则（2026-09-03 小火炉拍板：少数可展期中长线，硬上限1个月） ============
EXTEND_MAX_DAYS = 30         # 展期硬上限 30 天（超 1 个月强制离场，2026-09-03 确认）
EXTEND_MAX_COUNT = 2         # 同时展期票 ≤2 只（少数，非常态）
EXTEND_TP1 = 0.10            # 展期后第一档止盈 +10%（减半仓，基于成本价）
EXTEND_TP2 = 0.15            # 展期后第二档止盈 +15%（清仓，基于成本价）
EXTEND_PEAK_DRAWBACK = 0.12 # 展期后从峰值最大回撤超此值也触发离场

# ============ 市场温度权重 ============
WEIGHTS = {"trend": 30, "sentiment": 30, "volume": 20, "capital": 20}

# ============ 市场环境判定阈值（2026-09-03 用于策略打标） ============
# 环境由 market_analysis.judge_market_env() 根据温度计+指数MA排列+量能综合判定
MARKET_ENV_THRESHOLDS = {
    "strong_temp": 65,       # 温度≥65 + MA多头 → 强势市（右侧波段为主）
    "weak_temp": 35,         # 温度≤35 → 弱势市（短线/空仓观望）
    "bull_run_ma": "above",  # 强势市要求：收盘>MA20>MA60
}

# ============ 数据源 ============
DATA_SOURCE = "akshare"      # 备用: mx-stocks-screener / tushare

# ============ 评分体系 ============
SCORE_WEIGHTS = {"value": 30, "tech": 30, "capital": 25, "theme": 15}
SCORE_BUY = 70               # >=70 推荐
SCORE_STRONG = 80            # >=80 强烈推荐

# ============ 选股质量改进（2026-09-01 小火炉反馈） ============

# 行业去重：同行业最多入选 N 只（避免组合集中度过高）
MAX_SAME_INDUSTRY = 1

# 涨停股降级/剔除（1-2周波段：放宽，允许趋势中继票进入）：
# - 现价 >= 涨停价 * 0.95（已封板或几乎涨停）→ 判定"近涨停"，剔除出推荐
#   （原0.985过严，误杀波段启动票；放宽到0.95保留+5%以内涨幅的趋势票）
ZT_THRESHOLD = 0.95           # 现价/涨停价 阈值（原0.985）
ZT_BAN = True                 # 近涨停股直接剔除（不开可操作口）
ZT_LIMIT_5DAY = True          # 5日内连板≥2(即近2日涨停)的股，视为高位追高风险，剔除
ZT_MAX_LIANG = 0.20           # 5日累计涨幅 >20% 视为短线过热（波段上限，原0.10）

# 价值面细分权重（满分30）
# 设计：基准8（激进短线不苛求基本面，但高估/亏损要扣分）
# 加分项：合理PE +8 / 合理PB +5 / ROE>0 +5 / 毛利率高 +2 / 净利增长 +4
# 扣分项：PE高估 -6 / PE为负 -4 / PB高估 -4 / ROE非正 -5 / 净利下滑 -4
VAL_W = {"pe": 8, "pb": 5, "roe": 5, "margin": 2, "growth": 4, "base": 8}
# 估值容忍区间（激进短线，相对宽松）
VAL_PE_OK = (0, 40)           # 动态PE 健康区间
VAL_PE_WARN = (40, 80)        # 偏高
VAL_PB_OK = (0, 8)            # PB 健康区间
VAL_PB_WARN = (8, 20)         # 偏高

# 技术面补强开关（满分30，新增维度见 stock_screener.tech_screen）
TECH_USE_60D_POS = True       # 60日价格位置
TECH_USE_UPPER_SHADOW = True  # 长上影/冲高回落
TECH_USE_ZT = True            # 当日涨停/近涨停判定
TECH_USE_QUANTITY = True      # 量比（腾讯盘口）

# ============ 右侧趋势通道参数（2026-09-05 双策略分立） ============
TREND_ENABLED = True               # 右侧趋势通道总开关
TREND_MAX_PICKS = 2                # 右侧趋势每日最多 2 只
TREND_MIN_MARKET_CAP = 30_0000_000  # 流通市值≥30亿（避免流动性风险）
TREND_MIN_60D_POS = 0.45          # 60日价格位置 <45%（低位才考虑）
TREND_MAX_20D_AMPLITUDE = 0.25    # 近20日振幅 <25%（横盘）
TREND_MAX_20D_STD_RATIO = 0.05    # 近20日收盘价标准差/均价 <5%（横盘紧凑）
TREND_BREAKOUT_VOL_RATIO = 1.5    # 放量突破：成交量 ≥ 20日均量 × 此倍数
TREND_MIN_AMOUNT = 1.5_0000_000   # 成交额 >1.5亿
TREND_BREAKOUT_CHG_RANGE = (0.03, 0.07)  # 突破涨幅区间 3%-7%
TREND_HOLD_DAYS = (14, 28)        # 目标持仓 2-4 周
TREND_EXTEND_MAX_DAYS = 60        # 展期硬上限 60 天（1-2 个月）
TREND_EXTEND_MAX_COUNT = 2         # 同时展期票 ≤2 只

# ============ 冰点抄底子模块（2026-09-05 双策略分立） ============
BOTTOM_FISHING_ENABLED = True             # 冰点抄底总开关
BOTTOM_FISHING_TEMP_MAX = 35              # 温度≤35 才启用
BOTTOM_FISHING_ZT_MIN = 30                # 冰点时涨停家数参考下限（极低迷参考）
BOTTOM_FISHING_MAX_PICKS = 2              # 冰点抄底每日最多 2 只

# ============ 短线激进回测台账（2026-09-05 双策略分立） ============
SHORT_BACKTEST_PATH = DATA_DIR / "短线回测台账.csv"
SHORT_BACKTEST_AMOUNT = 10_000            # 短线回测每笔虚拟金额

# ============ 双策略台账路径（2026-09-05 双策略分立） ============
TRACK_SHORT_PATH = DATA_DIR / "推荐台账_短线激进.csv"   # 短线激进台账
TRACK_TREND_PATH = DATA_DIR / "推荐台账_右侧趋势.csv"   # 右侧趋势台账

