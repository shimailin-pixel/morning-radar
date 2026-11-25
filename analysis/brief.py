"""
方向一：个性化持仓简报
======================
核心理念：只推"和我有关的变化"，不罗列行情。

设计要点（回应用户提的三个问题）：
  1. 怎么控制长度：默认最多 max_items 条异动，无变化则明确说"今日无需关注"
  2. 10 秒看懂：三段式——总资产概览 / 异动卡片(每条一句话) / 一句话结论
  3. 什么情况不该打扰：静默时段标记 + 无实质异动时不生成提醒

异动检测维度（每条都给"为什么值得看"）：
  - 单日涨跌幅超阈值
  - 量比突增（成交量 / 前5日均量）
  - 均线突破/跌破（MA20/MA60）
  - MACD 金叉/死叉
  - 本地新闻命中（近 N 天，带情绪分）
  - 持仓盈亏触及止盈/止损参考线（若有成本）
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from data import get_kline, add_indicators, mysql_source
from analysis.scorer import score_stock, _clean_kline

ROOT = Path(__file__).resolve().parent.parent
WATCHLIST_PATH = ROOT / "watchlist.json"


def load_watchlist() -> dict:
    if not WATCHLIST_PATH.exists():
        return {"stocks": [], "alert": {}}
    try:
        return json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[简报] watchlist.json 解析失败: {e}")
        return {"stocks": [], "alert": {}}


def save_watchlist(data: dict) -> None:
    WATCHLIST_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _quiet_now(quiet_hours: list[str]) -> bool:
    """当前是否处于静默时段。"""
    if not quiet_hours or len(quiet_hours) != 2:
        return False
    now = datetime.now().strftime("%H:%M")
    try:
        start, end = quiet_hours
        return start <= now < end
    except Exception:
        return False


def _sector_context(code: str, chg_pct: float) -> dict:
    """取个股所属申万二级板块的当日表现，用于异动归因（板块拖累 or 个股独立）。"""
    ctx = {"name": "", "change_pct": 0.0, "net_inflow": 0.0, "relation": ""}
    try:
        rows = mysql_source._query(
            "SELECT sector_2 FROM trade_stock_status WHERE stock_code=%s LIMIT 1",
            (f"{code}.SH" if code.startswith(("6", "5")) else f"{code}.SZ",),
        )
        if not rows:
            rows = mysql_source._query(
                "SELECT sector_2 FROM trade_stock_status WHERE stock_code=%s LIMIT 1", (code,))
        sector_name = str(rows[0][0]) if rows else ""
        if not sector_name:
            return ctx
        for s in mysql_source.get_sector_rotation_mysql():
            if s["name"] == sector_name:
                sec_chg = float(s.get("change_pct", 0))
                ctx.update({
                    "name": sector_name,
                    "change_pct": round(sec_chg, 2),
                    "net_inflow": round(float(s.get("net_inflow", 0)), 2),
                })
                diff = chg_pct - sec_chg
                if abs(diff) < 0.5:
                    ctx["relation"] = "与板块同步"
                elif diff > 0:
                    ctx["relation"] = f"跑赢板块{diff:.1f}个百分点"
                else:
                    ctx["relation"] = f"跑输板块{abs(diff):.1f}个百分点"
                break
    except Exception:
        pass
    return ctx


def _detect_changes(stock: dict, alert: dict) -> dict | None:
    """对单只自选股做异动检测，无异动返回 None。"""
    code = stock["code"]
    try:
        # 关键：先清洗当日 volume=0 占位行，否则"今天 vs 昨天"涨跌/量比恒为0，
        # 所有股票都会被误判为无异动。用最新真实交易日做检测。
        raw = get_kline(code, days=90)
        clean = _clean_kline(raw)
        if clean is None:
            return None
        df = add_indicators(clean)
    except Exception as e:
        return {"code": code, "name": stock.get("name", code), "error": str(e)[:80], "changes": []}

    if df is None or len(df) < 25:
        return None

    last = df.iloc[-1]
    prev = df.iloc[-2]
    changes: list[dict] = []

    close = float(last["close"])
    chg_pct = (close / float(prev["close"]) - 1) * 100 if float(prev["close"]) else 0
    vol = float(last["volume"])
    vol_ma5 = df["volume"].iloc[-6:-1].mean()
    vol_ratio = vol / vol_ma5 if vol_ma5 > 0 else 1.0
    ma20 = float(last["ma20"])
    ma60 = float(last["ma60"])
    prev_close = float(prev["close"])
    prev_ma20 = float(prev["ma20"])

    # 板块归因上下文（一次查询，供涨跌/量能异动共用）
    sec = _sector_context(code, chg_pct)
    sec_part = ""
    if sec["name"]:
        flow = "净流入" if sec["net_inflow"] >= 0 else "净流出"
        sec_part = (f"{sec['name']}板块当日{sec['change_pct']:+.2f}%、资金{flow}"
                    f"{abs(sec['net_inflow']):.1f}亿，个股{sec['relation']}。")

    # 1) 涨跌幅异动
    if abs(chg_pct) >= float(alert.get("change_pct", 3.0)):
        direction = "大涨" if chg_pct > 0 else "大跌"
        # 量价配合：缩量下跌/放量下跌含义不同
        if vol_ratio < 0.8:
            vp = f"缩量至5日均量的{vol_ratio:.2f}倍，抛压未明显放大"
        elif vol_ratio >= 1.5:
            vp = f"放量至{vol_ratio:.2f}倍，分歧加剧"
        else:
            vp = f"量能{vol_ratio:.2f}倍，属正常范围"
        detail = f"收盘 {close:.2f}（前日 {prev_close:.2f}），{vp}。{sec_part}"
        changes.append({
            "type": "price", "level": "high" if abs(chg_pct) >= 5 else "mid",
            "text": f"{direction} {chg_pct:+.2f}%",
            "detail": detail,
        })

    # 2) 量能异动
    if vol_ratio >= float(alert.get("vol_ratio", 1.8)):
        changes.append({
            "type": "volume", "level": "mid",
            "text": f"放量 {vol_ratio:.1f} 倍",
            "detail": "成交量显著高于近5日均量",
        })

    # 3) 均线突破/跌破
    if alert.get("ma_break", True):
        if prev_close <= prev_ma20 and close > ma20:
            changes.append({"type": "ma", "level": "high", "text": "上穿20日线", "detail": "短期趋势转强信号"})
        elif prev_close >= prev_ma20 and close < ma20:
            changes.append({"type": "ma", "level": "high", "text": "跌破20日线", "detail": "短期趋势转弱信号"})
        prev_ma60 = float(prev["ma60"]) if "ma60" in prev else ma60
        if prev["close"] <= prev_ma60 and close > ma60:
            changes.append({"type": "ma", "level": "mid", "text": "站上60日线", "detail": "中期趋势转强"})
        elif prev["close"] >= prev_ma60 and close < ma60:
            changes.append({"type": "ma", "level": "mid", "text": "跌破60日线", "detail": "中期趋势转弱"})

    # 4) MACD 金叉/死叉
    if alert.get("macd_cross", True):
        dif, dea = float(last["dif"]), float(last["dea"])
        pdif, pdea = float(prev["dif"]), float(prev["dea"])
        macd_bar = float(last.get("macd", 0))
        if pdif <= pdea and dif > dea:
            changes.append({
                "type": "macd", "level": "mid", "text": "MACD金叉",
                "detail": f"DIF({dif:.2f})上穿DEA({dea:.2f})，红柱{macd_bar:+.2f}，短期动能转强。",
            })
        elif pdif >= pdea and dif < dea:
            changes.append({
                "type": "macd", "level": "mid", "text": "MACD死叉",
                "detail": (f"DIF由{pdif:.2f}降至{dif:.2f}，下穿DEA({dea:.2f})，"
                           f"绿柱{macd_bar:+.2f}，短期动能转弱"
                           + ("（前日尚在零轴上方，属高位回落）。" if pdif > 0 else "。")),
            })

    # 5) 持仓盈亏
    cost = stock.get("cost")
    if cost:
        pnl = (close / float(cost) - 1) * 100
        if abs(pnl) >= 8:
            changes.append({
                "type": "pnl", "level": "high" if abs(pnl) >= 15 else "mid",
                "text": f"持仓{pnl:+.1f}%",
                "detail": f"成本 {float(cost):.2f} → 现价 {close:.2f}",
            })

    # 6) 本地新闻命中
    news_hits: list[dict] = []
    if alert.get("news", True):
        try:
            news = mysql_source.get_news([code], since_days=int(alert.get("news_days", 3)), limit=5)
            news_hits = [
                {"title": n["title"], "sentiment": n["sentiment"],
                 "score": n["sentiment_score"], "published": n["published_at"]}
                for n in news
            ]
            if news_hits:
                latest = news_hits[0]
                changes.append({
                    "type": "news", "level": "low",
                    "text": f"近{alert.get('news_days',3)}日 {len(news_hits)} 条相关新闻",
                    "detail": f"最新（{latest.get('published', '')}）：{latest['title'][:60]}",
                })
        except Exception:
            pass

    if not changes:
        return None

    # 排序：high > mid > low
    order = {"high": 0, "mid": 1, "low": 2}
    changes.sort(key=lambda x: order.get(x["level"], 3))

    base20 = float(df["close"].iloc[-21]) if len(df) >= 21 else 0.0
    ret20 = (close / base20 - 1) * 100 if base20 > 0 else 0.0
    # 接入五维评分：自选股跨板块、无同板块分位上下文，量能走绝对阈值兜底
    scored = score_stock(df)
    return {
        "code": code,
        "name": stock.get("name") or code,
        "close": round(close, 2),
        "change_pct": round(chg_pct, 2),
        "ret20": round(ret20, 2),
        "vol_ratio": round(vol_ratio, 2),
        "score": scored["total"],
        "dims": scored["dims"],
        "signals": scored["signals"],
        "highlights": scored.get("highlights", []),
        "sector": sec.get("name", ""),
        "sector_change": sec.get("change_pct", 0),
        "sector_relation": sec.get("relation", ""),
        "source": str(last.get("source", "")),
        "cost": cost,
        "pnl_pct": round((close / float(cost) - 1) * 100, 2) if cost else None,
        "changes": changes,
        "news": news_hits,
    }


def generate_brief(alert_override: dict | None = None) -> dict:
    """生成个性化持仓简报。返回结构化 dict，前端直接渲染。"""
    wl = load_watchlist()
    stocks = wl.get("stocks", [])
    alert = dict(wl.get("alert", {}))
    if alert_override:
        alert.update(alert_override)

    quiet = _quiet_now(alert.get("quiet_hours", []))
    max_items = int(alert.get("max_items", 5))

    results = []
    for s in stocks:
        r = _detect_changes(s, alert)
        if r:
            results.append(r)

    # 按最高级别 + 涨跌幅排序，只保留前 N 条
    order = {"high": 0, "mid": 1, "low": 2}
    for r in results:
        r["_rank"] = min(order.get(c["level"], 3) for c in r["changes"])
    results.sort(key=lambda x: (x["_rank"], -abs(x["change_pct"])))
    kept = results[:max_items]
    for r in kept:
        r.pop("_rank", None)

    # 三段式结论
    high_count = sum(1 for r in kept if any(c["level"] == "high" for c in r["changes"]))
    if not kept:
        headline = "今日持仓无实质异动，无需关注。"
    elif high_count:
        headline = f"有 {high_count} 只持仓出现重要变化，建议优先查看。"
    else:
        headline = f"有 {len(kept)} 只持仓出现小幅异动，可留意。"

    # 一句话摘要（每条一句）
    one_liners = []
    for r in kept:
        top = r["changes"][0]
        one_liners.append(f"{r['name']}：{top['text']}（{r['change_pct']:+.2f}%）")

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "trade_date": mysql_source.get_latest_trade_date(),
        "quiet_now": quiet,
        "watch_count": len(stocks),
        "alert_count": len(kept),
        "headline": headline,
        "one_liners": one_liners,
        "items": kept,
        "watchlist": stocks,
        "alert_config": alert,
        "source": "本地MySQL" if any(r.get("source") == "本地MySQL" for r in kept) else "混合",
    }
