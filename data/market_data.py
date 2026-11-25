"""
市场数据接入层
==============
负责三类数据，每一层都带降级，保证任何环境都能跑：

  1. 板块轮动：本地MySQL(申万二级) → 同花顺 → 内置样例
  2. 板块成分股：本地MySQL → 东财直连 → akshare → 内置股票池
  3. A股K线：本地MySQL(前复权) → akshare → 内置模拟K线

本地 MySQL（wucai_trade）是首选数据源：500万+日线、130板块、5万+新闻，
全部本地化，不依赖外网，规避代理超时。密码走环境变量 WUCAI_SQL_PASSWORD。
mysql_source 不可用时自动降级到原有网络链路。

技术指标（MA/MACD/BOLL/RSI/KDJ）用 pandas 纯本地计算，不依赖 TA-Lib。

设计原则：
  - 真实数据优先，降级数据打标记（source 字段），前端明确展示
  - 全部结果缓存到 SQLite，避免每次刷新重复拉网络
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

# 加载 .env（与 arti_client 同一份），使 SECTOR_SOURCE 等开关可从文件配置
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
except Exception:
    pass

# 板块数据源开关：mysql=本地MySQL(申万二级)；ths=同花顺实时(akshare)
# MySQL 板块表更新不同步/开盘前空快照时，可切 ths 绕过
SECTOR_SOURCE = os.getenv("SECTOR_SOURCE", "mysql").strip().lower()

_CACHE_LOCK = threading.Lock()  # 并发写缓存保护


def probe_network(timeout: float = 6.0) -> bool:
    """启动时探测行情网络，失败则关闭网络开关，避免后续每个接口都重试等待。
    返回网络是否可用。"""
    import time as _time
    global _NET_OK, _NET_DISABLE_AT
    try:
        import requests
        # 用一个轻量东财接口探测（K线/成分股走的域名）
        requests.get(
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            params={"secid": "1.600519", "klt": "101", "fqt": "1",
                    "beg": "20261101", "end": "20261120", "fields1": "f1,f2,f3", "fields2": "f51,f53"},
            timeout=timeout,
        )
        _NET_OK = True
        return True
    except Exception:
        _NET_OK = False
        _NET_DISABLE_AT = _time.time()
        return False

# 内存级加速缓存：模拟K线 + 全局网络开关（避免每次重复尝试失败的网络请求）
_MEM_KLINE: dict[str, pd.DataFrame] = {}
_NET_OK = True  # 全局网络可用标志；一旦连续失败自动关闭，跳过所有网络尝试
_NET_DISABLE_AT = 0.0
_NET_RETRY_SEC = 300.0  # 网络关闭5分钟后重试一次

DB_PATH = Path(__file__).resolve().parent.parent / "market.db"

# 内置股票池（板块 -> 代表性成分股），东财/akshare 都失败时的最后兜底
# 每组是该板块的龙头/权重股，代码 + 名称
FALLBACK_POOL: dict[str, list[tuple[str, str]]] = {
    "半导体": [("600519", "贵州茅台"), ("002371", "北方华创"), ("688012", "中微公司"),
             ("603501", "韦尔股份"), ("002049", "紫光国微"), ("688111", "金山办公")],
    "银行": [("601398", "工商银行"), ("601939", "建设银行"), ("600036", "招商银行"),
            ("601288", "农业银行"), ("601988", "中国银行"), ("600000", "浦发银行")],
    "证券": [("600030", "中信证券"), ("300059", "东方财富"), ("601688", "华泰证券"),
            ("000776", "广发证券"), ("601211", "国泰君安"), ("600837", "海通证券")],
    "白酒": [("600519", "贵州茅台"), ("000858", "五粮液"), ("000568", "泸州老窖"),
            ("002304", "洋河股份"), ("600809", "山西汾酒"), ("000596", "古井贡酒")],
    "新能源汽车": [("300750", "宁德时代"), ("002594", "比亚迪"), ("300014", "亿纬锂能"),
               ("002460", "赣锋锂业"), ("603659", "璞泰来"), ("300124", "汇川技术")],
    "光伏设备": [("601012", "隆基绿能"), ("300274", "阳光电源"), ("002129", "TCL中环"),
              ("688599", "天合光能"), ("600438", "通威股份"), ("300118", "东方日升")],
    "医疗器械": [("300760", "迈瑞医疗"), ("300015", "爱尔眼科"), ("603259", "药明康德"),
              ("688180", "君实生物"), ("002007", "华兰生物"), ("300347", "泰格医药")],
    "军工电子": [("600893", "航发动力"), ("000768", "中航西飞"), ("600760", "中航沈飞"),
              ("002179", "中航光电"), ("600038", "中直股份"), ("300114", "中航电测")],
    "人工智能": [("002230", "科大讯飞"), ("300496", "中科创达"), ("688256", "寒武纪"),
              ("002415", "海康威视"), ("300454", "深信服"), ("688169", "石头科技")],
    "煤炭开采加工": [("601088", "中国神华"), ("601225", "陕西煤业"), ("600188", "兖矿能源"),
               ("601898", "中煤能源"), ("600985", "淮北矿业"), ("000937", "冀中能源")],
    "家用电器": [("000651", "格力电器"), ("600690", "海尔智家"), ("000333", "美的集团"),
              ("002508", "老板电器"), ("600839", "四川长虹"), ("002242", "九阳股份")],
    "房地产开发": [("000002", "万科A"), ("600048", "保利发展"), ("001979", "招商蛇口"),
               ("600383", "金地集团"), ("000001", "平安银行"), ("600606", "绿地控股")],
}


# -------------------- SQLite 缓存 --------------------

def _init_db() -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS cache (
                key TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                updated TEXT NOT NULL
            )
        """)


def _cache_get(key: str, max_age_hours: float = 6.0):
    _init_db()
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute("SELECT payload, updated FROM cache WHERE key=?", (key,)).fetchone()
    if not row:
        return None
    try:
        age = (datetime.now() - datetime.fromisoformat(row[1])).total_seconds() / 3600
        if age > max_age_hours:
            return None
        import json
        return json.loads(row[0])
    except Exception:
        return None


def _cache_set(key: str, payload) -> None:
    import json
    with _CACHE_LOCK:
        _init_db()
        with sqlite3.connect(DB_PATH) as con:
            con.execute(
                "INSERT OR REPLACE INTO cache(key,payload,updated) VALUES(?,?,?)",
                (key, json.dumps(payload, ensure_ascii=False, default=str), datetime.now().isoformat()),
            )


# -------------------- 1. 板块轮动 --------------------

def _try_eastmoney_sectors() -> list[dict]:
    """东财直连板块汇总（不走 akshare/代理），带真实主力净流入 f62。"""
    try:
        import requests
        p = {"pn": 1, "pz": 100, "po": 1, "np": 1,
             "ut": "bd1d9ddb04089700cf9c27f6f7426281", "fltt": 2, "invt": 2,
             "fid": "f3", "fs": "m:90 t:2",
             "fields": "f12,f14,f3,f6,f62,f104,f105,f128,f136"}
        diff = []
        for host in ("push2.eastmoney.com", "82.push2.eastmoney.com"):
            try:
                r = requests.get(f"https://{host}/api/qt/clist/get", params=p, timeout=8)
                diff = r.json().get("data", {}).get("diff", []) or []
                if diff:
                    break
            except Exception:
                continue
        out = []
        for d in diff:
            name = str(d.get("f14", ""))
            if not name:
                continue
            out.append({
                "name": name,
                "change_pct": _num(d.get("f3")),
                "net_inflow": round(_num(d.get("f62")) / 1e8, 2),  # 元→亿元，真实主力净流入
                "up_count": int(_num(d.get("f104"))),
                "down_count": int(_num(d.get("f105"))),
                "leader": str(d.get("f128", "")),
                "leader_change": _num(d.get("f136")),
                "turnover": round(_num(d.get("f6")) / 1e8, 2),  # 成交额 元→亿元
                "source": "东财直连(实时)",
            })
        return out
    except Exception:
        return []


def _sector_cache_hours() -> float:
    """板块轮动缓存时长：交易时段短(实时)、非交易时段长(省网络)。
    交易时段 9:30-11:30 / 13:00-15:00 用1分钟，其余用2小时。
    可用 SECTOR_CACHE_MINUTES 环境变量覆盖。"""
    try:
        override = os.getenv("SECTOR_CACHE_MINUTES")
        if override:
            return max(0.2, float(override)) / 60
    except Exception:
        pass
    now = datetime.now()
    t = now.hour * 60 + now.minute
    in_session = (9 * 60 + 30 <= t <= 11 * 60 + 30) or (13 * 60 <= t <= 15 * 60)
    return (1.0 / 60) if in_session else 2.0


def get_sector_rotation(use_cache: bool = True) -> list[dict]:
    """板块轮动数据：涨跌幅、资金净流入、上涨/下跌家数、领涨股。"""
    cache_key = f"sector_rotation:{SECTOR_SOURCE}"
    if use_cache:
        cached = _cache_get(cache_key, max_age_hours=_sector_cache_hours())
        if cached:
            return cached

    rows: list[dict] = []
    got_real = False
    # ① 本地 MySQL 优先（仅 mysql 模式）
    if SECTOR_SOURCE != "ths":
        try:
            from data import mysql_source as ms
            if ms.mysql_available():
                rows = ms.get_sector_rotation_mysql()
                got_real = bool(rows)
        except Exception as e:
            print(f"[数据] MySQL 板块轮动失败: {type(e).__name__}: {e}")
    # ② 同花顺（ths 模式首选 / mysql 模式降级）
    if not rows:
        try:
            import akshare as ak
            df = ak.stock_board_industry_summary_ths()
            got_real = len(df) > 0
            for _, r in df.iterrows():
                rows.append({
                    "name": str(r["板块"]),
                    "change_pct": _num(r.get("涨跌幅")),
                    "net_inflow": _num(r.get("净流入")),  # 亿元
                    "up_count": int(_num(r.get("上涨家数"))),
                    "down_count": int(_num(r.get("下跌家数"))),
                    "leader": str(r.get("领涨股", "")),
                    "leader_change": _num(r.get("领涨股-涨跌幅")),
                    "turnover": _num(r.get("总成交额")),
                    "source": "同花顺(实时)",
                })
        except Exception as e:
            print(f"[数据] 同花顺板块失败: {type(e).__name__}: {e}，尝试东财直连")
    # ③ 东财直连（同花顺失败时兜底，带真实主力净流入）
    if not rows:
        rows = _try_eastmoney_sectors()
        got_real = bool(rows)
        if rows:
            print(f"[数据] 板块轮动改用东财直连({len(rows)}个板块)")
    # ④ 旧缓存兜底：实时源全失败时，宁可用刚过期的真实数据，也不用内置假数据
    if not rows:
        stale = _cache_get(cache_key, max_age_hours=24.0)
        if stale:
            print("[数据] 板块轮动实时源失败，回退到最近一次真实缓存")
            rows = stale
    # ⑤ 内置样例（最终兜底，仅当连旧缓存都没有）
    if not rows:
        print("[数据] 板块轮动全部实时源失败且无缓存，使用内置样例")
        rows = _fallback_sectors()

    # 计算板块轮动综合评分：涨幅排名50% + 资金净流入排名50%
    if rows:
        _attach_momentum(rows)
    if rows and got_real:  # 只缓存真实数据，降级数据不缓存
        _cache_set(cache_key, rows)
    return rows


def _fallback_sectors() -> list[dict]:
    """内置板块样例（历史快照数据，明确标记）。"""
    import random
    rng = random.Random(20261120)
    base = [
        ("半导体", 2.35, 48.6), ("人工智能", 1.88, 35.2), ("证券", 1.42, 62.1),
        ("银行", 0.95, 28.4), ("白酒", 0.62, 12.7), ("新能源汽车", -0.35, -8.2),
        ("光伏设备", -1.12, -15.6), ("军工电子", 1.15, 9.8), ("医疗器械", 0.44, 4.3),
        ("煤炭开采加工", -0.78, -3.1), ("家用电器", 0.28, 5.6), ("房地产开发", -1.45, -22.3),
    ]
    rows = []
    for name, chg, inflow in base:
        up = max(1, int(rng.uniform(20, 80) * (1 if chg > 0 else 0.5)))
        down = max(1, int(100 - up))
        rows.append({
            "name": name, "change_pct": chg, "net_inflow": inflow,
            "up_count": up, "down_count": down, "leader": "", "leader_change": chg + 1.5,
            "turnover": rng.uniform(50, 300), "source": "内置样例(历史快照)",
        })
    return rows


def _attach_momentum(rows: list[dict]) -> None:
    """板块轮动评分 = 涨幅排名50% + 资金净流入排名50%（归一化到0-100）。

    rank_score 把全体板块按指标从高到低排名，第一名得100分、末名接近0分，
    两个排名等权合成，避免量纲差异（涨跌幅%/资金亿元）直接相加。
    """
    def rank_score(values: list[float], reverse=True) -> dict[int, float]:
        order = sorted(range(len(values)), key=lambda i: values[i], reverse=reverse)
        n = len(order)
        return {idx: (n - rank) / n * 100 for rank, idx in enumerate(order)}

    changes = [r["change_pct"] for r in rows]
    inflows = [r["net_inflow"] for r in rows]
    s_chg = rank_score(changes)
    s_flow = rank_score(inflows)
    for i, r in enumerate(rows):
        # breadth 仅作展示参考，不参与轮动评分
        r["breadth"] = round(r["up_count"] / max(1, r["up_count"] + r["down_count"]), 3)
        r["momentum_score"] = round(0.5 * s_chg[i] + 0.5 * s_flow[i], 1)


# -------------------- 2. 板块成分股 --------------------

def get_sector_constituents(sector: str, use_cache: bool = True) -> list[dict]:
    """板块成分股：东财直连 → akshare → 内置池。返回 [{code,name,price,change_pct}]"""
    import time as _time
    cache_key = f"cons:{SECTOR_SOURCE}:{sector}"
    if use_cache:
        cached = _cache_get(cache_key, max_age_hours=12.0)
        if cached:
            return cached

    # ① 本地 MySQL 优先（成分股用申万板块名，与同花顺板块名同源，匹配度高）
    #    即使轮动切到 ths/东财，成分股仍优先走本地 MySQL（稳定且名称对应）
    rows = []
    source = ""
    try:
        from data import mysql_source as ms
        if ms.mysql_available():
            rows = ms.get_sector_constituents_mysql(sector)
            source = "本地MySQL"
    except Exception:
        pass
    # ② 网络链路（网络关闭期内跳过）
    net_off = (not _NET_OK) and (_time.time() - _NET_DISABLE_AT) < _NET_RETRY_SEC
    if not rows and not net_off:
        rows = _try_eastmoney_cons(sector)
        source = "东财(实时)"
        if not rows:
            rows = _try_akshare_cons(sector)
            source = "akshare(实时)"
    got_real = bool(rows)
    if not rows:
        rows = _fallback_cons(sector)
        source = "内置股票池"

    for r in rows:
        r.setdefault("source", source)
    if rows and got_real:  # 只缓存真实数据
        _cache_set(cache_key, rows)
    return rows


def _try_eastmoney_cons(sector: str) -> list[dict]:
    """东财板块成分股直连（备用 host，规避被墙的17.push2）。"""
    try:
        import requests
        # 先按板块名找板块代码
        p = {"pn": 1, "pz": 100, "po": 1, "np": 1,
             "ut": "bd1d9ddb04089700cf9c27f6f7426281", "fltt": 2, "invt": 2,
             "fid": "f3", "fs": "m:90 t:2", "fields": "f12,f14"}
        for host in ("push2.eastmoney.com", "82.push2.eastmoney.com"):
            try:
                r = requests.get(f"https://{host}/api/qt/clist/get", params=p, timeout=6)
                diff = r.json().get("data", {}).get("diff", []) or []
                bk = next((d["f12"] for d in diff if d["f14"] == sector), None)
                if not bk:
                    continue
                p2 = dict(p, fs=f"b:{bk}", pz=60, fields="f12,f14,f2,f3")
                r2 = requests.get(f"https://{host}/api/qt/clist/get", params=p2, timeout=6)
                diff2 = r2.json().get("data", {}).get("diff", []) or []
                out = []
                for d in diff2:
                    if not d.get("f12"):
                        continue
                    out.append({
                        "code": str(d["f12"]), "name": str(d["f14"]),
                        "price": _num(d.get("f2")), "change_pct": _num(d.get("f3")),
                    })
                if out:
                    return out
            except Exception:
                continue
    except Exception:
        pass
    return []


def _try_akshare_cons(sector: str) -> list[dict]:
    try:
        import akshare as ak
        df = ak.stock_board_industry_cons_em(symbol=sector)
        col_code = "代码" if "代码" in df.columns else df.columns[0]
        col_name = "名称" if "名称" in df.columns else df.columns[1]
        out = []
        for _, r in df.head(60).iterrows():
            out.append({
                "code": str(r[col_code]), "name": str(r[col_name]),
                "price": _num(r.get("最新价")), "change_pct": _num(r.get("涨跌幅")),
            })
        return out
    except Exception:
        return []


def _fallback_cons(sector: str) -> list[dict]:
    pool = FALLBACK_POOL.get(sector)
    if not pool:
        # 未覆盖的板块，从所有池子里凑一组
        allp = [x for v in FALLBACK_POOL.values() for x in v]
        pool = allp[:6]
    return [{"code": c, "name": n, "price": 0.0, "change_pct": 0.0} for c, n in pool]


# -------------------- 3. K线 + 技术指标 --------------------

def get_kline(code: str, days: int = 120, use_cache: bool = True) -> pd.DataFrame:
    """A股K线（前复权）。失败时用确定性模拟K线。"""
    cache_key = f"kline:{code}:{days}"
    if use_cache:
        cached = _cache_get(cache_key, max_age_hours=6.0)
        if cached:
            return pd.DataFrame(cached)

    import time as _time
    global _NET_OK, _NET_DISABLE_AT
    now = _time.time()
    # 网络关闭期内，直接用内存缓存的模拟数据（不再尝试网络）
    net_off = (not _NET_OK) and (now - _NET_DISABLE_AT) < _NET_RETRY_SEC
    if net_off and code in _MEM_KLINE:
        return _MEM_KLINE[code].copy()

    df = None
    source = "本地MySQL"
    # ① 本地 MySQL 优先
    try:
        from data import mysql_source as ms
        if ms.mysql_available():
            df = ms.get_kline_mysql(code, days=days)
    except Exception as e:
        print(f"[数据] MySQL K线失败({code}): {type(e).__name__}: {e}")
        df = None

    # ② akshare 降级
    if (df is None or df.empty) and not net_off:
        source = "akshare(实时)"
        try:
            import akshare as ak
            end = datetime.now().strftime("%Y%m%d")
            start = (datetime.now() - timedelta(days=days * 2)).strftime("%Y%m%d")
            raw = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start,
                                     end_date=end, adjust="qfq")
            if raw is not None and len(raw) > 20:
                df = pd.DataFrame({
                    "date": raw["日期"].astype(str),
                    "open": pd.to_numeric(raw["开盘"]),
                    "close": pd.to_numeric(raw["收盘"]),
                    "high": pd.to_numeric(raw["最高"]),
                    "low": pd.to_numeric(raw["最低"]),
                    "volume": pd.to_numeric(raw["成交量"]),
                }).tail(days).reset_index(drop=True)
                _NET_OK = True  # 成功则恢复网络标志
        except Exception as e:
            print(f"[数据] {code} K线获取失败: {type(e).__name__}，使用模拟K线")
            _NET_OK = False
            _NET_DISABLE_AT = now

    if df is None or df.empty:
        source = "模拟K线"
        df = _mock_kline(code, days)
        with _CACHE_LOCK:
            _MEM_KLINE[code] = df.copy()  # 内存缓存，避免重复生成

    df["source"] = source
    if use_cache and source != "模拟K线":  # 只把真实数据写SQLite
        _cache_set(cache_key, df.to_dict("records"))
    return df


def _mock_kline(code: str, days: int) -> pd.DataFrame:
    """确定性模拟K线（按代码做种子，同一股票每次一致）。"""
    import numpy as np
    seed = sum(ord(c) for c in code)
    rng = np.random.RandomState(seed)
    base = 20 + (seed % 80)
    dates = pd.bdate_range(end=datetime.now(), periods=days)
    closes, highs, lows, opens = [], [], [], []
    price = base
    for _ in range(days):
        o = price * (1 + rng.normal(0, 0.008))
        c = o * (1 + rng.normal(0.0005, 0.018))
        h = max(o, c) * (1 + abs(rng.normal(0, 0.01)))
        l = min(o, c) * (1 - abs(rng.normal(0, 0.01)))
        opens.append(round(o, 2)); closes.append(round(c, 2))
        highs.append(round(h, 2)); lows.append(round(l, 2))
        price = c
    return pd.DataFrame({
        "date": [d.strftime("%Y-%m-%d") for d in dates],
        "open": opens, "close": closes, "high": highs, "low": lows,
        "volume": [int(rng.uniform(1e5, 1e6)) for _ in range(days)],
    })


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """计算 MA/MACD/BOLL/RSI/KDJ，返回带指标的 DataFrame。"""
    df = df.copy()
    c = df["close"]
    # MA
    for n in (5, 10, 20, 60):
        df[f"ma{n}"] = c.rolling(n, min_periods=1).mean().round(2)
    # MACD (12,26,9)
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    df["dif"] = (ema12 - ema26).round(3)
    df["dea"] = df["dif"].ewm(span=9, adjust=False).mean().round(3)
    df["macd"] = ((df["dif"] - df["dea"]) * 2).round(3)
    # BOLL(20,2)
    mid = c.rolling(20, min_periods=1).mean()
    std = c.rolling(20, min_periods=1).std()
    df["boll_mid"] = mid.round(2)
    df["boll_up"] = (mid + 2 * std).round(2)
    df["boll_low"] = (mid - 2 * std).round(2)
    # RSI(14)
    delta = c.diff()
    gain = delta.clip(lower=0).rolling(14, min_periods=1).mean()
    loss = (-delta.clip(upper=0)).rolling(14, min_periods=1).mean()
    df["rsi"] = (100 - 100 / (1 + gain / loss.replace(0, 1e-9))).round(2).fillna(50.0)
    # KDJ(9,3,3)
    low9 = df["low"].rolling(9, min_periods=1).min()
    high9 = df["high"].rolling(9, min_periods=1).max()
    rsv = (c - low9) / (high9 - low9).replace(0, 1e-9) * 100
    df["k"] = rsv.ewm(com=2, adjust=False).mean().round(2)
    df["d"] = df["k"].ewm(com=2, adjust=False).mean().round(2)
    df["j"] = (3 * df["k"] - 2 * df["d"]).round(2)
    return df


def _num(v) -> float:
    try:
        if v is None or v == "-" or v != v:
            return 0.0
        return float(v)
    except (ValueError, TypeError):
        return 0.0
