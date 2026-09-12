# -*- coding: utf-8 -*-
"""
低位启动观察池 → T+N 回溯脚本
对 watch_history.csv 中所有"观察中"的票，拉取入池后K线，
计算 T+1/T+3/T+5 实际收益，更新状态字段。
"""
import csv
from pathlib import Path
import subprocess

BASE = Path(__file__).resolve().parent.parent
HIST_CSV = BASE / "data" / "watch_history.csv"
OUT_CSV  = BASE / "data" / "watch_history_enriched.csv"

# WestockData CLI wrapper
def wcmd(args, timeout=30):
    result = subprocess.run(
        ["npx", "-y", "westock-data-clawhub@1.0.4"] + args,
        capture_output=True, text=True, timeout=timeout,
        cwd=str(BASE)
    )
    return result.stdout

def fetch_kline(code, limit=120):
    """获取近 limit 日 K 线（前复权），返回 [(date, close), ...] 升序列表"""
    raw = wcmd(["kline", code, "--period", "day", "--limit", str(limit), "--fq", "qfq"])
    rows = []
    for line in raw.strip().splitlines():
        if not line.startswith("|"):
            continue
        parts = [v.strip() for v in line.split("|")[1:-1]]
        # 字段序: date | open | last(收盘) | high | low | volume | amount | exchange
        if len(parts) >= 3 and parts[0] and parts[0][0].isdigit():
            try:
                date  = parts[0]
                close = float(parts[2])  # last = close
                rows.append((date, close))
            except (ValueError, IndexError):
                pass
    # WestockData 返回降序(新→旧)，反转回升序
    rows.reverse()
    return rows

def next_trading_date(entry_date_str, kline_rows):
    """entry_date 之后的第一个交易日"""
    for date, _ in kline_rows:
        if date > entry_date_str:
            return date
    return None

def get_nth_close(kline_rows, after_date, n):
    """after_date 之后第 n 个交易日的收盘价"""
    cnt = 0
    for date, close in kline_rows:
        if date > after_date:
            cnt += 1
            if cnt == n:
                return close
    return None

def get_nth_date(kline_rows, after_date, n):
    """after_date 之后第 n 个交易日的日期"""
    cnt = 0
    for date, _ in kline_rows:
        if date > after_date:
            cnt += 1
            if cnt == n:
                return date
    return None

def to_float(s, default=None):
    try:
        return float(s)
    except (TypeError, ValueError):
        return default

def fmt_code(code):
    """将纯代码转为 westock-data 格式（sz/sh/bj）"""
    code = code.strip()
    # 已有前缀
    if code.startswith(("sz", "sh", "bj")):
        return code
    # 根据代码号段判断
    if code.startswith(("6", "9", "5")):
        return "sh" + code
    if code.startswith(("0", "1", "2", "3")):
        return "sz" + code
    return code

def main():
    with HIST_CSV.open(encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fields = list(reader.fieldnames)
        records = list(reader)

    extra_cols = ["T+1收益%", "T+3收益%", "T+5收益%", "最大涨幅%",
                  "是否触发止损", "实际卖点", "T+1日期", "T+3日期", "T+5日期"]
    new_fields = fields + [c for c in extra_cols if c not in fields]

    print(f"观察池共 {len(records)} 条，开始回溯...\n")

    updated, skipped = 0, 0

    for i, rec in enumerate(records, 1):
        status = rec.get("状态", "").strip()
        if status and status not in ("观察中",):
            skipped += 1
            for col in extra_cols:
                rec.setdefault(col, "")
            continue

        code = rec.get("代码", "").strip()
        name = rec.get("名称", "").strip()
        entry_date = rec.get("日期", "").strip()
        entry_price = to_float(rec.get("现价", "0"), default=0)

        if not code or not entry_date:
            skipped += 1
            for col in extra_cols:
                rec.setdefault(col, "数据缺失")
            continue

        raw_code = fmt_code(code)
        klines = fetch_kline(raw_code, limit=120)

        if len(klines) < 5:
            for col in extra_cols:
                rec.setdefault(col, "K线不足")
            print(f"  [{i}] {code} {name} K线不足({len(klines)}条)")
            skipped += 1
            continue

        # T+N 收盘价
        t1_date  = next_trading_date(entry_date, klines)
        t1_close = get_nth_close(klines, entry_date, 1)
        t3_close = get_nth_close(klines, entry_date, 3)
        t5_close = get_nth_close(klines, entry_date, 5)
        t3_date  = get_nth_date(klines, entry_date, 3)
        t5_date  = get_nth_date(klines, entry_date, 5)

        def ret(close):
            if close and entry_price > 0:
                return round((close / entry_price - 1) * 100, 2)
            return None

        r1, r3, r5 = ret(t1_close), ret(t3_close), ret(t5_close)

        # 最大涨幅 & 止损检测（入池后 10 个交易日）
        sl_price = to_float(rec.get("止损价", "0"), default=0)
        max_close_val, max_close_date = 0.0, ""
        hit_sl = False
        seen_t1 = False
        for date, close in klines:
            if date == t1_date:
                seen_t1 = True
            if seen_t1:
                if close > max_close_val:
                    max_close_val, max_close_date = close, date
                if sl_price > 0 and close <= sl_price:
                    hit_sl = True

        max_r = (round((max_close_val / entry_price - 1) * 100, 2)
                  if max_close_val > 0 and entry_price > 0 else None)

        # 状态更新
        new_status, reason = status, ""
        if r5 is not None:
            if   r5 >= 15:  new_status, reason = "✅ 达标(15%+)", f"T+5达{r5:+.1f}%"
            elif r5 <= -8:  new_status, reason = "❌ 止损",        f"T+5亏{r5:+.1f}%"
            elif r5 >= 5:   new_status, reason = "📈 有效信号",    f"T+5赚{r5:+.1f}%"
            elif hit_sl:    new_status, reason = "⚠️ 触发止损",    f"跌破{sl_price}"

        def sf(v): return f"{v:+.2f}" if v is not None else ""
        rec["T+1收益%"]   = sf(r1)
        rec["T+3收益%"]   = sf(r3)
        rec["T+5收益%"]   = sf(r5)
        rec["最大涨幅%"]   = sf(max_r)
        rec["是否触发止损"] = "是" if hit_sl else "否"
        rec["实际卖点"]   = f"{max_close_date}@{max_close_val:.2f}" if max_close_date else ""
        rec["T+1日期"]   = t1_date or ""
        rec["T+3日期"]   = t3_date or ""
        rec["T+5日期"]   = t5_date or ""
        rec["状态"]       = new_status
        if reason:
            rec["关注逻辑"] = rec.get("关注逻辑", "") + f" | {reason}"

        updated += 1
        r1s = f"{r1:+.1f}%" if r1 is not None else "N/A"
        r3s = f"{r3:+.1f}%" if r3 is not None else "N/A"
        r5s = f"{r5:+.1f}%" if r5 is not None else "N/A"
        maxs = f"{max_r:+.1f}%" if max_r is not None else "N/A"
        print(f"  [{i}] {code} {name:8s} 入:{entry_date}  "
              f"T+1={r1s} T+3={r3s} T+5={r5s} | 最大{maxs} | {new_status} {reason}")

    # 写回 CSV
    with OUT_CSV.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=new_fields)
        writer.writeheader()
        writer.writerows(records)

    print(f"\n{'='*70}")
    print(f"完成：{updated} 条已更新 / {skipped} 条跳过（已处理/已观察）")
    print(f"输出 → {OUT_CSV}")

    # 统计
    valid = [r for r in records
             if r.get("T+5收益%") not in ("", None, "K线不足", "数据缺失")
             and r["T+5收益%"].strip() not in ("", "N/A")]
    if valid:
        import numpy as np
        from collections import Counter
        print(f"\n=== 统计（{len(valid)} 只有效样本）===")
        for label, col in [("T+1", "T+1收益%"), ("T+3", "T+3收益%"), ("T+5", "T+5收益%")]:
            vals = np.array([float(r[col]) for r in valid if r[col].strip()
                             and r[col] not in ("K线不足", "数据缺失", "")])
            if len(vals):
                win = (vals > 0).mean() * 100
                print(f"  {label}：均值{vals.mean():+.2f}% 中位{np.median(vals):+.2f}% "
                      f"胜率{win:.0f}%(n={len(vals)}) "
                      f"最差{vals.min():+.1f}% 最好{vals.max():+.1f}%")
        sc = Counter(r.get("状态", "") for r in valid)
        print(f"\n  状态分布：{dict(sc)}")
        sl_cnt = sum(1 for r in valid if r.get("是否触发止损") == "是")
        print(f"  触发止损：{sl_cnt}/{len(valid)} ({sl_cnt/len(valid)*100:.0f}%)")

if __name__ == "__main__":
    main()
