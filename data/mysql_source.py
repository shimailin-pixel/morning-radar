"""
本地 MySQL 行情数据源（wucai_trade）
====================================
优先数据源：本地库已有 500万+ 日线、130 个申万二级板块、5万+ 新闻，
完全本地化，不依赖外网，规避代理超时与数据造假。

连接信息：
  host/port/user/database 走环境变量（有默认值），密码读 WUCAI_SQL_PASSWORD。
  绝不把密码写进代码或仓库。

代码格式约定（内部统一纯 6 位，如 600519）：
  to_mysql_code('600519')  -> '600519.SH'
  from_mysql_code('600519.SH') -> '600519'

对外函数：
  mysql_available()                     连接是否可用（带降级开关）
  get_latest_trade_date()               库内最新交易日
  get_kline_mysql(code, days)           -> DataFrame[date,open,high,low,close,volume]
  get_sector_rotation_mysql()           -> list[dict]（字段对齐 get_sector_rotation）
  get_sector_constituents_mysql(name)   -> list[dict]（code,name,price,change_pct）
  get_stock_names(codes)                -> {code: name}
  get_news(codes, since_days, limit)    -> list[dict]
  get_financial(code, limit)            -> list[dict]
"""

from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta

import pandas as pd

# ---------- 连接配置 ----------
_HOST = os.getenv("WUCAI_SQL_HOST", "127.0.0.1")
_PORT = int(os.getenv("WUCAI_SQL_PORT", "3306"))
_USER = os.getenv("WUCAI_SQL_USER", "root")
_DB = os.getenv("WUCAI_SQL_DATABASE", "wucai_trade")
_PWD_ENV = "WUCAI_SQL_PASSWORD"

# 降级开关：连续失败后暂停尝试，避免每个请求都等超时
_OK = True
_DISABLE_AT = 0.0
_RETRY_SEC = 300.0


def _connect():
    import pymysql
    pwd = os.getenv(_PWD_ENV, "")
    if not pwd:
        raise RuntimeError(f"环境变量 {_PWD_ENV} 未设置")
    return pymysql.connect(
        host=_HOST, port=_PORT, user=_USER, password=pwd, database=_DB,
        charset="utf8mb4", connect_timeout=5, read_timeout=20,
    )


def _query(sql: str, params=None) -> list[tuple]:
    global _OK, _DISABLE_AT
    if not _OK and (time.time() - _DISABLE_AT) < _RETRY_SEC:
        return []
    conn = None
    try:
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        _OK = True
        return rows
    except Exception as e:
        _OK = False
        _DISABLE_AT = time.time()
        print(f"[MySQL] 查询失败，暂停{int(_RETRY_SEC)}秒重试: {type(e).__name__}: {e}")
        return []
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def mysql_available() -> bool:
    """探测本地库是否可用。"""
    global _OK, _DISABLE_AT
    if not os.getenv(_PWD_ENV):
        return False
    rows = _query("SELECT 1")
    if rows:
        _OK = True
        return True
    return False


# ---------- 代码格式转换 ----------
def to_mysql_code(code: str) -> str:
    """600519 -> 600519.SH；已有后缀则原样返回。"""
    c = str(code).strip().upper()
    if "." in c:
        return c
    c = c.zfill(6)
    if c.startswith(("6", "9")):
        return c + ".SH"
    if c.startswith(("0", "3")):
        return c + ".SZ"
    if c.startswith(("4", "8")):
        return c + ".BJ"
    return c + ".SH"


def from_mysql_code(code: str) -> str:
    """600519.SH -> 600519。"""
    return str(code).strip().split(".")[0].zfill(6)


# ---------- 交易日 ----------
def get_latest_trade_date() -> str:
    rows = _query("SELECT MAX(trade_date) FROM trade_stock_daily")
    if rows and rows[0][0]:
        d = rows[0][0]
        return d.strftime("%Y-%m-%d") if isinstance(d, (date, datetime)) else str(d)
    return ""


# ---------- 日线 ----------
def get_kline_mysql(code: str, days: int = 120) -> pd.DataFrame:
    """从本地库取前复权日线，返回标准 OHLCV DataFrame。"""
    mc = to_mysql_code(code)
    sql = """
        SELECT trade_date, open_price, high_price, low_price, close_price, volume
        FROM trade_stock_daily
        WHERE stock_code = %s
        ORDER BY trade_date DESC LIMIT %s
    """
    rows = _query(sql, (mc, days))
    if len(rows) < 20:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
    df = df.sort_values("date").reset_index(drop=True)
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    for col in ("open", "high", "low", "close"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
    df["source"] = "本地MySQL"
    return df


# ---------- 板块轮动 ----------
def get_sector_rotation_mysql() -> list[dict]:
    """
    取最新交易日的全部申万二级板块，字段对齐 get_sector_rotation()。
    net_inflow 用"成交额相对前5日均值的增量(亿元)"作资金关注度代理，
    非真实资金流，仅用于排序，source 字段已标注。

    注意：板块表可能比日线表滞后（更新不同步），因此这里用板块表自身的
    MAX(trade_date)，而不是日线表的最新日期，避免查空。
    """
    # 板块表自身的最新日期（不能用日线表的，两表更新可能不同步）
    date_rows = _query("SELECT MAX(trade_date) FROM trade_sector_daily")
    if not date_rows or not date_rows[0][0]:
        return []
    d = date_rows[0][0]
    latest = d.strftime("%Y-%m-%d") if isinstance(d, (date, datetime)) else str(d)

    # 最新一日板块快照
    sql = """
        SELECT sector_name, trade_date, change_pct, stock_count, rise_count, fall_count,
               limit_up, limit_down, top_stock, top_stock_name, top_stock_pct,
               total_amount, avg_turnover
        FROM trade_sector_daily
        WHERE trade_date = %s
    """
    rows = _query(sql, (latest,))
    if not rows:
        return []

    # 前5日成交额（用于资金关注度代理）
    hist = _query("""
        SELECT sector_name, AVG(total_amount) amt
        FROM trade_sector_daily
        WHERE trade_date BETWEEN %s AND %s
        GROUP BY sector_name
    """, ((datetime.strptime(latest, "%Y-%m-%d") - timedelta(days=12)).strftime("%Y-%m-%d"), latest))
    avg_map = {r[0]: float(r[1] or 0) for r in hist}

    out = []
    for r in rows:
        (name, d, chg, cnt, up, down, lu, ld, top, top_name, top_pct, amount, avg_to) = r
        amt_yi = float(amount or 0) / 1e8
        amt_avg_yi = avg_map.get(name, amt_yi) / 1e8
        # 资金关注度代理：当日成交额相对近5日均值的变化（亿元）
        flow_proxy = round(amt_yi - amt_avg_yi, 2) if amt_avg_yi > 0 else 0.0
        out.append({
            "name": str(name),
            "change_pct": float(chg or 0),
            "net_inflow": flow_proxy,
            "up_count": int(up or 0),
            "down_count": int(down or 0),
            "leader": from_mysql_code(top_name or top or ""),
            "leader_change": float(top_pct or 0),
            "turnover": round(amt_yi, 1),
            "stock_count": int(cnt or 0),
            "limit_up": int(lu or 0),
            "limit_down": int(ld or 0),
            "trade_date": latest,
            "source": "本地MySQL(申万二级)",
        })
    return out


# ---------- 板块成分股 ----------
def get_sector_constituents_mysql(sector: str, limit: int = 60) -> list[dict]:
    """按申万二级板块名取成分股，并补最新价与涨跌幅。"""
    rows = _query("""
        SELECT stock_code, stock_name
        FROM trade_stock_status
        WHERE sector_2 = %s OR sector_1 = %s
        LIMIT %s
    """, (sector, sector, limit))
    if not rows:
        return []

    codes = [to_mysql_code(r[0]) for r in rows]
    name_map = {from_mysql_code(r[0]): (r[1] or from_mysql_code(r[0])) for r in rows}

    # 批量取最新交易日的价格与涨跌幅（用窗口函数取每股最后一天）
    latest = get_latest_trade_date()
    ph = ",".join(["%s"] * len(codes))
    price_rows = _query(f"""
        SELECT stock_code, close_price,
               ROUND((close_price / MAX(prev_close) - 1) * 100, 2) AS chg
        FROM (
            SELECT stock_code, trade_date, close_price,
                   LAG(close_price) OVER (PARTITION BY stock_code ORDER BY trade_date) prev_close
            FROM trade_stock_daily
            WHERE stock_code IN ({ph}) AND trade_date <= %s
        ) t
        WHERE prev_close IS NOT NULL
          AND trade_date = (SELECT MAX(trade_date) FROM trade_stock_daily WHERE stock_code = t.stock_code AND trade_date <= %s)
        GROUP BY stock_code, close_price
    """, (*codes, latest, latest))

    price_map = {}
    for r in price_rows:
        c = from_mysql_code(r[0])
        try:
            price_map[c] = (float(r[1]), float(r[2] or 0))
        except (ValueError, TypeError):
            pass

    out = []
    for c in name_map:
        price, chg = price_map.get(c, (0.0, 0.0))
        out.append({
            "code": c,
            "name": name_map[c],
            "price": round(price, 2),
            "change_pct": chg,
            "source": "本地MySQL",
        })
    out.sort(key=lambda x: -x["change_pct"])
    return out


# ---------- 股票名称 ----------
def get_stock_names(codes: list[str]) -> dict[str, str]:
    if not codes:
        return {}
    mcs = [to_mysql_code(c) for c in codes]
    ph = ",".join(["%s"] * len(mcs))
    rows = _query(f"""
        SELECT stock_code, stock_name FROM trade_stock_status
        WHERE stock_code IN ({ph})
    """, tuple(mcs))
    return {from_mysql_code(r[0]): (r[1] or from_mysql_code(r[0])) for r in rows}


# ---------- 新闻 ----------
def get_news(codes: list[str], since_days: int = 7, limit: int = 30) -> list[dict]:
    """取自选股相关新闻（本地库，带情绪分）。"""
    if not codes:
        return []
    mcs = [to_mysql_code(c) for c in codes]
    ph = ",".join(["%s"] * len(mcs))
    since = (datetime.now() - timedelta(days=since_days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = _query(f"""
        SELECT stock_code, title, summary, source, published_at, sentiment, sentiment_score, is_important
        FROM trade_stock_news
        WHERE stock_code IN ({ph}) AND published_at >= %s
        ORDER BY published_at DESC LIMIT %s
    """, (*mcs, since, limit))
    out = []
    for r in rows:
        out.append({
            "code": from_mysql_code(r[0]),
            "title": r[1] or "",
            "summary": (r[2] or "")[:200],
            "source": r[3] or "",
            "published_at": r[4].strftime("%Y-%m-%d %H:%M") if isinstance(r[4], (date, datetime)) else str(r[4]),
            "sentiment": r[5] or "neutral",
            "sentiment_score": float(r[6] or 0),
            "important": bool(r[7]),
        })
    return out


# ---------- 财务 ----------
def get_financial(code: str, limit: int = 8) -> list[dict]:
    rows = _query("""
        SELECT report_date, revenue, net_profit, eps, roe, gross_margin,
               net_margin, debt_ratio, operating_cashflow
        FROM trade_stock_financial
        WHERE stock_code = %s
        ORDER BY report_date DESC LIMIT %s
    """, (to_mysql_code(code), limit))
    out = []
    for r in rows:
        d = r[0].strftime("%Y-%m-%d") if isinstance(r[0], (date, datetime)) else str(r[0])
        out.append({
            "report_date": d,
            "revenue": float(r[1] or 0), "net_profit": float(r[2] or 0),
            "eps": float(r[3] or 0), "roe": float(r[4] or 0),
            "gross_margin": float(r[5] or 0), "net_margin": float(r[6] or 0),
            "debt_ratio": float(r[7] or 0), "operating_cashflow": float(r[8] or 0),
        })
    return out


# ---------- 研报一致预期 ----------
def get_consensus(code: str, limit: int = 6) -> list[dict]:
    """研报一致预期/机构评级（本地库）。"""
    rows = _query("""
        SELECT broker, report_date, rating, target_price,
               eps_forecast_current, eps_forecast_next, revenue_forecast
        FROM trade_report_consensus
        WHERE stock_code = %s
        ORDER BY report_date DESC LIMIT %s
    """, (to_mysql_code(code), limit))
    out = []
    for r in rows:
        d = r[1].strftime("%Y-%m-%d") if isinstance(r[1], (date, datetime)) else str(r[1])
        out.append({
            "broker": str(r[0] or ""), "report_date": d, "rating": str(r[2] or ""),
            "target_price": float(r[3] or 0),
            "eps_current": float(r[4] or 0), "eps_next": float(r[5] or 0),
            "revenue_forecast": float(r[6] or 0),
        })
    return out
