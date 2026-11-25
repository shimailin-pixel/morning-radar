"""
每日投研雷达 - Web 后端
=======================
FastAPI 提供 REST API，前端单页消费。

端点：
  GET  /                          首页
  GET  /api/overview              板块轮动 + 各板块精选股票（首屏一次加载）
  GET  /api/sector/{name}/stocks  单个板块的选股（按需刷新）
  GET  /api/kline/{code}?days=120 K线 + 全部技术指标
  GET  /api/analyze/{code}?sector=&kind=  ARTi 三维分析（按需+缓存）
  POST /api/position/advice        持仓去留诊断（喂名称+现状+持仓移动给ARTi）
  GET  /api/status                数据源与ARTi状态
  POST /api/refresh               清缓存强制刷新
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Query, Body
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from data import get_sector_rotation, get_sector_constituents, get_kline, add_indicators, get_client, probe_network, mysql_source, live_news, arti_brief
from analysis import pick_sector_stocks, generate_morning_brief, analyze_stock
from analysis.brief import generate_brief, load_watchlist, save_watchlist
from analysis.dip_picker import get_dip_picks

app = FastAPI(title="每日投研雷达", version="1.0")
STATIC = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.on_event("startup")
def _startup_probe():
    """启动时探测行情网络，避免首屏每个接口都重复超时重试。"""
    ok = probe_network()
    print(f"[启动] 行情网络探测: {'可用' if ok else '不可用(使用内置数据)'}")

# 首屏扫描的板块数（轮动评分Top N）
TOP_SECTORS = int(os.getenv("TOP_SECTORS", "3"))
STOCKS_PER_SECTOR = int(os.getenv("STOCKS_PER_SECTOR", "4"))


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
def status():
    mock = os.getenv("ARTI_MOCK", "").lower() in ("1", "true", "yes") or not os.getenv("ARTI_API_KEY")
    try:
        db_ok = mysql_source.mysql_available()
        db_date = mysql_source.get_latest_trade_date()
    except Exception:
        db_ok, db_date = False, ""
    try:
        news_stat = live_news.news_status()
    except Exception:
        news_stat = {}
    return {
        "arti_mode": "mock" if mock else "真实API",
        "db_mode": "本地MySQL" if db_ok else "akshare/降级",
        "db_date": db_date,
        "sector_source": os.getenv("SECTOR_SOURCE", "mysql").strip().lower(),
        "sector_source_label": (
            "同花顺实时(akshare)" if os.getenv("SECTOR_SOURCE", "mysql").strip().lower() == "ths"
            else "本地MySQL(申万二级)"
        ),
        "news_mode": "实时抓取(东财+财联社)",
        "news_count": news_stat.get("count", 0),
        "news_latest": news_stat.get("latest_news_at", ""),
        "top_sectors": TOP_SECTORS,
        "stocks_per_sector": STOCKS_PER_SECTOR,
        "arti_brief": arti_brief.brief_status(),
        "version": "1.3",
    }


@app.get("/api/arti-brief")
def arti_brief_api(date: str = Query(""), kind: str = Query("morning"), force: bool = Query(False)):
    """ARTi 官方早晚报：默认取当天早报，正文为完整 HTML。date 格式 YYYY-MM-DD。"""
    return arti_brief.get_morning_brief(date=date or None, force=force, kind=kind)


@app.get("/api/dip-picks")
def dip_picks(force: bool = Query(False)):
    """低吸观察池：低关注+超跌+缩量企稳候选股（观察工具，非买入信号）。"""
    return get_dip_picks(force=force)


@app.get("/api/overview")
def overview():
    """首屏：板块轮动 + 每个热门板块的精选股票 + 晨报摘要（板块并发处理）。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    sectors = get_sector_rotation()
    top = sorted(sectors, key=lambda x: -x.get("momentum_score", 0))[:TOP_SECTORS]
    picks: dict = {}
    names = [s["name"] for s in top]
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(pick_sector_stocks, n, STOCKS_PER_SECTOR): n for n in names}
        for f in as_completed(futures):
            name = futures[f]
            try:
                picks[name] = f.result()
            except Exception as e:
                picks[name] = {"sector": name, "stocks": [], "error": str(e)[:100]}
    # 保持板块顺序（按轮动强度）
    picks = {n: picks[n] for n in names if n in picks}
    brief = generate_morning_brief(sectors, picks)
    return {
        "updated": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M"),
        "sectors": sectors,
        "top_sectors": [s["name"] for s in top],
        "picks": picks,
        "brief": brief,
        "total_stocks": sum(len(p.get("stocks", [])) for p in picks.values()),
    }


@app.get("/api/sector/{name}/stocks")
def sector_stocks(name: str):
    return pick_sector_stocks(name, top_n=STOCKS_PER_SECTOR)


@app.get("/api/kline/{code}")
def kline(code: str, days: int = Query(120, ge=30, le=500)):
    df = get_kline(code, days=days)
    df = add_indicators(df)
    records = df.to_dict("records")
    # 前端只需要这些列，减小体积
    cols = ["date", "open", "close", "high", "low", "volume",
            "ma5", "ma10", "ma20", "ma60", "dif", "dea", "macd",
            "boll_up", "boll_mid", "boll_low", "rsi", "k", "d", "j", "source"]
    clean = [{c: (None if c in r and __import__("pandas").isna(r[c]) else r.get(c)) for c in cols} for r in records]
    return {"code": code, "kline": clean, "source": clean[-1].get("source", "") if clean else ""}


@app.get("/api/analyze/{code}")
def analyze(code: str, sector: str = Query(""), kind: str = Query("news"), force: bool = Query(False)):
    """按需生成分析：本地结果秒回，ARTi 增强后台异步。kind: news / report / council / all"""
    from analysis import analyze_all
    stock = {
        "code": code,
        "name": _lookup_name(code),
        "close": 0, "ret20": 0, "score": 0, "signals": [],
    }
    # 尝试从选股结果里补全股票信息
    try:
        picks = _find_stock_info(code)
        if picks:
            stock.update(picks)
    except Exception:
        pass
    # 仍未找到（不在Top板块精选里）→ 实时算一次评分，避免显示0分
    if not stock.get("score"):
        try:
            from analysis.scorer import score_stock
            df = add_indicators(get_kline(code, days=120))
            r = score_stock(df)
            stock["score"] = r.get("total", 0)
            stock["signals"] = r.get("signals", [])
            stock["close"] = r.get("close", 0)
            stock["ret20"] = r.get("ret20", 0)
        except Exception:
            pass

    if kind == "all":
        return analyze_all(stock, sector or "未知板块")
    return analyze_stock(stock, sector or "未知板块", kind, force=force)


@app.get("/api/analyze/{code}/arti")
def analyze_arti_status(code: str, kind: str = Query("news")):
    """轮询 ARTi 后台增强结果：未完成时秒回 {ready:false}，完成后返回解读文本。"""
    from analysis.ai_analysis import arti_status
    return arti_status(code, kind)


def _lookup_name(code: str) -> str:
    # 优先本地 MySQL 真实名称
    try:
        names = mysql_source.get_stock_names([code])
        if names.get(code):
            return names[code]
    except Exception:
        pass
    from data import FALLBACK_POOL
    for pool in FALLBACK_POOL.values():
        for c, n in pool:
            if c == code:
                return n
    return code


def _find_stock_info(code: str) -> dict | None:
    """从缓存的板块选股结果里找股票详情（避免重复拉K线）。"""
    sectors = get_sector_rotation()
    top = sorted(sectors, key=lambda x: -x.get("momentum_score", 0))[:TOP_SECTORS]
    for s in top:
        pick = pick_sector_stocks(s["name"], top_n=STOCKS_PER_SECTOR)
        for st in pick.get("stocks", []):
            if st["code"] == code:
                return st
    return None


# -------------------- 方向一：个性化持仓简报 --------------------

@app.get("/api/brief")
def brief():
    """我的持仓简报：只返回自选股的异动，10秒看懂。"""
    return generate_brief()


@app.post("/api/position/advice")
def position_advice(payload: dict = Body(...), force: bool = Query(False)):
    """持仓去留诊断：把股票名称+当前情况+持仓移动喂给 ARTi，输出该不该平仓。

    payload 直接是 /api/brief items 里的单只股票 dict；若字段不全，
    后端按 code 从简报里补全后再分析。
    """
    from analysis.position_advice import advise_position

    holding = dict(payload or {})
    code = holding.get("code", "")
    if not code:
        return {"error": "缺少 code"}
    # 字段不全时从简报按 code 补全（前端一般已传全，这里是健壮性兜底）
    if not holding.get("changes") or holding.get("score") in (None, "", 0):
        try:
            brief_data = generate_brief()
            match = next((i for i in brief_data.get("items", []) if i["code"] == code), None)
            if match:
                for k, v in match.items():
                    holding.setdefault(k, v)
        except Exception:
            pass
    return advise_position(holding, force=force)


@app.get("/api/watchlist")
def get_watchlist():
    return load_watchlist()


@app.post("/api/watchlist")
def post_watchlist(payload: dict = Body(...)):
    """保存自选股清单。期望 {'stocks':[...], 'alert':{...}}"""
    if "stocks" not in payload:
        return {"ok": False, "message": "缺少 stocks 字段"}
    save_watchlist({
        "stocks": payload["stocks"],
        "alert": payload.get("alert", load_watchlist().get("alert", {})),
    })
    return {"ok": True, "message": f"已保存 {len(payload['stocks'])} 只自选股"}


@app.post("/api/watchlist/add")
def add_to_watchlist(payload: dict = Body(...)):
    """从低吸观察池等处把单只股票加入持仓：代码规范化(去.SZ/.SH后缀)+按代码去重。"""
    raw_code = str(payload.get("code", "") or "").strip()
    code = raw_code.split(".")[0].strip()
    if not (code.isdigit() and len(code) == 6):
        return {"ok": False, "message": f"股票代码格式无效：{raw_code}"}
    wl = load_watchlist()
    stocks = list(wl.get("stocks", []))
    if any(str(s.get("code", "")).split(".")[0] == code for s in stocks):
        return {"ok": False, "already": True, "message": "该股票已在持仓中"}
    name = str(payload.get("name", "") or "").strip() or code
    note = str(payload.get("note", "") or "").strip()
    # 成本价 = 加入时该股显示的价格（"当时的价格"）；无效值留空
    import math
    raw_cost = payload.get("cost")
    cost = None
    if raw_cost not in (None, ""):
        try:
            v = float(raw_cost)
            if v > 0 and math.isfinite(v):
                cost = round(v, 2)
        except (TypeError, ValueError):
            pass
    stocks.append({"code": code, "name": name, "cost": cost, "note": note})
    save_watchlist({"stocks": stocks, "alert": wl.get("alert", {})})
    return {"ok": True, "code": code, "cost": cost,
            "message": f"已加入持仓：{name}（{code}）" + (f"，成本价 {cost}" if cost else "")}


@app.get("/api/news/{code}")
def stock_news(code: str, days: int = Query(7, ge=1, le=90), limit: int = Query(20, ge=1, le=100)):
    """实时新闻优先（东财快讯+财联社），本地库补充，供抽屉详情用。"""
    name = _lookup_name(code)
    live = []
    try:
        live = live_news.get_news_for_stock(code, name, limit=limit)
    except Exception as e:
        print(f"[新闻] 实时获取失败: {e}")
    local = []
    try:
        local = mysql_source.get_news([code], since_days=max(days, 30), limit=limit)
    except Exception:
        pass
    # 统一字段：实时新闻的 published_at 已是 'YYYY-MM-DD HH:MM'
    for it in live:
        it["sentiment_score"] = ""
    seen = set()
    merged = []
    for it in live + local:
        key = it.get("title", "")[:40]
        if key in seen:
            continue
        seen.add(key)
        merged.append(it)
    merged.sort(key=lambda x: str(x.get("published_at", "")), reverse=True)
    return {"code": code, "news": merged[:limit], "live_count": len(live), "local_count": len(local)}


@app.get("/api/financial/{code}")
def stock_financial(code: str, limit: int = Query(8, ge=1, le=40)):
    """本地财务数据，供'买前三问'等深度分析用。"""
    return {"code": code, "financial": mysql_source.get_financial(code, limit=limit)}


@app.post("/api/refresh")
def refresh():
    """清除数据缓存，强制下次拉取真实数据。"""
    for db in ("market.db",):
        p = Path(__file__).resolve().parent.parent / db
        if p.exists():
            import sqlite3
            with sqlite3.connect(p) as con:
                con.execute("DELETE FROM cache")
    # 新闻缓存（15分钟）也强制刷新
    try:
        nl = live_news.fetch_all(force=True)
        msg = f"数据缓存已清除，实时新闻已刷新（{nl}条）"
    except Exception:
        msg = "数据缓存已清除，实时新闻刷新失败"
    return {"ok": True, "message": msg}
