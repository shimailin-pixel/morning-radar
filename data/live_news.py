"""
实时财经新闻抓取
================
本地 MySQL 新闻表滞后（实测停在 11-18），本模块从公开接口抓当下新闻：

  1. 东方财富 724 全球快讯（主源）—— 实时、每页200条、自带 stockList 股票代码关联
  2. 财联社电报（辅源）—— 实时、带标题/内容/重要等级

设计要点：
  - 双源合并去重，按股票代码/名称匹配
  - SQLite 缓存 15 分钟（新闻时效强，缓存不能太久）；手动 force 强制刷新
  - 数据源标记 source 字段，前端明确展示，绝不冒充本地库
  - 任一源失败不影响另一源；全部失败返回空，由调用方降级到本地新闻表
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

import requests

DB_PATH = Path(__file__).resolve().parent.parent / "live_news.db"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

EM_URL = "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
CLS_URL = "https://www.cls.cn/v1/roll/get_roll_list"


def _init_db() -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS live_news (
                fid TEXT PRIMARY KEY,
                title TEXT,
                summary TEXT,
                published_at TEXT,
                source TEXT,
                codes TEXT,
                sentiment TEXT,
                important INTEGER DEFAULT 0,
                url TEXT,
                fetched_at TEXT
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_live_time ON live_news(published_at)")


def _get_cached(max_age_sec: int = 900):
    _init_db()
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute("SELECT MAX(fetched_at), COUNT(*) FROM live_news").fetchone()
    if not row or not row[0]:
        return None
    try:
        age = (datetime.now() - datetime.fromisoformat(row[0])).total_seconds()
    except Exception:
        return None
    if age > max_age_sec or row[1] < 50:
        return None
    return row[1]


def _fetch_em(pages: int = 3) -> list[dict]:
    """东财724快讯。每页200条，通常覆盖近3天。stockList 形如 ['1.600519','90.BK0447']。"""
    out: list[dict] = []
    sort_end = ""
    for _ in range(pages):
        params = {
            "client": "web", "biz": "web_724", "fastColumn": "102",
            "sortEnd": sort_end, "pageSize": "200",
            "req_trace": str(int(time.time() * 1000)),
        }
        r = requests.get(EM_URL, params=params, timeout=10, headers=HEADERS)
        d = r.json()
        data = d.get("data") or {}
        items = data.get("fastNewsList") or []
        if not items:
            break
        for x in items:
            codes = []
            for s in (x.get("stockList") or []):
                if s.startswith("1.") or s.startswith("0."):
                    code = s[2:]
                    if len(code) == 6 and code.isdigit():
                        codes.append(code)
            out.append({
                "fid": "em_" + str(x.get("code", hashlib.md5(str(x.get("title")).encode()).hexdigest())),
                "title": str(x.get("title") or "").strip(),
                "summary": str(x.get("summary") or "").strip(),
                "published_at": str(x.get("showTime") or "")[:16],
                "source": "东财快讯",
                "codes": codes,
                "url": f"https://finance.eastmoney.com/a/{x.get('code')}.html" if x.get("code") else "",
            })
        sort_end = data.get("sortEnd") or ""
        if not sort_end:
            break
    return out


def _fetch_cls(rn: int = 40) -> list[dict]:
    """财联社电报。无直接股票关联，靠标题/内容关键词匹配。"""
    params = {
        "app": "CailianpressWeb", "category": "", "last_time": int(time.time()),
        "os": "web", "refresh_type": "1", "rn": str(rn), "sv": "8.4.6",
    }
    params["sign"] = hashlib.md5(
        hashlib.sha1(urlencode(params).encode()).hexdigest().encode()
    ).hexdigest()
    r = requests.get(CLS_URL, params=params, timeout=10, headers={**HEADERS, "Referer": "https://www.cls.cn/telegraph"})
    data = r.json().get("data") or {}
    out = []
    for x in (data.get("roll_data") or []):
        title = str(x.get("title") or "").strip()
        content = str(x.get("content") or "").strip()
        ct = x.get("ctime")
        published = datetime.fromtimestamp(ct).strftime("%Y-%m-%d %H:%M") if ct else ""
        out.append({
            "fid": "cls_" + str(x.get("id", hashlib.md5((title + content).encode()).hexdigest()[:16])),
            "title": title or content[:60],
            "summary": content,
            "published_at": published,
            "source": "财联社电报",
            "codes": [],  # 关键词匹配在 get_news_for_stock 里做
            "important": 1 if x.get("level") in ("A", "B") else 0,
            "url": f"https://www.cls.cn/detail/{x.get('id')}" if x.get("id") else "",
        })
    return out


_POS = ("涨停", "大涨", "上涨", "利好", "增持", "超预期", "创新高", "中标", "订单", "回购", "突破", "新高", "净流入", "增长")
_NEG = ("跌停", "大跌", "下跌", "利空", "减持", "低于预期", "亏损", "下滑", "处罚", "立案", "风险", "退市", "净流出", "承压")


def _sentiment(text: str) -> str:
    pos = sum(text.count(w) for w in _POS)
    neg = sum(text.count(w) for w in _NEG)
    if pos > neg and pos >= 1:
        return "positive"
    if neg > pos and neg >= 1:
        return "negative"
    return "neutral"


def fetch_all(force: bool = False) -> int:
    """抓取全部源写入缓存，返回条数。缓存新鲜时直接跳过（force 除外）。"""
    if not force:
        n = _get_cached()
        if n:
            return n
    _init_db()
    all_items: list[dict] = []
    errors = []
    for name, fn in (("东财", _fetch_em), ("财联社", _fetch_cls)):
        try:
            items = fn()
            all_items.extend(items)
            print(f"[新闻] {name}抓取 {len(items)} 条")
        except Exception as e:
            errors.append(f"{name}:{type(e).__name__}")
            print(f"[新闻] {name}抓取失败: {type(e).__name__}: {e}")

    now = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(DB_PATH) as con:
        for it in all_items:
            text = it["title"] + " " + it.get("summary", "")
            con.execute("""
                INSERT OR REPLACE INTO live_news
                (fid,title,summary,published_at,source,codes,sentiment,important,url,fetched_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
            """, (
                it["fid"], it["title"], it.get("summary", ""), it["published_at"],
                it["source"], json.dumps(it.get("codes", []), ensure_ascii=False),
                _sentiment(text), it.get("important", 0), it.get("url", ""), now,
            ))
        con.execute("DELETE FROM live_news WHERE fetched_at < ?", (now,))  # 清掉上批次残留
        total = con.execute("SELECT COUNT(*) FROM live_news").fetchone()[0]
    if errors:
        print(f"[新闻] 部分源失败: {','.join(errors)}")
    return total


def get_news_for_stock(code: str, name: str = "", limit: int = 12, force: bool = False) -> list[dict]:
    """取某只股票的实时新闻：代码命中优先，名称关键词补充。"""
    fetch_all(force=force)
    _init_db()
    with sqlite3.connect(DB_PATH) as con:
        # 代码精确命中
        rows = con.execute(
            "SELECT * FROM live_news ORDER BY published_at DESC LIMIT 2000"
        ).fetchall()
    cols = ["fid", "title", "summary", "published_at", "source", "codes", "sentiment", "important", "url", "fetched_at"]
    items = [dict(zip(cols, r)) for r in rows]
    hits = []
    for it in items:
        codes = json.loads(it["codes"] or "[]")
        matched = code in codes
        match_type = "代码" if matched else ""
        if not matched and name and len(name) >= 2:
            if name in it["title"]:
                matched, match_type = True, "标题"
            elif name in (it["summary"] or ""):
                matched, match_type = True, "正文"
        if matched:
            it["important"] = bool(it["important"])
            it["match_type"] = match_type
            # 正文提及（非标题非代码）时情绪可能是别人的，降级为中性避免误导
            if match_type == "正文" and it.get("sentiment") in ("positive", "negative"):
                it["sentiment"] = "neutral"
                it["sentiment_note"] = "正文提及，情绪主体可能非本股"
            hits.append(it)
    hits.sort(key=lambda x: (x["published_at"], x["important"]), reverse=True)
    return hits[:limit]


def get_recent(limit: int = 40, force: bool = False) -> list[dict]:
    """取最近的实时新闻（用于全局新闻面分析）。"""
    fetch_all(force=force)
    _init_db()
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute(
            "SELECT * FROM live_news ORDER BY published_at DESC LIMIT ?", (limit,)
        ).fetchall()
    cols = ["fid", "title", "summary", "published_at", "source", "codes", "sentiment", "important", "url", "fetched_at"]
    out = [dict(zip(cols, r)) for r in rows]
    for it in out:
        it["important"] = bool(it["important"])
    return out


def news_status() -> dict:
    _init_db()
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute("SELECT COUNT(*), MAX(fetched_at), MAX(published_at) FROM live_news").fetchone()
    return {
        "count": row[0] or 0,
        "fetched_at": row[1] or "",
        "latest_news_at": row[2] or "",
        "cache_minutes": 15,
    }
