# -*- coding: utf-8 -*-
"""低位启动池 · 近期超卖路径的「杀逻辑 vs 杀估值」基本面过滤验证（林奇困境反转框架）。

背景（2026-09-13 用户任务①）：验证彼得·林奇「困境反转要看债务结构（短债压顶 vs 长债喘息）、
区分杀估值 vs 杀逻辑」能否提升近期超卖路径的转化率/涨幅/落袋收益。

方法论：
  - 近期超卖路径 = 距60日高点 <-30% 后放量启动确认（本质是「超跌反弹/困境反转」标的）。
  - 林奇核心：困境反转最怕「价值陷阱」——跌了是因为逻辑坏了（亏损/高杠杆/短债压顶），
    这种票跌完还会跌；反之「杀估值」（盈利仍在、债务可控）才会真反弹。
  - 因此给每笔触发做「点时间」基本面打标：只取触发日前已披露的最新财报（InfoPublDate<=触发日，
    防未来函数），区分杀逻辑/杀估值，对比两组的 7日涨幅/转化率/胜率/T+5落袋收益。

数据源：westock-data-clawhub 财务报表（lrb 利润表 + zcfz 资产负债表，腾讯自选股）。
"""
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

from low_pos_7day_backtest import _load_cache, _indicators, KLINE_DIR  # 复用 K 线读取

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
TRIGGER_CSV = DATA_DIR / "低位启动_7日回测.csv"
RAW_JSON = DATA_DIR / "超卖票_财报_raw.json"
PKG = "westock-data-clawhub@1.0.4"


def _to_westock(codes):
    out = []
    for c in codes:
        c = str(c).zfill(6)
        pre = "sh" if c.startswith(("6", "9")) else "sz"
        out.append(pre + c)
    return out


def _parse_md(text):
    """解析 CLI 输出的 markdown 表格为 DataFrame（含 symbol 列）。"""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    header_idx = None
    for i, ln in enumerate(lines):
        if ln.startswith("|") and "_date" in ln:
            header_idx = i
            break
    if header_idx is None:
        return None
    header = [h.strip() for h in lines[header_idx].strip("|").split("|")]
    rows = []
    for ln in lines[header_idx + 1:]:
        if not ln.startswith("|"):
            continue
        if re.match(r"^\|\s*-", ln) or re.match(r"^\|\s*:-", ln):
            continue
        cells = [c.strip() for c in ln.strip("|").split("|")]
        if len(cells) != len(header):
            continue
        rows.append(cells)
    if not rows:
        return None
    return pd.DataFrame(rows, columns=header)


def fetch_finance(codes, kind):
    """调用 CLI 拉取 kind∈{lrb,zcfz} 财报，返回 DataFrame。"""
    frames = []
    for i in range(0, len(codes), 20):
        batch = codes[i:i + 20]
        arg = ",".join(batch)
        cmd = ["npx", "-y", PKG, "finance", arg, "--type", kind, "--num", "8"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        df = _parse_md(r.stdout)
        if df is None or df.empty:
            print(f"  [warn] {kind} 批次 {i//20+1} 解析为空", flush=True)
            continue
        frames.append(df)
        print(f"  {kind} 批次 {i//20+1}: {len(batch)} 只 -> {len(df)} 行", flush=True)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_raw():
    if RAW_JSON.exists():
        with open(RAW_JSON) as f:
            return json.load(f)
    return None


def main():
    trig = pd.read_csv(TRIGGER_CSV)
    os_trig = trig[trig["蓄势路径"] == "近期超卖"].copy()
    os_trig["代码"] = os_trig["代码"].astype(str).str.zfill(6)
    os_trig["触发日"] = pd.to_datetime(os_trig["触发日"])
    codes = sorted(os_trig["代码"].unique().tolist())
    wcodes = _to_westock(codes)
    print(f"近期超卖触发 {len(os_trig)} 笔 / {len(codes)} 只票，触发日 {os_trig['触发日'].min().date()} ~ {os_trig['触发日'].max().date()}", flush=True)

    raw = load_raw()
    if raw is None:
        print("[1/2] 拉取利润表 lrb ...", flush=True)
        lrb = fetch_finance(wcodes, "lrb")
        print("[1/2] 拉取资产负债表 zcfz ...", flush=True)
        zcfz = fetch_finance(wcodes, "zcfz")
        raw = {"lrb": lrb.to_dict("records"), "zcfz": zcfz.to_dict("records")}
        with open(RAW_JSON, "w") as f:
            json.dump(raw, f, ensure_ascii=False)
        print(f"  已缓存 -> {RAW_JSON.name}", flush=True)
    else:
        print("[1/2] 复用已缓存财报", flush=True)

    lrb = pd.DataFrame(raw["lrb"])
    zcfz = pd.DataFrame(raw["zcfz"])

    for d in (lrb, zcfz):
        # InfoPublDate 形如 "2024-04-30 00:00:00 +0800 CST"，带时区后缀会导致 to_datetime 解析失败，
        # 只取前 10 位日期部分。
        d["InfoPublDate"] = pd.to_datetime(d["InfoPublDate"].astype(str).str[:10], errors="coerce")
        d["_date"] = pd.to_datetime(d["_date"].astype(str).str[:10], errors="coerce")
        d["symbol"] = d["symbol"].str.replace("sh", "").str.replace("sz", "")
        d["代码"] = d["symbol"].str.zfill(6)
    for col in ["NPParentCompanyOwners", "NPParentCompanyOwnersTTM",
                "TotalAssets", "TotalLiability", "TotalCurrentLiability",
                "ShortTermLoan", "LongtermLoan", "CashEquivalents",
                "TotalCurrentAssets"]:
        if col in lrb.columns:
            lrb[col] = lrb[col].apply(_num)
        if col in zcfz.columns:
            zcfz[col] = zcfz[col].apply(_num)

    # 点时间选最新财报（InfoPublDate <= 触发日）
    def latest_row(df, code, date):
        sub = df[(df["代码"] == code) & (df["InfoPublDate"] <= date)]
        if sub.empty:
            return None
        return sub.sort_values("InfoPublDate").iloc[-1]

    print("\n[2/2] 点时间打标 + 回测对比 ...", flush=True)
    rows = []
    for _, t in os_trig.iterrows():
        code, date = t["代码"], t["触发日"]
        inc = latest_row(lrb, code, date)
        bal = latest_row(zcfz, code, date)
        if inc is None and bal is None:
            continue
        rec = {"代码": code, "触发日": date, "7日最大涨幅%": t["7日内最大涨幅%"],
               "T5收益%": t["T5收益%"] if "T5收益%" in t else None}
        ttm_np = inc["NPParentCompanyOwnersTTM"] if inc is not None else None
        np_q = inc["NPParentCompanyOwners"] if inc is not None else None
        rec["TTM净利润亿"] = round(ttm_np / 1e8, 2) if ttm_np is not None else None
        rec["报告期净利亿"] = round(np_q / 1e8, 2) if np_q is not None else None
        da_ratio = None
        cur_ratio = None
        cash_cover = None
        stl = None
        ltl = None
        if bal is not None:
            ta = bal.get("TotalAssets")
            tl = bal.get("TotalLiability")
            tcl = bal.get("TotalCurrentLiability")
            tca = bal.get("TotalCurrentAssets")
            cash = bal.get("CashEquivalents")
            stl = bal.get("ShortTermLoan")
            ltl = bal.get("LongtermLoan")
            if ta and tl and ta > 0:
                da_ratio = tl / ta
            if tca and tcl and tcl > 0:
                cur_ratio = tca / tcl
            if cash is not None and tcl and tcl > 0:
                cash_cover = cash / tcl
            rec["资产负债率"] = round(da_ratio * 100, 1) if da_ratio is not None else None
            rec["流动比率"] = round(cur_ratio, 2) if cur_ratio is not None else None
            rec["现金/流动负债"] = round(cash_cover, 2) if cash_cover is not None else None
            rec["短债亿"] = round(stl / 1e8, 2) if stl is not None else None
            rec["长债亿"] = round(ltl / 1e8, 2) if ltl is not None else None

        # 杀逻辑信号（任一命中即判「杀逻辑」）
        flags = []
        if ttm_np is not None and ttm_np < 0:
            flags.append("TTM亏损")
        if da_ratio is not None and da_ratio > 0.70:
            flags.append("高杠杆>70%")
        if (stl is not None and ltl is not None and stl > 0 and ltl < stl * 0.3
                and cur_ratio is not None and cur_ratio < 1.0):
            flags.append("短债压顶+流动比<1")
        if cash_cover is not None and cash_cover < 0.2:
            flags.append("现金不足")
        rec["杀逻辑信号"] = ";".join(flags) if flags else ""
        rec["分类"] = "杀逻辑" if flags else "杀估值"
        rows.append(rec)

    df = pd.DataFrame(rows)
    print(f"有效样本 {len(df)} 笔（有财报数据）\n")
    print("=== 分类分布 ===")
    print(df["分类"].value_counts().to_string())
    print("\n=== 杀逻辑信号分布 ===")
    print(df[df["分类"] == "杀逻辑"]["杀逻辑信号"].value_counts().to_string())

    def stat(grp, name):
        if grp.empty:
            print(f"{name}: 无样本")
            return
        mr = grp["7日最大涨幅%"]
        p10 = (mr >= 10).mean() * 100
        win5 = (grp["T5收益%"] > 0).mean() * 100 if grp["T5收益%"].notna().any() else float("nan")
        t5 = grp["T5收益%"].dropna()
        print(f"{name}: {len(grp)}笔  7日最大涨幅 均值{mr.mean():+.2f}%/中位{mr.median():+.2f}%  "
              f"≥10%概率{p10:.0f}%  T5收益均值{t5.mean():+.2f}%/中位{t5.median():+.2f}%/胜率{win5:.0f}%")

    print("\n=== 杀估值 vs 杀逻辑（标的属性 + 落袋收益） ===")
    for cls in ["杀估值", "杀逻辑"]:
        stat(df[df["分类"] == cls], cls)

    print("\n=== 单信号拆分（判断哪个信号最有效） ===")
    base = df
    print("[基线] ", end="")
    stat(base, "全部")
    signal_map = {
        "TTM亏损": "TTM亏损", "高杠杆>70%": "高杠杆>70%",
        "短债压顶+流动比<1": "短债压顶+流动比<1", "现金不足": "现金不足",
    }
    for sig in signal_map:
        # 剔除命中该信号的样本，看剩余「干净组」是否更优
        clean = df[~df["杀逻辑信号"].str.contains(sig, na=False)]
        print(f"[剔除 {sig}] ", end="")
        stat(clean, f"剩余{len(clean)}笔")

    # 落袋收益（T+5 持有，-8% 止损，收盘口径）——复用 K 线精确重算
    print("\n=== 精确落袋收益（T+5 持有 -8%止损，收盘口径） ===")
    # 从触发明细拿入场价与未来收盘
    trig_map = {}
    for _, t in os_trig.iterrows():
        trig_map[(t["代码"], str(t["触发日"].date()))] = t
    # 用 K 线重算（更精确，含 T+1..T+5 收盘）
    from low_pos_7day_backtest import KLINE_DIR
    exact = []
    for _, r in df.iterrows():
        code = r["代码"]
        _, h = _load_cache(code)
        if h is None:
            continue
        ind = _indicators(h)
        ind["date"] = pd.to_datetime(ind["date"])
        tgt = r["触发日"]
        pos = ind[ind["date"] == tgt]
        if pos.empty:
            continue
        i = pos.index[0]
        if i + 6 >= len(ind):
            continue
        entry = float(ind.iloc[i]["收盘"])
        closes = ind.iloc[i + 1:i + 6]["收盘"].astype(float).tolist()
        # T+5 持有 -8% 止损模拟
        pnl = None
        for k, c in enumerate(closes, start=1):
            if c <= entry * 0.92:
                pnl = (c / entry - 1) * 100
                break
            if k == 5:
                pnl = (c / entry - 1) * 100
        if pnl is not None:
            exact.append({"分类": r["分类"], "T5落袋%": pnl, "7日最大涨幅%": r["7日最大涨幅%"]})
    edf = pd.DataFrame(exact)
    print(f"精确样本 {len(edf)} 笔\n")
    for cls in ["杀估值", "杀逻辑"]:
        g = edf[edf["分类"] == cls]
        if g.empty:
            continue
        pnl = g["T5落袋%"]
        mr = g["7日最大涨幅%"]
        print(f"{cls}: {len(g)}笔  T5落袋 均值{pnl.mean():+.2f}%/中位{pnl.median():+.2f}%/胜率{(pnl>0).mean()*100:.0f}%/≥10%{(pnl>=10).mean()*100:.0f}%  "
              f"7日最大涨幅≥10%概率{(mr>=10).mean()*100:.0f}%")

    print("\n明细落盘 ...")
    out = DATA_DIR / "超卖票_杀逻辑分类.csv"
    df.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"-> {out.name}")


if __name__ == "__main__":
    main()
