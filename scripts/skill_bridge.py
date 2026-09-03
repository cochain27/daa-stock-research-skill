# -*- coding: utf-8 -*-
"""stock-researcher 技能桥接层
封装 skill 的 CLI 调用（子进程隔离 + 超时 + 失败降级），供日报流程使用。
数据源：腾讯/东财（与skill内部一致），任何一步失败返回 None，绝不抛异常阻断日报。
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

def _locate_skill_dir():
    """定位 stock-researcher 技能目录；找不到则返回 None（调用方降级）。
    优先级：环境变量 DAA_SKILL_DIR > 本机已安装 skill 目录 > None
    """
    env = os.environ.get("DAA_SKILL_DIR")
    if env and Path(env).exists():
        return Path(env)
    for base in (
        Path.home() / ".workbuddy" / "skills" / "aistockresearcher__skillhub",
        Path.home() / ".workbuddy" / "plugins" / "cache" / "workbuddy-builtin" / "skill-aistockresearcher__skillhub",
    ):
        if (base / "scripts").exists():
            return base
    return None


SKILL_DIR = _locate_skill_dir()
CLI = SKILL_DIR / "scripts" / "stock_predict.py" if SKILL_DIR else None


def _skill_available():
    """skill 是否可用；不可用时所有桥接函数直接返回 None，绝不阻断日报"""
    return CLI is not None and CLI.exists()


def _run_cli(args, timeout=120):
    """跑CLI，返回stdout文本；失败返回None"""
    if not _skill_available():
        return None
    try:
        r = subprocess.run(
            ["python", str(CLI)] + args,
            cwd=str(SKILL_DIR), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        return r.stdout if r.returncode == 0 and r.stdout else None
    except Exception:
        return None


def get_sector_rps():
    """板块RPS相对强度排名 → list[dict]；失败返回None"""
    out = _run_cli(["--sector-ranking", "cn"], timeout=120)
    if not out:
        return None
    rows = []
    for line in out.splitlines():
        m = re.match(r"\s*\d+\s+(\S+)\s+([+-]?[\d.]+)\s+(\d+)%\s+([+-]?[\d.]+)%\s+([+-]?[\d.]+)%\s+(\S+)", line)
        if m:
            rows.append({
                "板块": m.group(1), "RPS": float(m.group(2)),
                "百分位": int(m.group(3)), "20d": float(m.group(4)),
                "60d": float(m.group(5)), "趋势": m.group(6),
            })
    return rows or None


def get_market_regime():
    """市场体制（牛/熊/震荡）→ dict；失败返回None"""
    out = _run_cli(["--index-regime", "sh000001"], timeout=120)
    if not out:
        return None
    m = re.search(r"市场体制:\s*(\S+)\s*\(置信(\d+)%\)", out)
    m2 = re.search(r"牛市:(\d+)%\s*熊市:(\d+)%\s*震荡:(\d+)%", out)
    m3 = re.search(r"体制分([+-]?\d+)", out)
    if not m:
        return None
    return {
        "体制": m.group(1), "置信": int(m.group(2)),
        "牛市%": int(m2.group(1)) if m2 else None,
        "熊市%": int(m2.group(2)) if m2 else None,
        "震荡%": int(m2.group(3)) if m2 else None,
        "体制分": int(m3.group(1)) if m3 else 0,
    }


def get_stock_score(code):
    """个股预测评分（--simple --quick --json）→ dict；失败返回None。
    返回: {score, signal, confidence, summary}"""
    out = _run_cli([str(code), "--simple", "--quick", "--json"], timeout=90)
    if not out:
        return None
    try:
        # JSON可能混在日志里，从第一个{开始截取
        start = out.find("{")
        if start < 0:
            return None
        d = json.loads(out[start:])
        score = d.get("score")
        if isinstance(score, dict):
            score = score.get("total")
        return {
            "score": score,
            "signal": d.get("signal"),
            "confidence": d.get("confidence"),
            "summary": d.get("summary", ""),
        }
    except Exception:
        return None


def get_value_decision(code, timeout=180):
    """价值投资交叉验证（stock-researcher 6大模块：护城河/财务/DCF/管理层/行业/因子）。
    直接进程内调用 ValueInvestingDecision.analyze(code)，避免 CLI 序列化损耗。
    返回 {code, verdict, score, fair_value, margin, breakdown, insufficient, error}；
    失败返回 None，绝不抛异常阻断日报。
    """
    import os
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    if not _skill_available():
        return None
    try:
        sys_path = str(SKILL_DIR / "scripts")
        if sys_path not in sys.path:
            sys.path.insert(0, sys_path)
        from stock_researcher.value_investing import ValueInvestingDecision
        vd = ValueInvestingDecision()
        r = vd.analyze(str(code))
        d = {
            "code": str(code),
            "verdict": getattr(r, "verdict", None),
            "score": round(r.weighted_score, 1) if getattr(r, "weighted_score", None) else None,
            "fair_value": list(r.fair_value_range) if getattr(r, "fair_value_range", None) else None,
            "margin": round(r.margin_of_safety, 2) if getattr(r, "margin_of_safety", None) is not None else None,
            "insufficient": bool(getattr(r, "insufficient_data", False)),
        }
        if getattr(r, "breakdown", None):
            d["breakdown"] = {
                k: round(v["score"], 1) if isinstance(v, dict) and v.get("score") is not None else None
                for k, v in r.breakdown.items()
            }
        return d
    except Exception:
        return None
