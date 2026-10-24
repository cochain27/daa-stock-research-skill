# -*- coding: utf-8 -*-
"""初动池漂移归因（2026-10-23 新增）

背景：初动池 10-16 周胜率 60%（≈基线 59%）、10-19~10-23 周胜率 0%、均值 -6.52%。
10-23 周复盘原计划「下周做归因」，本次当场完成。

做法：把初动笔记本全部信号（09-21 起 27 条）逐条用 V2 规则本地 K 线重放，
再与「信号日市场环境」（温度/情绪分/涨停家数，从每日推送文件解析）交叉分组，
定位「什么样的信号日 → 什么样的 T+5 结果」。

输出：
- 全样本基线复算（27 条，校准回放链路是否与周报一致）
- 按信号日情绪分 / 温度 / 涨停家数分组的表现
- 按信号自身特征（量比 / 5日涨幅 / 60日位置 / 成交额）分组的表现
- 次日开盘缺口（T+1 开盘 vs 信号日收盘）分布——"买在情绪高点"的直接证据

用法：python scripts/warm_drift_attribution.py
"""
import re
import sys
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "scripts"))

from weekly_review import warm_signals, warm_replay  # noqa: E402

PUSH = BASE / "05_每日推送"
NB = BASE / "data" / "笔记本" / "策略笔记本_温和放量初动池.md"


def market_env():
    """从每日推送解析 温度/情绪分/涨停家数"""
    out = {}
    for p in sorted(PUSH.glob("推送_2026-*.md")):
        day = p.stem.replace("推送_", "")
        t = p.read_text(encoding="utf-8")
        temp = re.search(r"市场温度：(\d+)/100", t)
        sent = re.search(r"\|\s*情绪\s*\|\s*(\d+)\s*\|", t)
        zt = re.search(r"涨停\s*(\d+)", t)
        out[day] = {
            "温度": int(temp.group(1)) if temp else None,
            "情绪分": int(sent.group(1)) if sent else None,
            "涨停家数": int(zt.group(1)) if zt else None,
        }
    return out


def signal_features():
    """从笔记本流水取信号自身特征"""
    txt = NB.read_text(encoding="utf-8")
    rows = []
    for line in txt.splitlines():
        m = re.match(r"\|\s*(\d{4}-\d{2}-\d{2})\s*\|\s*(\d{6})\s*\|\s*([^|]+)\|\s*([\d.]+)\s*\|"
                     r"\s*([\d.]+)x\s*\|\s*([-\d.]+)%\s*\|\s*(\d+)%\s*\|\s*([\d.]+)亿", line)
        if m:
            d, code, name, px, vr, chg5, pos60, amt = m.groups()
            rows.append({"信号日": d, "代码": code, "名称": name.strip(),
                         "量比": float(vr), "5日涨幅": float(chg5),
                         "pos60": int(pos60), "成交额亿": float(amt)})
    df = pd.DataFrame(rows).drop_duplicates(subset=["信号日", "代码"])
    return df


def gap_pct(code, sig_date):
    """T+1 开盘 vs 信号日收盘的跳空幅度"""
    from weekly_review import _load_kline
    df = _load_kline(code)
    if df is None:
        return None
    dates = df["日期"].astype(str).tolist()
    i = next((k for k, d in enumerate(dates) if sig_date in d), None)
    if i is None or i + 1 >= len(dates):
        return None
    try:
        c = float(df["收盘"].astype(float).iloc[i])
        o = float(df["开盘"].astype(float).iloc[i + 1])
    except Exception:
        return None
    return (o / c - 1) * 100 if c else None


def summarize(df, by, label):
    print(f"\n== 按 {label} 分组 ==")
    if by not in df.columns:
        print("  字段缺失，跳过")
        return
    sub = df.dropna(subset=[by, "收益%"])
    if sub.empty:
        print("  无有效样本")
        return
    bins = {"低": sub[by] <= sub[by].quantile(0.33),
            "中": (sub[by] > sub[by].quantile(0.33)) & (sub[by] <= sub[by].quantile(0.66)),
            "高": sub[by] > sub[by].quantile(0.66)}
    print(f"  {'档':<4}{'n':<5}{'胜率':<8}{'均值%':<9}{'均值区间':<22}{bool and '盈亏比'}")
    for k, mask in bins.items():
        s = sub[mask]
        if s.empty:
            continue
        n = len(s)
        wr = (s["收益%"] > 0).sum() / n * 100
        avg = s["收益%"].mean()
        win = s.loc[s["收益%"] > 0, "收益%"]
        los = s.loc[s["收益%"] <= 0, "收益%"]
        pl = (win.mean() / abs(los.mean())) if len(win) and len(los) and los.mean() != 0 else None
        rng = f"{s[by].min():.1f}~{s[by].max():.1f}"
        print(f"  {k:<4}{n:<5}{wr:>5.0f}%  {avg:>+7.2f}  {rng:<22}"
              f"{('%.2f' % pl) if pl else '—'}")


def main():
    env = market_env()
    feat = signal_features()
    sigs = {s["代码"] + s["信号日"]: s for s in warm_signals()}

    recs = []
    for _, r in feat.iterrows():
        rp = warm_replay(r["信号日"], r["代码"], name=r["名称"])
        if not rp:
            continue
        e = env.get(r["信号日"], {})
        recs.append({
            "信号日": r["信号日"], "代码": r["代码"], "名称": r["名称"],
            "收益%": round(rp["收益%"], 2), "结果": rp["结果"],
            "量比": r["量比"], "5日涨幅": r["5日涨幅"], "pos60": r["pos60"],
            "成交额亿": r["成交额亿"],
            "温度": e.get("温度"), "情绪分": e.get("情绪分"),
            "涨停家数": e.get("涨停家数"),
            "次日跳空%": gap_pct(r["代码"], r["信号日"]),
        })
    df = pd.DataFrame(recs)
    if df.empty:
        print("无有效样本")
        return

    print(f"样本 {len(df)} 条（09-21 ~ 10-23 全部初动信号，V2 规则本地 K 线重放）")
    n = len(df)
    wr = (df["收益%"] > 0).sum() / n * 100
    print(f"全样本：胜率 {wr:.0f}%  均值 {df['收益%'].mean():+.2f}%  "
          f"中位 {df['收益%'].median():+.2f}%")
    print(f"出场方式分布：{df['结果'].value_counts().to_dict()}")

    print("\n== 逐笔 ==")
    cols = ["信号日", "代码", "名称", "收益%", "结果", "次日跳空%", "温度", "情绪分", "量比", "pos60"]
    print(df[cols].to_string(index=False))

    summarize(df, "情绪分", "信号日情绪分")
    summarize(df, "温度", "信号日大盘温度")
    summarize(df, "涨停家数", "信号日涨停家数")
    summarize(df, "量比", "信号量比")
    summarize(df, "5日涨幅", "信号5日涨幅")
    summarize(df, "pos60", "信号60日位置")
    summarize(df, "成交额亿", "信号成交额")

    # 次日跳空与收益的相关性——"买在情绪高点"的直接检验
    sub = df.dropna(subset=["次日跳空%", "收益%"])
    if len(sub) >= 5:
        print("\n== 次日跳空 vs T+5 收益 ==")
        print(f"  相关系数 r = {sub['次日跳空%'].corr(sub['收益%']):.2f}（n={len(sub)}）")
        hi = sub[sub["次日跳空%"] >= 0]
        lo = sub[sub["次日跳空%"] < 0]
        for lbl, s in (("平/高开", hi), ("低开", lo)):
            if len(s):
                print(f"  {lbl}: n={len(s)} 胜率 {(s['收益%']>0).sum()/len(s)*100:.0f}% "
                      f"均值 {s['收益%'].mean():+.2f}%")

    # 情绪分极值样本
    hi_s = df[df["情绪分"] >= 80]
    lo_s = df[df["情绪分"] < 80]
    print("\n== 情绪分 ≥80（情绪高潮日）vs <80 ==")
    for lbl, s in (("≥80", hi_s), ("<80", lo_s)):
        if len(s):
            print(f"  {lbl}: n={len(s)} 胜率 {(s['收益%']>0).sum()/len(s)*100:.0f}% "
                  f"均值 {s['收益%'].mean():+.2f}%")

    out = BASE / "data" / "初动漂移归因_20261023.csv"
    df.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"\n明细已存：{out.relative_to(BASE)}")


if __name__ == "__main__":
    main()
