# -*- coding: utf-8 -*-
"""行业热度归因回测：验证"板块热度持续（主线）→ 低位票启动概率更高"假设。

输入：
  data/转化研究_全量信号.csv —— 16743 条低位启动信号（日期/代码/行业[30粗分类]/成功/7日收益 + 前兆特征）
  data/industry_heat.csv     —— THS 90 板块日涨幅（2026-02 起，已回填）

信号里的"行业"是 30 粗分类（含"其他"57.6%），THS 板块是 90 细分类。
这里用「粗行业 → THS 板块组」映射（GROSS_TO_BOARDS），组内任一板块近 W 日
涨幅进前 top_n 的天数 = 热度分。其他/未映射 → 直接归入"冷门(0)"。

防未来函数：只用信号日 D 之前的板块数据；行业当天才首次上榜也计 0。

结论（2026-09-10）：
- 热度对低位池转化率几乎无增益（冷门 10.0% vs 主线 11.1%）
- 但对 7 日胜率有提升（46.2%→48.0%）；叠加前兆剔劣后 45.5%→53.3%，7日均收 -0.06%→+0.49%
- 窗口敏感性：W5 增益最明显，W10/W20 衰减 → 短期效应 → 落地做"排序加分不硬过滤"
"""
import csv, os
from collections import defaultdict

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIG = os.path.join(BASE, "data", "转化研究_全量信号.csv")
HEAT = os.path.join(BASE, "data", "industry_heat.csv")
OUT = os.path.join(BASE, "data", "行业热度归因_转化.csv")

# ---------- 粗行业 → THS 二级板块组 ----------
GROSS_TO_BOARDS = {
    "传媒": ["文化传媒", "游戏", "影视院线", "互联网电商", "广告营销"],
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
    "军工": ["军工装备", "军工电子", "航天航空"],
    "有色金属": ["工业金属", "小金属", "贵金属", "能源金属", "金属新材料"],
    "钢铁": ["钢铁"],
    "煤炭": ["煤炭开采加工"],
    "石油石化": ["油气开采及服务", "石油加工贸易"],
    "电力": ["电力", "电网设备"],
    "新能源": ["光伏设备", "风电设备", "电池", "能源金属", "其他电源设备"],
    "化工": ["化学制品", "化学原料", "化学纤维", "塑料制品", "橡胶制品", "农化制品", "非金属材料"],
    "农业": ["种植业与林业", "养殖业", "农产品加工"],
    "地产": ["房地产", "房地产开发"],
    "建筑": ["建筑装饰", "建筑材料", "工程咨询服务"],
    "机械": ["通用设备", "专用设备", "自动化设备", "工程机械", "轨交设备"],
    "家电": ["白色家电", "黑色家电", "小家电", "厨卫电器"],
    "纺织服装": ["纺织制造", "服装家纺"],
    "交运": ["港口航运", "机场航运", "公路铁路运输", "物流", "铁路公路"],
    "环保": ["环保设备", "环境治理"],
    "教育": ["教育"],
    "旅游": ["旅游及酒店", "酒店餐饮"],
    "其他": [],  # 粗分类"其他"无法映射 → 冷门
}

def gross_to_boards(gross):
    return GROSS_TO_BOARDS.get(gross, [])

def read_csv(path):
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))

def clean(s):
    return str(s).replace(" ", "")

def _load_heat():
    """载入行业热度，返回 (day_board, heat_days, BOARD2CLEAN)。"""
    heat_rows = read_csv(HEAT)
    day_board = defaultdict(dict)
    for r in heat_rows:
        d, b = r["日期"], str(r["板块"]).strip()
        try:
            pct = float(r["涨跌幅"])
        except (TypeError, ValueError):
            continue
        day_board[d][b] = pct
    heat_days = sorted(day_board.keys())
    BOARD2CLEAN = {}
    for d in heat_days:
        for b in day_board[d]:
            BOARD2CLEAN.setdefault(clean(b), b)
    return day_board, heat_days, BOARD2CLEAN

DAY_BOARD, HEAT_DAYS, BOARD2CLEAN = _load_heat()

def heat_score(gross, date, window=5, top_n=15):
    """信号日 date 之前最近 window 个交易日，该粗行业映射的 THS 板块组内
    任一板块涨幅进全市场前 top_n 的天数。返回 (score, last_rank)"""
    boards = gross_to_boards(gross)
    if not boards or date not in DAY_BOARD:
        return 0, None
    di = HEAT_DAYS.index(date)
    win = HEAT_DAYS[max(0, di - window):di]
    score, last_rank = 0, None
    for d in win:
        row = DAY_BOARD.get(d)
        if not row:
            continue
        ranked = sorted(row.items(), key=lambda kv: kv[1], reverse=True)
        for i, (b, pct) in enumerate(ranked, 1):
            if BOARD2CLEAN.get(clean(b)) in boards:
                last_rank = i
                if i <= top_n:
                    score += 1
                break
    return score, last_rank

def precursor_ok(s):
    try:
        return (float(s["amp20_prev"]) < 0.15 and float(s["std20_prev"]) < 0.04
                and float(s["dist_close_max60"]) > -22)
    except (TypeError, ValueError):
        return False

def stats(sub):
    """返回 (转化率, n, 7日胜率, 7日均收, 转化票7日均收)。空值容错。"""
    if not sub:
        return None
    ok = sum(1 for x in sub if int(x["成功"]) == 1)
    conv = ok / len(sub)
    r7 = []
    for x in sub:
        try:
            r7.append(float(x["7日收益"]))
        except (TypeError, ValueError):
            continue
    win7 = sum(1 for x in r7 if x > 0) / len(r7) if r7 else 0
    avg7 = sum(r7) / len(r7) if r7 else 0
    c7 = []
    for x in sub:
        if int(x["成功"]) != 1:
            continue
        try:
            c7.append(float(x["7日收益"]))
        except (TypeError, ValueError):
            continue
    avg_conv_ret = sum(c7) / len(c7) if c7 else None
    return conv, len(sub), win7, avg7, avg_conv_ret

def main():
    signals = read_csv(SIG)
    print(f"行业热度: {len(DAY_BOARD)} 交易日, {HEAT_DAYS[0]}~{HEAT_DAYS[-1]}")
    print(f"信号: {len(signals)} 条")

    rows = []
    mapped = 0
    for s in signals:
        gross = s["行业"]
        if gross_to_boards(gross):
            mapped += 1
        score, last_rank = heat_score(gross, s["日期"])
        rows.append((score, last_rank, s))
    print(f"可映射行业信号: {mapped}/{len(signals)} ({mapped/len(signals):.1%}), 其余归冷门")

    def fmt(st):
        # 口径：7日收益列本身是百分数数值（2.25 = 2.25%），均收/转化票均收直接写；
        # 只有转化率/胜率是小数（0.105 = 10.5%），写盘时才 ×100。
        conv, n, win7, avg7, avg_conv_ret = st
        conv_ret_s = f"{avg_conv_ret:.2f}" if avg_conv_ret is not None else ""
        return n, conv, win7, avg7, conv_ret_s

    print("\n===== 按信号日前5日 板块涨幅前15 上榜天数 分层 =====")
    layers = [
        ("0天(冷门)", lambda sc: sc == 0),
        ("1天(偶发)", lambda sc: sc == 1),
        ("2天(升温)", lambda sc: sc == 2),
        ("3天+(主线)", lambda sc: sc >= 3),
    ]
    table = []
    for name, cond in layers:
        sub = [r[2] for r in rows if cond(r[0])]
        st = stats(sub)
        if st:
            n, conv, win7, avg7, c7 = fmt(st)
            table.append((name, n, conv, win7, avg7, c7))
            print(f"{name:<12} n={n:<6} 转化率={conv:<8.1%} 7日胜率={win7:<7.1%} 7日均收={avg7:<+8.2f}% 转化票7日均收={c7}")

    print("\n===== 关键对比 =====")
    hot = [r[2] for r in rows if r[0] >= 3]
    warm = [r[2] for r in rows if r[0] >= 1]
    cold = [r[2] for r in rows if r[0] == 0]
    key = []
    for name, g in (("热度>=3(持续主线)", hot), ("热度>=1(有热度)", warm), ("热度=0(冷门)", cold), ("全部", [r[2] for r in rows])):
        st = stats(g)
        if st:
            n, conv, win7, avg7, c7 = fmt(st)
            key.append((name, n, conv, win7, avg7, c7))
            print(f"{name:<16} n={n:<6} 转化率={conv:<8.1%} 7日胜率={win7:<7.1%} 7日均收={avg7:<+8.2f}% 转化票7日均收={c7}")

    print("\n===== 热度 × 前兆剔劣三件套 叠加 =====")
    combos = [
        ("前兆剔劣(基线)", lambda s: precursor_ok(s)),
        ("前兆+热度>=3", lambda s: precursor_ok(s) and heat_score(s["行业"], s["日期"])[0] >= 3),
        ("前兆+热度>=1", lambda s: precursor_ok(s) and heat_score(s["行业"], s["日期"])[0] >= 1),
        ("前兆+热度0冷门", lambda s: precursor_ok(s) and heat_score(s["行业"], s["日期"])[0] == 0),
    ]
    combo_out = []
    for name, cond in combos:
        sub = [s for s in signals if cond(s)]
        st = stats(sub)
        if st:
            n, conv, win7, avg7, c7 = fmt(st)
            combo_out.append((name, n, conv, win7, avg7, c7))
            print(f"{name:<18} n={n:<6} 转化率={conv:<8.1%} 7日胜率={win7:<7.1%} 7日均收={avg7:<+8.2f}% 转化票7日均收={c7}")

    print("\n===== 窗口敏感性：前兆+热度>=3 (W5/W10/W20) vs 前兆基线 =====")
    base = [s for s in signals if precursor_ok(s)]
    cb = stats(base)
    print(f"前兆基线          n={cb[1]:<6} 转化率={cb[0]:.1%} 7日胜率={cb[2]:.1%} 7日均收={cb[3]:+.2f}%")
    win_out = [("前兆基线", cb[1], cb[0], cb[2], cb[3], "")] if cb else []
    for W in (5, 10, 20):
        sub = [s for s in base if heat_score(s["行业"], s["日期"], window=W, top_n=15)[0] >= 3]
        st = stats(sub)
        if st:
            n, conv, win7, avg7, c7 = fmt(st)
            win_out.append((f"前兆+热度>=3 W{W}", n, conv, win7, avg7, c7))
            print(f"前兆+热度>=3 W{W:<2} n={n:<6} 转化率={conv:.1%} 7日胜率={win7:.1%} 7日均收={avg7:+.2f}%")

    # ---------- 落盘 ----------
    with open(OUT, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["分组", "信号数", "20日转化率%", "7日胜率%", "7日均收%", "转化票7日均收%"])
        for row in table + key:
            name, n, conv, win7, avg7, c7 = row
            w.writerow([name, n, f"{conv*100:.1f}", f"{win7*100:.1f}", f"{avg7:.2f}", c7])
        w.writerow([])
        for row in combo_out:
            name, n, conv, win7, avg7, c7 = row
            w.writerow([f"叠加:{name}", n, f"{conv*100:.1f}", f"{win7*100:.1f}", f"{avg7:.2f}", c7])
        w.writerow([])
        for row in win_out:
            name, n, conv, win7, avg7, c7 = row
            w.writerow([f"窗口:{name}", n, f"{conv*100:.1f}", f"{win7*100:.1f}", f"{avg7:.2f}", c7])
    print(f"\n已输出: {OUT}")

if __name__ == "__main__":
    main()
