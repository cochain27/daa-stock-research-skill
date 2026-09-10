# -*- coding: utf-8 -*-
"""数据获取模块
主源：新浪（稳定）  补充：东财（涨停池/跌停池/龙虎榜等，已验证稳定）
统一列名，内置重试与降级。
"""
import os
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")  # 保护 akshare 百度源 numpy 线程
import socket
import time
import akshare as ak
import pandas as pd
import requests

# 全局 socket 超时：akshare/requests 多数调用未设 timeout，
# 遇不可达源会永久阻塞在 SSL_read（2026-09-07 午间复盘挂死 30min+）。
# 设为 30s 后超时会抛异常，交给 _retry 降级或快速失败，不再无限等待。
socket.setdefaulttimeout(60)

# requests 在 timeout=None 时会显式 settimeout(None) 覆盖全局默认值，
# 必须在 Session 层强制注入默认超时，否则依然无限阻塞。
# 30s 不够：新浪全A快照 70 页经本机代理平均 3.7s/页，偶发单页 >30s（2026-09-07 收盘复盘失败）。
_HTTP_TIMEOUT = 60

# 东财数字子域（82.push2 / 1.push2 …）会随机不可达（2026-09-07 收盘复盘因此失败），
# 主域 push2.eastmoney.com 始终可用：超时后自动降级到主域重试一次。
import re as _re
_EM_NUM_HOST = _re.compile(r"^(https?://)\d+\.push2\.eastmoney\.com")

# 强制直连开关：默认关代理。
# 原因：macOS 系统代理开启时 urllib/requests(trust_env=True) 会自动读取，
# 全局代理模式下出口 IP 落在境外，东财/新浪按境外 IP 限流或封禁
# （2026-09-08 收盘复盘东财 clist 持续 502）。国内行情源一律直连。
# 确需走代理时：export DAA_USE_PROXY=1
_USE_PROXY = os.environ.get("DAA_USE_PROXY", "").strip().lower() in ("1", "true", "yes")

if not getattr(requests.Session, "_daa_timeout_patched", False):
    _orig_session_request = requests.Session.request

    def _session_request(self, *args, **kwargs):
        kwargs.setdefault("timeout", _HTTP_TIMEOUT)
        if not _USE_PROXY:
            self.trust_env = False
            kwargs["proxies"] = kwargs.get("proxies") or {"http": None, "https": None}
        try:
            return _orig_session_request(self, *args, **kwargs)
        except Exception:
            url = args[1] if len(args) > 1 else kwargs.get("url", "")
            if not (isinstance(url, str) and _EM_NUM_HOST.match(url)):
                raise
            alt = _EM_NUM_HOST.sub(r"\1push2.eastmoney.com", url)
            if len(args) > 1:
                args = (args[0], alt) + tuple(args[2:])
            else:
                kwargs["url"] = alt
            return _orig_session_request(self, *args, **kwargs)

    requests.Session.request = _session_request
    requests.Session._daa_timeout_patched = True


def _retry(fn, tries=3, delay=2.0, name=""):
    last_err = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            last_err = e
            if i < tries - 1:
                time.sleep(delay)
    raise last_err


# ============ 指数（新浪主源，东财降级） ============

def get_index_daily(symbol="sh000001", days=250):
    """指数日K，返回含 date/open/high/low/close/volume/amount。
    symbol: sh000001上证 / sz399001深成 / sz399006创业板"""
    try:
        df = _retry(lambda: ak.stock_zh_index_daily(symbol=symbol), tries=2, name=f"新浪指数{symbol}")
    except Exception:
        df = _retry(lambda: ak.stock_zh_index_daily_em(symbol=symbol), tries=2, name=f"东财指数{symbol}")
        df = df.rename(columns={"date": "date"})
    df = df.tail(days).copy()
    df["date"] = pd.to_datetime(df["date"])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


# ============ 全A快照（新浪主源） ============

def get_market_snapshot():
    """全A实时快照，统一列：代码/名称/最新价/涨跌幅/成交额/换手率/市盈率-动态/市净率
    （新浪源无 PE/PB，置 None）"""
    try:
        df = _retry(lambda: ak.stock_zh_a_spot(), tries=3, name="新浪全A")
    except Exception:
        df = _retry(lambda: ak.stock_zh_a_spot_em(), tries=2, name="东财全A")
    if "代码" not in df.columns:  # 东财列名适配
        df = df.rename(columns={"序号": "序号"})
    # 新浪列：代码(sh600519)/名称/最新价/涨跌额/涨跌幅/... → 代码统一为纯6位数字
    if "代码" in df.columns:
        df["代码"] = (df["代码"].astype(str)
                      .str.replace(r"^(sh|sz|bj)", "", regex=True)
                      .str.replace(r"\.0$", "", regex=True))
    # 新浪列：代码/名称/最新价/涨跌额/涨跌幅/买入/卖出/昨收/今开/最高/最低/成交量/成交额/时间戳
    for col in ["换手率", "市盈率-动态", "市净率"]:
        if col not in df.columns:
            df[col] = None
    for col in ["最新价", "涨跌幅", "成交额", "换手率", "市盈率-动态", "市净率"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


# ============ 板块（新浪主源） ============

def get_industry_boards():
    """行业板块行情。返回 (df, source)。
    df统一列：板块名称/涨跌幅/总成交额/换手率/领涨股票/公司家数"""
    try:
        df = _retry(lambda: ak.stock_sector_spot(indicator="新浪行业"), tries=2, name="新浪行业")
        df = df.rename(columns={
            "板块": "板块名称", "涨跌幅": "涨跌幅", "总成交额": "总成交额",
            "股票名称": "领涨股票", "公司家数": "公司家数",
        })
        if "换手率" not in df.columns:
            df["换手率"] = None
        df["涨跌幅"] = pd.to_numeric(df["涨跌幅"], errors="coerce")
        return df, "sina"
    except Exception:
        df = _retry(lambda: ak.stock_board_industry_name_em(), tries=2, name="东财行业")
        return df, "em"


def get_concept_boards():
    """概念板块行情（东财，失败返回空）"""
    try:
        df = _retry(lambda: ak.stock_board_concept_name_em(), tries=2)
        return df, "em"
    except Exception:
        return pd.DataFrame(), "sina"


def get_board_cons(board_name, board_type="industry"):
    """板块成分股（东财）"""
    if board_type == "industry":
        return _retry(lambda: ak.stock_board_industry_cons_em(symbol=board_name), tries=2)
    return _retry(lambda: ak.stock_board_concept_cons_em(symbol=board_name), tries=2)


# ============ 个股（新浪主源） ============

def _sina_symbol(symbol):
    """代码转新浪前缀格式：600519→sh600519，000001→sz000001"""
    if symbol.startswith(("6", "9", "5")):
        return f"sh{symbol}"
    return f"sz{symbol}"


def get_stock_hist(symbol, days=120, adjust="qfq"):
    """个股日K（前复权），统一中文列：日期/开盘/收盘/最高/最低/成交量/成交额/换手率/涨跌幅"""
    end = pd.Timestamp.now().strftime("%Y%m%d")
    start = (pd.Timestamp.now() - pd.Timedelta(days=int(days * 1.7))).strftime("%Y%m%d")
    try:
        df = _retry(lambda: ak.stock_zh_a_daily(symbol=_sina_symbol(symbol),
                                                start_date=start, end_date=end, adjust=adjust),
                    tries=3, name=f"新浪K线{symbol}")
    except Exception:
        df = _retry(lambda: ak.stock_zh_a_hist(symbol=symbol, period="daily",
                                               start_date=start, end_date=end, adjust=adjust),
                    tries=2, name=f"东财K线{symbol}")
        df = df.rename(columns={"日期": "date", "开盘": "open", "收盘": "close",
                                "最高": "high", "最低": "low", "成交量": "volume",
                                "成交额": "amount", "换手率": "turnover"})
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["amount"] = pd.to_numeric(df.get("amount"), errors="coerce")
    df["turnover"] = pd.to_numeric(df.get("turnover"), errors="coerce")
    # 涨跌幅
    df["pct"] = df["close"].pct_change() * 100
    df = df.rename(columns={"date": "日期", "open": "开盘", "close": "收盘",
                            "high": "最高", "low": "最低", "volume": "成交量",
                            "amount": "成交额", "turnover": "换手率", "pct": "涨跌幅"})
    return df


def get_stock_info(symbol):
    """个股基本信息（东财）"""
    try:
        df = _retry(lambda: ak.stock_individual_info_em(symbol=symbol), tries=2, name=f"个股信息{symbol}")
        return dict(zip(df["item"], df["value"]))
    except Exception:
        return {}


# 东财三级行业名缓存：避免重复查 f127
_IND_EM_CACHE = {}


def get_industry_em(symbol):
    """个股所属东财三级行业名（f127，如'IT服务Ⅱ''半导体'）。
    绕代理直连 push2delay.eastmoney.com（2026-09-09 实测 push2 全站被 IP 级风控
    返回空，push2delay 延迟行情 host 可用且 f127 字段同构）。
    失败返回 None（调用方自行兜底）。进程内缓存。
    """
    symbol = str(symbol).split(".")[0]
    if not symbol:
        return None
    if symbol in _IND_EM_CACHE:
        return _IND_EM_CACHE[symbol]
    market = "1" if symbol.startswith("6") else "0"
    try:
        r = requests.get(
            f"https://push2delay.eastmoney.com/api/qt/stock/get?secid={market}.{symbol}&fields=f57,f58,f127",
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
            timeout=_HTTP_TIMEOUT,
        )
        d = r.json().get("data") or {}
        name = (d.get("f127") or "").strip() or None
        _IND_EM_CACHE[symbol] = name
        return name
    except Exception:
        _IND_EM_CACHE[symbol] = None
        return None


# ============ 估值补充源（百度，东财不稳时的兜底） ============

def get_valuation_baidu(symbol):
    """百度估值数据：PE(TTM)/PB 历史序列 → 当日值+历史分位。
    indicator 支持 {"总市值","市盈率(TTM)","市盈率(静)","市净率","市现率"}
    返回 {pe, pe_pct, pb, pb_pct}；失败返回 {}。
    用途：东财个股信息(PB/ROE)不可用时，作为估值分位兜底"""
    try:
        out = {}
        for ind, key in [("市盈率(TTM)", "pe"), ("市净率", "pb")]:
            try:
                df = _retry(lambda: ak.stock_zh_valuation_baidu(
                    symbol=symbol, indicator=ind, period="近一年"), tries=2, name=f"百度估值{symbol}")
            except Exception:
                df = pd.DataFrame()
            if df is None or df.empty or "value" not in df.columns:
                continue
            s = pd.to_numeric(df["value"], errors="coerce").dropna()
            if s.empty:
                continue
            cur = float(s.iloc[-1])
            if cur <= 0:
                continue
            pct = float((s <= cur).mean()) * 100  # 历史分位
            out[key] = round(cur, 2)
            out[f"{key}_pct"] = round(pct, 1)
        return out
    except Exception:
        return {}


def get_financial_indicators(symbol):
    """财务指标（百度源）：ROE/毛利率/净利同比 → dict；失败返回 {}。
    用途：value_screen 里 ROE/毛利率/净利增长 的可靠兜底源"""
    try:
        df = _retry(lambda: ak.stock_financial_analysis_indicator(symbol=symbol, start_year="2024"),
                    tries=2, name=f"财务指标{symbol}")
        if df is None or df.empty:
            return {}
        latest = df.iloc[0]
        out = {}
        # 净资产收益率(加权) / 销售毛利率 / 净利润同比增长率
        for kw, key in [("净资产收益率", "roe"), ("销售毛利率", "margin"), ("净利润增长率", "growth"),
                        ("净利润同比增长率", "growth")]:
            col = next((c for c in df.columns if kw in str(c)), None)
            if col:
                try:
                    v = float(latest[col])
                    out[key] = round(v, 1)
                except (TypeError, ValueError):
                    pass
        return out
    except Exception:
        return {}


def get_stock_news(symbol, limit=10):
    """个股新闻（东财）"""
    try:
        df = _retry(lambda: ak.stock_news_em(symbol=symbol), tries=1)
        return df.head(limit) if df is not None and not df.empty else None
    except Exception:
        return None


# ============ 实时行情（腾讯，轻量稳定，竞价阶段可用） ============

def get_realtime_quotes(symbols):
    """腾讯实时行情（完整盘口）。symbols为纯数字代码列表，返回 {代码: {...}}
    字段：名称/现价/涨跌幅/涨停价/跌停价/最高/最低/今开/昨收/量比/换手率/市盈率/流通市值/总市值/
          买一价/买一量(封单)/卖一价/卖一量/成交额(万)
    优点：毫秒级、9:25竞价后即有价格、含涨停/量比/PE 等选股关键字段"""
    if not symbols:
        return {}
    sym_map = {s: f"{'sh' if str(s).startswith(('6', '5')) else 'sz'}{s}" for s in symbols}
    q = ",".join(sym_map.values())
    out = {}
    try:
        r = requests.get(f"https://qt.gtimg.cn/q={q}", timeout=8)
        r.encoding = "gbk"
        for line in r.text.strip().split(";"):
            line = line.strip()
            if not line or "~" not in line:
                continue
            p = line.split("~")
            if len(p) < 50:
                continue
            code = str(p[2])
            try:
                price = float(p[3])
                chg = float(p[32])
            except (ValueError, IndexError):
                continue
            if price <= 0:
                continue

            def _f(i, d=None):
                try:
                    v = float(p[i])
                    return v if v != 0 or d is None else d
                except (ValueError, IndexError):
                    return d

            out[code] = {
                "名称": p[1], "现价": price, "涨跌幅": chg,
                "今开": _f(5), "昨收": _f(4), "最高": _f(33), "最低": _f(34),
                "买一价": _f(9), "买一量": _f(10),   # 买一量=封单(手)
                "卖一价": _f(19), "卖一量": _f(20),
                "换手率": _f(38), "市盈率": _f(39), "量比": _f(49),
                "流通市值": _f(44), "总市值": _f(45),
                "涨停价": _f(47), "跌停价": _f(48),
                "成交额万": _f(37),  # 单位:万元
            }
    except Exception:
        pass
    return out


# 涨停/停牌缓存：避免同一次运行反复请求
_ZT_CACHE = {"date": None, "df": None}


def get_zt_pool_cached():
    """涨停池（带进程内缓存），返回 DataFrame，失败返回空 DataFrame"""
    today = pd.Timestamp.now().strftime("%Y%m%d")
    if _ZT_CACHE["date"] == today and _ZT_CACHE["df"] is not None:
        return _ZT_CACHE["df"]
    df = get_stock_zt_pool(today)
    if df is None or df.empty:
        df = get_stock_zt_pool((pd.Timestamp.now() - pd.Timedelta(days=1)).strftime("%Y%m%d"))
    df = pd.DataFrame() if df is None else df
    _ZT_CACHE["date"] = today
    _ZT_CACHE["df"] = df
    return df


# 行业兜底关键词映射（东财接口不稳定时的最后防线）
_IND_KEYWORDS = {
    "传媒": ["传媒", "影视", "游戏", "出版", "广告", "广电", "文娱", "动漫", "芒果", "粤传媒"],
    "券商": ["证券", "券商", "中信", "国泰", "华泰", "招商证券", "东方财富", "同花顺"],
    "银行": ["银行", "工商", "建设", "农业银行", "中国银行", "招商银行", "宁波银行"],
    "保险": ["保险", "平安", "人寿", "太保", "新华保险"],
    "白酒": ["茅台", "五粮液", "泸州", "汾酒", "洋河", "古井", "酒鬼", "舍得", "金种子"],
    "食品饮料": ["食品", "饮料", "乳业", "伊利", "蒙牛", "海天", "安井", "双汇", "调味"],
    "医药": ["医药", "制药", "生物", "医疗", "药业", "疫苗", "中药", "康", "药"],
    "汽车": ["汽车", "整车", "比亚迪", "长安", "长城", "一汽", "东风", "广汽", "零部件"],
    "电子": ["电子", "半导体", "芯片", "集成电路", "光刻", "面板", "歌尔", "立讯", "京东方", "兆易"],
    "通信": ["通信", "中兴", "烽火", "光模块", "5G", "大唐"],
    "计算机": ["软件", "计算机", "科技", "信息", "数据", "云", "智能", "网安", "浪潮", "用友", "金山"],
    "军工": ["军工", "航天", "航空", "兵器", "船舶", "卫星", "无人机", "菲利华", "航发", "中航"],
    "有色金属": ["有色", "黄金", "铜", "铝", "锂", "稀土", "钨", "锌", "锡", "镍", "钴"],
    "钢铁": ["钢铁", "宝钢", "鞍钢", "太钢", "首钢", "河钢"],
    "煤炭": ["煤炭", "煤业", "焦煤", "动力煤", "中国神华", "陕西煤业"],
    "石油石化": ["石油", "石化", "油气", "页岩", "中国石油", "中国石化", "中海油"],
    "电力": ["电力", "发电", "国电", "华能", "华电", "大唐发电", "核电"],
    "新能源": ["新能源", "光伏", "风电", "储能", "电池", "宁德", "隆基", "通威", "阳光", "亿纬"],
    "化工": ["化工", "化学", "材料", "氟", "钛", "烧碱", "纯碱", "万华", "恒力", "巨石", "玻纤"],
    "农业": ["农业", "种业", "养殖", "牧业", "林业", "渔业", "亚盛", "北大荒", "生猪", "农产品", "农发"],
    "地产": ["地产", "房地产", "万科", "保利", "招商蛇口", "绿地", "华夏幸福"],
    "建筑": ["建筑", "工程", "建设", "中铁", "中国建筑", "交建", "电建"],
    "机械": ["机械", "设备", "机床", "重工", "三一", "徐工", "中联", "工程机械", "专用设备"],
    "家电": ["家电", "美的", "格力", "海尔", "海信", "TCL", "苏泊尔"],
    "纺织服装": ["纺织", "服装", "服饰", "家纺", "鞋"],
    "交运": ["航空", "机场", "港口", "航运", "物流", "铁路", "高速", "快递", "顺丰"],
    "环保": ["环保", "水务", "垃圾", "节能", "环境"],
    "教育": ["教育", "培训", "学大", "中公", "昂立"],
    "旅游": ["旅游", "酒店", "景区", "旅行社", "餐饮", "免税", "中国中免"],
}
_IND_KEYWORD_DEFAULT = "其他"


def _industry_by_name(name):
    """按名称关键词兜底推断行业（东财行业接口不可用时的最后防线）"""
    if not name:
        return _IND_KEYWORD_DEFAULT
    n = str(name)
    for ind, kws in _IND_KEYWORDS.items():
        if any(kw in n for kw in kws):
            return ind
    return _IND_KEYWORD_DEFAULT


def get_industry_map():
    """构建 代码→行业 映射。
    主源：涨停池所属行业（稳定）；辅源：个股信息接口（东财，逐个查）。
    返回 {代码: 行业}，查询失败或未知返回 None"""
    out = {}
    # 主源：涨停池（稳定，且覆盖当日所有涨停股）
    try:
        zt = get_zt_pool_cached()
        if zt is not None and not zt.empty and "所属行业" in zt.columns:
            for _, r in zt.iterrows():
                code = str(r.get("代码", "")).replace(r"\.0$", "", regex=True) if False else str(r.get("代码", "")).split(".")[0]
                if code and pd.notna(r.get("所属行业")):
                    out[code] = str(r["所属行业"]).strip()
    except Exception:
        pass
    return out


def get_industry_of(symbol, name=None):
    """单只股票行业：涨停池映射 → 个股信息接口 → 名称关键词兜底。
    任何来源失败都能给出兜底结论，保证调用方拿到字符串"""
    symbol = str(symbol)
    try:
        m = get_industry_map()
        if symbol in m and m[symbol]:
            return m[symbol]
    except Exception:
        pass
    # 个股信息接口（平时可用；今日东财不稳定时跳过）
    try:
        info = get_stock_info(symbol)
        if "行业" in info and info["行业"]:
            return str(info["行业"])
    except Exception:
        pass
    return _industry_by_name(name)


# ============ 资金/情绪（东财补充，不稳定） ============

def get_market_fund_flow():
    """大盘资金流向（不稳定，失败返回None）"""
    try:
        return _retry(lambda: ak.stock_market_fund_flow(), tries=2)
    except Exception:
        return None


def get_stock_fund_flow(symbol, market="sh"):
    """个股资金流（不稳定，失败返回None）"""
    try:
        return _retry(lambda: ak.stock_individual_fund_flow(stock=symbol, market=market), tries=2)
    except Exception:
        return None


def get_stock_zt_pool(date=None):
    """涨停板池（东财，稳定）"""
    try:
        return _retry(lambda: ak.stock_zt_pool_em(date=date or pd.Timestamp.now().strftime("%Y%m%d")),
                      tries=2, name="涨停池")
    except Exception:
        return None


def get_stock_dt_pool(date=None):
    """跌停板池（东财）"""
    try:
        return _retry(lambda: ak.stock_zt_pool_dtgc_em(date=date or pd.Timestamp.now().strftime("%Y%m%d")),
                      tries=2, name="跌停池")
    except Exception:
        return None


def get_stock_zb_pool(date=None):
    """炸板股池（东财：曾涨停后开板的票）——短线通道情绪判定用"""
    try:
        return _retry(lambda: ak.stock_zt_pool_zbgc_em(date=date or pd.Timestamp.now().strftime("%Y%m%d")),
                      tries=2, name="炸板池")
    except Exception:
        return None
