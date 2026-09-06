# -*- coding: utf-8 -*-
"""风险日历排雷（2026-09-07 新增）

拦截两类「能提前知道、却常被忽略」的坑：
1. 财报预约披露落在未来 N 天内 —— 业绩不确定性，短线/波段都该回避或减仓
2. 已发业绩预告且类型为负面（预减/首亏/续亏/增亏/略减/转亏）—— 基本面已知恶化

数据源：akshare 免费接口（巨潮预约披露 + 东财业绩预告），零成本。
TODO: 个股级解禁（stock_restricted_release_detail_em 需按日遍历，成本高，暂不接）

设计原则（必须遵守）：
- 全市场数据每日只拉一次，缓存到 data/risk_calendar_cache.json
- 任何一步失败/超时一律降级为空 dict，绝不抛异常阻断日报
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

from config import DATA_DIR

CACHE_FILE = DATA_DIR / "risk_calendar_cache.json"
NEGATIVE_TYPES = {"预减", "首亏", "续亏", "增亏", "略减", "转亏", "减亏"}


def _periods():
    """返回需要关注的报告期标签（当前期 + 下一期）"""
    y = datetime.now().year
    m = datetime.now().month
    if m <= 4:
        return [f"{y-1}年报", f"{y}一季"]
    if m <= 8:
        return [f"{y}一季", f"{y}半年报"]
    if m <= 10:
        return [f"{y}半年报", f"{y}三季"]
    return [f"{y}三季", f"{y}年报"]


def _yjyg_periods():
    """业绩预告期：[上一期(全量), 当期(少量)]，当期覆盖上一期"""
    y, m = datetime.now().year, datetime.now().month
    if m >= 11:
        return [f"{y}0930", f"{y}1231"]
    if m >= 9:
        return [f"{y}0630", f"{y}0930"]
    if m >= 7:
        return [f"{y}0331", f"{y}0630"]
    if m >= 4:
        return [f"{y-1}1231", f"{y}0331"]
    return [f"{y-1}0930", f"{y-1}1231"]


def _fetch_all():
    """拉取全市场披露日 + 业绩预告；失败返回 ({}, {})"""
    import akshare as ak
    disclosure, forecast = {}, {}
    for p in _periods():
      try:
        df = ak.stock_report_disclosure(market="沪深京", period=p)
        if df is None or df.empty:
            continue
            for _, r in df.iterrows():
                code = str(r.get("股票代码", "")).zfill(6)
                if not code or code == "000000":
                    continue
                # 预约日取「实际披露」，未披露则用最后一次变更/首次预约
                d = r.get("实际披露")
                if d is None or (hasattr(d, "to_pydatetime") and str(d) == "NaT"):
                    for col in ("三次变更", "二次变更", "初次变更", "首次预约"):
                        v = r.get(col)
                        if v is not None and str(v) != "NaT":
                            d = v
                            break
                if d is None or str(d) == "NaT":
                    continue
                ds = str(d)[:10]
                # 同一只票多期，保留最近的一个未来日期
                if code not in disclosure or ds < disclosure[code]:
                    if ds >= datetime.now().strftime("%Y-%m-%d"):
                        disclosure[code] = ds
      except Exception:
        continue

    # 业绩预告：先拉「上一报告期」（全量已披露），再用「当期」（少量）覆盖
    for rep in _yjyg_periods():
      try:
        df = ak.stock_yjyg_em(date=rep)
        if df is not None and not df.empty:
            for _, r in df.iterrows():
                code = str(r.get("股票代码", "")).zfill(6)
                t = r.get("预告类型")
                if code and t:
                    forecast[code] = str(t).strip()
      except Exception:
        continue
    return disclosure, forecast


def _load():
    """带当日缓存的加载"""
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        if CACHE_FILE.exists():
            c = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            if c.get("date") == today:
                return c.get("disclosure", {}), c.get("forecast", {})
    except Exception:
        pass
    disclosure, forecast = _fetch_all()
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(
            json.dumps({"date": today, "disclosure": disclosure, "forecast": forecast},
                       ensure_ascii=False),
            encoding="utf-8")
    except Exception:
        pass
    return disclosure, forecast


def check(codes, days_ahead=10):
    """对候选股做排雷。
    返回 list[dict]: {code, name, events:[str], level: '高'|'中'|None}
    level 为 None 表示无风险事件，不输出。
    """
    codes = [str(c).zfill(6) for c in codes if c]
    if not codes:
        return []
    disclosure, forecast = _load()
    if not disclosure and not forecast:
        return []  # 数据源全挂，静默跳过

    today = datetime.now().date()
    horizon = today + timedelta(days=days_ahead)
    rows = []
    for code in codes:
        events, level = [], None
        d = disclosure.get(code)
        if d:
            try:
                dd = datetime.strptime(d, "%Y-%m-%d").date()
                if today <= dd <= horizon:
                    events.append(f"{d[5:]} 披露财报")
                    level = "中"
            except Exception:
                pass
        f = forecast.get(code)
        if f and f in NEGATIVE_TYPES:
            events.append(f"业绩预告「{f}」")
            level = "高"  # 已公开的负面信息，优先级最高
        if events:
            rows.append({"code": code, "name": "", "events": events, "level": level})
    return rows


def render_md(rows):
    """渲染为日报可插入的 markdown；无风险返回空串"""
    if not rows:
        return ""
    lines = ["\n### ⚠️ 风险日历排雷\n",
             "| 代码 | 风险项 | 等级 |", "|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['code']} | {'；'.join(r['events'])} | {r['level']} |")
    lines.append("\n> 排雷规则：未来 10 天内披露财报＝业绩不确定；已发负面预告＝基本面已知恶化。"
                 "等级「高」建议剔除或等披露后再看。\n")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    codes = sys.argv[1:] or ["600519", "000001", "601398"]
    res = check(codes)
    print(render_md(res) or "所选代码无风险事件（或数据源暂不可用）")
