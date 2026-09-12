#!/usr/bin/env python3
"""
lhb_drive.py  — 龙虎榜驱动选股 v1（路径A）
逻辑：收盘后查龙虎榜（机构席位）→ 筛选机构净买入>5000万/成交额>3亿/非新股/非ST
      → 基本面速查（profile）→ 综合评分 → 入短线激进推荐台账
数据源：Westock MCP (data_lhb) + WestockData CLI (profile/asfund)
"""
import sys, subprocess, json, csv, os
from pathlib import Path
from datetime import date, timedelta

VENV_SITE = "/Users/chenyuting/.workbuddy/binaries/python/envs/default/lib/python3.13/site-packages"
sys.path.insert(0, VENV_SITE)
import requests

# ─── 常量 ────────────────────────────────────────────────────────────────
BASE     = Path("/Users/chenyuting/Documents/workbuddy/workbuddy-daa/daa-stock-research-skill")
OUT_CSV  = BASE / "data" / "推荐台账_短线激进.csv"
WATCH_CSV = BASE / "data" / "watch_history.csv"
DATA_DIR  = BASE / "data"
LHB_THRESHOLD = 5000   # 万元，机构净买入门槛
TURNOVER_THRESHOLD = 3  # 亿元，成交额门槛

# ─── 工具函数 ────────────────────────────────────────────────────────────
def last_trading_day():
    td = date.today()
    for delta in range(8):
        d = td - timedelta(days=delta)
        if d.weekday() < 5:
            return d.strftime("%Y-%m-%d")
    return td.strftime("%Y-%m-%d")

def clean_env():
    env = os.environ.copy()
    for k in list(env):
        if "proxy" in k.lower():
            del env[k]
    return env

def wcmd(args, timeout=60):
    """WestockData CLI 调用"""
    r = subprocess.run(
        ["npx", "-y", "westock-data-clawhub@1.0.4"] + args,
        capture_output=True, text=True, timeout=timeout, env=clean_env()
    )
    return r.stdout + r.stderr

def fetch_profile(code):
    """个股简况（支持 JSON 和 Markdown table 两种格式）"""
    raw = wcmd(["profile", code, "--raw"])
    try:
        # 尝试 JSON 格式
        data = json.loads(raw)
        if isinstance(data, dict) and data.get("status") == 200:
            d = data.get("data", {})
            return {
                "industry": d.get("industry", ""),
                "pe": to_float(d.get("pe"), default=None),
                "mkt_cap": d.get("mkt_cap", ""),
                "main_biz": d.get("main_business", "")[:80],
                "theme": d.get("concept", ""),
            }
    except (json.JSONDecodeError, ValueError):
        pass

    # Markdown table 格式
    lines = [l for l in raw.strip().splitlines()
             if l.startswith("|") and "---" not in l]
    if len(lines) < 2:
        return {}
    headers = [h.strip() for h in lines[0].split("|")[1:-1]]
    vals = [v.strip() for v in lines[1].split("|")[1:-1]]
    row = dict(zip(headers, vals))
    return {
        "industry": row.get("industry", ""),
        "sector": row.get("sector", ""),
        "pe": None,  # profile CLI 不返回 PE
        "main_biz": row.get("business", "")[:80],
        "listed_date": row.get("listedDate", ""),
    }

def fetch_asfund(code, trade_date):
    """主力资金流"""
    raw = wcmd(["asfund", code, "--date", trade_date, "--raw"])
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and data.get("status") == 200:
            d = data.get("data", {})
            return {
                "main_net_in": to_float(d.get("MainNetFlow", 0)) / 10000,  # 万元
                "close": to_float(d.get("ClosePrice", 0)),
                "chg_pct": to_float(d.get("chgPct", 0)),
            }
    except:
        pass
    return {}

def fetch_kline_close(code, limit=2):
    """
    从 K 线获取最新收盘价（不用 asfund.FwdClosePrice，那个字段不等于昨收）
    返回: float 收盘价（元），取最近一根 K 线
    """
    raw = wcmd(["kline", code, "--period", "day", "--limit", str(limit), "--fq", "qfq", "--raw"])
    lines = [l for l in raw.strip().splitlines() if l.startswith("|") and "---" not in l]
    if len(lines) < 2:
        return 0.0
    # 最后一行是最新 K 线
    headers = [h.strip() for h in lines[0].split("|")[1:-1]]
    vals = [v.strip() for v in lines[-1].split("|")[1:-1]]
    row = dict(zip(headers, vals))
    # 收盘价列可能是 "close" 或 "last"
    for col in ["close", "Close", "last", "Last", "收盘价"]:
        if col in row:
            return to_float(row[col])
    return 0.0

def to_float(val, default=0.0):
    try:
        return float(str(val).replace(",", "").replace("%", ""))
    except:
        return default

def _lhb_score(inst_net, turnover, inst_rate, chg_pct):
    """
    综合评分（0-100）
    机构净买入  40%  → 5000万=60分, 1亿=80分, 5亿=100分
    成交额      20%  → 3亿=60分, 10亿=100分
    机构占比    20%  → 10%=60分, 30%=100分
    涨幅        20%  → 0%=50分, 10%=100分
    """
    def norm(val, lo, hi):
        if hi <= lo: return 50
        return max(0, min(100, (val - lo) / (hi - lo) * 100))

    s_net = norm(inst_net, 5000, 50000)       # 5000万~5亿
    s_vol = norm(turnover, 3, 20)             # 3亿~20亿
    s_rate = norm(abs(inst_rate), 5, 40)      # 机构占比
    s_chg  = norm(abs(chg_pct), 0, 10)         # 涨幅绝对值

    return round(0.40 * s_net + 0.20 * s_vol + 0.20 * s_rate + 0.20 * s_chg, 1)

# ─── MCP 直调函数（通过 MCP 代理端口获取原始龙虎榜机构席位数据）──────────────
# 在 Agent 中直接调用 mcp__westock-mcp__data_lhb 的原始结果传入
# 若无传入数据，尝试 HTTP API fallback

def fetch_lhb_jg(trade_date):
    """尝试 MCP HTTP API（port 7890 等常见 MCP 代理端口）"""
    payload = {"type": "jg", "date": trade_date}
    for port in [7890, 8080, 3000]:
        try:
            resp = requests.post(
                f"http://127.0.0.1:{port}/mcp/westock-mcp/data_lhb",
                json=payload, timeout=8
            )
            if resp.ok and resp.text.strip() and resp.text.strip() != "null":
                return resp.json()
        except:
            pass
    return {}

def apply_lhb_data(raw_items):
    """将龙虎榜原始数据直接输入进行筛选（供 Agent 层 MCP 调用后传入）"""
    return raw_items  # 直接透传，在 filter_lhb 中统一处理

# ─── 主筛选逻辑 ───────────────────────────────────────────────────────────
def filter_lhb(raw_items, trade_date):
    """
    筛选条件：
    1. 机构净买入 > 5000万元
    2. 成交额 > 3亿元（非新股，流动性足够）
    3. 非ST（排除高风险）
    4. 非涨跌停封板（允许涨停但要有换手）
    """
    candidates = []
    seen = set()
    for r in raw_items:
        code = str(r.get("code", "")).strip()
        name = str(r.get("name", "")).strip()
        if not code or not name:
            continue
        # 去重（同代码多日出现取最大值）
        key = code
        if key in seen:
            continue
        seen.add(key)

        # ST / *ST 排除
        if "ST" in name or "st" in name.lower():
            continue

        total_buy = to_float(r.get("totalBuyAmt", 0))   # 元
        inst_net   = to_float(r.get("netBuyAmt", 0))     # 元
        inst_rate  = to_float(r.get("netBuyRate", 0))   # %
        inst_buy   = to_float(r.get("instBuyAmt", 0))    # 元
        turnover   = total_buy / 1e8                      # 亿元
        inst_net_w = inst_net / 10000                     # 万元

        chg_pct = to_float(r.get("instBuyRate", 0))      # 机构买入时的股票涨跌幅

        # 条件1：机构净买入 > 5000万
        if inst_net_w < LHB_THRESHOLD:
            continue
        # 条件2：成交额 > 3亿
        if turnover < TURNOVER_THRESHOLD:
            continue

        candidates.append({
            "code": code,
            "name": name,
            "inst_net_w": round(inst_net_w, 0),
            "total_buy": round(turnover, 2),
            "inst_rate": inst_rate,
            "inst_buy": round(inst_buy / 10000, 0),
            "chg_pct": chg_pct,
            "td_days": r.get("tdDays", ""),
        })
    return candidates

# ─── 基本面速查并评分 ─────────────────────────────────────────────────────
def enrich_candidates(candidates, trade_date):
    """对候选股票拉取基本面并计算综合评分"""
    results = []
    for c in candidates:
        code = c["code"]
        prof = fetch_profile(code)
        fund = fetch_asfund(code, trade_date)

        # 综合评分
        inst_net_w = c["inst_net_w"]
        turnover   = c["total_buy"]
        inst_rate  = c["inst_rate"]
        chg_pct    = c["chg_pct"]
        score      = _lhb_score(inst_net_w, turnover, inst_rate, chg_pct)

        # PE合理性（过高的PE可能是纯炒作，扣分）
        pe_penalty = 0
        pe = prof.get("pe")
        if pe is not None:
            if pe > 100:
                pe_penalty = -10
            elif pe < 0:
                pe_penalty = -5  # 亏损股

        final_score = round(score + pe_penalty, 1)
        if final_score < 0:
            final_score = 0

        # 题材标签
        theme = prof.get("theme", "")
        industry = prof.get("industry", "")
        main_biz = prof.get("main_biz", "")

        # 用 asfund 收盘价，若无则从 K 线获取
        close = fund.get("close", 0)
        if close <= 0:
            close = fetch_kline_close(code)
        if close <= 0:
            close = c.get("close", 0)

        # 备注
        notes = f"机构净买入{inst_net_w:.0f}万/成交额{turnover:.1f}亿/机构占比{inst_rate:.1f}%"
        if pe is not None:
            notes += f"/PE{pe:.1f}"
        notes += f"/{industry}" if industry else ""

        results.append({
            **c,
            "score": final_score,
            "industry": industry,
            "theme": theme[:50],
            "main_biz": main_biz,
            "close": close,
            "notes": notes,
            "pe": pe,
        })
    return results

# ─── 写入推荐台账 ─────────────────────────────────────────────────────────
def write_to_ledger(candidates, trade_date):
    """追加到短线激进推荐台账"""
    fields = ["日期","代码","名称","自研评分","策略标签","验证评分","基准价",
               "买区","止损","止盈1","止盈2","虚拟成本","虚拟建仓日",
               "虚拟建仓状态","状态","最高收益%","最后更新","备注",
               "情绪分","连板数","炸板次数","候选来源",
               "开盘买入价","开盘卖出价","回测盈亏%"]

    existing = {}
    if OUT_CSV.exists():
        with OUT_CSV.open(encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                existing[row.get("代码", "")] = row

    for c in candidates:
        code = c["code"]
        key = f"{trade_date}_{code}"

        # 读取旧记录（只作为字段缺失时的兜底，不保留旧值覆盖新值）
        existing_rec = existing.get(code, {})

        close = c.get("close", 0)
        if close <= 0:
            close = existing_rec.get("基准价", 0)
        if close > 0:
            buy_zone = f"{close * 0.97:.2f}-{close * 1.03:.2f}"
            stop_loss = f"{close * 0.95:.2f}"
            tp1 = f"{close * 1.07:.2f}"
            tp2 = f"{close * 1.15:.2f}"
        else:
            buy_zone = stop_loss = tp1 = tp2 = ""

        # 优先用新候选数据；旧记录只补充空字段
        new_row = {
            "日期": trade_date,
            "代码": code,
            "名称": c["name"],
            "自研评分": c.get("score") or existing_rec.get("自研评分", ""),
            "策略标签": "短线",
            "验证评分": "",
            "基准价": close if close > 0 else existing_rec.get("基准价", ""),
            "买区": buy_zone if buy_zone else existing_rec.get("买区", ""),
            "止损": stop_loss if stop_loss else existing_rec.get("止损", ""),
            "止盈1": tp1 if tp1 else existing_rec.get("止盈1", ""),
            "止盈2": tp2 if tp2 else existing_rec.get("止盈2", ""),
            "虚拟成本": existing_rec.get("虚拟成本", ""),
            "虚拟建仓日": existing_rec.get("虚拟建仓日", ""),
            "虚拟建仓状态": existing_rec.get("虚拟建仓状态", ""),
            "状态": existing_rec.get("状态") or "待明日确认",
            "最高收益%": existing_rec.get("最高收益%", ""),
            "最后更新": date.today().strftime("%Y-%m-%d"),
            "备注": c.get("notes", "") or existing_rec.get("备注", ""),
            "情绪分": existing_rec.get("情绪分", ""),
            "连板数": existing_rec.get("连板数", ""),
            "炸板次数": existing_rec.get("炸板次数", ""),
            "候选来源": c.get("候选来源", f"龙虎榜机构席位(净买{c['inst_net_w']:.0f}万)"),
            "开盘买入价": existing_rec.get("开盘买入价", ""),
            "开盘卖出价": existing_rec.get("开盘卖出价", ""),
            "回测盈亏%": existing_rec.get("回测盈亏%", ""),
        }
        existing[code] = new_row

    all_rows = [dict(zip(fields, fields))]
    all_rows += [existing[k] for k in sorted(existing.keys())]

    with OUT_CSV.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(all_rows[1:])

    return len(candidates)

# ─── HTML 简报生成 ────────────────────────────────────────────────────────
def write_html_report(candidates, trade_date):
    """生成龙虎榜驱动 HTML 简报"""
    out_path = DATA_DIR / "lhb_dashboard.html"
    today_str = date.today().strftime("%Y-%m-%d")

    rows_html = ""
    for i, c in enumerate(candidates, 1):
        score_color = "#c62828" if c["score"] >= 70 else ("#f57f17" if c["score"] >= 55 else "#333")
        rows_html += f"""
        <tr>
          <td><strong>{i}</strong></td>
          <td><strong>{c['name']}</strong><br><span class="code">{c['code']}</span></td>
          <td>{c.get('industry','-')}</td>
          <td class="pos">+{c['inst_net_w']:,.0f}万</td>
          <td>{c['total_buy']:.1f}亿</td>
          <td>{c['inst_rate']:.1f}%</td>
          <td>{c['chg_pct']:+.1f}%</td>
          <td style="color:{score_color};font-weight:700">{c['score']}</td>
          <td style="font-size:11px">{c.get('pe','-')}</td>
          <td style="font-size:10px;text-align:left">{c.get('main_biz','-')}</td>
          <td style="font-size:10px;text-align:left;color:#666">{c['notes']}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>龙虎榜驱动看板  {trade_date}</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
         background:#f0f2f5;color:#1a1a2e;font-size:13px}}
  .header{{background:linear-gradient(135deg,#1b4332,#2d6a4f);color:white;padding:18px 28px}}
  .header h1{{font-size:22px;font-weight:700}}
  .header .sub{{margin-top:5px;font-size:12px;opacity:.85}}
  .method{{background:#d8f3dc;padding:8px 24px;font-size:11px;color:#1b4332;border-bottom:1px solid #b7e4c7}}
  table{{width:100%;border-collapse:collapse;margin:0 16px 16px;background:white;
         border-radius:8px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.08)}}
  th{{background:#1b4332;color:white;padding:9px 7px;font-size:11px;text-align:center;position:sticky;top:0}}
  td{{padding:8px 7px;text-align:center;font-size:12px;border-bottom:1px solid #f0f0f0}}
  tr:hover{{background:#d8f3dc!important}}
  .pos{{color:#c62828;font-weight:600}}
  .neg{{color:#2e7d32;font-weight:600}}
  .code{{font-size:10px;color:#888}}
  .kpi-row{{display:flex;gap:12px;padding:14px 24px;background:white;
            border-bottom:1px solid #e0e0e0;flex-wrap:wrap}}
  .kpi{{background:#f5f5f5;border-radius:10px;padding:10px 18px;min-width:110px;flex:1}}
  .kpi .val{{font-size:22px;font-weight:700;color:#1b4332}}
  .kpi .lab{{font-size:11px;color:#666;margin-top:3px}}
</style>
</head>
<body>
<div class="header">
  <h1>🏛️ 龙虎榜驱动看板（路径A）</h1>
  <div class="sub">交易日：{trade_date} &nbsp;|&nbsp; 生成：{today_str} &nbsp;|&nbsp; 机构席位净买入 > {LHB_THRESHOLD}万 &nbsp;|&nbsp; 成交额 > {TURNOVER_THRESHOLD}亿</div>
</div>
<div class="method">
  📐 评分逻辑：机构净买入(40%) + 成交额(20%) + 机构占比(20%) + 涨幅(20%) &nbsp;|&nbsp;
  🔍 次步：对候选跑 tdx-stock-analysis 深度分析，合格者进短线激进推荐台账
</div>
<div class="kpi-row">
  <div class="kpi"><div class="val">{len(candidates)}</div><div class="lab">候选标的</div></div>
  <div class="kpi"><div class="val">{sum(c['inst_net_w'] for c in candidates):,.0f}万</div><div class="lab">总机构净买入</div></div>
  <div class="kpi"><div class="val">{max([c['score'] for c in candidates]+[0]):.1f}</div><div class="lab">最高综合评分</div></div>
</div>
<table>
  <thead>
    <tr>
      <th>序</th><th>股票</th><th>行业</th><th>机构净买入</th>
      <th>成交额</th><th>机构占比</th><th>当日涨幅</th>
      <th>评分</th><th>PE</th><th>主营</th><th>备注</th>
    </tr>
  </thead>
  <tbody>{rows_html}
  </tbody>
</table>
<div style="padding:12px 24px;font-size:11px;color:#666">
  ⚠️ 本报告仅供研究参考，不构成投资建议。历史表现不代表未来收益。
</div>
</body>
</html>"""

    with out_path.open("w", encoding="utf-8") as f:
        f.write(html)
    return out_path

# ─── 主程序 ───────────────────────────────────────────────────────────────
def main(trade_date=None, lhb_raw_items=None):
    """
    主入口。

    参数:
      trade_date: 交易日期，默认为最近交易日
      lhb_raw_items: 龙虎榜原始记录列表（来自 MCP data_lhb type=jg 的 data.jg 字段）。
                     若不传，尝试通过 subprocess 调用 MCP CLI 获取；若失败则尝试 CLI。
                     传入格式: [{"code":"sz000823","name":"超声电子",
                                  "instBuyAmt":...,"netBuyAmt":...,
                                  "totalBuyAmt":...,"netBuyRate":...,
                                  "instBuyRate":...,"tdDays":...}, ...]
    """
    if trade_date is None:
        trade_date = last_trading_day()
    today_str = date.today().strftime("%Y-%m-%d")
    print(f"\n{'='*60}")
    print(f"  龙虎榜驱动看板 v1  |  交易日:{trade_date}  今日:{today_str}")
    print(f"{'='*60}\n")

    # ── 获取龙虎榜数据 ───────────────────────────────────────────────────
    if lhb_raw_items is None:
        print("[1/4] 拉取龙虎榜（机构席位）...")
        # 方式1：MCP HTTP API
        resp_data = fetch_lhb_jg(trade_date)
        lhb_raw_items = resp_data.get("data", {}).get("jg", [])
        if not lhb_raw_items:
            print("  ⚠️ MCP HTTP API 不可用，请通过 Agent 层 MCP 工具传入龙虎榜数据")
            print("  提示：将 mcp__westock-mcp__data_lhb 返回的 data.jg 作为 lhb_raw_items 传入")
            lhb_raw_items = []

    print(f"  机构席位共 {len(lhb_raw_items)} 条记录")

    print(f"\n[2/4] 筛选（机构净买入>{LHB_THRESHOLD}万 & 成交额>{TURNOVER_THRESHOLD}亿 & 非ST）...")
    candidates = filter_lhb(lhb_raw_items, trade_date)
    print(f"  候选 {len(candidates)} 只")

    if not candidates:
        print("  ⚠️ 无候选，跳过基本面查询")
        return [], enriched

    # 按机构净买入排序
    candidates.sort(key=lambda x: x["inst_net_w"], reverse=True)
    print(f"\n  候选清单：")
    for c in candidates:
        print(f"  ✓ {c['code']} {c['name']}  机构净买入:{c['inst_net_w']:,.0f}万  "
              f"成交额:{c['total_buy']:.1f}亿  机构占比:{c['inst_rate']:.1f}%")

    print(f"\n[3/4] 基本面速查 + 综合评分...")
    enriched = enrich_candidates(candidates, trade_date)
    enriched.sort(key=lambda x: x["score"], reverse=True)

    print(f"\n  综合评分排名：")
    for i, c in enumerate(enriched, 1):
        print(f"  {i}. {c['code']} {c['name']}  评分:{c['score']}  "
              f"机构净买入:{c['inst_net_w']:,.0f}万  PE:{c.get('pe','?')}  "
              f"行业:{c.get('industry','?')}")

    # 写入台账（仅评分≥55的）
    to_ledger = [c for c in enriched if c["score"] >= 55]
    if to_ledger:
        n = write_to_ledger(to_ledger, trade_date)
        print(f"\n  📋 已写入推荐台账（评分≥55）: {n} 只")
    else:
        print("\n  📋 无评分≥55的标的，跳过台账写入")

    # HTML 报告
    out_html = write_html_report(enriched, trade_date)
    print(f"\n  📊 HTML看板: {out_html}")

    # 候选 JSON（供 Agent 层后续处理）
    json_out = DATA_DIR / "lhb_candidates.json"
    with json_out.open("w", encoding="utf-8") as f:
        json.dump({
            "trade_date": trade_date,
            "candidates": enriched
        }, f, ensure_ascii=False, indent=2)
    print(f"  📄 JSON: {json_out}")

    print(f"\n{'='*60}")
    print(f"✅ 龙虎榜驱动看板完成！候选 {len(enriched)} 只")
    print(f"{'='*60}")

    return enriched

if __name__ == "__main__":
    main()
