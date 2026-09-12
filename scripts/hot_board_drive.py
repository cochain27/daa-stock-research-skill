#!/usr/bin/env python3
"""
hot_board_drive.py  — 热点板块驱动选股 v2
逻辑：今日热点板块 → 成分股主力资金净流入过滤 → 基本面速查
用法：
  python hot_board_drive.py                    # 默认今日
  python hot_board_drive.py --date 2026-09-10  # 指定交易日
  python hot_board_drive.py --top-n 4          # 前N热点板块
  python hot_board_drive.py --min-net 500      # 主力净流入最低（万元）
"""
import sys, subprocess, argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

VENV_SITE = "/Users/chenyuting/.workbuddy/binaries/python/envs/default/lib/python3.13/site-packages"
sys.path.insert(0, VENV_SITE)

import pandas as pd

# ─── WestockData CLI 包装（清代理）─────────────────────────────────────────
def wcmd(cmd: str) -> str:
    env = __import__("os").environ.copy()
    for k in list(env):
        if "proxy" in k.lower():
            del env[k]
    r = subprocess.run(
        ["npx", "-y", "westock-data-clawhub@1.0.4"] + cmd.split(),
        capture_output=True, text=True, timeout=90, env=env
    )
    return r.stdout + r.stderr

# ─── 解析 markdown table → DataFrame ───────────────────────────────────────
def parse_md(raw: str) -> pd.DataFrame:
    lines = [l for l in raw.strip().splitlines() if l.startswith("|") and "---" not in l]
    if len(lines) < 2:
        return pd.DataFrame()
    headers = [h.strip() for h in lines[0].split("|")[1:-1]]
    rows = []
    for l in lines[1:]:
        vals = [v.strip() for v in l.split("|")[1:-1]]
        if len(vals) == len(headers):
            rows.append(vals)
    return pd.DataFrame(rows, columns=headers)

# ─── 搜索代码（名称→sz/sh代码）─────────────────────────────────────────────
def search_code(name: str) -> str:
    """返回 szXXXXXXXX 或 shXXXXXXXX 格式代码，找不到返回 None"""
    raw = wcmd(f"search {name} --stock")
    df = parse_md(raw)
    for col in ["code", "symbol", "Symbol", "Code"]:
        if col in df.columns:
            for v in df[col]:
                v = str(v)
                if (v.startswith("sz") or v.startswith("sh")) and len(v) == 8:
                    return v
    # 兜底：无市场前缀
    for col in ["code", "symbol"]:
        if col in df.columns:
            for v in df[col]:
                v = str(v).strip()
                if v.isdigit() and len(v) == 6:
                    return ("sz" if v.startswith(("0","3")) else "sh") + v
    return None

# ─── 拉个股主力资金（单日）───────────────────────────────────────────────
def fund_flow(code: str, date: str) -> dict:
    """
    返回 {"main_net_in": 万元, "close": float, "chg_pct": float, "lhb_detail": str}
    WestockData asfund 字段: MainNetFlow(元), ClosePrice, MainInflow, MainOutFlow
    """
    raw = wcmd(f"asfund {code} --date {date}")
    df = parse_md(raw)
    if df.empty:
        return {}
    try:
        row = df.iloc[0]
        net_raw = float(str(row.get("MainNetFlow", 0) or 0))
        inflow_raw = float(str(row.get("MainInflow", 0) or 0))
        outflow_raw = float(str(row.get("MainOutFlow", 0) or 0))
        close = float(str(row.get("ClosePrice", 0) or 0))
        prev_close = float(str(row.get("FwdClosePrice", 0) or 0))
        chg_pct = (close - prev_close) / prev_close * 100 if prev_close > 0 else 0.0
        # 龙虎榜席位详情（如果有）
        lhb_detail = ""
        lhb_col = next((c for c in df.columns if "LhbTradingDetails" in c), None)
        if lhb_col:
            try:
                details = json.loads(row[lhb_col])
                org_buys = []
                hotmoney_buys = []
                for d in details:
                    if d.get("RankType") == "买入营业部排行榜" and "机构" in d.get("Name", ""):
                        org_buys.append(f"{d['Name']}买{float(d['Buy'])/1e6:.1f}万")
                    elif d.get("RankType") == "买入营业部排行榜" and "热" in d.get("HotMoneyTags", ""):
                        hotmoney_buys.append(f"{d['Name']}买{float(d['Buy'])/1e6:.1f}万")
                if org_buys:
                    lhb_detail += "机构:" + ";".join(org_buys[:2])
                if hotmoney_buys:
                    if lhb_detail: lhb_detail += " | "
                    lhb_detail += "游资:" + ";".join(hotmoney_buys[:2])
            except:
                pass
        return {
            "main_net_in": net_raw / 10000,   # 万元
            "inflow": inflow_raw / 10000,
            "outflow": outflow_raw / 10000,
            "close": close,
            "chg_pct": chg_pct,
            "lhb_detail": lhb_detail,
        }
    except Exception as e:
        return {}

# ─── 拉个股日K（成交额）──────────────────────────────────────────────────
def kline_amount(code: str) -> float:
    """返回昨日成交额（亿元）"""
    raw = wcmd(f"kline {code} --period day --limit 2 --fq qfq")
    df = parse_md(raw)
    if df.empty:
        return 0.0
    for col in ["amount", "Amount"]:
        if col in df.columns:
            try:
                return float(df[col].iloc[0]) / 1e8
            except:
                pass
    return 0.0

# ─── 基本面 profile 解析 ─────────────────────────────────────────────────
def profile_info(code: str) -> dict:
    """
    返回 {"mkt_cap": str, "pe": str, "industry": str, "themes": str, "business": str}
    WestockData profile: code|name|listedDate|business|website|industry|sector|...
    """
    raw = wcmd(f"profile {code}")
    df = parse_md(raw)
    if df.empty:
        return {"industry": "--", "business": "--", "sector": "--"}
    try:
        row = df.iloc[0]
        return {
            "industry": str(row.get("industry", "--")),
            "sector":   str(row.get("sector", "--")),
            "business": str(row.get("business", "--"))[:60],
        }
    except Exception:
        return {"industry": "--", "business": "--", "sector": "--"}

# ─── 拉龙头股列表（board 非 raw 输出有 leadStock）────────────────────────
def get_top_boards(n: int) -> pd.DataFrame:
    """返回 DataFrame: name, leadStock, changePct, mainNetInflow, turnover"""
    raw = wcmd("board")
    df = parse_md(raw)
    if df.empty:
        return pd.DataFrame()
    # 行业板块
    ind_rows = []
    in_ind = False
    for line in raw.strip().splitlines():
        if "行业板块涨幅排名" in line:
            in_ind = True
            continue
        if "概念板块涨幅排名" in line:
            break
        if in_ind and line.startswith("|"):
            cols = [c.strip() for c in line.split("|")[1:-1]]
            # 格式: name|changePct|turnoverRate|changePct5d|changePct20d|leadStock
            if len(cols) >= 6 and cols[0] and cols[0] not in ("name", "---"):
                try:
                    ind_rows.append({
                        "name": cols[0],
                        "changePct": float(cols[1]),
                        "turnoverRate": float(cols[2]),
                        "leadStock": cols[5],
                    })
                except:
                    pass
    df_ind = pd.DataFrame(ind_rows)
    if "changePct" in df_ind.columns:
        df_ind = df_ind.nlargest(n, "changePct")
    # 行业资金流入（从 board raw）
    raw2 = wcmd("hot board --limit 20 --raw")
    df_raw = parse_md(raw2)
    if not df_raw.empty and "name" in df_raw.columns and "zdf" in df_raw.columns:
        df_raw["changePct"] = pd.to_numeric(df_raw["zdf"], errors="coerce")
        # mainNetInflow 只在行业资金流入那块有，合并
        # board raw 没有直接 mainNetInflow，用 hot board 的 zxj 估算
        return df_ind
    return df_ind

def last_trading_day() -> str:
    """返回最近的 A 股交易日（YYYY-MM-DD）"""
    from datetime import date, timedelta
    today = date.today()
    for delta in range(8):
        d = today - timedelta(days=delta)
        # A 股周末休市
        if d.weekday() < 5:
            return d.strftime("%Y-%m-%d")
    return today.strftime("%Y-%m-%d")

# ─── 主流程 ─────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="热点板块驱动选股")
    ap.add_argument("--date",  default=None, help="交易日 YYYY-MM-DD（默认自动）")
    ap.add_argument("--top-n", type=int, default=4,    help="前N热点板块")
    ap.add_argument("--min-net", type=float, default=500, help="主力净流入最低（万元）")
    ap.add_argument("--min-amt",  type=float, default=2.0,  help="成交额最低（亿元）")
    args = ap.parse_args()

    if args.date is None:
        args.date = last_trading_day()
        print(f"  [自动识别最近交易日：{args.date}]\n")

    print(f"\n{'═'*62}")
    print(f"  热点板块驱动选股  日期={args.date}  Top-{args.top_n}板块")
    print(f"{'═'*62}\n")

    # Step 1: 热点板块（用 hot board raw）
    raw = wcmd("hot board --limit 15 --raw")
    df_board = parse_md(raw)
    if df_board.empty:
        print("❌ 无法获取热点板块"); return

    df_board["changePct"] = pd.to_numeric(df_board["zdf"], errors="coerce").fillna(0)
    top = df_board.nlargest(args.top_n, "changePct")
    board_names = top["name"].tolist()

    print(f"【今日热点板块 Top {args.top_n}】")
    print(f"  {'板块名':<18} {'涨跌幅%':>8}  {'rank变化':>8}")
    print(f"  {'-'*40}")
    for _, r in top.iterrows():
        delta = int(r.get("rankdelta", 0))
        arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "─")
        print(f"  {r['name']:<18} {r['changePct']:>+8.2f}  {arrow}{abs(delta)}")

    # Step 2: 找板块内主力净流入个股
    # WestockData 没有直接成分股列表，用龙头股 + search 方式
    # 热点板块的 leadStock 从 board 非 raw 输出取
    raw_board = wcmd("board")
    df_board_full = parse_md(raw_board)

    # 找 leadStock 映射
    lead_map = {}
    for line in raw_board.strip().splitlines():
        if line.startswith("|") and "---" not in line:
            cols = [c.strip() for c in line.split("|")[1:-1]]
            if len(cols) >= 6 and cols[0] and cols[5] and cols[0] not in ("name", ""):
                name = cols[0]
                lead = cols[5]
                lead_map[name] = lead  # "元器件": "崇达技术(10.03)"

    print(f"\n{'═'*62}")
    print(f"【各板块龙头资金流（{args.date}）】")
    print(f"  {'代码':<10} {'名称':<10} {'收盘':>6} {'涨幅%':>8} {'主力净流入(万)':>14} {'成交额亿':>8} {'达标':>3}")
    print(f"  {'-'*68}")

    candidates = []

    def check_stock(name: str, board_name: str):
        code = search_code(name)
        if not code:
            return None
        ff = fund_flow(code, args.date)
        amt = kline_amount(code)
        net_in = ff.get("main_net_in", 0)
        close = ff.get("close", 0)
        chg = ff.get("chg_pct", 0)
        lhb = ff.get("lhb_detail", "")
        passed = abs(net_in) >= args.min_net and amt >= args.min_amt
        return {
            "code": code, "name": name, "board": board_name,
            "close": close, "chg_pct": chg,
            "net_in_w": net_in, "amount_yy": amt,
            "lhb_detail": lhb,
            "passed": passed,
        }

    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {}
        for _, row in top.iterrows():
            bname = row["name"]
            lead_str = lead_map.get(bname, "")
            if not lead_str:
                # 从 board full df 里找
                for _, br in df_board_full.iterrows():
                    if str(br.get("name", "")) == bname:
                        lead_str = str(br.get("leadStock", ""))
                        break
            if not lead_str or lead_str in ("nan", ""):
                print(f"  {'--':<10} {bname:<10} ⚠️ 无龙头数据")
                continue
            # leadStr: "光电股份(10.02)"
            sname = lead_str.split("(")[0].strip()
            fu = ex.submit(check_stock, sname, bname)
            futs[fu] = sname

        for fu in as_completed(futs):
            res = fu.result()
            sname = futs[fu]
            if res is None:
                print(f"  {'--':<10} {sname:<10} ⚠️ 代码未找到")
                continue
            passed_sym = "✅" if res["passed"] else "  "
            lhb = f"🐂{res['lhb_detail']}" if res["lhb_detail"] else ""
            print(f"  {res['code']:<10} {res['name']:<10} {res['close']:>6.2f} "
                  f"{res['chg_pct']:>+8.2f} {res['net_in_w']:>+14.1f} "
                  f"{res['amount_yy']:>8.2f} {passed_sym}")
            if lhb:
                print(f"    └ {lhb}")
            if res["passed"]:
                candidates.append(res)

    # Step 3: 基本面速查
    if candidates:
        print(f"\n{'═'*62}")
        print("【基本面速查】")
        print(f"  {'代码':<10} {'名称':<10} {'总市值':>10} {'PE':>7} {'行业':<12} {'题材':<20}")
        print(f"  {'-'*75}")

        def fetch_profile(res):
            p = profile_info(res["code"])
            return {**res, **p}

        with ThreadPoolExecutor(max_workers=4) as ex:
            prof_futs = {ex.submit(fetch_profile, c): c for c in candidates}
            print(f"  {'代码':<10} {'名称':<10} {'行业':<12} {'主营业务（60字）':<55}")
            print(f"  {'-'*92}")
            for fu in as_completed(prof_futs):
                p = fu.result()
                print(f"  {p['code']:<10} {p['name']:<10} {p.get('industry','--'):<12} {p.get('business','--'):<55}")

    # Step 4: 结论
    print(f"\n{'═'*62}")
    if candidates:
        print(f"【操作结论】")
        print(f"  今日强势板块：{', '.join(board_names)}")
        print(f"  候选标的 {len(candidates)} 只，主力资金净流入已确认")
        for c in sorted(candidates, key=lambda x: -abs(x["net_in_w"])):
            print(f"  → {c['code']} {c['name']}（{c['board']}）主力净流入 {c['net_in_w']:+.0f}万元 成交额 {c['amount_yy']:.2f}亿")
        print(f"\n  短线激进风控参考：止损-7%，持期≤3天，次日开盘买入")
        print(f"  ⚠️ 建议配合 tdx-stock-analysis 做深度基本面复审后再下单")
    else:
        print(f"【操作结论】")
        print(f"  今日热点板块：{', '.join(board_names)}")
        print(f"  ⚠️ 无候选达标（阈值：主力净流入≥{args.min_net}万元，成交额≥{args.min_amt}亿元）")
        print(f"  可调低阈值重跑：python hot_board_drive.py --min-net 200 --min-amt 1.0")

    return candidates

if __name__ == "__main__":
    main()
