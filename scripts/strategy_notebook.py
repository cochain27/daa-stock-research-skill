# -*- coding: utf-8 -*-
"""策略笔记本自动维护 —— 每策略一本，每日推荐流水 + 跟踪状态。

数据源（权威，不重复计算）：
  - 右侧趋势：data/推荐台账_右侧趋势.csv（日期=今日的行=当日推荐；状态非已了结=跟踪）
  - 短线激进：data/推荐台账_短线激进.csv（同上）
  - 温和放量初动：warm_start_entry.pick_warm_start(update=False)（只读 K 线缓存，与 close_review 同口径）
  - 低位埋伏：data/watch_history.csv（日期=今日=当日候选；状态=观察中=连续追踪）

幂等：流水表按「日期+代码」去重，同日同票不重复追加。
跟踪表：趋势/短线/低位全量重写（表头保留，正文替换）；初动例外——旧行保留并用本地K线刷新
  （现价/距信号%/破MA5/操作建议，超 T+5 清除），当日候选追加，保证 T+1~T+5 持有期跨日可追踪。
调用：close_review.run_close() 末尾 strategy_notebook.update_all(today)；也可命令行 --date 单独跑。
"""
import csv
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

BASE = Path(__file__).resolve().parent.parent
NB_DIR = BASE / "data" / "笔记本"

NB_FILES = {
    "趋势": NB_DIR / "策略笔记本_右侧趋势池.md",
    "短线": NB_DIR / "策略笔记本_短线激进池.md",
    "初动": NB_DIR / "策略笔记本_温和放量初动池.md",
    "低位": NB_DIR / "策略笔记本_低位埋伏观察池.md",
}
TREND_LEDGER = BASE / "data" / "推荐台账_右侧趋势.csv"
SHORT_LEDGER = BASE / "data" / "推荐台账_短线激进.csv"
WATCH_HISTORY = BASE / "data" / "watch_history.csv"

# 关闭的跟踪状态
CLOSED = {"已了结", "已失效", "技术离场", "止损", "止盈"}

# 各池止盈规则（方案B：拼进「操作建议」列，不动表结构；2026-09-22 定版）
TP_RULES = {
    "趋势": "破MA10移动止盈",
    "短线": "+5%减半→盈利>3%破MA10清仓",
    "初动": "破MA5次日离场·无固定止盈",
    "低位": "观察池·触发启动再决策",
}


def _fmt(v, nd=2, suffix="", pct_fraction=False):
    if v is None or v == "" or v == "-":
        return "—"
    try:
        val = float(v)
        if pct_fraction:
            return f"{val * 100:.{nd}f}{suffix}"
        return f"{val:.{nd}f}{suffix}"
    except (ValueError, TypeError):
        return str(v)


def _load_lines(path):
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _table_block(lines, marker):
    """定位并返回表头所在行号 + 表体行（不含表头/分隔线）。"""
    start, body = None, []
    for i, ln in enumerate(lines):
        if ln.startswith("|") and marker in ln:
            start = i
            # 跳过表头 + 分隔行
            j = i + 1
            while j < len(lines) and lines[j].startswith("|") and "---" in lines[j][:40]:
                j += 1
            while j < len(lines) and lines[j].startswith("|"):
                body.append(lines[j])
                j += 1
            break
    return start, body


def _replace_table(lines, marker, new_header, new_rows):
    """把含 marker 的整张表替换为新表；找不到则追加到文件末尾。返回新 lines。"""
    start, _ = _table_block(lines, marker)
    if start is None:
        out = list(lines) + [""] + [new_header]
        end = len(lines) + 1
    else:
        end = start
        while end < len(lines) and lines[end].startswith("|"):
            end += 1
        out = lines[:start]
        out.append(new_header)
    if new_rows:
        n_cols = len(new_header.split("|")) - 2
        sep = "|" + "|".join("---" for _ in range(n_cols)) + "|"
        out.append(sep)
        out.extend(new_rows)
    if start is not None:
        out.extend(lines[end:])
    return out


def _header_marker(header):
    cells = [c.strip() for c in header.strip("|").split("|")]
    return cells[0] if cells else header


# ---------------- 各池流水构建 ----------------

def _read_ledger(path):
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _build_trend_short(date, path, pool):
    rows = _read_ledger(path)
    today_rows = [r for r in rows if r.get("日期") == date]
    flow = []
    for r in today_rows:
        flow.append(
            f"| {date} | {r['代码']} | {r['名称']} | {_fmt(r.get('基准价'))} | {r.get('买区') or '—'} | "
            f"{_fmt(r.get('止损'))} | — | — | {r.get('状态') or '新推荐'} | — |")
    # 未结清跟踪（按代码取最新）
    latest = {}
    for r in rows:
        latest[r["代码"]] = r
    tp_rule = TP_RULES[pool]
    track = []
    for r in latest.values():
        if r.get("状态") in CLOSED:
            continue
        advice = r.get("备注") or r.get("状态") or "—"
        track.append(
            f"| {r.get('日期')} | {r['代码']} | {r['名称']} | {_fmt(r.get('虚拟成本'))} | "
            f"{_fmt(r.get('累计盈亏%'), 1, '%')} | {_fmt(r.get('最高收益%'), 1, '%')} | {_fmt(r.get('止损'))} | "
            f"止盈:{tp_rule}｜{advice} |")
    return flow, track


def build_trend(date):
    return _build_trend_short(date, TREND_LEDGER, "趋势")


def build_short(date):
    return _build_trend_short(date, SHORT_LEDGER, "短线")


WARM_TRACK_HDR = "| 信号日 | 代码 | 名称 | 现价 | 距信号% | 破MA5? | 硬止损 | 操作建议 |"


def _load_kline(code):
    """读本地 K 线缓存（data/klines/{sh,sz,bj}<code>.csv），列名统一映射中文，返回 DataFrame 或 None。"""
    import pandas as pd
    for pfx in ("sh", "sz", "bj"):
        p = BASE / "data" / "klines" / f"{pfx}{code}.csv"
        if p.exists():
            try:
                df = pd.read_csv(p)
                df.columns = [c.strip("\ufeff") for c in df.columns]
                rename = {"date": "日期", "open": "开盘", "last": "收盘",
                          "high": "最高", "low": "最低", "volume": "成交量", "amount": "成交额"}
                df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
                return df
            except Exception:
                return None
    return None


def warm_tracking_rows(today):
    """初动池跨日跟踪行（结构化，供笔记本跟踪表与收盘复盘共用）。

    读笔记本初动跟踪表旧行（信号日 != today），用本地 K 线刷新跟踪指标。
    返回 dict 列表：信号日/代码/名称/现价/距信号%/最高浮盈%/破MA5/天数/建议/止损。
    天数=T+n（n=最新收盘距信号日的交易日数，信号日=T+0）；
    最高浮盈%=信号日次日(T+1)以来最高价相对信号日收盘的最大涨幅（让利润奔跑策略的回吐可视化，
    2026-09-23 版面新增，供晨报/复盘跟踪表「最高浮盈」列）。
    距今 >6 个交易日（早已过 T+5 强制离场）的行剔除（流水表仍有记录）。
    """
    path = NB_FILES["初动"]
    if not path.exists():
        return []
    _, body = _table_block(_load_lines(path), _header_marker(WARM_TRACK_HDR))
    # 2026-09-23 新增：行业名（供晨报/复盘跟踪表「行业热度」列），先批量补齐再逐行组装
    try:
        from industry_heat_tool import prefetch_industries, stock_industry
    except Exception:
        prefetch_industries = None
        stock_industry = None
    meta = []
    for ln in body:
        cells = [c.strip() for c in ln.strip("|").split("|")]
        if len(cells) < 8 or cells[0] == today:
            continue
        meta.append((cells[0], cells[1], cells[2], cells[6]))
    if prefetch_industries and meta:
        try:
            prefetch_industries([m[1] for m in meta])
        except Exception:
            pass
    out = []
    for sig_date, code, name, hard_stop in meta:
        industry = (stock_industry(code) or "") if stock_industry else ""
        row = {"信号日": sig_date, "代码": code, "名称": name, "现价": None,
               "距信号%": None, "最高浮盈%": None, "破MA5": None, "天数": None, "建议": "", "止损": hard_stop,
               "行业": industry}
        df = _load_kline(code)
        if df is None or "日期" not in df.columns or len(df) < 6:
            row["建议"] = "K线缺失，待刷新"
            out.append(row)  # K线缺失：保行但指标置空，不强改旧值
            continue
        try:
            closes = df["收盘"].astype(float).tolist()
            dates = df["日期"].astype(str).tolist()
        except Exception:
            row["建议"] = "K线异常，待刷新"
            out.append(row)
            continue
        sig_idx = next((i for i, d in enumerate(dates) if sig_date in d), None)
        if sig_idx is None:
            row["建议"] = "信号日不在K线内，待刷新"
            out.append(row)
            continue
        t_days = len(closes) - 1 - sig_idx
        if t_days > 6:
            continue  # T+5 已过，清除（记录在流水表）
        last = closes[-1]
        sig_close = closes[sig_idx]
        # 最高浮盈%：T+1（信号日次日）以来最高价 / 信号日收盘 - 1（2026-09-23 新增）
        peak = None
        if "最高" in df.columns:
            try:
                highs = df["最高"].astype(float).tolist()
                seg = highs[sig_idx + 1:]
                if seg and sig_close:
                    peak = (max(seg) / sig_close - 1) * 100
            except Exception:
                peak = None
        try:
            dist = (last / sig_close - 1) * 100 if sig_close else float("nan")
        except Exception:
            dist = float("nan")
        ma5 = sum(closes[-5:]) / 5
        below = last < ma5
        if dist == dist and dist <= -8:
            advice = "❌ 触发-8%硬止损，应离场"
        elif t_days >= 5:
            advice = "⏰ T+5 到期，强制离场"
        elif t_days >= 2 and below:
            advice = "破MA5 → 次日开盘离场"
        else:
            advice = "持有中"
        row.update({"现价": last, "距信号%": dist if dist == dist else None,
                    "最高浮盈%": peak if peak == peak else None,
                    "破MA5": below, "天数": t_days, "建议": advice})
        out.append(row)
    return out


def _warm_track_old(today):
    """（兼容包装）旧行刷新为笔记本跟踪表 md 行，列结构与 WARM_TRACK_HDR 一致。"""
    out = []
    for r in warm_tracking_rows(today):
        last_s = f"{r['现价']:.2f}" if r["现价"] is not None else "—"
        dist_s = f"{r['距信号%']:+.1f}" if r["距信号%"] is not None else "—"
        below_s = ("是" if r["破MA5"] else "否") if r["破MA5"] is not None else "—"
        out.append(
            f"| {r['信号日']} | {r['代码']} | {r['名称']} | {last_s} | {dist_s} | {below_s} | "
            f"{r['止损']} | 止盈:{TP_RULES['初动']}｜{r['建议']} |")
    return out


def build_warm(date, warm=None):
    """初动池：实时扫描（只读缓存）→ 当日候选即流水；跟踪表=旧行刷新+当日追加（跨日保留）。"""
    if warm is None:
        try:
            from warm_start_entry import pick_warm_start
            warm = pick_warm_start(top=600, update=False, quiet=True)
        except Exception as e:
            print(f"[笔记本-初动] 扫描失败 {e}")
            warm = []
    flow = []
    for w in warm:
        flow.append(
            f"| {date} | {w['代码']} | {w['名称']} | {_fmt(w.get('现价'))} | {_fmt(w.get('量比'), 2, 'x')} | "
            f"{_fmt(w.get('5日涨幅%'), 1, '%')} | {_fmt(w.get('60日位置'), 0, '%', pct_fraction=True)} | {_fmt(w.get('成交额亿'), 1, '亿')} | "
            f"{_fmt(w.get('止损价'))} | T+1待介入 | — |")
    today_track = [
        f"| {date} | {w['代码']} | {w['名称']} | {_fmt(w.get('现价'))} | 0.0 | 否 | "
        f"{_fmt(w.get('止损价'))} | 止盈:{TP_RULES['初动']}｜T+1 开盘介入（-8% 硬止损） |"
        for w in warm
    ]
    track = _warm_track_old(date) + today_track
    return flow, track


def build_low_pos(date):
    if not WATCH_HISTORY.exists():
        return [], []
    with WATCH_HISTORY.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    today_rows = [r for r in rows if r.get("日期") == date]
    flow = []
    for r in today_rows:
        path_icon = "📉超卖" if r.get("蓄势路径") == "近期超卖" else "📊蓄势"
        flow.append(
            f"| {date} | {r['代码']} | {r['名称']} | {path_icon} | {_fmt(r.get('现价'))} | "
            f"{_fmt(r.get('60日位置'), 0, '%', pct_fraction=True)} | — | {_fmt(r.get('量比'), 2, 'x')} | — | "
            f"{_fmt(r.get('止损价'))} | {r.get('状态') or '观察中'} |")
    latest = {}
    for r in rows:
        latest[r["代码"]] = r
    active = [r for r in latest.values() if r.get("状态") == "观察中"]
    track = []
    for r in active:
        track.append(
            f"| {r.get('日期')} | {r['代码']} | {r['名称']} | — | {_fmt(r.get('现价'))} | — | "
            f"{r.get('买点区间') or '—'} | {_fmt(r.get('止损价'))} | 止盈:{TP_RULES['低位']}｜观察中 |")
    return flow, track


# ---------------- 主入口 ----------------

JOBS = [
    ("趋势", "策略笔记本_右侧趋势池.md",
     "| 日期 | 代码 | 名称 | 推荐价 | 买入区间 | 止损 | 当日收盘 | 当日% | 状态 | 建议 |",
     "| 推荐日 | 代码 | 名称 | 现价 | 累计% | 最高% | 止损 | 操作建议 |"),
    ("短线", "策略笔记本_短线激进池.md",
     "| 日期 | 代码 | 名称 | 推荐价 | 买入区间 | 止损 | 当日收盘 | 当日% | 状态 | 建议 |",
     "| 推荐日 | 代码 | 名称 | 现价 | 累计% | 最高% | 止损 | 操作建议 |"),
    ("初动", "策略笔记本_温和放量初动池.md",
     "| 日期 | 代码 | 名称 | 现价 | 量比 | 5日涨幅 | 60日位置 | 成交额亿 | 硬止损 | 状态 | 建议 |",
     "| 信号日 | 代码 | 名称 | 现价 | 距信号% | 破MA5? | 硬止损 | 操作建议 |"),
    ("低位", "策略笔记本_低位埋伏观察池.md",
     "| 日期 | 代码 | 名称 | 路径 | 现价 | 60日位置 | 距高% | 量比 | 成交额亿 | 止损 | 状态 |",
     "| 首现日 | 代码 | 名称 | 天数 | 现价 | 累计% | 买点区间 | 止损 | 状态 |"),
]


def update_all(date=None, warm=None):
    date = date or datetime.now().strftime("%Y-%m-%d")
    NB_DIR.mkdir(parents=True, exist_ok=True)
    builders = {
        "趋势": lambda: build_trend(date),
        "短线": lambda: build_short(date),
        "初动": lambda: build_warm(date, warm),
        "低位": lambda: build_low_pos(date),
    }
    for name, fname, flow_hdr, track_hdr in JOBS:
        path = NB_DIR / fname
        try:
            flow, track = builders[name]()
            lines = _load_lines(path)
            # 幂等：已有流水行（日期+代码）跳过
            _, body = _table_block(lines, _header_marker(flow_hdr))
            existing = set()
            for ln in body:
                cells = [c.strip() for c in ln.strip("|").split("|")]
                if len(cells) >= 2:
                    existing.add((cells[0], cells[1]))
            new_flow = [r for r in flow if (r.split("|")[1].strip(), r.split("|")[2].strip()) not in existing]
            # 追加流水：新行置顶（最新在上），保留旧行
            lines = _load_lines(path)
            all_flow = new_flow + [ln for ln in _table_block(lines, _header_marker(flow_hdr))[1]]
            lines = _replace_table(lines, _header_marker(flow_hdr), flow_hdr, all_flow)
            # 跟踪表全量重写
            lines = _replace_table(lines, _header_marker(track_hdr), track_hdr, track)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            print(f"[笔记本-{name}] 流水+{len(new_flow)}（跳过重复{len(flow) - len(new_flow)}），跟踪{len(track)}只")
        except Exception as e:
            print(f"[笔记本-{name}] 失败 {e}")
    print(f"[笔记本] 全部更新完成 -> {NB_DIR}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    args = ap.parse_args()
    update_all(args.date)
