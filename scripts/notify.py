# -*- coding: utf-8 -*-
"""远程推送模块：PushPlus / Server酱 → 微信
配置在 data/push_config.json；未配置时所有推送静默跳过（不阻断流程）。
长文用手机端定制HTML版面（小字号/色块分区/紧凑表格）。
"""
import json
import re
from pathlib import Path
import requests

CONFIG_PATH = Path(__file__).resolve().parent.parent / "data" / "push_config.json"

# 手机端样式 v5-DM：雅灰蓝骨架 + 宣纸朱砂阅读区（红涨绿跌）+ 夜间模式自适应
_CSS = """<style>
:root{color-scheme:light dark;}
body{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;font-size:12px;color:#4a3f33;line-height:1.5;margin:0;padding:4px;background:#fdfbf6;}
.rt-title{font-size:13.5px;font-weight:700;color:#fff;background:#2c3e50;padding:7px 10px;border-radius:6px;margin:6px 0;letter-spacing:.5px;}
.rt-sec{font-size:12px;font-weight:700;color:#2c3e50;border-left:3px solid #c9a063;padding-left:6px;margin:10px 0 4px;letter-spacing:.3px;}
.rt-sub{font-size:12px;font-weight:700;color:#5d6d7e;margin:6px 0 2px;}
table{border-collapse:collapse;width:100%;font-size:10.5px;margin:3px 0;background:#fffdf8;}
th{background:#f5ecdd;color:#712b13;padding:3px 4px;border:1px solid #ead9c2;text-align:left;white-space:nowrap;font-weight:600;}
td{padding:3px 4px;border:1px solid #f2e8d8;vertical-align:top;}
tr:nth-child(even){background:#fbf7ee;}
.rt-quote{color:#9099a2;font-size:10.5px;border-left:2px solid #e5d5b8;padding-left:7px;margin:5px 0;}
.rt-item{margin:1px 0 1px 2px;}
.up{color:#d64541;font-weight:600;}
.dn{color:#1e9e6a;font-weight:600;}
b{color:#3a2e20;}
hr{border:none;border-top:1px dashed #e5d5b8;margin:6px 0;}
.rt-card{border:1px solid #ead9c2;border-radius:8px;margin:6px 0;overflow:hidden;background:#fffdf8;}
.rt-card-h{background:#faf6ef;padding:5px 9px;font-weight:700;font-size:12.5px;color:#712b13;border-bottom:1px solid #ead9c2;position:relative;}
.rt-badge{float:right;background:#993c1d;color:#fff;border-radius:9px;padding:1px 7px;font-size:10.5px;font-weight:600;}
.rt-card-b{padding:5px 9px;font-size:11px;line-height:1.55;}
.rt-grid{width:100%;font-size:11px;margin:1px 0;}
.rt-grid td{border:none;padding:1px 3px;background:transparent !important;}
.rt-lab{color:#b09b7a;font-size:10px;white-space:nowrap;}
.rt-warn{background:#fdf6e5;color:#9a6d00;padding:3px 8px;font-size:10.5px;border-top:1px solid #f5e6bd;}
.rt-cv{background:#f7f1e6;color:#8a7a5c;padding:3px 8px;font-size:10.5px;border-top:1px solid #ead9c2;}
.rt-hero{background:#2c3e50;border-radius:8px;color:#fff;padding:10px 12px;margin:6px 0;}
.rt-hero-t{font-size:11px;color:#aeb9c5;}
.rt-hero-v{font-size:24px;font-weight:700;line-height:1.2;}
.rt-hero-s{font-size:11px;color:#d5dbdb;margin-top:2px;}
.rt-hero-bar{height:4px;background:#455a6e;border-radius:2px;margin-top:6px;overflow:hidden;}
.rt-hero-fill{height:4px;border-radius:2px;}
.rt-hold{border:1px solid #ead9c2;border-radius:8px;margin:5px 0;background:#fffdf8;padding:6px 9px;font-size:11px;}
.rt-hold-n{font-weight:700;font-size:12px;color:#712b13;}
.rt-pill{display:inline-block;background:#f5ecdd;color:#8a6d4a;border-radius:8px;padding:0 6px;font-size:10px;margin:1px 2px 1px 0;}
.rt-code{font-weight:400;color:#a93226;}
.rt-tech{color:#6f6a62;font-size:11.5px;margin-top:2px;}
.rt-adv{color:#55636b;}
/* ---- 夜间模式（系统深色时自动切换：深底浅字，红涨绿跌提亮） ---- */
@media (prefers-color-scheme:dark){
body{background:#1a1a1c;color:#d6d0c4;}
b{color:#efe9dc;}
.rt-title{background:#39485c;}
.rt-sec{color:#cdd6e0;}
.rt-sub{color:#a8b4c0;}
table{background:#232326;}
th{background:#2c2c30;color:#e6c79c;border-color:#3c3c40;}
td{border-color:#3c3c40;}
tr:nth-child(even){background:#26262a;}
.rt-quote{color:#8f96a0;border-left-color:#3c3c40;}
.rt-card{background:#232326;border-color:#3c3c40;}
.rt-card-h{background:#2a2a2e;color:#e6c79c;border-bottom-color:#3c3c40;}
.rt-lab{color:#9a938a;}
.rt-warn{background:#2e2712;color:#e8c063;border-top-color:#463a18;}
.rt-cv{background:#26262a;color:#b8b0a0;border-top-color:#3c3c40;}
.rt-hold{background:#232326;border-color:#3c3c40;}
.rt-hold-n{color:#e6c79c;}
.rt-pill{background:#2c2c30;color:#cbb996;}
.rt-code{color:#e8837a;}
.rt-tech{color:#a8a29a;}
.rt-adv{color:#b0bcc4;}
.up{color:#ff7066;}
.dn{color:#4ade80;}
hr{border-top-color:#3c3c40;}
}
</style>"""


def _hero_card(md_text):
    """从md提取市场温度/仓位 → 头图卡（取不到返回空串）"""
    m = re.search(r"\*\*市场温度：([\d.]+)/100\*\*", md_text)
    p = re.search(r"\*\*仓位建议：(.+?)\*\*", md_text)
    g = re.search(r"\*\*操作指引：(.+?)\*\*", md_text)
    if not m:
        return ""
    temp = float(m.group(1))
    color = "#d64541" if temp >= 55 else ("#c9a063" if temp >= 35 else "#1e9e6a")
    pos = p.group(1) if p else "-"
    guide = g.group(1) if g else ""
    return (f'<div class="rt-hero"><div class="rt-hero-t">市场温度</div>'
            f'<div class="rt-hero-v">{temp:.0f}<span style="font-size:12px;color:#aeb9c5">/100</span>'
            f'<span style="float:right;font-size:11px;margin-top:10px;background:{color};color:#fff;padding:2px 10px;border-radius:10px">{pos}</span></div>'
            f'<div class="rt-hero-bar"><div class="rt-hero-fill" style="width:{temp:.0f}%;background:{color}"></div></div>'
            f'<div class="rt-hero-s">{guide}</div></div>')


def _render_hold_card(cells):
    """持仓行 → 体检卡。晨报6列/午收盘4列两种格式"""
    if len(cells) >= 6:
        name, price, pnl, pct, advice = cells[0], cells[1], cells[3], cells[4], cells[5]
    elif len(cells) >= 4:
        name, price, pnl, pct, advice = cells[0], cells[1], "-", cells[2], cells[3]
    else:
        return None
    try:
        v = float(re.sub(r"[^\d.+-]", "", pct))
        cls = "up" if v >= 0 else "dn"
        pct_txt = f"{v:+.2f}%"
    except ValueError:
        cls, pct_txt = "", pct
    advice_short = re.sub(r"\*\*", "", advice)
    pnl_html = f'　<span class="rt-lab">浮动盈亏</span> {pnl}' if pnl != "-" else ""
    return (f'<div class="rt-hold"><table class="rt-grid"><tr>'
            f'<td class="rt-hold-n">{name}</td>'
            f'<td style="text-align:right"><span class="{cls}" style="font-size:12.5px">{pct_txt}</span></td></tr>'
            f'<tr><td colspan="2"><span class="rt-lab">现价</span> {price}{pnl_html}</td></tr>'
            f'<tr><td colspan="2" class="rt-adv">{advice_short}</td></tr></table></div>')


def _parse_pick_block(block_lines):
    """解析推荐股markdown块为结构化dict（容错：缺字段也能渲染）"""
    d = {"cond": {}}
    first = block_lines[0]
    m = re.match(r"###\s*(\d+)\.\s*(.+?)（(\d{6})）", first)
    if not m:
        return None
    d["rank"], raw_name, d["code"] = m.group(1), m.group(2).strip(), m.group(3)
    # 剥离策略标签（⚡短线 / 📈趋势 / 📈波段），避免卡片里显示脏名称
    d["name"] = re.sub(r"\s*(⚡短线|📈趋势|📈波段)\s*[\d分/]*$", "", raw_name).strip()
    for ln in block_lines[1:]:
        ln = ln.strip().lstrip("- ").strip()
        if ln.startswith("现价："):
            # 2026-09-04 修复：原三连严格正则因日报新增"行业｜🟡纯波段"字段而失配，
            # 评分解析失败导致微信卡片不显示分数。改为独立正则分别解析价格/涨跌/评分。
            m_price = re.search(r"现价：([\d.]+)", ln)
            m_chg = re.search(r"当日涨跌：([\d.+-]+)%", ln)
            m_score = re.search(r"\*\*评分：([\d.]+)/100\*\*", ln)
            if m_price:
                d["price"] = m_price.group(1)
            if m_chg:
                d["chg"] = m_chg.group(1)
            if m_score:
                d["score"] = m_score.group(1)
        elif ln.startswith("价值面："):
            d["dims"] = " / ".join(x.strip() for x in ln.split("｜"))
        elif ln.startswith("技术说明："):
            d["tech"] = ln.replace("技术说明：", "")
        elif ln.startswith("🔁"):
            d["cv"] = ln
        elif ln.startswith("⚠️"):
            d["warn"] = re.sub(r"\*\*", "", ln)
        elif ln.startswith("突破买入："):
            d["cond"]["突破"] = ln.split("：", 1)[1]
        elif ln.startswith("回踩买入："):
            d["cond"]["回踩"] = ln.split("：", 1)[1]
        elif ln.startswith("建议买价区间："):
            d["cond"]["买区"] = ln.split("：", 1)[1]
        elif ln.startswith("止损价："):
            d["cond"]["止损"] = ln.split("：", 1)[1]
        elif ln.startswith("止盈1："):
            d["cond"]["止盈1"] = ln.split("：", 1)[1]
        elif ln.startswith("止盈2："):
            d["cond"]["止盈2"] = ln.split("：", 1)[1]
    return d


def _render_pick_card(d):
    """推荐股卡片HTML"""
    chg = d.get("chg", "-")
    try:
        _c = float(str(chg).replace("+", ""))
        chg = f"{_c:+.2f}" if "+" not in str(chg) and "-" not in str(chg) else str(chg)
        chg_cls = "up" if _c >= 0 else "dn"
    except ValueError:
        chg_cls = "up"
    h = [f'<div class="rt-card"><div class="rt-card-h">']
    if d.get("score"):
        h.append(f'<span class="rt-badge">{d["score"]}分</span>')
    h.append(f'{d["rank"]}. {d["name"]} <span class="rt-code">{d["code"]}</span></div>')
    h.append('<div class="rt-card-b"><table class="rt-grid">')
    h.append(f'<tr><td class="rt-lab">现价</td><td><b>{d.get("price","-")}</b></td>'
             f'<td class="rt-lab">涨跌</td><td><span class="{chg_cls}">{chg}%</span></td></tr>')
    if d.get("dims"):
        h.append(f'<tr><td class="rt-lab">四维</td><td colspan="3">{d["dims"]}</td></tr>')
    c = d.get("cond", {})
    if c:
        h.append(f'<tr><td class="rt-lab">买区</td><td><b>{c.get("买区","-")}</b></td>'
                 f'<td class="rt-lab">突破</td><td>{c.get("突破","-")}</td></tr>')
        h.append(f'<tr><td class="rt-lab">止损</td><td><span class="dn">{c.get("止损","-").split("（")[0]}</span></td>'
                 f'<td class="rt-lab">止盈</td><td><span class="up">{c.get("止盈1","-").split("（")[0]} / {c.get("止盈2","-").split("（")[0]}</span></td></tr>')
    h.append('</table>')
    if d.get("tech"):
        h.append(f'<div class="rt-tech">{d["tech"]}</div>')
    h.append('</div>')
    if d.get("cv"):
        h.append(f'<div class="rt-cv">{d["cv"]}</div>')
    if d.get("warn"):
        h.append(f'<div class="rt-warn">{d["warn"]}</div>')
    h.append('</div>')
    return "".join(h)


def _inline(text):
    """行内元素：加粗、涨跌着色"""
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(\+\d[\d.,]*%?)", r'<span class="up">\1</span>', text)
    text = re.sub(r"(?<![\w.%])(-\d[\d.,]*%?)(?![\d.])", r'<span class="dn">\1</span>', text)
    return text


def md_to_mobile_html(md):
    """把我们日报的markdown转成手机端紧凑HTML（针对自有格式定制，非通用转换器）"""
    out = [_CSS, "<div>"]
    hero_done = False
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i].rstrip()
        # 表格块
        if ln.startswith("|") and i + 1 < len(lines) and set(lines[i + 1].replace("|", "").replace(" ", "")) <= set("-:"):
            headers = [c.strip() for c in ln.strip("|").split("|")]
            i += 2
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            # 持仓表 → 体检卡
            if headers and headers[0] == "持仓":
                for r in rows:
                    card = _render_hold_card(r)
                    if card:
                        out.append(card)
                    else:
                        out.append(f'<div class="rt-item">{_inline(" | ".join(r))}</div>')
                continue
            html = ["<table><tr>"] + [f"<th>{_inline(h)}</th>" for h in headers] + ["</tr>"]
            for r in rows:
                html.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>")
            html.append("</table>")
            out.append("".join(html))
            continue
        if ln.startswith("# "):
            out.append(f'<div class="rt-title">{_inline(ln[2:])}</div>')
            if not hero_done:
                hero = _hero_card(md)
                if hero:
                    out.append(hero)
                    hero_done = True
        elif re.match(r"###\s*\d+\.\s*.+?（\d{6}）", ln):
            # 推荐股块 → 卡片（收集到下一个###/##为止）
            block = [ln]
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(("### ", "## ", "# ")):
                block.append(lines[i].rstrip())
                i += 1
            d = _parse_pick_block(block)
            if d:
                out.append(_render_pick_card(d))
            else:
                for bl in block:
                    if bl.strip():
                        out.append(f'<div class="rt-item">{_inline(bl)}</div>')
            continue
        elif ln.startswith("### "):
            out.append(f'<div class="rt-sub">{_inline(ln[4:])}</div>')
        elif ln.startswith("## "):
            out.append(f'<div class="rt-sec">{_inline(ln[3:])}</div>')
        elif ln.startswith("> "):
            out.append(f'<div class="rt-quote">{_inline(ln[2:])}</div>')
        elif re.match(r"^\s*[-*] ", ln):
            out.append(f'<div class="rt-item">· {_inline(ln.strip().lstrip("-* ").strip())}</div>')
        elif re.match(r"^\s*\d+\. ", ln):
            out.append(f'<div class="rt-item">{_inline(ln.strip())}</div>')
        elif ln.strip() == "---" or ln.strip() == "":
            out.append("<hr/>" if ln.strip() == "---" else "")
        else:
            out.append(f'<div class="rt-item">{_inline(ln)}</div>')
        i += 1
    out.append("</div>")
    return "".join(out)


def _config():
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def push_wechat(title, content, channel=None, html=False):
    """推送微信消息。title 标题，content 正文；html=True 时按HTML模板发送。
    返回 (ok, 渠道, 信息)。未配置或失败都不抛异常。"""
    cfg = _config()
    if not cfg.get("enabled"):
        return False, None, "推送未启用(未配置)"
    ch = channel or cfg.get("default_channel", "pushplus")

    if ch == "pushplus" and cfg.get("pushplus_token"):
        try:
            r = requests.post("https://www.pushplus.plus/send", json={
                "token": cfg["pushplus_token"],
                "title": title[:100],
                "content": content,
                "template": "html" if html else "markdown",
            }, timeout=10)
            ok = r.json().get("code") == 200
            return ok, "pushplus", "成功" if ok else r.text[:100]
        except Exception as e:
            return False, "pushplus", str(e)[:100]

    if ch == "serverchan" and cfg.get("serverchan_key"):
        try:
            key = cfg["serverchan_key"]
            r = requests.post(f"https://sctapi.ftqq.com/{key}.send", data={
                "title": title[:32],
                "desp": content,
            }, timeout=10)
            ok = r.json().get("code") == 0
            return ok, "serverchan", "成功" if ok else r.text[:100]
        except Exception as e:
            return False, "serverchan", str(e)[:100]

    return False, ch, "该渠道未配置token"


def push_report(title, md_text, max_len=3500):
    """推送日报类长文：转手机端紧凑HTML（超长截断，完整版看E盘文件）"""
    body = md_text if len(md_text) <= max_len else md_text[:max_len] + "\n\n> …（完整版见E盘推送文件）"
    return push_wechat(title, md_to_mobile_html(body), html=True)


def push_alert(title, text):
    """推送告警：纯文本转简单HTML，保持清爽"""
    body = _CSS + "<div>" + "".join(
        f'<div class="rt-item">{_inline(ln)}</div>' for ln in text.splitlines() if ln.strip()
    ) + "</div>"
    return push_wechat(title, body, html=True)


def test_push():
    """配置自检"""
    ok, ch, msg = push_wechat("大发推送测试", "如果你在微信看到这条消息，说明推送通道已打通。")
    print(f"渠道={ch} 结果={'OK' if ok else 'FAIL'} 信息={msg}")
    return ok


if __name__ == "__main__":
    test_push()
