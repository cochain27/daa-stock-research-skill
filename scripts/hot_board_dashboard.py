#!/usr/bin/env python3
"""
hot_board_dashboard.py  — 热点板块驱动看板 v3（路径B）
逻辑：热点板块动量 + 行业资金流排名（龙龙股） → 综合评分 → HTML看板
数据源：WestockData CLI (npx westock-data-clawhub)
"""
import sys, subprocess, json, csv
import os
from datetime import date, timedelta

VENV_SITE = "/Users/chenyuting/.workbuddy/binaries/python/envs/default/lib/python3.13/site-packages"
sys.path.insert(0, VENV_SITE)
import pandas as pd

# ─── 工具函数 ────────────────────────────────────────────────────────────
def clean_env():
    env = os.environ.copy()
    for k in list(env):
        if "proxy" in k.lower():
            del env[k]
    return env

def wcmd(args, timeout=90):
    """执行 WestockData CLI，返回 stdout+stderr"""
    r = subprocess.run(
        ["npx", "-y", "westock-data-clawhub@1.0.4"] + args,
        capture_output=True, text=True, timeout=timeout, env=clean_env()
    )
    return r.stdout + r.stderr

def parse_md(raw):
    """Markdown table → DataFrame"""
    lines = [l for l in raw.strip().splitlines()
             if l.startswith("|") and "---" not in l]
    if len(lines) < 2:
        return pd.DataFrame()
    headers = [h.strip() for h in lines[0].split("|")[1:-1]]
    rows = []
    for l in lines[1:]:
        vals = [v.strip() for v in l.split("|")[1:-1]]
        if len(vals) == len(headers):
            rows.append(vals)
    return pd.DataFrame(rows, columns=headers)

def last_trading_day():
    td = date.today()
    for delta in range(8):
        d = td - timedelta(days=delta)
        if d.weekday() < 5:
            return d.strftime("%Y-%m-%d")
    return td.strftime("%Y-%m-%d")

def to_float(val, default=0.0):
    try:
        return float(str(val).replace(",", "").replace("%", ""))
    except:
        return default

# ─── 数据拉取 ─────────────────────────────────────────────────────────────
def fetch_hot_boards(limit=15):
    """热点板块（涨幅排行）"""
    raw = wcmd(["hot", "board", "--limit", str(limit), "--raw"])
    df = parse_md(raw)
    if df.empty:
        return {}
    # 返回 {name: {symbol, zdf, zxj}}
    result = {}
    for _, r in df.iterrows():
        name = str(r.get("name", "")).strip()
        if name and name != "name":
            result[name] = {
                "symbol": str(r.get("symbol", "")),
                "zdf": to_float(r.get("zdf", 0)),
                "zxj": to_float(r.get("zxj", 0)) / 1e8,  # 亿
            }
    return result

def fetch_sector_ranking(rtype="mainNetInflow", limit=30):
    """
    行业资金流排名（sector ranking 包含龙头股信息）
    返回 [{name, code, zdf, turnover, turnoverRate, mainNetInflow,
            leader_code, leader_name, leader_chgPct}, ...]
    """
    raw = wcmd([
        "sector", "ranking", "--kind", "industry",
        "--type", rtype, "--order", "desc",
        "--limit", str(limit), "--raw"
    ])
    df = parse_md(raw)
    if df.empty:
        # WestockData CLI 没有 sector ranking，降级用 board
        return _fetch_board_with_leaders()
    result = []
    for _, r in df.iterrows():
        leader_info = {}
        for col in df.columns:
            if "leader" in col.lower() and col.lower() != "leader":
                pass
        # 从原始输出找龙头股（board 非 raw 有 leadStock）
        board_raw = wcmd(["board"])
        lead_map = _parse_lead_stocks(board_raw)
        name = str(r.get("name", "")).strip()
        lead = lead_map.get(name, {})
        result.append({
            "name": name,
            "symbol": str(r.get("symbol", "")),
            "zdf": to_float(r.get("changePct", r.get("zdf", 0))),
            "turnover": to_float(r.get("turnover", 0)),
            "turnoverRate": to_float(r.get("turnoverRate", 0)),
            "mainNetInflow": to_float(r.get("mainNetInflow", 0)) / 1e4,
            "mainNetInflow5d": to_float(r.get("mainNetInflow5d", 0)) / 1e4,
            "mainNetInflow20d": to_float(r.get("mainNetInflow20d", 0)) / 1e4,
            "mainInflow": to_float(r.get("mainInflow", 0)) / 1e4,
            "upCount": str(r.get("upCount", "")),
            "leader_code": lead.get("code", ""),
            "leader_name": lead.get("name", ""),
            "leader_chgPct": lead.get("chg", 0.0),
        })
    return result

def _parse_lead_stocks(raw_board):
    """从 board 非 raw 输出解析龙头股"""
    lead_map = {}
    for line in raw_board.splitlines():
        if not line.startswith("|") or "---" in line:
            continue
        cols = [c.strip() for c in line.split("|")[1:-1]]
        if len(cols) < 6:
            continue
        name, lead_str = cols[0], cols[5]
        if not name or name in ("name", "") or not lead_str or lead_str in ("nan", ""):
            continue
        # "光电股份(10.02)" → (name, chg)
        try:
            if "(" in lead_str:
                sname = lead_str[:lead_str.rindex("(")].strip()
                schg = float(lead_str[lead_str.rindex("(")+1:-2])
            else:
                sname = lead_str.strip()
                schg = 0.0
            lead_map[name] = {"name": sname, "chg": schg}
        except:
            pass
    return lead_map

def _fetch_board_with_leaders():
    """降级方案：board 接口 + 龙头股解析"""
    raw = wcmd(["board"])
    result = []
    lead_map = _parse_lead_stocks(raw)
    df = parse_md(raw)
    for _, r in df.iterrows():
        name = str(r.get("name", "")).strip()
        if not name or name in ("name", ""):
            continue
        lead = lead_map.get(name, {})
        result.append({
            "name": name,
            "symbol": "",
            "zdf": to_float(r.get("changePct", 0)),
            "turnover": to_float(r.get("turnoverRate", 0)),
            "turnoverRate": to_float(r.get("turnoverRate", 0)),
            "mainNetInflow": 0,
            "mainNetInflow5d": 0,
            "mainNetInflow20d": 0,
            "mainInflow": 0,
            "upCount": str(r.get("upCount", "")),
            "leader_code": "",
            "leader_name": lead.get("name", ""),
            "leader_chgPct": lead.get("chg", 0.0),
        })
    return result

def fetch_fund_flow(code, trade_date):
    """个股主力资金流（涨幅从 K 线计算，不用 asfund 的 FwdClosePrice）"""
    # K 线取最新两天收盘价算涨幅
    raw_kl = wcmd(["kline", code, "--period", "day", "--limit", "2", "--fq", "qfq"])
    df_kl = parse_md(raw_kl)
    chg_pct = 0.0
    close = 0.0
    prev_close = 0.0
    if not df_kl.empty:
        for col in df_kl.columns:
            if col.lower() in ("last", "close"):
                try:
                    close = float(df_kl[col].iloc[0])
                    prev_close = float(df_kl[col].iloc[1]) if len(df_kl) > 1 else close
                    chg_pct = (close - prev_close) / prev_close * 100 if prev_close > 0 else 0.0
                    break
                except:
                    pass

    raw = wcmd(["asfund", code, "--date", trade_date])
    df = parse_md(raw)
    net_raw = inflow_raw = outflow_raw = 0.0
    if not df.empty:
        row = df.iloc[0]
        net_raw = to_float(row.get("MainNetFlow", 0))
        inflow_raw = to_float(row.get("MainInflow", 0))
        outflow_raw = to_float(row.get("MainOutFlow", 0))

    # 龙虎榜席位
    lhb_seats = ""
    lhb_col = next((c for c in df.columns
                     if "LhbTradingDetails" in c or "lhb" in c.lower()), None)
    if lhb_col and not df.empty:
        try:
            details = json.loads(str(df.iloc[0][lhb_col]))
            org_buys, hot_buys = [], []
            for d in (details if isinstance(details, list) else []):
                tags = str(d.get("HotMoneyTags", ""))
                if d.get("RankType") == "买入营业部排行榜":
                    buy_amt = to_float(d.get("Buy", 0)) / 1e4
                    if "机构" in d.get("Name", ""):
                        org_buys.append(f"机构({buy_amt:.0f}万)")
                    elif "热" in tags or "游资" in tags:
                        hot_buys.append(f"{d.get('Name','')[:8]}({buy_amt:.0f}万)")
            if org_buys:
                lhb_seats += "🏛️" + ";".join(org_buys[:2])
            if hot_buys:
                if lhb_seats: lhb_seats += " | "
                lhb_seats += "🐂" + ";".join(hot_buys[:2])
        except:
            pass

    return {
        "net": net_raw / 1e4,
        "inflow": inflow_raw / 1e4,
        "outflow": outflow_raw / 1e4,
        "close": close,
        "prev_close": prev_close,
        "chgPct": chg_pct,
        "lhb_seats": lhb_seats,
    }

def fetch_profile(code):
    """基本面速查"""
    raw = wcmd(["profile", code])
    info = {}
    for line in raw.splitlines():
        if ":" in line and not line.startswith("|") and "===" not in line:
            k, v = line.split(":", 1)
            k, v = k.strip(), v.strip()
            if k in ("industry", "sector", "business", "name"):
                info[k] = v[:80]
    return {
        "industry": info.get("industry", "--"),
        "business": info.get("business", "--")[:60],
    }

# ─── 综合评分 ─────────────────────────────────────────────────────────────
def composite_score(board_chg, board_net_in, stock_net_in, stock_chg, turnover_rate):
    """
    综合评分 = 板块涨幅(25%) + 板块资金(20%) + 个股资金(30%) + 个股涨幅(15%) + 换手率加成(10%)
    归一化阈值基于市场经验值
    """
    def norm(val, lo, hi):
        if hi <= lo: return 50
        return max(0, min(100, (val - lo) / (hi - lo) * 100))

    board_momentum = norm(board_chg,    -4,    7) * 0.25
    board_fund     = norm(board_net_in, -10000, 60000) * 0.20
    stock_fund     = norm(stock_net_in, -2000, 20000) * 0.30
    stock_chg_s    = norm(stock_chg,   -5,    15) * 0.15
    turnover_bonus = norm(turnover_rate, 1,    15) * 0.10
    return round(board_momentum + board_fund + stock_fund + stock_chg_s + turnover_bonus, 1)

# ─── 搜索股票代码（名称 → sz/sh代码）────────────────────────────────────
def search_code(name):
    raw = wcmd(["search", name, "--stock"])
    df = parse_md(raw)
    for col in ["code", "symbol", "Symbol", "Code"]:
        if col in df.columns:
            for v in df[col]:
                v = str(v)
                if v.startswith("sz") or v.startswith("sh"):
                    return v
    # 裸码兜底
    for col in ["code", "symbol"]:
        if col in df.columns:
            for v in df[col]:
                v = str(v).strip()
                if v.isdigit() and len(v) == 6:
                    return ("sz" if v.startswith(("0","3")) else "sh") + v
    return None

# ─── 主逻辑 ────────────────────────────────────────────────────────────────
def build_dashboard(
    out_html="/Users/chenyuting/Documents/workbuddy/workbuddy-daa/daa-stock-research-skill/data/hot_board_dashboard.html",
    out_csv=None,
):
    today = date.today().strftime("%Y-%m-%d")
    trade_date = last_trading_day()
    print(f"\n{'='*62}")
    print(f"  热点板块驱动看板 v3  |  交易日:{trade_date}  今日:{today}")
    print(f"{'='*62}")

    # Step 1: 热点板块
    print("\n[1/4] 拉取热点板块...")
    hot_boards = fetch_hot_boards(limit=15)
    hot_names = list(hot_boards.keys())
    print(f"  共 {len(hot_boards)} 个板块，Top5: {hot_names[:5]}")

    # Step 2: 行业资金流排名（找龙头股）
    print("\n[2/4] 拉取行业资金流排名（龙头股）...")
    sectors = fetch_sector_ranking(rtype="mainNetInflow", limit=30)
    print(f"  共 {len(sectors)} 个行业板块")

    # 合并热点涨幅
    for s in sectors:
        if s["name"] in hot_boards:
            s["hot_zdf"] = hot_boards[s["name"]]["zdf"]
        else:
            s["hot_zdf"] = s["zdf"]

    # 候选：热点板块（当日涨幅 > 0）
    candidates = [s for s in sectors if s["hot_zdf"] > 0 and s["leader_name"]]
    if not candidates:
        candidates = [s for s in sectors if s["leader_name"]][:8]
    print(f"  候选板块 {len(candidates)} 个: {[s['name'] for s in candidates[:5]]}")

    # Step 3: 查龙头个股资金流
    print("\n[3/4] 拉取龙头个股资金流...")
    stocks = []
    checked_names = set()

    for s in candidates[:8]:
        lname = s["leader_name"]
        if not lname or lname in checked_names:
            continue
        checked_names.add(lname)

        # 名称 → 代码
        code = search_code(lname)
        if not code:
            print(f"  ⚠️ 找不到 {lname} 代码，跳过")
            continue

        # 资金流（今日）
        ff = fetch_fund_flow(code, trade_date)

        # 如果今日资金流为0（周末），说明天没数据，尝试用昨天的
        if not ff or ff.get("net", 0) == 0:
            # 周六/周日 → 找上一个交易日
            prev_raw = wcmd(["asfund", code])
            df_prev = parse_md(prev_raw)
            if not df_prev.empty:
                row = df_prev.iloc[0]
                net_raw = to_float(row.get("MainNetFlow", 0))
                inflow_raw = to_float(row.get("MainInflow", 0))
                outflow_raw = to_float(row.get("MainOutFlow", 0))
                close = to_float(row.get("ClosePrice", 0))
                prev = to_float(row.get("FwdClosePrice", 0))
                chg_pct = (close - prev) / prev * 100 if prev > 0 else 0.0
                ff = {
                    "net": net_raw / 1e4,
                    "inflow": inflow_raw / 1e4,
                    "outflow": outflow_raw / 1e4,
                    "close": close,
                    "chgPct": chg_pct,
                    "lhb_seats": "",
                }

        if not ff:
            print(f"  ⚠️ {code} {lname} 无资金流数据，跳过")
            continue

        score = composite_score(
            board_chg=s["hot_zdf"],
            board_net_in=s["mainNetInflow"],
            stock_net_in=ff.get("net", 0),
            stock_chg=ff.get("chgPct", 0),
            turnover_rate=s.get("turnoverRate", 0),
        )

        # 基本面速查（并发太慢，顺序查 Top3）
        prof = fetch_profile(code) if score >= 50 else {"industry": "--", "business": "--"}

        net_in = ff.get("net", 0)
        stocks.append({
            "code": code,
            "name": lname,
            "board": s["name"],
            "board_chg": s["hot_zdf"],
            "board_net_in": s["mainNetInflow"],
            "board_turnover_rate": s.get("turnoverRate", 0),
            "board_up_count": s.get("upCount", ""),
            "close": ff.get("close", 0),
            "leader_chg": ff.get("chgPct", 0),
            "stock_net_in": net_in,
            "stock_net_in5d": s.get("mainNetInflow5d", 0),
            "stock_net_in20d": s.get("mainNetInflow20d", 0),
            "main_in": ff.get("inflow", 0),
            "main_out": ff.get("outflow", 0),
            "score": score,
            "lhb_seats": ff.get("lhb_seats", ""),
            "industry": prof.get("industry", "--"),
            "business": prof.get("business", "--"),
        })
        print(f"  ✓ {code} {lname} 板块:{s['name']} 板块涨幅{s['hot_zdf']:+.2f}% "
              f"主力净流入:{net_in:+,.0f}万 评分:{score}")

    if not stocks:
        print("\n❌ 无候选标的（可能是周末/节假日无交易数据）")
        return None, []

    # 按评分排序
    stocks.sort(key=lambda x: -x["score"])

    # ── CSV ────────────────────────────────────────────────────────────
    if out_csv is None:
        out_csv = out_html.replace(".html", ".csv")
    csv_path = out_csv
    fieldnames = [
        "score","code","name","board","industry","business",
        "board_chg","board_net_in","board_turnover_rate","board_up_count",
        "leader_chg","close","stock_net_in","stock_net_in5d","stock_net_in20d",
        "main_in","main_out","lhb_seats"
    ]
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for s in stocks:
            w.writerow({k: s[k] for k in fieldnames})
    print(f"\n  CSV: {csv_path}")

    # ── HTML 看板 ──────────────────────────────────────────────────────
    print("[4/4] 生成 HTML 看板...")
    html = _make_html(stocks, trade_date, today)
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  看板: {out_html}")
    return out_html, stocks


# ─── HTML 生成 ─────────────────────────────────────────────────────────────
def _make_html(stocks, trade_date, today_str):
    total = len(stocks)
    avg_score = sum(s["score"] for s in stocks) / total
    hot_boards = list({s["board"] for s in stocks})
    top_board = stocks[0]["board"] if stocks else "--"
    pos_fund = sum(1 for s in stocks if s["stock_net_in"] > 0)
    pos_chg = sum(1 for s in stocks if s["leader_chg"] > 0)

    rows_html = ""
    for i, s in enumerate(stocks):
        rank_colors = ["#e8f5e9","#f1f8e9","#fffde7","white","white","white","white","white"]
        bg = rank_colors[i] if i < len(rank_colors) else "white"
        badges = ["🥇","🥈","🥉","④","⑤","⑥","⑦","⑧"]
        badge = badges[i] if i < len(badges) else f" {i+1}"
        score_clr = "#1565c0" if s["score"]>=70 else ("#2e7d32" if s["score"]>=50 else "#555")
        rank_tag = "🔥强" if s["score"]>=70 else ("📈关注" if s["score"]>=50 else "⚠️观察")
        chg_cls = "pos" if s["leader_chg"]>=0 else "neg"
        board_cls = "pos" if s["board_chg"]>=0 else "neg"
        net_cls = "pos" if s["stock_net_in"]>=0 else "neg"
        lhb_html = f'<br><span style="font-size:10px;color:#888">{s["lhb_seats"]}</span>' if s["lhb_seats"] else ""
        rows_html += f"""<tr style="background:{bg}">
  <td style="font-size:18px;text-align:center">{badge}</td>
  <td><strong>{s['name']}</strong><br><span class="code">{s['code']}</span>{lhb_html}</td>
  <td>{s['board']}</td>
  <td class="{board_cls}">{s['board_chg']:+.2f}%</td>
  <td class="{chg_cls}">{s['leader_chg']:+.2f}%</td>
  <td>{s['close']:.2f}</td>
  <td class="{net_cls}">{s['stock_net_in']:+,.0f}</td>
  <td class="{net_cls}">{s['stock_net_in5d']:+,.0f}</td>
  <td class="{net_cls}">{s['stock_net_in20d']:+,.0f}</td>
  <td><span style="font-size:16px;font-weight:bold;color:{score_clr}">{s['score']}</span></td>
  <td>{rank_tag}</td>
  <td>{s['board_turnover_rate']:.1f}%</td>
  <td>{s['industry'][:14]}</td>
  <td>{s['business'][:22]}</td>
</tr>"""

    filter_btns = "".join(
        f'<button class="filter-btn" onclick="filterBoard(\'{b}\')">{b}</button>'
        for b in hot_boards[:6]
    )

    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>热点板块驱动看板  {trade_date}</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
         background:#f0f2f5;color:#1a1a2e;font-size:13px}}
  .header{{background:linear-gradient(135deg,#1a237e,#283593);color:white;padding:18px 28px}}
  .header h1{{font-size:22px;font-weight:700;letter-spacing:1px}}
  .header .sub{{margin-top:5px;font-size:12px;opacity:.85}}
  .kpi-row{{display:flex;gap:12px;padding:14px 24px;background:white;
            border-bottom:1px solid #e0e0e0;flex-wrap:wrap}}
  .kpi{{background:#f5f5f5;border-radius:10px;padding:10px 18px;min-width:110px;flex:1}}
  .kpi .val{{font-size:22px;font-weight:700;color:#1a237e}}
  .kpi .lab{{font-size:11px;color:#666;margin-top:3px}}
  .filters{{padding:10px 24px;background:white;display:flex;gap:8px;
            flex-wrap:wrap;align-items:center;border-bottom:1px solid #eee}}
  .filter-btn{{padding:4px 13px;border-radius:20px;border:1px solid #c5cae9;
                background:white;cursor:pointer;font-size:12px;transition:.2s}}
  .filter-btn.active,.filter-btn:hover{{background:#3949ab;color:white;border-color:#3949ab}}
  table{{width:100%;border-collapse:collapse;margin:0 16px 16px;background:white;
         border-radius:8px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.08)}}
  th{{background:#1a237e;color:white;padding:9px 7px;font-size:11px;text-align:center;position:sticky;top:0}}
  td{{padding:8px 7px;text-align:center;font-size:12px;border-bottom:1px solid #f0f0f0}}
  tr:hover{{background:#e8eaf6!important}}
  .pos{{color:#c62828;font-weight:600}}
  .neg{{color:#2e7d32;font-weight:600}}
  .code{{font-size:10px;color:#888}}
  .legend{{padding:10px 24px;display:flex;gap:20px;font-size:11px;color:#666;flex-wrap:wrap}}
  .legend span{{display:flex;align-items:center;gap:5px}}
  .dot{{width:9px;height:9px;border-radius:50%;display:inline-block}}
  .method{{background:#fff8e1;padding:8px 24px;font-size:11px;color:#795548;
           border-bottom:1px solid #ffe082}}
  @media(max-width:900px){{table{{font-size:11px}}th,td{{padding:5px 3px}}}}
</style>
</head>
<body>

<div class="header">
  <h1>🔥 热点板块驱动看板（路径B）</h1>
  <div class="sub">交易日：{trade_date} &nbsp;|&nbsp; 生成：{today_str} &nbsp;|&nbsp; 板块动量×主力资金×综合评分</div>
</div>
<div class="method">📐 综合评分权重：板块涨幅25% + 板块主力净流入20% + 个股主力净流入30% + 个股涨幅15% + 换手率加成10%</div>

<div class="kpi-row">
  <div class="kpi"><div class="val">{total}</div><div class="lab">候选标的</div></div>
  <div class="kpi"><div class="val">{len(hot_boards)}</div><div class="lab">涉及板块</div></div>
  <div class="kpi"><div class="val">{avg_score:.0f}</div><div class="lab">平均评分</div></div>
  <div class="kpi"><div class="val">{pos_fund}/{total}</div><div class="lab">资金净流入</div></div>
  <div class="kpi"><div class="val">{pos_chg}/{total}</div><div class="lab">当日上涨</div></div>
  <div class="kpi"><div class="val" style="color:#c62828;font-size:18px">{top_board}</div><div class="lab">最强板块</div></div>
</div>

<div class="filters">
  <span style="font-size:12px;color:#555;font-weight:600;">排序：</span>
  <button class="filter-btn active" onclick="sortBy('score')">综合评分</button>
  <button class="filter-btn" onclick="sortBy('board_chg')">板块涨幅</button>
  <button class="filter-btn" onclick="sortBy('leader_chg')">个股涨幅</button>
  <button class="filter-btn" onclick="sortBy('stock_net_in')">主力净流入</button>
  <button class="filter-btn" onclick="sortBy('stock_net_in5d')">5日净流入</button>
  <span style="margin-left:18px;font-size:12px;color:#555;font-weight:600;">板块筛选：</span>
  <button class="filter-btn active" onclick="filterBoard('all')">全部</button>
  {filter_btns}
</div>

<table id="tbl">
<thead>
<tr>
  <th>排名</th><th>个股</th><th>所属板块</th>
  <th>板块涨幅</th><th>个股涨幅</th><th>收盘</th>
  <th>主力净流入(万)</th><th>5日净流入(万)</th><th>20日净流入(万)</th>
  <th>综合评分</th><th>信号</th><th>换手率</th><th>行业</th><th>主营业务</th>
</tr>
</thead>
<tbody id="tb">{rows_html}</tbody>
</table>

<div class="legend">
  <span><span class="dot" style="background:#c62828"></span> 红色=上涨/净流入</span>
  <span><span class="dot" style="background:#2e7d32"></span> 绿色=下跌/净流出</span>
  <span><span style="background:#3949ab;color:white;padding:1px 6px;border-radius:3px">🔥强</span> 评分≥70</span>
  <span><span style="background:#43a047;color:white;padding:1px 6px;border-radius:3px">📈关注</span> 评分50-69</span>
  <span><span style="background:#757575;color:white;padding:1px 6px;border-radius:3px">⚠️观察</span> 评分&lt;50</span>
  <span>🏛️=机构席位  🐂=游资席位</span>
</div>

<script>
let rows = Array.from(document.querySelectorAll('#tb tr'));
const cols = {{score:9,board_chg:3,leader_chg:4,stock_net_in:6,stock_net_in5d:7,stock_net_in20d:8}};
function sortBy(col){{
  document.querySelectorAll('.filters .filter-btn').forEach(b=>b.classList.remove('active'));
  event.target.classList.add('active');
  const idx = cols[col]||9;
  rows.sort((a,b)=>{{let av=parseFloat(a.cells[idx].textContent.replace(/[^\\d.\\-]/g,''))||0;
                     let bv=parseFloat(b.cells[idx].textContent.replace(/[^\\d.\\-]/g,''))||0;
                     return bv-av}});
  const tb=document.getElementById('tb');rows.forEach(r=>tb.appendChild(r));
}}
function filterBoard(name){{
  document.querySelectorAll('.filters .filter-btn').forEach(b=>b.classList.remove('active'));
  event.target.classList.add('active');
  rows.forEach(r=>{{r.style.display=(name==='all'||r.cells[2].textContent.includes(name))?'':'none'}});
}}
</script>
</body>
</html>"""


if __name__ == "__main__":
    out_html = "/Users/chenyuting/Documents/workbuddy/workbuddy-daa/daa-stock-research-skill/data/hot_board_dashboard.html"
    path, results = build_dashboard(out_html=out_html)
    if results:
        print(f"\n✅ 看板生成完成！")
        print("\n【Top-3 候选】")
        for i, s in enumerate(results[:3], 1):
            print(f"  {i}. {s['code']} {s['name']}（{s['board']}）"
                  f"\n     板块涨幅 {s['board_chg']:+.2f}%  个股涨幅 {s['leader_chg']:+.2f}%"
                  f"\n     主力净流入 {s['stock_net_in']:+,.0f}万  综合评分 {s['score']}"
                  f"{'  '+s['lhb_seats'] if s['lhb_seats'] else ''}")
        print(f"\n  📊 HTML看板：{path}")
        print(f"  📄 CSV数据：{path.replace('.html','.csv')}")
    else:
        print("\n⚠️ 无候选（今日为周末/节假日无交易数据），周一再跑即可。")
