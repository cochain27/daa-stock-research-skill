# -*- coding: utf-8 -*-
"""盘中实时监控：持仓止损/止盈 + 今日推荐股买点/卖点触发
只在有新触发时输出告警（state文件防重复推送）；无触发输出一行状态。
"""
import json
from datetime import datetime
from pathlib import Path
from fetch_data import get_realtime_quotes

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data"
STATE_PATH = DATA / "monitor_state.json"
PICKS_PATH = DATA / "today_picks.json"


def _load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def _save(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def run_monitor():
    today = datetime.now().strftime("%Y-%m-%d")
    picks = _load(PICKS_PATH, [])
    state = _load(STATE_PATH, {})
    today_state = state.setdefault(today, {})

    # 加载台账中未结清的推荐股（今日推荐 + 历史仍在跟踪池）
    try:
        from trend_tracker import load_open_picks as _trend_open
        from short_tracker import load_open_picks as _short_open
        open_picks = _trend_open() + _short_open()
    except Exception:
        open_picks = []

    # 合并代码：今日推荐优先用 today_picks 的 buy 数据；台账补充历史跟踪股
    track_map = {}
    for p in picks:
        track_map[str(p["symbol"])] = {
            "名称": p.get("名称", p["symbol"]),
            "buy": p.get("buy", {}),
            "策略标签": p.get("strategy_tag", "波段"),
            "展期": False,
        }
    for r in open_picks:
        code = r["代码"]
        if code not in track_map:
            track_map[code] = {
                "名称": r["名称"],
                "buy": {
                    "止损价": r.get("止损"), "止盈1": r.get("止盈1"), "止盈2": r.get("止盈2"),
                    "建议买价区间": r.get("买区"), "基准价": r.get("基准价"),
                },
                "策略标签": r.get("策略标签", "波段"),
                "展期": r.get("状态") == "展期中" or ("展期" in (r.get("备注") or "")),
            }

    codes = list(track_map.keys())
    quotes = get_realtime_quotes(list(set(codes)))
    alerts = []

    # ===== 跟踪池触发（今日新推荐 + 历史未结清） =====
    for code, info in track_map.items():
        q = quotes.get(code)
        if not q:
            continue
        price = q["现价"]
        b = info.get("buy", {})
        name = info.get("名称", code)
        seen = today_state.setdefault(f"t_{code}", [])
        base = None
        try:
            base = float(b.get("基准价")) if b.get("基准价") else None
        except (ValueError, TypeError):
            pass

        def _f(v):
            try:
                return float(v) if v not in (None, "") else None
            except (ValueError, TypeError):
                return None

        checks = []
        stop = _f(b.get("止损价"))
        tp1 = _f(b.get("止盈1"))
        tp2 = _f(b.get("止盈2"))
        breakout = _f(b.get("突破买点"))

        # 已展期票：止盈档位升级为 +10%/+15%（基于基准价重算）
        # 短线票：覆盖为短线参数（台账可能存的是波段档位）
        from config import (EXTEND_TP1, EXTEND_TP2,
                            SHORT_STOP_LOSS, SHORT_TAKE_PROFIT_1, SHORT_TAKE_PROFIT_2)
        extended = info.get("展期", False)
        tag = info.get("策略标签", "波段")
        if tag == "短线" and base and base > 0:
            stop = round(base * (1 + SHORT_STOP_LOSS), 2)
            tp1 = round(base * (1 + SHORT_TAKE_PROFIT_1), 2)
            tp2 = round(base * (1 + SHORT_TAKE_PROFIT_2), 2)
        elif extended and base and base > 0:
            tp1 = round(base * (1 + EXTEND_TP1), 2)
            tp2 = round(base * (1 + EXTEND_TP2), 2)

        tag_icon = "⚡短线" if tag == "短线" else ("🟢展期" if extended else "📈波段")
        if stop and price <= stop:
            checks.append(("止损", f"⛔ 跟踪池 {name}({code}) {tag_icon} 现价{price} 跌破止损{stop}，移除跟踪"))
        if tp2 and price >= tp2:
            pct = "+15%" if extended else ("+8%" if tag == "短线" else "+10%")
            checks.append(("止盈2", f"💰 跟踪池 {name}({code}) {tag_icon} 现价{price} 达止盈2 {tp2}（{pct}），清仓移除"))
        if tp1 and price >= tp1:
            pct = "+10%" if extended else ("+5%" if tag == "短线" else "+6%")
            checks.append(("止盈1", f"💰 跟踪池 {name}({code}) {tag_icon} 现价{price} 达止盈1 {tp1}（{pct}），建议减半"))

        lo, hi = None, None
        rng = str(b.get("建议买价区间", ""))
        if "-" in rng:
            try:
                lo, hi = [float(x) for x in rng.split("-")]
            except ValueError:
                pass
        if breakout and price >= breakout:
            checks.append(("突破买点", f"🔥 跟踪池 {name}({code}) 现价{price} 突破买点{breakout}，可按计划介入"))
        if lo and hi and lo <= price <= hi:
            checks.append(("进入买区", f"🎯 跟踪池 {name}({code}) 现价{price} 进入建议买区 {rng}，可挂单"))

        for level, msg in checks:
            if level not in seen:
                alerts.append(msg)
                seen.append(level)

    _save(STATE_PATH, state)
    now = datetime.now().strftime("%H:%M")
    if alerts:
        print(f"【盘中触发 {now}】")
        for a in alerts:
            print(a)
        # 微信推送告警
        try:
            from notify import push_alert
            ok, ch, msg = push_alert(f"⚡ 盘中触发 {now}", "\n".join(alerts))
            print(f"[微信推送] {'成功' if ok else '跳过/失败: '+msg}")
        except Exception:
            pass
    else:
        # 无触发也输出跟踪池现价快照
        snap = []
        for code, info in track_map.items():
            q = quotes.get(code)
            if q and info.get("buy", {}).get("基准价"):
                try:
                    pct = (q["现价"] / float(info["buy"]["基准价"]) - 1) * 100
                    snap.append(f"{info['名称']}{q['现价']}({pct:+.1f}%)")
                except (ValueError, TypeError):
                    pass
            elif q:
                snap.append(f"{info['名称']}{q['现价']}")
        print(f"【盘中监控 {now}】无触发。跟踪池：{'、'.join(snap) if snap else '无数据'}")


if __name__ == "__main__":
    run_monitor()
