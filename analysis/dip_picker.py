# -*- coding: utf-8 -*-
"""低吸观察池：低关注 + 超跌 + 缩量企稳(转涨启动) 候选股筛选。

定位：观察工具，非买入信号。回测结论见模块底部 BACKTEST_NOTE。
数据源：本地 MySQL trade_stock_daily + trade_stock_status。
每只股票用自己的最新有效成交日(volume>0)算信号，规避当日占位行。
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from data import mysql_source

DB_PATH = Path(__file__).resolve().parent.parent / "dip_cache.db"
CACHE_MINUTES = 30

# 申万一级行业 → 同花顺细分板块名列表（用于聚合"真资金净流入"，来源：同花顺实时 f62）
# 一只候选股按其申万一级行业，映射到若干同花顺板块，资金求和作为"所属板块真资金"。
SW_TO_THS = {
    "非银金融": ["保险", "证券", "多元金融"],
    "家用电器": ["白色家电", "黑色家电", "小家电", "厨卫电器"],
    "电子": ["半导体", "消费电子", "光学光电子", "元件", "电子化学品", "其他电子"],
    "计算机": ["软件开发", "IT服务", "计算机设备"],
    "通信": ["通信设备", "通信服务"],
    "传媒": ["文化传媒", "游戏", "影视院线"],
    "国防军工": ["军工装备", "军工电子"],
    "机械设备": ["自动化设备", "通用设备", "专用设备", "工程机械", "轨交设备"],
    "电力设备": ["电池", "光伏设备", "风电设备", "电网设备", "电机", "其他电源设备"],
    "汽车": ["汽车整车", "汽车零部件", "汽车服务及其他"],
    "医药生物": ["化学制药", "中药", "生物制品", "医药商业", "医疗器械", "医疗服务"],
    "食品饮料": ["白酒", "饮料制造", "食品加工制造"],
    "农林牧渔": ["种植业与林业", "养殖业", "农产品加工", "农化制品"],
    "基础化工": ["化学原料", "化学制品", "化学纤维", "塑料制品", "橡胶制品", "农化制品", "非金属材料"],
    "有色金属": ["工业金属", "小金属", "贵金属", "能源金属", "金属新材料"],
    "钢铁": ["钢铁"],
    "煤炭": ["煤炭开采加工"],
    "石油石化": ["油气开采及服务", "石油加工贸易"],
    "银行": ["银行"],
    "房地产": ["房地产"],
    "建筑装饰": ["建筑装饰"],
    "建筑材料": ["建筑材料"],
    "公用事业": ["电力", "燃气"],
    "交通运输": ["物流", "机场航运", "港口航运", "公路铁路运输"],
    "商贸零售": ["零售", "互联网电商", "贸易"],
    "社会服务": ["教育", "旅游及酒店", "其他社会服务"],
    "纺织服饰": ["纺织制造", "服装家纺"],
    "轻工制造": ["包装印刷", "造纸", "家居用品"],
    "美容护理": ["美容护理"],
    "环保": ["环保设备", "环境治理"],
    "综合": ["综合"],
}

# 硬门槛
VOLR_MAX = 0.85      # 近3日均量 / 前20日均量，缩量
RET3_MIN = 0.0       # 近3日转涨
TURNOVER_MAX = 3.0   # 低关注：换手率<3%
DIP_MAX = -8.0       # A组超跌：近20日跌幅<=-8%

BACKTEST_NOTE = (
    "超跌缩量企稳TOP10回测(2024-07~2026-10,110期)相对全市场等权基准，10日超额+1.19%、"
    "胜率54%，但多空分组反向、2025-08后失效，仅作观察非买入信号。"
    "本页已叠加硬条件：所属板块真资金净流入>0(同花顺f62实时)——当前多数板块资金流出，"
    "故候选很少属正常现象，说明超跌股暂无资金认可，宁缺毋滥。"
)


def _conn():
    return pymysql_connect()


def pymysql_connect():
    import pymysql
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
    return pymysql.connect(
        host=os.getenv("WUCAI_SQL_HOST", "127.0.0.1"),
        port=int(os.getenv("WUCAI_SQL_PORT", "3306")),
        user=os.getenv("WUCAI_SQL_USER", "root"),
        password=os.getenv("WUCAI_SQL_PASSWORD", ""),
        database=os.getenv("WUCAI_SQL_DATABASE", "wucai_trade"), charset="utf8mb4",
    )


def _init_cache():
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS dip_cache (key TEXT PRIMARY KEY, result TEXT, updated TEXT)")
    con.commit()
    con.close()


def _get_cache(key: str):
    try:
        _init_cache()
        con = sqlite3.connect(DB_PATH)
        row = con.execute("SELECT result, updated FROM dip_cache WHERE key=?", (key,)).fetchone()
        con.close()
        if not row:
            return None
        age = (datetime.now() - datetime.fromisoformat(row[1])).total_seconds() / 60
        if age > CACHE_MINUTES:
            return None
        import json
        return json.loads(row[0])
    except Exception:
        return None


def _set_cache(key: str, result: dict):
    try:
        _init_cache()
        import json
        con = sqlite3.connect(DB_PATH)
        con.execute("INSERT OR REPLACE INTO dip_cache VALUES (?,?,?)",
                    (key, json.dumps(result, ensure_ascii=False), datetime.now().isoformat()))
        con.commit()
        con.close()
    except Exception:
        pass


def _score_frame(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    for col, asc in [("ret3", False), ("volr", True), ("turnover", True)]:
        d[col + "_r"] = d[col].rank(pct=True, ascending=asc)
    base = (d["ret3_r"] + d["volr_r"] + d["turnover_r"]) / 3 * 100
    if len(d) > 1:
        base = base * 0.85 + d["ret20"].rank(pct=True, ascending=True) * 15
    d["score"] = base
    return d.sort_values("score", ascending=False)


def _sector_flow_map() -> tuple[dict, str, dict, list]:
    """取实时板块真资金(f62)，聚合到申万一级。

    返回 (申万一级→净流入合计, 数据来源, 细分板块→净流入, 原始板块行)。
    细分板块明细用于：① 页面单列"真资金流入板块"；② 标注个股所属的具体受益板块。
    """
    try:
        from data.market_data import get_sector_rotation
        ths = get_sector_rotation()
    except Exception:
        return {}, "", {}, []
    if not ths:
        return {}, "", {}, []
    flow_by_ths = {s["name"]: float(s.get("net_inflow", 0) or 0) for s in ths}
    sw_flow = {}
    for sw, ths_list in SW_TO_THS.items():
        vals = [flow_by_ths[t] for t in ths_list if t in flow_by_ths]
        if vals:
            sw_flow[sw] = round(sum(vals), 2)
    src = ths[0].get("source", "同花顺") if ths else ""
    return sw_flow, src, flow_by_ths, ths


def _compute() -> dict:
    # 板块真资金（同花顺实时 f62），作为硬条件
    sw_flow, flow_src, ths_flow, ths_rows = _sector_flow_map()
    conn = _conn()
    try:
        daily = pd.read_sql(
            "SELECT stock_code,trade_date,close_price,volume,amount,turnover_rate "
            "FROM trade_stock_daily WHERE trade_date>=DATE_SUB(CURDATE(), INTERVAL 120 DAY)",
            conn)
        status = pd.read_sql(
            "SELECT stock_code,stock_name,sector_1,sector_2 FROM trade_stock_status", conn)
    finally:
        conn.close()

    daily["trade_date"] = pd.to_datetime(daily["trade_date"])
    daily = daily[daily["close_price"] > 0].sort_values(["stock_code", "trade_date"])
    name_map = dict(zip(status.stock_code, status.stock_name))
    sec_map = {r.stock_code: (r.sector_1 or r.sector_2 or "") for r in status.itertuples()}

    rows = []
    for code, g in daily.groupby("stock_code"):
        g = g[g["volume"] > 0]
        if len(g) < 25:
            continue
        c = g["close_price"].values; v = g["volume"].values
        t = g["turnover_rate"].values; amt = g["amount"].values
        nx = len(c) - 1
        ret20 = c[nx] / c[nx - 20] - 1
        ret3 = c[nx] / c[nx - 3] - 1
        v3 = np.mean(v[nx - 2:nx + 1]); v20 = np.mean(v[nx - 22:nx - 2])
        volr = v3 / v20 if v20 > 0 else np.nan
        if np.sum(v[nx - 19:nx + 1] <= 0) > 3 or not np.isfinite(volr):
            continue
        # B组硬门槛：低关注 + 缩量 + 转涨
        if not (volr <= VOLR_MAX and ret3 > RET3_MIN and float(t[nx]) <= TURNOVER_MAX):
            continue
        sw = sec_map.get(code, "")
        sflow = sw_flow.get(sw)
        # 该股所属申万一级映射下，真正资金净流入为正的同花顺细分板块
        sector_detail = sorted(
            [{"name": t, "flow": round(ths_flow[t], 2)}
             for t in SW_TO_THS.get(sw, []) if ths_flow.get(t, 0) > 0],
            key=lambda x: -x["flow"])
        rows.append(dict(
            code=code, name=name_map.get(code, code), sector=sw,
            sector_detail=sector_detail,
            sector_flow=sflow if sflow is not None else 0.0,
            flow_ok=bool(sflow is not None and sflow > 0),
            date=str(g["trade_date"].iloc[nx].date()), close=round(float(c[nx]), 2),
            ret20=round(float(ret20) * 100, 1), ret3=round(float(ret3) * 100, 1),
            volr=round(float(volr), 2), turnover=round(float(t[nx]), 2),
            amount=round(float(amt[nx]) / 1e8, 2),
        ))

    df = pd.DataFrame(rows)
    # ★ 核心硬条件：所属板块必须有真资金净流入（同花顺 f62）
    flow_ok_df = df[df.flow_ok].copy() if not df.empty else df

    a = _score_frame(flow_ok_df[flow_ok_df.ret20 <= DIP_MAX].copy()) if not flow_ok_df.empty else flow_ok_df
    b = _score_frame(flow_ok_df) if not flow_ok_df.empty else flow_ok_df

    cols = ["code", "name", "sector", "sector_detail", "sector_flow", "date", "close",
            "ret20", "ret3", "volr", "turnover", "amount", "score"]
    def to_records(d, n=15):
        if d.empty:
            return []
        out = d[cols].head(n).to_dict("records")
        for r in out:
            r["score"] = round(float(r["score"]), 1)
            r["sector_flow"] = round(float(r["sector_flow"]), 2)
        return out

    data_date = df["date"].mode().iloc[0] if not df.empty else ""
    pos_sectors = sorted([(k, v) for k, v in sw_flow.items() if v > 0], key=lambda x: -x[1])
    # 同花顺原始细分板块（真资金净流入>0），完整列出，供页面单独展示
    flow_sectors = sorted(
        [s for s in ths_rows if float(s.get("net_inflow", 0) or 0) > 0],
        key=lambda s: -float(s.get("net_inflow", 0) or 0))
    return {
        "ok": True, "date": data_date,
        "group_a": to_records(a), "group_b": to_records(b),
        "count_a": len(a), "count_b": len(b),
        "candidates_before_flow": len(df) if not df.empty else 0,
        "flow_source": flow_src or "同花顺实时(真资金f62)",
        "positive_sectors": [{"name": k, "flow": v} for k, v in pos_sectors],
        "flow_sectors": [{
            "name": s.get("name", ""),
            "flow": round(float(s.get("net_inflow", 0) or 0), 2),
            "change_pct": round(float(s.get("change_pct", 0) or 0), 2),
        } for s in flow_sectors],
        "note": BACKTEST_NOTE,
        "thresholds": {"volr_max": VOLR_MAX, "ret3_min": RET3_MIN,
                       "turnover_max": TURNOVER_MAX, "dip_max": DIP_MAX,
                       "sector_flow_min": 0},
    }


def get_dip_picks(force: bool = False) -> dict:
    if not force:
        cached = _get_cache("dip:v3")
        if cached:
            cached["from_cache"] = True
            return cached
    try:
        result = _compute()
        result["from_cache"] = False
        _set_cache("dip:v3", result)
        return result
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:150]}",
                "note": BACKTEST_NOTE, "group_a": [], "group_b": []}
