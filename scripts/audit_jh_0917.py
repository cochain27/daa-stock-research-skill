"""9/17 实跑口径逐条审计 —— 还原「标准蓄势通过到底几只」。

背景：9/17 实跑 watch_history 显示当日入池 3 只（东财/宁德=近期超卖，江淮=标准蓄势），
      标准蓄势仅江淮 1 只。而此前我用自己的简化脚本回放 9/17 得到「132 只通过」，
      两者差两个数量级 → 必须查清差异来源。

方法：用 git 1cc3e00（09-12 保存，=9/17 实跑时点最接近的口径）的判据逐条还原：
      MAX_POS60=0.55 / MIN_DIST60=-25 / MAX_LB5=1.30 / LB5_MEAN=1.20
      / CHG5=(-10,5) / CHG_TODAY<=5 / AMP20<30 / AMOUNT=2-15亿
      无 MA20 上行、无 MA20 乖离、无 topN 截断。

对照组：当前口径（SCAN_POS60_MAX=0.40 / MAX_AMOUNT=15亿 / AMP20<25 / LB5<=2.0
        / BIAS20>=-5 / 无 MA20 硬过滤但软排序）。
"""
import sys, glob, os
import numpy as np
import pandas as pd

sys.path.insert(0, "scripts")
KL = "data/klines"
TARGET = "600418"
SCAN_DATE = pd.Timestamp("2026-09-17")


def indicators(df):
    d = df.copy()
    for w in (5, 10, 20):
        d[f"MA{w}"] = d["收盘"].rolling(w).mean()
    d["v5"] = d["成交量"].shift(1).rolling(5).mean()
    d["lb"] = d["成交量"] / d["v5"]
    d["lb5mean"] = d["lb"].rolling(5).mean()
    d["hi60"] = d["最高"].rolling(60, min_periods=40).max()
    d["lo60"] = d["最低"].rolling(60, min_periods=40).min()
    d["pos60"] = (d["收盘"] - d["lo60"]) / (d["hi60"] - d["lo60"])
    d["dist60"] = (d["收盘"] / d["hi60"] - 1) * 100
    d["amp20"] = (d["收盘"].rolling(20).max() - d["收盘"].rolling(20).min()) / d["收盘"].rolling(20).mean() * 100
    d["chg5"] = (d["收盘"] / d["收盘"].shift(5) - 1) * 100
    d["bias20"] = (d["收盘"] / d["MA20"] - 1) * 100
    d["ma20up"] = d["MA20"] > d["MA20"].shift(5)
    return d


def load(sym):
    df = pd.read_csv(os.path.join(KL, f"{sym}.csv"))
    df = df.rename(columns={"date": "日期", "open": "开盘", "last": "收盘",
                            "high": "最高", "low": "最低", "volume": "成交量", "amount": "成交额"})
    df["日期"] = pd.to_datetime(df["日期"])
    df = df.sort_values("日期").reset_index(drop=True)
    df["涨跌幅"] = df["收盘"].pct_change() * 100
    for c in ("开盘", "收盘", "最高", "最低", "成交量", "成交额"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["收盘"])


def check_0912(row):
    """9/12 (1cc3e00) 口径逐条判据。返回 (ok, why)。"""
    if pd.isna(row.get("pos60")) or pd.isna(row.get("dist60")):
        return False, "指标不足"
    if row["pos60"] >= 0.55:
        return False, f"位置{row['pos60']:.0%}>=55%"
    if row["dist60"] > -25:
        return False, f"距高{row['dist60']:.0f}%>-25%"
    lb, lbm = row.get("lb"), row.get("lb5mean")
    if pd.isna(lb) or pd.isna(lbm):
        return False, "量比不足"
    if lb > 1.30 or lbm > 1.20:
        return False, f"量比{lb:.2f}/{lbm:.2f}未缩量"
    c5 = row.get("chg5", 0)
    if not (-10.0 <= c5 <= 5.0):
        return False, f"5日{c5:+.1f}%非横盘"
    ct = row.get("涨跌幅", 0) or 0
    if ct > 5.0:
        return False, f"候选日{ct:+.1f}%>5%"
    if row.get("amp20", 0) >= 30:
        return False, f"振幅{row['amp20']:.0f}%>=30%"
    amt = row.get("成交额", 0)
    if not (2e8 <= amt <= 15e8):
        return False, f"额{amt/1e8:.1f}亿出界"
    return True, "ok"


def check_now(row):
    """当前生产口径逐条。"""
    if pd.isna(row.get("pos60")) or pd.isna(row.get("dist60")):
        return False, "指标不足"
    if row["pos60"] >= 0.40:
        return False, f"位置{row['pos60']:.0%}>=40%"
    if row["dist60"] > -25:
        return False, f"距高{row['dist60']:.0f}%>-25%"
    if pd.isna(row.get("bias20")) or row["bias20"] < -5:
        return False, f"乖离{row.get('bias20', 0):.1f}%<-5%"
    lb, lbm = row.get("lb"), row.get("lb5mean")
    if pd.isna(lb) or pd.isna(lbm):
        return False, "量比不足"
    if lb > 2.0 or lbm > 1.20:
        return False, f"量比{lb:.2f}/{lbm:.2f}未过"
    c5 = row.get("chg5", 0)
    if not (-10.0 <= c5 <= 5.0):
        return False, f"5日{c5:+.1f}%非横盘"
    ct = row.get("涨跌幅", 0) or 0
    if ct > 5.0:
        return False, f"候选日{ct:+.1f}%>5%"
    if row.get("amp20", 0) >= 25:
        return False, f"振幅{row['amp20']:.0f}%>=25%"
    amt = row.get("成交额", 0)
    if not (2e8 <= amt <= 15e8):
        return False, f"额{amt/1e8:.1f}亿出界"
    return True, "ok"


def check_mid(row):
    """09-23 收紧口径（无 MA20 上行、无 topN，但有 POS60<0.40 + 乖离≥-5% + 额≤6亿）。
    用于逼近 9/17 实跑时点的中间态。"""
    if pd.isna(row.get("pos60")) or pd.isna(row.get("dist60")):
        return False, "指标不足"
    if row["pos60"] >= 0.40:
        return False, f"位置{row['pos60']:.0%}>=40%"
    if row["dist60"] > -25:
        return False, f"距高{row['dist60']:.0f}%>-25%"
    if pd.isna(row.get("bias20")) or row["bias20"] < -5:
        return False, f"乖离{row.get('bias20', 0):.1f}%<-5%"
    lb, lbm = row.get("lb"), row.get("lb5mean")
    if pd.isna(lb) or pd.isna(lbm):
        return False, "量比不足"
    if lb > 2.0 or lbm > 1.20:
        return False, f"量比{lb:.2f}/{lbm:.2f}未过"
    c5 = row.get("chg5", 0)
    if not (-10.0 <= c5 <= 5.0):
        return False, f"5日{c5:+.1f}%非横盘"
    ct = row.get("涨跌幅", 0) or 0
    if ct > 5.0:
        return False, f"候选日{ct:+.1f}%>5%"
    if row.get("amp20", 0) >= 25:
        return False, f"振幅{row['amp20']:.0f}%>=25%"
    amt = row.get("成交额", 0)
    if not (2e8 <= amt <= 6e8):
        return False, f"额{amt/1e8:.1f}亿出界"
    return True, "ok"


def run(checker, label, top=None):
    hits, fails = [], {}
    total = 0
    files = sorted(glob.glob(os.path.join(KL, "*.csv")))
    for fi, p in enumerate(files, 1):
        sym = os.path.basename(p)[:-4]
        code = sym[2:]
        if not code.startswith(("60", "00", "30")):
            continue
        df = load(sym)
        df = df[df["日期"] <= SCAN_DATE]
        if len(df) < 70:
            continue
        total += 1
        if fi % 400 == 0:
            print(f"  ...{fi}/{len(files)} 已扫 {total}", flush=True)
        ind = indicators(df)
        row = ind.iloc[-1]
        ok, why = checker(row)
        if ok:
            hits.append({"code": code, "pos60": float(row["pos60"]), "dist60": float(row["dist60"]),
                         "lb": float(row["lb"]), "lb5mean": float(row["lb5mean"]),
                         "amp20": float(row["amp20"]), "chg5": float(row["chg5"]),
                         "amt": float(row["成交额"]) / 1e8, "bias20": float(row["bias20"]),
                         "up": bool(row["ma20up"])})
        else:
            key = why.split(">")[0].split("<")[0][:6]
            fails[key] = fails.get(key, 0) + 1
    hits.sort(key=lambda x: (not x["up"], x["pos60"], x["lb"]))
    print(f"\n{'='*70}")
    print(f"【{label}】样本 {total} 只，通过 {len(hits)} 只")
    print(f"{'='*70}")
    print("否决原因分布（Top8）:")
    for k, v in sorted(fails.items(), key=lambda x: -x[1])[:8]:
        print(f"    {k:<10} {v:>5} 只")
    print("通过名单（前15）:")
    for i, h in enumerate(hits[:15], 1):
        m = "   <<<< 江淮" if h["code"] == TARGET else ""
        print(f"  {i:>2}. {h['code']} pos{h['pos60']:.3f} 距高{h['dist60']:.0f}% 量比{h['lb']:.2f} "
              f"5均{h['lb5mean']:.2f} 振幅{h['amp20']:.0f}% 额{h['amt']:.1f}亿 up={h['up']}{m}")
    r = [i for i, h in enumerate(hits, 1) if h["code"] == TARGET]
    print(f"  → 江淮汽车排名: {r[0] if r else '未通过'}")
    return hits


if __name__ == "__main__":
    run(check_0912, "9/12 口径（MAX_POS60=0.55 / 额2-15亿 / 无MA20 / 无截断）")
    run(check_mid, "09-23 收紧口径（POS60<0.40 + 乖离≥-5% + 额≤6亿）")
    run(check_now, "当前生产口径（POS60<0.40 + 乖离≥-5% + 额≤15亿 + MA20软排序）")
