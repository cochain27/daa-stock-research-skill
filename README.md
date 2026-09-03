# daa-stock-research · 大A每日投研系统（WorkBuddy Skill 版）

> A股个人投资者每日投研自动化：晨报荐股 → 跟踪池连续跟踪 → 展期/止盈/止损生命周期管理。
> 本项目为可复用的 Skill 打包版，原始数据源全部为免费公开接口（akshare）。

## 功能

- **盘前晨报**：市场温度(0-100) + 仓位建议 + 1-2只候选股（六维评分 / 买点区间 / 止损 / 双档止盈 / 🟢可展期分）
- **选股引擎**：右侧追启动（放量突破20日新高）、涨停剔除、行业去重、代码白名单（无科创板权限）
- **推荐池跟踪**：推荐股写入台账，收盘自动更新状态机（持有中→止盈减半→清仓/止损/到期）
- **展期机制**：满10天可展期评估（少数派，≤2只，最长30天），展期后止盈切换 +10%/+15%
- **三类复盘 + 盘中监控**：午间复盘 / 收盘复盘 / 每15分钟盘中触发告警，微信推送

## 目录结构

```
daa-stock-research/
├── SKILL.md            # WorkBuddy Skill 定义（能力/用法/触发词）
├── README.md
├── requirements.txt
├── .gitignore
├── scripts/            # 全部核心脚本（同目录互相 import）
│   ├── config.py           # ★ 风控/展期参数唯一来源
│   ├── daily_report.py     # 晨报入口（主流程）
│   ├── market_analysis.py  # 市场温度/仓位
│   ├── stock_screener.py   # 选股评分引擎
│   ├── tracker.py          # 推荐台账/状态机/展期评估
│   ├── fetch_data.py       # akshare/腾讯行情数据层
│   ├── portfolio.py        # 持仓分析
│   ├── notify.py           # 微信推送（未配置token自动跳过）
│   ├── skill_bridge.py     # stock-researcher 桥接（可选）
│   ├── monitor.py          # 盘中监控
│   ├── noon_review.py      # 午间复盘
│   └── close_review.py     # 收盘复盘
├── data/               # 运行时生成（台账/today_picks/监控状态），已 gitignore
├── 05_每日推送/          # 日报存档（已 gitignore）
└── 04_每日复盘/          # 复盘存档（已 gitignore）
```

## 快速开始

```bash
pip install -r requirements.txt
python scripts/daily_report.py    # 生成日报 + 可选微信推送
```

配置与定时化建议详见 [SKILL.md](SKILL.md)。

## 数据源

| 用途 | 来源 |
|------|------|
| 涨停池/跌停池/板块/资金流 | akshare（东方财富接口） |
| 个股K线/实时行情 | akshare（腾讯/新浪） |
| 盘口量比/五档 | 腾讯行情 qt.gtimg.cn |
| 估值(PE/PB历史分位) | akshare + 百度兜底 |

## 免责声明

仅供个人学习研究，不构成投资建议。股市有风险，入市需谨慎。
