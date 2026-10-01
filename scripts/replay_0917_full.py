"""9/17 12:51 实跑精确复现 —— 对齐 1cc3e00(09-12) 版 pick_low_pos_entry 全链路。

复现要素（对齐点）：
  1) 票池 = 成交额前 top 名（daily_report/close_review 传 top=600）
  2) 判据 = 9/12 版八条（pos60<0.55 / 距高<=-25 / lb<=1.30 / lb5mean<=1.20
            / chg5∈[-10,5] / 候选日涨幅<=5 / amp20<30 / 额∈[2,15]亿）
  3) 双路径：标准蓄势失败 → 试「近期超卖」（前25日 pos60<0.5 或 dist60<-30）
             超卖路径 pos60 上限放宽到 0.65
  4) 行业去重 MAX_SAME_INDUSTRY=1
  5) 排序：pos60 升序 → 量比升序

数据：本地 K 线 data/klines/*.csv，严格切片 日期<=2026-09-17。
输出：与 9/17 watch_history_backup 落盘的 3 只（东财/宁德/江淮）对照。
"""
import sys, glob, os
import numpy as np
import pandas as pd

KL = "data/klines"
SCAN_DATE = pd.Timestamp("2026-09-17")
TARGET = "600418"

# ---- 9/12 版参数（config 1cc3e00） ----
MAX_POS60 = 0.55
MIN_DIST60 = -25.0
MAX_LB = 1.30
MAX_LB5MEAN = 1.20
CHG5 = (-10.0, 5.0)
MAX_CHG_TODAY = 5.0
MAX_AMP20 = 30.0
AMT = (2e8, 15e8)
# 超卖路径
OS_LOOKBACK = 25
OS_POS60_MAX = 0.65
OS_DIST_MAX = -30.0
MAX_SAME_INDUSTRY = 1


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
    d["涨跌幅"] = d["收盘"].pct_change() * 100
    return d


def load(sym):
    df = pd.read_csv(os.path.join(KL, f"{sym}.csv"))
    df = df.rename(columns={"date": "日期", "open": "开盘", "last": "收盘",
                            "high": "最高", "low": "最低", "volume": "成交量", "amount": "成交额"})
    df["日期"] = pd.to_datetime(df["日期"])
    df = df.sort_values("日期").reset_index(drop=True)
    for c in ("开盘", "收盘", "最高", "最低", "成交量", "成交额"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["收盘"])


def check_蓄势(row):
    if pd.isna(row.get("pos60")) or pd.isna(row.get("dist60")):
        return False, "指标不足"
    if row["pos60"] >= MAX_POS60:
        return False, f"位置{row['pos60']:.0%}>=55%"
    if row["dist60"] > MIN_DIST60:
        return False, f"距高{row['dist60']:.0f}%>-25%"
    lb, lbm = row.get("lb"), row.get("lb5mean")
    if pd.isna(lb) or pd.isna(lbm):
        return False, "量比不足"
    if lb > MAX_LB or lbm > MAX_LB5MEAN:
        return False, f"量比{lb:.2f}/{lbm:.2f}未缩量"
    c5 = row.get("chg5", 0)
    if not (CHG5[0] <= c5 <= CHG5[1]):
        return False, f"5日{c5:+.1f}%非横盘"
    ct = row.get("涨跌幅", 0) or 0
    if ct > MAX_CHG_TODAY:
        return False, f"候选日{ct:+.1f}%>5%"
    if row.get("amp20", 0) >= MAX_AMP20:
        return False, f"振幅{row['amp20']:.0f}%>=30%"
    amt = row.get("成交额", 0)
    if not (AMT[0] <= amt <= AMT[1]):
        return False, f"额{amt/1e8:.1f}亿出界"
    return True, "ok"


def recent_oversold(ind, cur_idx):
    start = max(0, cur_idx - OS_LOOKBACK)
    win = ind.iloc[start:cur_idx]
    for _, r in win.iterrows():
        p60 = r.get("pos60")
        d60 = r.get("dist60")
        if p60 is not None and not pd.isna(p60) and p60 < 0.5:
            return True, str(pd.Timestamp(r["日期"]).date()), float(p60)
        if d60 is not None and not pd.isna(d60) and d60 < OS_DIST_MAX:
            return True, str(pd.Timestamp(r["日期"]).date()), float(p60) if not pd.isna(p60) else None
    return False, None, None


def main(top=600):
    files = sorted(glob.glob(os.path.join(KL, "*.csv")))
    # 只保留 sh/sz 主板/中小/创业（对齐 ALLOW_CODE_PREFIX，粗略用 60/00/30）
    rows = []
    for p in files:
        sym = os.path.basename(p)[:-4]
        code = sym[2:]
        if not code.startswith(("60", "00", "30")):
            continue
        df = load(sym)
        df = df[df["日期"] <= SCAN_DATE]
        if len(df) < 70:
            continue
        last = df.iloc[-1]
        amt = float(last["成交额"]) if not pd.isna(last["成交额"]) else 0.0
        rows.append((code, sym, amt, df))
    # 成交额降序取前 top
    rows.sort(key=lambda x: -x[2])
    pool = rows[:top]
    print(f"[票池] 全A合格 {len(rows)} 只，取成交额前 {top} 只；"
          f"门槛={pool[-1][2]/1e8:.2f}亿；"
          f"江淮成交额={[r[2]/1e8 for r in rows if r[0]==TARGET][0]:.2f}亿 "
          f"排第 {[i for i,r in enumerate(rows,1) if r[0]==TARGET][0]}")

    picks = []
    for code, sym, amt, df in pool:
        ind = indicators(df)
        row = ind.iloc[-1]
        ok, why = check_蓄势(row)
        os_hit, os_date, os_p60 = (False, None, None)
        if not ok:
            os_hit, os_date, os_p60 = recent_oversold(ind, len(ind) - 1)
        if not ok and not os_hit:
            continue
        ct = float(row.get("涨跌幅") or 0)
        if ct > MAX_CHG_TODAY:
            continue
        pos60_max = MAX_POS60 if ok else OS_POS60_MAX
        if float(row["pos60"]) >= pos60_max:
            continue
        picks.append({
            "code": code, "sym": sym,
            "pos60": float(row["pos60"]), "dist60": float(row["dist60"]),
            "lb": float(row["lb"]), "lb5mean": float(row["lb5mean"]),
            "chg5": float(row["chg5"] or 0), "amp20": float(row["amp20"] or 0),
            "amt": float(row["成交额"] or 0) / 1e8,
            "path": "标准蓄势" if ok else "近期超卖",
            "os_date": os_date,
        })
    picks.sort(key=lambda x: (x["pos60"], x["lb"]))
    print(f"\n{'='*72}")
    print(f"[9/17 12:51 复现] top={top} 票池，通过 {len(picks)} 只")
    print(f"{'='*72}")
    n_std = sum(1 for p in picks if p["path"] == "标准蓄势")
    n_os = sum(1 for p in picks if p["path"] == "近期超卖")
    print(f"  标准蓄势 {n_std} 只 / 近期超卖 {n_os} 只")
    print(f"  排序前20：")
    for i, p in enumerate(picks[:20], 1):
        m = "  <<<< 江淮" if p["code"] == TARGET else ""
        print(f"   {i:>2}. {p['code']} {p['path']} pos{p['pos60']:.3f} 距高{p['dist60']:.0f}% "
              f"量比{p['lb']:.2f} 5日{p['chg5']:+.1f}% 振幅{p['amp20']:.0f}% 额{p['amt']:.1f}亿{m}")
    r = [i for i, p in enumerate(picks, 1) if p["code"] == TARGET]
    print(f"\n  ★ 江淮汽车(600418): {'排名 '+str(r[0]) if r else '未通过'}")
    # 标准蓄势名单
    std = [p for p in picks if p["path"] == "标准蓄势"]
    print(f"\n  标准蓄势全名单（{len(std)} 只，前30）：")
    for i, p in enumerate(std[:30], 1):
        m = "  <<<< 江淮" if p["code"] == TARGET else ""
        print(f"   {i:>2}. {p['code']} pos{p['pos60']:.3f} 距高{p['dist60']:.0f}% "
              f"量比{p['lb']:.2f} 5日{p['chg5']:+.1f}% 额{p['amt']:.1f}亿{m}")
    # 与落盘对照
    print(f"\n  落盘对照（watch_history_backup 9/17 新增 3 只）：")
    for c in ("300059", "300750", TARGET):
        hit = [p for p in picks if p["code"] == c]
        if hit:
            p = hit[0]
            print(f"   ✓ {c} 在通过名单，排名 {picks.index(p)+1}，路径={p['path']}")
        else:
            print(f"   ✗ {c} 不在通过名单")


if __name__ == "__main__":
    main(top=600)
