---
name: daa-stock-research
description: A股每日投研系统。双策略并行（右侧趋势2-4周波段 + 短线激进3天）生成市场温度+仓位建议+候选股（评分/买点/止损/止盈），推荐股进入双跟踪池做连续跟踪（收盘体检/展期评估/止盈止损触发/技术面离场），支持虚拟盘净值与微信推送。适用于需要A股波段+短线组合投研、个人交易风控纪律执行的场景。当用户提到"每日投研/荐股/选股/仓位建议/跟踪池/展期/止盈止损/收盘复盘/午间复盘/盘中监控/双策略/右侧趋势/短线"等时使用。
---

# 大A每日投研系统 (daa-stock-research)

A股个人投资者的每日投研自动化系统：**双策略荐股 → 双跟踪池连续跟踪 → 止盈/止损/展期生命周期管理 + 虚拟盘净值**，数据源为 akshare（免费）+ 腾讯行情。

## 工作流总览

```
09:25 盘前  daily_report.py  晨报：市场温度+仓位建议+双通道候选股 → 微信推送 + 台账落账
11:35 午间  noon_review.py   午间复盘：双跟踪池半日表现
15:10 收盘  close_review.py  收盘复盘：双台账状态更新(状态机)+跟踪池体检+次日关注
盘中    15min monitor.py     盘中监控：买区/止损/止盈触发告警 → 微信推送
```

## 核心概念

### 双策略并行（2026-09 重构）

| 通道 | 定位 | 持股周期 | 台账 | 触发条件 |
|------|------|----------|------|----------|
| 📈 右侧趋势 | 横盘放量突破，追趋势中继 | 2-4 周，可展期至 60 天 | `推荐台账_右侧趋势.csv` | 每日 0-2 只，横盘紧凑+放量突破 |
| ⚡ 短线激进 | 情绪驱动，快进快出 | 3 天（T日买→T+2日开盘卖） | `推荐台账_短线激进.csv` + `短线回测台账.csv` | 涨停家数≥50 且 炸板率<40% 才开闸 |
| 🧊 冰点抄底 | 温度≤35 时错杀强势票 | 随趋势通道 | 右侧趋势台账 | 市场温度 ≤35，0-2 只 |

- 两本台账**独立虚拟净值/胜率/累计**，虚拟盘推荐即成交（实时价入账）
- 短线 3 日回测：`roll_backtest()` 按 T+2 开盘价自动结算，写入 `短线回测台账.csv`

### 推荐池生命周期

- 每日双通道荐股 **合计 0-4 只**（趋势0-2 + 短线0-2），入选即写入对应台账
- 台账状态机：`持有中 → 止盈1减半 → (止盈2清仓|止损出局|到期离场|技术离场)`，另有 `展期中`
- **右侧趋势止损 -6% / 双档止盈 +6%/+10%**（基于成本价，到了就走）
- **短线止损 -4% / 止盈 +5%/+8%**（更紧，匹配3天快进快出）
- **技术面破坏自动剔除**：波段/展期票 MA10<MA20 且破 MA10 → 趋势走坏离场；短线破 MA5 → 生命线失守

### 展期机制（右侧趋势）

- 满 14 天触发展期评估（小概率转中长期）：盈利>0、价>MA10>MA20、名额<2 才可展期
- **展期硬约束**：同时 ≤2 只（`TREND_EXTEND_MAX_COUNT`）、最长 60 天（`TREND_EXTEND_MAX_DAYS`）强制离场
- **展期后双档止盈**：+10%减半 / +15%清仓（基于成本价）

### 选股引擎

- 评分体系：价值30 + 技术30 + 资金25 + 题材15 = 100，≥70 推荐 / ≥80 强烈推荐
- **右侧追启动**：放量(量比≥1.5)突破20日新高、近20日振幅<25%横盘紧凑
- **涨停剔除**：现价/涨停价 ≥0.95（近涨停）或 5 日内连板≥2 → 剔除（解决"好看不好买"）
- **行业去重**：同行业最多 1 只（`MAX_SAME_INDUSTRY`）
- **代码白名单**：主板(60/00)+创业板(30)；排除科创板(68)/北交所(92/43/83)——无交易权限

## 使用方式

### 1. 安装依赖

```bash
pip install -r requirements.txt
# 建议：pip install akshare pandas numpy requests
```

> **macOS 本机环境（2026-09 迁移后）**：项目自带 venv，直接用绝对路径运行，勿用系统 python：
> `/Users/chenyuting/Documents/workbuddy/workbuddy-daa/daa-stock-research-skill/.venv/bin/python`
> 若需重建 venv，注意 `jsonpath==0.82.2` 的 sdist 会触发 pip 的 EEXIST 解压 bug，
> 需手动下载解包后 `pip install ./jsonpath-0.82.2`，再装其余依赖。

### 2. 配置（可选）

- **数据目录**：首次运行自动在包根创建 `data/`、`05_每日推送/`、`04_每日复盘/`
- **项目根**：默认包根；可用环境变量覆盖 `DAA_PROJECT_ROOT`
- **微信推送**：创建 `data/push_config.json`（未配置则推送静默跳过）。**必须带 `"enabled": true`**，否则填了 token 也不推：
  ```json
  {"enabled": true, "default_channel": "pushplus", "pushplus_token": "你的pushplus令牌", "serverchan_key": ""}
  ```
  渠道二选一：PushPlus 填 `pushplus_token`；Server酱 填 `serverchan_key` 并把 `default_channel` 改为 `serverchan`。该文件已被 .gitignore 排除，不会入库。
- **研究层增强（依赖外部 skill `stock-researcher`）**：通过 `scripts/skill_bridge.py` 桥接，缺失时**全部静默降级为 None**（日报照跑，但研究板块变空）：
  | 桥接函数 | 提供能力 |
  |---|---|
  | `get_sector_rps()` | 板块 RPS 相对强度排名（20d/60d/趋势） |
  | `get_market_regime()` | 市场体制判定（牛/熊/震荡 + 置信度） |
  | `get_stock_score(code)` | 个股预测评分 + 信号 |
  | `get_value_decision(code)` | 价值投资交叉验证 6 模块（护城河/财务健康/DCF/管理层/行业/因子）→ 公允价值区间 + 安全边际 |

  安装方式（本机已装）：
  ```bash
  # SkillHub slug: aistockresearcher
  cp -R <下载解压目录>/. ~/.workbuddy/skills/aistockresearcher__skillhub/
  ```
  目录名必须为 `aistockresearcher__skillhub`（桥接层硬编码查找），或设 `DAA_SKILL_DIR` 覆盖。

  > ⚠️ **已知偏差（2026-09-07 实测）**：`get_value_decision` 的**相对分可用、绝对值不可信**。
  > 例：600519 现价 1330 元，返回公允价值区间 [602.81, 665.37, 731.91]，DCF 模块仅得 10/100，
  > margin 输出 -99.9（异常）。原因待查（疑为财务数据陈旧或复权/股本口径问题）。
  > **使用纪律：只看 moat / financial_health / factor 等分项排序做交叉验证，禁止直接引用 fair_value 与 margin 下单。**

### 3. 运行

```bash
cd <skill根目录>
python scripts/daily_report.py    # 晨报：生成 05_每日推送/推送_YYYY-MM-DD.md 并推送
python scripts/noon_review.py     # 午间复盘（双策略）
python scripts/close_review.py    # 收盘复盘（双策略，15:10 后）
python scripts/monitor.py         # 盘中监控（配合定时任务每15分钟）
```

> 本机生产环境示例（macOS）：
> `cd /Users/chenyuting/Documents/workbuddy/workbuddy-daa/daa-stock-research-skill/scripts && /Users/chenyuting/Documents/workbuddy/workbuddy-daa/daa-stock-research-skill/.venv/bin/python daily_report.py`

> ⚠️ **必须走包装器，不要直跑 `daily_report.py`**（2026-09-07/08 两次验证）：
> akshare 大量接口无超时，链路抖动时会**永久挂起**（直跑实测无输出卡死，整套流程 ~13 分钟）。
> 固化包装器：`scripts/_run_daily_wrapper.py`（requests 25s 超时 + 浏览器 UA + 退避重试 + 全A快照并发分页 + faulthandler 看门狗）。
> 用法：`cd scripts && <.venv>/bin/python _run_daily_wrapper.py`
> 已知坑：
> ① **必须绕过系统代理**（2026-09-08 修正，旧结论「必须走系统代理」是错的）：macOS 开系统代理时 `urllib`/`requests(trust_env=True)` 会自动继承，全局代理模式（trojan 等）下出口 IP 在境外，东财按境外 IP 限流/封禁 → `clist` 接口持续 502。`fetch_data.py` 已内置强制直连（`trust_env=False` + `proxies=None`），需走代理时 `export DAA_USE_PROXY=1`。
> ② 东财拒 `python-requests` 默认 UA，高频后会封 IP，封了切新浪；
> ③ 包装器里 snapshot 缓存补丁会报 `No module named 'fetch_data'`（无害，快照会被拉两次）。
>
> 排查口诀：**行情接口报 502/超时，先查系统代理是否开着**（`scutil --proxy`），再怀疑 UA/限流。

### 4. 风险日历排雷（2026-09-07 新增）

`scripts/risk_calendar.py` —— 拦截两类可预知的坑，晨报自动调用：

| 风险类型 | 判定 | 等级 |
|---|---|---|
| 未来 10 天内披露财报 | 巨潮预约披露日落在窗口内 | 中 |
| 已发负面业绩预告 | 预告类型 ∈ 预减/首亏/续亏/增亏/略减/转亏/减亏 | 高 |

- 数据源：akshare 免费接口，全市场数据**每日仅拉一次**并缓存到 `data/risk_calendar_cache.json`
- **只提示、不自动剔除**：等级「高」建议人工剔除或等披露后再看
- 任何失败静默降级（打印 `[排雷] 跳过`），绝不阻断日报
- 手动检查：`python scripts/risk_calendar.py 600519 000001`
- TODO：个股级解禁（接口需按日遍历，成本高，暂未接入）

> 已知限制：财报空窗期（如 9 月初，半年报已披露完、三季报预约未出）"待披露"项会为空，属正常。

### 5. 定时化（工作日）

macOS 本机已用 WorkBuddy 自动化托管（无需 cron / 计划任务）：

| 时间 | 自动化 | 脚本 |
|------|--------|------|
| 09:25 | A股晨报 | `daily_report.py` |
| 10:35 | A股盘中监控(上午) | `monitor.py` |
| 11:35 | A股午间复盘 | `noon_review.py` |
| 14:45 | A股盘中监控(尾盘) | `monitor.py` |
| 15:10 | A股收盘复盘 | `close_review.py` |
| 21:30 | 代码自动推送 GitHub | `上传到GitHub.sh` |

- 非交易日（休市/无数据）自动化自动静默跳过，不产生噪音
- 代码仓库：https://github.com/cochain27/daa-stock-research-skill

## 风控参数速查

| 参数 | 值 | 含义 |
|------|----|----|
| `STOP_LOSS` | -6% | 趋势止损线（基于成本价） |
| `TAKE_PROFIT_1/2` | +6%/+10% | 趋势双档止盈（减半/清仓） |
| `MOVE_STOP` | MA10 | 盈利>3%上移成本线后跌破MA10离场 |
| `SHORT_STOP_LOSS` | -4% | 短线止损（3天快进快出，更紧） |
| `SHORT_TAKE_PROFIT_1/2` | +5%/+8% | 短线双档止盈 |
| `SHORT_HOLD_DAYS_MAX` | 5天 | 短线目标3天，极限5天 |
| `TREND_HOLD_DAYS` | 14-28天 | 趋势目标持仓 2-4 周 |
| `TREND_EXTEND_MAX_DAYS` | 60天 | 趋势展期硬上限 |
| `TREND_EXTEND_MAX_COUNT` | 2只 | 同时展期上限 |
| `MAX_POSITION_PER_STOCK` | 30% | 单票最大仓位 |
| `VIRTUAL_TRADE_AMOUNT` | 1万 | 每笔虚拟成交金额（等权记账） |
| `SHORTLINE_ZT_MIN` | 50 | 短线开闸：涨停家数≥50 |
| `SHORTLINE_ZB_RATE_MAX` | 40% | 短线开闸：炸板率<40% |
| `BOTTOM_FISHING_TEMP_MAX` | 35 | 冰点抄底：温度≤35 才启用 |

> 风控参数集中在 `scripts/config.py`，为全系统唯一来源。

## 输出物

- `data/推荐台账_右侧趋势.csv` — 趋势票生命周期台账
- `data/推荐台账_短线激进.csv` — 短线票台账
- `data/短线回测台账.csv` — 短线3日回测流水
- `data/today_picks.json` — 当日推荐（供 monitor 读取）
- `05_每日推送/` — 晨报/午间/复盘存档
- `04_每日复盘/` — 收盘复盘存档

## 免责声明

本项目仅供个人学习研究，不构成任何投资建议。股市有风险，入市需谨慎。
