# -*- coding: utf-8 -*-
"""全局配置：风格参数、路径、风控规则"""
import os
from pathlib import Path

# ============ 路径（可移植：环境变量优先，默认按 skill 包内相对定位） ============
# 本机生产环境用 DAA_PROJECT_ROOT 指向 E:\workbuddy——大A 覆盖；
# 默认按脚本位置定位：scripts/config.py -> parent.parent = skill 包根目录
PROJECT_ROOT = Path(os.environ.get("DAA_PROJECT_ROOT", Path(__file__).resolve().parent.parent))
DATA_DIR = PROJECT_ROOT / "data"
PUSH_DIR = PROJECT_ROOT / "05_每日推送"
REVIEW_DIR = PROJECT_ROOT / "04_每日复盘"

for _d in [DATA_DIR, PUSH_DIR, REVIEW_DIR]:
    _d.mkdir(parents=True, exist_ok=True)

# ============ 风格参数（用户设定） ============
TOTAL_CAPITAL = 100_000      # 总资金 10万以下（按10万估）
RISK_STYLE = "短线波段"       # 持仓7-10天（目标波段），2026-09-03 调整为推荐池跟踪模式
MAX_POSITION_PER_STOCK = 0.30  # 单票最大仓位 30%
DAILY_PICKS = 2              # 每日推荐只数上限（实际按质量1-2只灵活输出）

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
EXTEND_TP2 = 0.15            # 展期后第二档止盈 +15%（清仓，基于成本价）—— 到了就走，不追趋势顶

# ============ 市场温度权重 ============
WEIGHTS = {"trend": 30, "sentiment": 30, "volume": 20, "capital": 20}

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

