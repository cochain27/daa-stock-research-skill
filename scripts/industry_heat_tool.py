# -*- coding: utf-8 -*-
"""行业热度工具：个股行业名 → THS 板块名匹配 + 板块热度状态判定。

从 close_review.py 抽出的纯函数（不依赖 tracker/回测引擎），供 stock_screener
（低位池/趋势池排序加分）与 close_review（复盘主线判定）复用，避免反向导入形成环。
数据源 data/industry_heat.csv：THS 90 二级板块日涨幅（2026-02 起）。
"""
import csv
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
INDUSTRY_HEAT_FILE = BASE / "data" / "industry_heat.csv"

_IND_ROMAN = re.compile(r"[ⅠⅡⅢⅣⅤ]+$")

# 东财三级名 → 同花顺二级板块名 同义词映射（THS 无同名板块、但语义一致）
_IND_SYN = {
    "航运港口": "港口航运",
    "炼化及贸易": "石油加工贸易",
    "装修装饰": "建筑装饰",
    "航天装备": "军工装备",
    "航空装备": "军工装备",
    "地面兵装": "军工装备",
}


def _norm_ind(name):
    """个股行业名 → industry_heat.csv 里的板块名（同花顺二级行业）。
    东财 f127 三级名（IT服务Ⅱ/白酒Ⅱ/煤炭开采）去罗马数字后缀后与同花顺板块名
    精确一致；仍不匹配时做关键词包含匹配。"""
    if not name:
        return ""
    name = str(name).strip()
    # 1) 去罗马数字后缀（IT服务Ⅱ→IT服务，白酒Ⅱ→白酒）
    s = _IND_ROMAN.sub("", name).strip()
    base = s or name
    # 2) 同义词映射（先查原始名、再查去后缀名）
    syn = _IND_SYN.get(name) or _IND_SYN.get(base)
    if syn:
        return syn
    return base


# 粗分类行业名 → THS 二级板块组（2026-09-10 归因回测用映射，补强单板块匹配的漏配）。
# 选股/回测中行业名可能是 30 粗分类（计算机/有色金属/新能源…），在 90 板块全名里
# 匹配不到或只匹配到单个板块（如 计算机→计算机设备 漏了 IT服务/软件开发），
# 用组映射后按"组内任一板块上榜"计热度。
GROSS_TO_BOARDS = {
    "传媒": ["文化传媒", "游戏", "影视院线", "互联网电商"],
    "券商": ["证券", "多元金融"],
    "银行": ["银行"],
    "保险": ["保险"],
    "白酒": ["白酒", "饮料制造", "食品加工制造"],
    "食品饮料": ["食品加工制造", "饮料制造", "白酒"],
    "医药": ["化学制药", "生物制品", "中药", "医疗器械", "医疗服务", "医药商业"],
    "汽车": ["汽车整车", "汽车零部件", "汽车服务及其他"],
    "电子": ["半导体", "光学光电子", "消费电子", "元件", "其他电子", "电子化学品"],
    "通信": ["通信服务", "通信设备", "软件开发", "IT服务"],
    "计算机": ["软件开发", "IT服务", "计算机设备"],
    "军工": ["军工装备", "军工电子"],
    "有色金属": ["工业金属", "小金属", "贵金属", "能源金属", "金属新材料"],
    "钢铁": ["钢铁"],
    "煤炭": ["煤炭开采加工"],
    "石油石化": ["油气开采及服务", "石油加工贸易"],
    "电力": ["电力", "电网设备"],
    "新能源": ["光伏设备", "风电设备", "电池", "能源金属", "其他电源设备"],
    "化工": ["化学制品", "化学原料", "化学纤维", "塑料制品", "橡胶制品", "农化制品", "非金属材料"],
    "农业": ["种植业与林业", "养殖业", "农产品加工"],
    "地产": ["房地产"],
    "建筑": ["建筑装饰", "建筑材料"],
    "机械": ["通用设备", "专用设备", "自动化设备", "工程机械", "轨交设备"],
    "家电": ["白色家电", "黑色家电", "小家电", "厨卫电器"],
    "纺织服装": ["纺织制造", "服装家纺"],
    "交运": ["港口航运", "机场航运", "公路铁路运输", "物流"],
    "环保": ["环保设备", "环境治理"],
    "教育": ["教育"],
    "旅游": ["旅游及酒店"],
    "其他": [],
}


def _boards_for(industry):
    """行业名 → 匹配的 THS 板块名列表（优先粗分类组映射，其次单板块归一化匹配）。"""
    gross = _norm_ind(industry)
    if gross in GROSS_TO_BOARDS:
        return GROSS_TO_BOARDS[gross]
    _, boards_all = load_heat()
    b = _match_heat_board(gross, boards_all)
    return [b] if b else []


def _match_heat_board(target, boards):
    """行业名 → 同花顺板块名的归一化匹配（返回板块名或 None）。
    优先级：精确 → 目标含于板块名（≥3字取最短，如 煤炭开采→煤炭开采加工）
    → 板块名含于目标（≥3字取最长）→ 前缀匹配（≥2字，如 通信→通信服务）
    → 逐字截尾精确匹配（如 电力设备→电力）。"""
    target = str(target).strip()
    if not target:
        return None
    if target in boards:
        return target
    cands = [b for b in boards if len(target) >= 3 and target in b]
    if cands:
        return min(cands, key=len)
    cands = [b for b in boards if len(b) >= 3 and b in target]
    if cands:
        return max(cands, key=len)
    cands = [b for b in boards if len(target) >= 2 and b.startswith(target)]
    if cands:
        return min(cands, key=len)
    s = target
    while len(s) >= 2:
        if s in boards:
            return s
        s = s[:-1]
    return None


def load_heat():
    """读 industry_heat.csv → (rows, boards_all)。rows 为原始 dict 列表。"""
    path = INDUSTRY_HEAT_FILE
    rows = []
    if path.exists():
        with path.open(encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    boards_all = sorted({str(r["板块"]).strip() for r in rows if str(r["板块"]).strip()})
    return rows, boards_all


def industry_heat_status(industry_name, today, window=5, top_n=15):
    """判定某行业近 window 个交易日的板块热度状态。

    读 industry_heat.csv，统计最近 window 个交易日中该行业板块
    （行业名→板块组映射，组内任一板块）涨跌幅排名进入前 top_n 的天数：
        >= ceil(window*0.6)  → 🔥主线（持续领涨）
        >= 1                → 🌤升温（偶发上榜）
        0                   → ❄️冷门（无热度）
    返回 (status_tag, rank_days, total_days, last_rank)
    """
    rows, boards_all = load_heat()
    if not rows or not industry_name:
        return "❄️冷门", 0, 0, None
    boards = _boards_for(industry_name)
    if not boards:
        return "❄️冷门", 0, 0, None
    rows_today = [r for r in rows if str(r.get("日期", "")) <= today]
    days = sorted({str(r["日期"]) for r in rows_today})[-window:]
    if not days:
        return "❄️冷门", 0, 0, None
    rank_days = 0
    last_rank = None
    for d in days:
        day_rows = [r for r in rows_today if r["日期"] == d
                    and str(r.get("涨跌幅", "")).replace("-", "").replace(".", "").isdigit()]
        if not day_rows:
            continue
        day_rows.sort(key=lambda x: float(x["涨跌幅"]), reverse=True)
        for i, r in enumerate(day_rows, 1):
            if str(r["板块"]).strip() in boards:
                last_rank = i
                if i <= top_n:
                    rank_days += 1
                break
    total_days = len(days)
    if rank_days >= max(1, int(total_days * 0.6)):
        return "🔥主线", rank_days, total_days, last_rank
    if rank_days >= 1:
        return "🌤升温", rank_days, total_days, last_rank
    return "❄️冷门", rank_days, total_days, last_rank


_heat_days_cache = None


def industry_heat_status_simple(industry_name, window=5, top_n=15):
    """选股场景版：以 industry_heat.csv 最近交易日为基准的热度判定。
    省去调用方传 today，只返回 (tag, rank_days, total_days, last_rank)。"""
    global _heat_days_cache
    if _heat_days_cache is None:
        rows, _ = load_heat()
        days = sorted({str(r["日期"]) for r in rows})
        _heat_days_cache = days[-1] if days else ""
    return industry_heat_status(industry_name, _heat_days_cache, window=window, top_n=top_n)
