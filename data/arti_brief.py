"""
ARTi 早晚报数据接入
===================
直接读取 ARTi 官方公开内容接口（Supabase REST / content_briefs 表），
拿到每日早晚报的完整正文（body_html），无需浏览器渲染。

数据源
------
GET https://api.artifin.ai/rest/v1/content_briefs
    ?select=slug,kind,title,summary,body_html,published_at
    &slug=eq.morning-2026-11-24
Headers: apikey / Authorization: Bearer <publishable key>

说明
----
- publishable key 是 Supabase 公开可读取键（前端站点 JS 内也明文存在），
  仅能读取公开内容表，不涉及用户数据或写入权限，放后端仅为不依赖前端。
- 缓存 1 小时到 SQLite；失败时回退最近一期同类晨报，保证页面始终有内容。
- 稳定源失效时可切源：若官方改版，只需改 API_URL / HEADERS 或降级到 SEO 摘要。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DB_PATH = Path(__file__).resolve().parent.parent / "arti_brief.db"
API_URL = "https://api.artifin.ai/rest/v1/content_briefs"
PUBLISHABLE_KEY = "sb_publishable_5SIVwCD2q2QjtijkX8zn5Q_NSiocgl5"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (market-radar brief reader)",
    "apikey": PUBLISHABLE_KEY,
    "Authorization": f"Bearer {PUBLISHABLE_KEY}",
}
CACHE_MINUTES = 60


def _init_db() -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS arti_brief_cache (
                slug TEXT PRIMARY KEY,
                kind TEXT,
                title TEXT,
                summary TEXT,
                body_html TEXT,
                published_at TEXT,
                fetched_at TEXT
            )
        """)


def _utc_to_local(ts: str) -> str:
    """ISO UTC → 北京时间 'YYYY-MM-DD HH:MM'。"""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo:
            dt = dt.astimezone()  # 服务器本地时区（北京时间）
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts or "")[:16]


def _query(kind: str, date: str | None = None, limit: int = 1) -> list[dict]:
    """从官方接口查询晨报/晚报。date 指定则精确匹配 slug，否则取最近一期。"""
    params = {
        "select": "slug,kind,title,summary,body_html,published_at",
        "kind": f"eq.{kind}",
        "order": "published_at.desc",
        "limit": str(limit),
    }
    if date:
        params["slug"] = f"eq.{kind}-{date}"
    url = API_URL + "?" + urlencode(params)
    req = Request(url, headers=HEADERS)
    with urlopen(req, timeout=15) as resp:
        rows = json.loads(resp.read().decode("utf-8", "ignore"))
    return rows or []


def _normalize(row: dict) -> dict:
    return {
        "slug": row.get("slug", ""),
        "kind": row.get("kind", ""),
        "title": row.get("title", ""),
        "summary": row.get("summary", ""),
        "html": row.get("body_html", ""),
        "date": _utc_to_local(row.get("published_at", "")),
        "source": "ARTi 官方早晚报",
    }


def _get_cached(slug: str, max_age_min: int = CACHE_MINUTES) -> dict | None:
    _init_db()
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT kind,title,summary,body_html,published_at,fetched_at "
            "FROM arti_brief_cache WHERE slug=?", (slug,)
        ).fetchone()
    if not row:
        return None
    try:
        age = (datetime.now() - datetime.fromisoformat(row[5])).total_seconds() / 60
        if age > max_age_min:
            return None
    except Exception:
        return None
    return _normalize({
        "slug": slug, "kind": row[0], "title": row[1], "summary": row[2],
        "body_html": row[3], "published_at": row[4],
    })


def _set_cached(item: dict) -> None:
    _init_db()
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT OR REPLACE INTO arti_brief_cache"
            "(slug,kind,title,summary,body_html,published_at,fetched_at) VALUES(?,?,?,?,?,?,?)",
            (item["slug"], item["kind"], item["title"], item["summary"],
             item["html"], item["date"], datetime.now().isoformat(timespec="seconds")),
        )


def get_morning_brief(date: str | None = None, force: bool = False, kind: str = "morning") -> dict:
    """取当天（或指定日期）ARTi 早报。

    优先缓存 → 官方接口精确日期 → 最近一期降级。
    date 格式 YYYY-MM-DD，默认今天。
    返回 {ok, slug, kind, title, summary, html, date, source, from_cache, error}
    """
    date = date or datetime.now().strftime("%Y-%m-%d")
    slug = f"{kind}-{date}"

    if not force:
        cached = _get_cached(slug)
        if cached and cached.get("html"):
            cached.update({"ok": True, "from_cache": True})
            return cached

    errors = []
    # 1) 精确日期
    try:
        rows = _query(kind, date=date)
        if rows:
            item = _normalize(rows[0])
            _set_cached(item)
            item.update({"ok": True, "from_cache": False})
            return item
        errors.append(f"{date} 无记录")
    except Exception as e:
        errors.append(f"精确查询失败: {type(e).__name__}: {str(e)[:80]}")

    # 2) 精确日期缓存兜底（接口临时挂时用最近一次成功抓取的同日缓存）
    cached = _get_cached(slug, max_age_min=7 * 24 * 60)
    if cached and cached.get("html"):
        cached.update({"ok": True, "from_cache": True, "stale": True})
        return cached

    # 3) 最近一期降级
    try:
        rows = _query(kind, limit=1)
        if rows:
            item = _normalize(rows[0])
            item.update({"ok": True, "from_cache": False, "fallback": True})
            return item
        errors.append("无最近一期")
    except Exception as e:
        errors.append(f"降级查询失败: {type(e).__name__}")

    return {
        "ok": False, "slug": slug, "kind": kind, "title": "", "summary": "",
        "html": "", "date": date, "source": "ARTi 官方早晚报",
        "from_cache": False, "error": "; ".join(errors),
    }


def brief_status() -> dict:
    """返回缓存概况，供 /api/status 展示。"""
    try:
        _init_db()
        with sqlite3.connect(DB_PATH) as con:
            row = con.execute(
                "SELECT COUNT(*), MAX(published_at) FROM arti_brief_cache"
            ).fetchone()
        return {"cached": row[0] or 0, "latest": row[1] or ""}
    except Exception:
        return {"cached": 0, "latest": ""}
