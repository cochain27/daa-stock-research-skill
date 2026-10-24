#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""策略周报生成器（2026-09-24 新增）。

聚合三策略笔记本/台账 + 虚拟盘 + 本地K线，输出周度命中率/均值/盈亏比，对照基线看是否漂移。

数据源与口径：
- 短线/趋势：data/推荐台账_*.csv「已了结」行（虚拟建仓状态=已了结 / 状态∈{技术离场,止损出局,时间止损,止盈离场}）
  收益% 取「最高收益%」（=已实现收益%口径，含止损/离场结算）
- 初动池：data/笔记本/策略笔记本_温和放量初动池.md 每日候选流水 → 本地 K 线 T+5 重放
  口径同 warm_start_entry V2：信号日收盘基准，T+1开盘介入≈信号日收盘（近似），
  -8%硬止损 / T+2起破MA5次日离场 / 最长T+5收盘；胜率=收益>0 占比
- 基线（docs/策略台账_2026-09-20.md 快照）：短线 47.7%/+0.44%/1.34；趋势 40.5%/+1.24%/2.17；初动 59.0%/+4.20%/3.07（V2后盈亏比4.46）

用法：python weekly_review.py [--start YYYY-MM-DD] [--end YYYY-MM-DD]
默认统计最近 7 个自然日窗口（周报口径），--all 全量。
"""
import argparse
import csv
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    import pandas as pd
except ImportError:
    pd = None

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data"
PUSH = BASE / "05_每日推送"
NB_FILES = {
    "趋势": DATA / "笔记本" / "策略笔记本_右侧趋势池.md",
    "短线": DATA / "笔记本" / "策略笔记本_短线激进池.md",
    "初动": DATA / "笔记本" / "策略笔记本_温和放量初动池.md",
    "低位": DATA / "笔记本" / "策略笔记本_低位埋伏观察池.md",
}

# 基线（台账 2026-09-20 快照，勿手改；策略变更时在台账登记后同步此处）
BASELINE = {
    "短线": {"胜率%": 47.7, "均值%": 0.44, "盈亏比": 1.34, "样本": 1051, "说明": "长样本2024-03~09-18"},
    "趋势": {"胜率%": 40.5, "均值%": 1.24, "盈亏比": 2.17, "样本": 482, "说明": "新规则长样本"},
    "初动": {"胜率%": 59.0, "均值%": 4.20, "盈亏比": 3.07, "样本": 1366, "说明": "T5信号级（V2后盈亏比4.46）"},
}

CLOSED_STATES = {"已了结", "技术离场", "止损出局", "时间止损", "止盈离场"}
BOM = "\ufeff"


def _num(v):
    try:
        return float(str(v).replace("%", "").replace(",", ""))
    except Exception:
        return None


def load_trades():
    """读虚拟交易.csv（已了结实盘结算为准）。"""
    p = DATA / "虚拟交易.csv"
    if not p.exists():
        return []
    rows = []
    with open(p, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rows.append(r)
    return rows


def load_ledger(pool):
    """读推荐台账 CSV 已了结行。"""
    p = DATA / f"推荐台账_{pool}.csv"
    if not p.exists():
        return []
    out = []
    with open(p, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            st = (r.get("状态") or "").strip()
            vst = (r.get("虚拟建仓状态") or "").strip()
            if vst == "已了结" or st in CLOSED_STATES:
                out.append(r)
    return out


def load_ledger_all(pool):
    """读推荐台账全部行（含持有中），供低位触发池周报用。"""
    p = DATA / f"推荐台账_{pool}.csv"
    if not p.exists():
        return []
    with open(p, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _load_kline(code):
    if pd is None:
        return None
    for pfx in ("sh", "sz", "bj"):
        p = DATA / "klines" / f"{pfx}{code}.csv"
        if p.exists():
            try:
                df = pd.read_csv(p)
                df.columns = [c.strip(BOM) for c in df.columns]
                ren = {"date": "日期", "open": "开盘", "last": "收盘",
                       "high": "最高", "low": "最低", "volume": "成交量", "amount": "成交额"}
                df = df.rename(columns={k: v for k, v in ren.items() if k in df.columns})
                return df
            except Exception:
                return None
    return None


def warm_replay(sig_date, code, max_hold=5, stop=-8.0, name=""):
    """V2 出场规则本地重放，返回 dict（信号级）。

    口径（与台账 V2 固化一致）：T+1 开盘介入；-8% 硬止损（当日最低≤止损价→按止损价出）；
    T+2 收盘起收盘破 MA5 → 次日开盘离场；无固定止盈；最长 T+5 收盘强制离场。
    介入基准=信号日次日开盘价（近似 T+1 开盘介入）。
    """
    df = _load_kline(code)
    if df is None or "日期" not in df.columns:
        return None
    try:
        dates = df["日期"].astype(str).tolist()
        opens = df["开盘"].astype(float).tolist()
        closes = df["收盘"].astype(float).tolist()
        lows = df["最低"].astype(float).tolist()
    except Exception:
        return None
    i = next((k for k, d in enumerate(dates) if sig_date in d), None)
    if i is None or i + 1 >= len(dates):
        return None
    entry = opens[i + 1]
    if not entry:
        return None
    stop_price = entry * (1 + stop / 100)
    exit_px, exit_reason, exit_idx = None, None, None
    for k in range(i + 1, min(i + 1 + max_hold, len(dates))):
        # -8% 硬止损（盘中触发，近似当日最低触及）
        if lows[k] <= stop_price:
            exit_px, exit_reason, exit_idx = stop_price, "硬止损", k
            break
        # T+2（即 k>=i+2）收盘起：收盘破 MA5 → 次日开盘离场
        if k >= i + 2 and k + 1 < len(dates):
            ma5 = sum(closes[k - 4:k + 1]) / 5
            if closes[k] < ma5:
                exit_px, exit_reason, exit_idx = opens[k + 1], "破MA5离场", k + 1
                break
    if exit_px is None:
        # K 线覆盖完整 T+5 窗口（信号日索引 i，需 i+5 收盘存在）才计 T+5 到期
        if len(dates) >= i + 6:
            k = i + max_hold
            exit_px, exit_reason, exit_idx = closes[k], "T+5到期", k
        else:
            return None  # 未到期/数据不足，不计结算
    ret = (exit_px / entry - 1) * 100
    return {"信号日": sig_date, "代码": code, "名称": name, "介入": entry,
            "收益%": ret, "结果": exit_reason}


def warm_signals():
    """从初动笔记本流水表解析全部信号（日期/代码/名称/现价/止损）。"""
    p = NB_FILES["初动"]
    if not p.exists():
        return []
    txt = p.read_text(encoding="utf-8")
    m = re.search(r"## 每日候选流水\n+((?:\|[^\n]*\n)+)", txt, re.M)
    if not m:
        return []
    out = []
    for ln in m.group(1).strip().split("\n"):
        if ln.startswith("| 日期"):
            continue
        cells = [c.strip() for c in ln.strip("|").split("|")]
        if len(cells) < 9 or not re.match(r"\d{4}-\d{2}-\d{2}", cells[0]):
            continue
        out.append({"信号日": cells[0], "代码": cells[1], "名称": cells[2],
                    "现价": cells[3], "止损": cells[8], "状态": cells[9] if len(cells) > 9 else ""})
    return out


def stats_of(items):
    """items: 收益% 列表 → (n, 胜率%, 均值%, 盈亏比)。"""
    n = len(items)
    if n == 0:
        return 0, None, None, None
    wins = [x for x in items if x > 0]
    losses = [x for x in items if x <= 0]
    wr = len(wins) / n * 100
    avg = sum(items) / n
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0
    pl = avg_win / avg_loss if avg_loss else None
    return n, wr, avg, pl


def fmt_pct(v, nd=1):
    return "—" if v is None else f"{v:.{nd}f}%"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", help="起始日 YYYY-MM-DD（默认7天前）")
    ap.add_argument("--end", default=date.today().isoformat(), help="结束日 YYYY-MM-DD")
    ap.add_argument("--all", action="store_true", help="全量（忽略窗口）")
    args = ap.parse_args()
    end = datetime.strptime(args.end, "%Y-%m-%d").date()
    start = datetime.strptime(args.start, "%Y-%m-%d").date() if args.start else end - timedelta(days=7)

    # 1) 短线：虚拟交易.csv 已了结（台账为补充）
    trades = load_trades()
    short = [r for r in trades if r.get("策略标签") == "短线"]
    short_closed = [r for r in short if r.get("虚拟了结日")]
    if args.all:
        short_win = short_closed
    else:
        short_win = [r for r in short_closed if start.isoformat() <= (r.get("虚拟了结日") or "") <= end.isoformat()]
    short_ret = [_num(r.get("浮动盈亏%")) for r in short_win]
    short_ret = [x for x in short_ret if x is not None]
    sn, swr, savg, spl = stats_of(short_ret)

    # 2) 趋势：台账已了结（虚拟盘通常不单独记趋势）
    trend_ledger = load_ledger("右侧趋势")
    if args.all:
        trend_win = trend_ledger
    else:
        trend_win = [r for r in trend_ledger if start.isoformat() <= (r.get("最后更新") or r.get("虚拟了结日") or "")[:10] <= end.isoformat()]
    trend_ret = [_num(r.get("最高收益%")) for r in trend_win]
    trend_ret = [x for x in trend_ret if x is not None]
    tn, twr, tavg, tpl = stats_of(trend_ret)

    # 2.5) 低位触发：台账（推荐即虚拟成交）——2026-10-23 新增
    #     旧版周报只覆盖短线/趋势/初动三池，把「唯一正收益的低位触发池」整段漏掉，
    #     导致周度汇总低估。口径：已了结按 了结收益；持仓按 最新收盘 浮动。
    low_rows = load_ledger_all("低位触发")
    low_closed, low_hold = [], []
    for r in low_rows:
        fired = (r.get("触发日") or r.get("虚拟建仓日") or "")[:10]
        if fired and not (start.isoformat() <= fired <= end.isoformat()):
            continue
        st = (r.get("状态") or "").strip()
        if (r.get("虚拟建仓状态") or "").strip() == "已了结" or st in CLOSED_STATES:
            low_closed.append(r)
        else:
            low_hold.append(r)
    # 2026-10-23 修复：原口径用「最高收益%」(峰值) 当结算收益，把昂利康 +1.31% 的
    # 实际了结报成 +7.41%，系统性虚增该池表现。改为以「虚拟交易.csv」的真实了结为准：
    # 每笔结算事件按剩余仓位加权（分批止盈的半仓记 0.5 笔），峰值仅作展示。
    low_events = []   # [(ret%, weight)]
    vt_low = [t for t in trades if (t.get("策略标签") or "").strip() == "低位触发"]
    for t in vt_low:
        exit_day = (t.get("虚拟了结日") or "")[:10]
        if not exit_day:
            continue
        if not args.all and not (start.isoformat() <= exit_day <= end.isoformat()):
            continue
        ret = _num(t.get("浮动盈亏%"))
        if ret is None:
            continue
        way = (t.get("了结方式") or "")
        w = 0.5 if ("减半" in way or "50%仓" in way) else 1.0
        low_events.append((ret, w))
    if low_events:
        _w = sum(w for _, w in low_events)
        ln = len(low_events)   # 笔数=真实结算事件数；均值按剩余仓位加权
        lwr = len([1 for r, w in low_events if r > 0]) / len(low_events) * 100
        lavg = sum(r * w for r, w in low_events) / _w if _w else None
        _win = [r for r, w in low_events if r > 0]
        _los = [r for r, w in low_events if r <= 0]
        if _win and _los:
            lpl = (sum(_win) / len(_win)) / abs(sum(_los) / len(_los))
        else:
            lpl = None
    else:
        ln, lwr, lavg, lpl = 0, None, None, None

    # 3) 初动：笔记本流水 → 本地 K 线 T+5 重放
    sigs = warm_signals()
    if args.all:
        sigs_win = sigs
    else:
        sigs_win = [s for s in sigs if start.isoformat() <= s["信号日"] <= end.isoformat()]
    wn, wwr, wavg, wpl, wdetail = 0, None, None, None, []
    name_of = {s["代码"]: s["名称"] for s in sigs}
    for s in sigs_win:
        r = warm_replay(s["信号日"], s["代码"], name=s.get("名称", ""))
        if r:
            r["名称"] = name_of.get(s["代码"], "")
            wdetail.append(r)
    wn = len(wdetail)
    if wn:
        wret = [r["收益%"] for r in wdetail]
        wn, wwr, wavg, wpl = stats_of(wret)

    # 输出
    PUSH.mkdir(exist_ok=True)
    out = PUSH / f"策略周报_{end.isoformat()}.md"
    L = []
    L.append(f"# 📊 策略周报 {end.isoformat()}")
    L.append(f"\n> 统计窗口：{start.isoformat()} ~ {end.isoformat()}（{'全量' if args.all else '近7个自然日'}）｜生成：{datetime.now():%Y-%m-%d %H:%M}")
    L.append("> 口径：短线/趋势=已了结结算（虚拟盘+台账）；初动=信号级本地K线T+5重放（V2规则，近似口径）。基线见 docs/策略台账_2026-09-20.md。")
    L.append("\n## 一、四池周度表现 vs 基线\n")
    L.append("| 池 | 本周笔数 | 胜率 | 均值 | 盈亏比 | 基线(胜率/均值/盈亏比) | 判定 |")
    L.append("|---|---|---|---|---|---|---|")

    def judge(pool, n, wr, avg, pl, bl):
        if n < 3:
            return "样本不足，暂不判定"
        if n < 5:
            return "样本偏少（参考）"
        drift = []
        if wr is not None and abs(wr - bl["胜率%"]) >= 5:
            drift.append(f"胜率{'↑' if wr > bl['胜率%'] else '↓'}{abs(wr - bl['胜率%']):.1f}pp")
        if avg is not None and bl["均值%"]:
            r = (avg - bl["均值%"]) / abs(bl["均值%"]) * 100
            if abs(r) >= 30:
                drift.append(f"均值{'↑' if r > 0 else '↓'}{abs(r):.0f}%")
        if pl is not None and bl["盈亏比"]:
            r = (pl - bl["盈亏比"]) / bl["盈亏比"] * 100
            if abs(r) >= 30:
                drift.append(f"盈亏比{'↑' if r > 0 else '↓'}{abs(r):.0f}%")
        return ("⚠️ 漂移：" + "、".join(drift)) if drift else "✅ 在基线内"

    L.append(f"| 短线激进 | {sn} | {fmt_pct(swr)} | {fmt_pct(savg, 2)} | {('—' if spl is None else f'{spl:.2f}')} | {BASELINE['短线']['胜率%']}%/{BASELINE['短线']['均值%']}%/{BASELINE['短线']['盈亏比']} | {judge('短线', sn, swr, savg, spl, BASELINE['短线'])} |")
    L.append(f"| 右侧趋势 | {tn} | {fmt_pct(twr)} | {fmt_pct(tavg, 2)} | {('—' if tpl is None else f'{tpl:.2f}')} | {BASELINE['趋势']['胜率%']}%/{BASELINE['趋势']['均值%']}%/{BASELINE['趋势']['盈亏比']} | {judge('趋势', tn, twr, tavg, tpl, BASELINE['趋势'])} |")
    L.append(f"| 温和放量初动 | {wn} | {fmt_pct(wwr)} | {fmt_pct(wavg, 2)} | {('—' if wpl is None else f'{wpl:.2f}')} | {BASELINE['初动']['胜率%']}%/{BASELINE['初动']['均值%']}%/{BASELINE['初动']['盈亏比']} | {judge('初动', wn, wwr, wavg, wpl, BASELINE['初动'])} |")
    L.append(f"| 低位触发 | {ln} | {fmt_pct(lwr)} | {fmt_pct(lavg, 2)} | {('—' if lpl is None else f'{lpl:.2f}')} | 无基线（10-08 起实盘化，样本积累中） | {judge('低位', ln, lwr, lavg, lpl, {'胜率%': 50, '均值%': 0, '盈亏比': 1}) if ln >= 3 else '样本不足，仅报真实值'} |")

    L.append("\n## 二、本周已了结明细\n")
    if short_win:
        L.append("**短线激进**")
        L.append("| 建仓日 | 代码 | 名称 | 了结日 | 方式 | 收益% |")
        L.append("|---|---|---|---|---|---|")
        for r in short_win:
            L.append(f"| {r.get('虚拟建仓日','')} | {r.get('代码','')} | {r.get('名称','')} | {r.get('虚拟了结日','')} | {r.get('了结方式','')} | {r.get('浮动盈亏%','')} |")
    else:
        L.append("短线：本周无已了结。")
    if trend_win:
        L.append("\n**右侧趋势**")
        L.append("| 推荐日 | 代码 | 名称 | 结算收益% | 状态 |")
        L.append("|---|---|---|---|---|")
        for r in trend_win:
            L.append(f"| {r.get('日期','')} | {r.get('代码','')} | {r.get('名称','')} | {r.get('最高收益%','')} | {r.get('状态','')} |")
    else:
        L.append("\n右侧趋势：本周无已了结（跟踪池仍在持有或池空）。")

    # 低位触发：已了结 + 持仓（2026-10-23 新增，旧版遗漏）
    L.append("\n**低位触发**（推荐即虚拟成交，入池↔触发分离）")
    # 真实结算收益（来自虚拟交易，分批止盈单独成行）
    _settle = {}
    for t in vt_low:
        _c = str(t.get("代码", "")).strip().zfill(6)
        _settle.setdefault(_c, []).append(t)
    if low_closed or low_hold:
        L.append("| 代码 | 名称 | 触发日 | 触发价 | 状态 | 实际结算% | 峰值% | 出场/现价 |")
        L.append("|---|---|---|---|---|---|---|---|")
        for r in low_closed:
            _c = str(r.get("代码", "")).strip().zfill(6)
            _rows_ = _settle.get(_c, [])
            _txt = "；".join(f"{(_num(t.get('浮动盈亏%')) or 0):+.1f}%"
                            f"{'（半仓）' if ('减半' in (t.get('了结方式') or '')) else ''}"
                            for t in _rows_) or "—"
            L.append(f"| {r.get('代码','')} | {r.get('名称','')} | {r.get('触发日','')} | "
                     f"{r.get('触发价','')} | {r.get('状态','')} | {_txt} | "
                     f"{r.get('最高收益%','')} | {r.get('备注','')[:32]} |")
        for r in low_hold:
            code = str(r.get("代码", "")).strip()
            cost = _num(r.get("触发价"))
            cur, flt = None, None
            df = _load_kline(code)
            if df is not None and "收盘" in df.columns and cost:
                try:
                    cur = float(df["收盘"].astype(float).iloc[-1])
                    flt = (cur / cost - 1) * 100
                except Exception:
                    pass
            # 止盈1减半的票：台账状态仍是「持有中/止盈1减半」，但半仓已结算，
            # 这里必须把半仓已实现收益透出，否则读者以为全仓都没动。
            _rows_h_ = _settle.get(code.zfill(6), [])
            _txt_h = "；".join(f"{(_num(t.get('浮动盈亏%')) or 0):+.1f}%"
                              f"{'（半仓）' if ('减半' in (t.get('了结方式') or '')) else ''}"
                              for t in _rows_h_)
            L.append(f"| {code} | {r.get('名称','')} | {r.get('触发日','')} | {r.get('触发价','')} | "
                     f"{r.get('状态','')} | {_txt_h + ' 已结算，余仓持有' if _txt_h else '持有中'} | "
                     f"{r.get('最高收益%','')} | "
                     f"{('%.2f（%+.1f%%）' % (cur, flt)) if cur else '—'} |")
    else:
        L.append("本周无触发。")

    L.append("\n## 三、初动池信号明细（本周）\n")
    if sigs_win:
        L.append("| 信号日 | 代码 | 名称 | 介入价(T+1开盘) | T+5收益% | 结果 |")
        L.append("|---|---|---|---|---|---|")
        for s in sigs_win:
            r = warm_replay(s["信号日"], s["代码"], name=s.get("名称", ""))
            if r:
                L.append(f"| {r['信号日']} | {r['代码']} | {r['名称']} | {r['介入']:.2f} | {r['收益%']:+.2f} | {r['结果']} |")
            else:
                L.append(f"| {s['信号日']} | {s['代码']} | {s['名称']} | {s['现价']} | — | ⏳ 未到期/持有中 |")
    else:
        L.append("本周无新信号（或窗口内信号不足）。")

    L.append("\n## 四、异常与提示\n")
    notes = []
    # 2026-10-23：初动「-8%硬止损」为盘中口径（当日最低触及即出），日跟踪表为收盘口径，
    #   两者在"盘中破线、收盘拉回"的日子会分叉（拉卡拉 10-23 最低13.86≤止损13.87，收盘14.99）。
    #   周报采盘中口径（与V2规则一致），此处显式标注，避免与当日复盘"持有中"打架。
    _intraday_stop = []
    for _s in sigs_win:
        _r = warm_replay(_s["信号日"], _s["代码"], name=_s.get("名称", ""))
        if _r and _r["结果"] == "硬止损":
            _df = _load_kline(_s["代码"])
            if _df is not None and "收盘" in _df.columns and len(_df) >= 1:
                _intraday_stop.append((_s["名称"], float(_df["收盘"].astype(float).iloc[-1]),
                                       (float(_df["收盘"].astype(float).iloc[-1]) / _r["介入"] - 1) * 100))
    for _nm, _c, _ret in _intraday_stop:
        if _ret > -8:
            notes.append(
                f"**{_nm} 口径分叉**：初动 -8% 为盘中口径，本周最低已破止损价（周报按止损价结算 "
                f"-8.00%），但最新收盘 {_c:.2f}（{_ret:+.1f}%）已收回线上、日跟踪表仍显示持有中。"
                f"若实际按收盘口径执行则该笔不计止损。V2 规则明定盘中硬止损，周报以规则为准。")
    if sn < 3:
        notes.append(f"短线样本仅 {sn} 笔，判定不具统计意义；继续积累。")
    if tn < 3:
        notes.append(f"趋势池已了结样本 {tn} 笔；趋势持仓周期 2-4 周，周报窗口内常无结算属正常。")
    # 2026-10-23：趋势池连续 2 周 0 出票已定性为筛选器 bug（head(100) 按换手率截断漏票），
    #   根因与修复见 docs/策略台账_2026-09-20.md 与踩坑日记同日条目。此处每次生成时提示，
    #   避免被历史周报的旧结论反复误读成"阈值过严/市场无标的"。
    if tn == 0:
        notes.append(
            "**趋势池连续 0 出票已定性为筛选器 bug（非\"无合格标的\"，也非阈值过严）**："
            "`pick_trend_stocks` 原 `cand = df.head(100)` 按**换手率**降序截断，而合格趋势票的换手率/成交额排名"
            "普遍在 139~944 位，**100% 落在截断线外**，筛选器从未看到它们。已修：前置\"涨跌幅 2.5~8%\"粗筛 + "
            "`TREND_SCAN_LIMIT` 100→400。离线全池重放 10-12~10-23 共 9 只合格（10-23 当天 5 只），"
            "线上实跑 10-23 出票 2 只（300253 卫宁健康、300494 盛天网络）。")
    if wn < 3:
        notes.append(f"初动池窗口信号 {wn} 条（T+5 未走完的不计结算）；初动是 09-20 才升级的正式策略，样本积累中。")
    if wpl is not None and wpl > 4:
        notes.append("初动盈亏比高于基线 3.07：若为小样本请忽略；连续4周高于4.0 再考虑上调基线预期。")
    # 低位触发净值（唯一正收益池，单独披露）
    try:
        import sys as _sys
        _sys.path.insert(0, str(BASE / "scripts"))
        from low_pos_tracker import stats as _lp_stats
        _lps = _lp_stats().get("虚拟净值", {})
        if _lps:
            notes.append(
                f"低位触发虚拟净值 {_lps.get('净值')}（累计 {_lps.get('累计收益率')}%，"
                f"已实现 {_lps.get('已实现盈亏')}，浮动 {_lps.get('浮动盈亏')}，"
                f"持仓 {_lps.get('持仓笔数')} 笔）；**本周唯一正收益池**，四池合计口径不应只看前三池。")
    except Exception as _e:
        notes.append(f"低位触发净值读取失败：{_e}")
    # 2026-10-23：初动池漂移归因已闭环——三个候选改进（情绪分闸门 / pos60 下限 / 市场广度闸门）
    #   在全历史回测上全部证伪，参数不动。详见 docs/策略台账_2026-09-20.md 10-23 行。
    #   只在初动池明显偏离基线时提示，避免每周重复刷屏。
    if wn >= 3 and (wpl is not None and wpl < 1.0):
        notes.append(
            "**初动池漂移归因已完成（2026-10-23）——三个候选改进全历史回测全部证伪，参数不动**："
            "① \"买在情绪高点\"证伪（信号次日跳空 vs T+5 收益 r=0.03；情绪分≥80 反而 50% 胜率 vs <80 的 19%）；"
            "② \"加 pos60 下限\"证伪（25 条小样本看似低位置拖累，20416 条全样本方向反转：pos60 0-25% 均值 +1.36% > 60-81% 的 +0.94%，"
            "实测 0.32/0.35/0.40 三档闸门全部微降）；"
            "③ \"市场广度闸门\"证伪（最优档 3157 条口径下砍掉 9.8% 信号后累计收益 +9289.6%→+9147.7%，净增量为负）。"
            "**结论：初动低胜率是弱市环境固有回撤（10 月全池 MA20 上方占比均值仅 23.8%），非参数缺陷**，"
            "维持 59.0%/4.20%/3.07 基线，等环境修复。")
    L.append("\n".join(f"- {x}" for x in notes) if notes else "- 本周无异常。")

    out.write_text("\n".join(L), encoding="utf-8")
    print(out)
    print("\n".join(L))


if __name__ == "__main__":
    main()
