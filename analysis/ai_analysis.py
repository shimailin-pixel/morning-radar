"""
智能分析引擎（数据驱动）
========================
对单只股票生成三类分析，按需加载 + 缓存：
  1. 新闻分析   —— 基于实时抓取的新闻（东财快讯/财联社）+ 本地新闻库，真实标题+情绪
  2. 投研报告   —— 基于真实K线指标（MA/MACD/RSI/BOLL/量能）的结构化研判
  3. 大师总结   —— 用真实数据驱动的多角色观点（价值/趋势/风控）

设计要点：
  - 默认走本地数据引擎：所有结论都能追溯到具体数据点，不再输出空泛 mock 套话
  - 若配置了 ARTI_API_KEY（真实模式），叠加 ARTi 的三模型分析作为增强
  - 缓存：本地引擎10分钟（跟随行情），ARTi 结果7天
  - 三类独立失败隔离
"""

from __future__ import annotations

import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from data import live_news
from data.arti_client import ARTIClient, get_client

DB_PATH = Path(__file__).resolve().parent.parent / "ai_cache.db"

# ARTi 单次调用常在 15~30 秒，绝不能让前端同步等待。
# 用后台线程池异步增强：主接口秒回本地数据结果，增强完成后写独立缓存，前端轮询补齐。
# 实测 4 并发时 ARTi 服务端频繁 502/超时，降到 2 排队，提升单次成功率
_arti_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="arti-enhance")
_arti_running: set[str] = set()
_arti_fail: dict[str, dict] = {}   # cache_key -> {reason, attempts, last_time} 失败诊断
_arti_lock = threading.Lock()
ARTI_CACHE_DAYS = 7

PROMPTS = {
    "news": (
        "你是财经新闻分析师。基于提供的真实新闻标题和情绪标签，梳理该股近期消息面："
        "分「正面因素」「负面因素」「需要关注的事件」三部分，只引用提供的新闻，"
        "不编造事实。最后加一句风险提示。用中文，简洁。"
    ),
    "report": (
        "你是证券分析师。基于提供的真实技术指标数值，写一份结构化投研速评："
        "包含「技术面解读」「逻辑与催化」「风险与证伪条件」三部分。"
        "必须引用具体指标数值（MA/MACD/RSI/量比），不编造数据。用中文，500字以内。"
    ),
    "council": (
        "你是投资大师圆桌，含价值投资者、趋势交易者、风险控制官三位角色。"
        "基于提供的真实数据，各发表一段简短观点（80字内），最后主持人综合。"
        "观点要引用数据、有分歧，不要和稀泥。用中文。"
    ),
    "arti": (
        "你是资深证券投资顾问。请对这家上市公司做一份全面、独立的投研报告，"
        "涵盖：公司概况与主营业务、近期消息面与催化、技术面与资金面、估值与投资逻辑、"
        "以及主要风险点。基于可核验的公开信息，不编造数据。用中文，结构清晰。"
    ),
}

MODEL_MAP = {"news": "news", "report": "chat", "council": "council", "arti": "chat"}
LABELS = {"news": "新闻分析", "report": "投研报告", "council": "大师总结", "arti": "ARTi 智能解读"}


def _init_db() -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS ai_cache (
                cache_key TEXT PRIMARY KEY,
                result TEXT NOT NULL,
                model TEXT NOT NULL,
                updated TEXT NOT NULL
            )
        """)


def _get_cached(key: str, max_age_min: int = 10):
    _init_db()
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute("SELECT result, updated FROM ai_cache WHERE cache_key=?", (key,)).fetchone()
    if not row:
        return None
    try:
        age = (datetime.now() - datetime.fromisoformat(row[1])).total_seconds() / 60
        if age > max_age_min:
            return None
        return row[0]
    except Exception:
        return None


def _set_cached(key: str, result: str, model: str) -> None:
    _init_db()
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT OR REPLACE INTO ai_cache(cache_key,result,model,updated) VALUES(?,?,?,?)",
            (key, result, model, datetime.now().isoformat()),
        )


# -------------------- 本地数据驱动引擎 --------------------

def _fmt(v, suffix="", digits=2) -> str:
    try:
        if v is None:
            return "N/A"
        return f"{float(v):.{digits}f}{suffix}"
    except Exception:
        return "N/A"


def _news_sentiment(title: str) -> str:
    """从标题自己判情绪（本地库的 sentiment 字段几乎全 neutral，不可靠）。"""
    neg_words = ("下降", "下滑", "缩减", "亏损", "下跌", "跌停", "减持", "处罚", "立案",
                 "低于预期", "预亏", "退市", "风险", "由盈转亏", "降超", "下调", "承压", "净卖出")
    pos_words = ("增长", "上涨", "创新高", "中标", "扩产", "涨停", "增持", "超预期",
                 "扭亏", "预增", "新高", "突破", "回购", "净买入", "提价")
    neg = sum(title.count(w) for w in neg_words)
    pos = sum(title.count(w) for w in pos_words)
    if neg > pos:
        return "negative"
    if pos > neg:
        return "positive"
    return "neutral"


def _extract_facts(title: str) -> list[str]:
    """从新闻标题抽取关键事实（数字+事件），做成实质要点。"""
    import re
    facts = []
    # 业绩类：净利润/营收 + 金额 + 同比
    m = re.search(r'(归母净利润|净利润|营收|营业收入|毛利|利润)[^0-9]{0,6}(\d+(?:\.\d+)?\s*亿元?)', title)
    if m:
        facts.append(f"{m.group(1)}{m.group(2)}")
    yoy = re.search(r'(同比|环比)\s*(增长|下降|上升|下滑)\s*(\d+(?:\.\d+)?%)', title)
    if yoy:
        facts.append(f"{yoy.group(1)}{yoy.group(2)}{yoy.group(3)}")
    # 金额变动优先（由X降至Y / 由X减至Y）——这条信息量最大
    change = re.search(r'由\s*(\d+(?:\.\d+)?\s*亿元?)\s*[^0-9]{0,4}?(\d+(?:\.\d+)?\s*亿元?)', title)
    if change:
        facts.append(f"{change.group(1)}→{change.group(2)}")
    # 投资/募资/项目金额（去掉已在变动里的，避免重复）
    inv = re.findall(r'(拟投|拟募资|拟募|募资|总投资|投资额)[^0-9]{0,8}(\d+(?:\.\d+)?\s*亿元)', title)
    if not inv:
        # "拟1.01亿元投建" 这种：拟+金额+投建
        m = re.search(r'拟\s*(\d+(?:\.\d+)?\s*亿元)\s*(?:投建|投资|用于)', title)
        if m:
            inv = [("拟投", m.group(1))]
    for label, amt in inv[:2]:
        fact = f"{label}{amt}"
        # 若变更抽取已包含该金额（如 4.22亿元→1.85亿元），跳过重复单项
        amt_core = amt.replace(" ", "")
        if fact not in facts and not any(amt_core in f for f in facts):
            facts.append(fact)
    cut = re.search(r'缩减超?(\d+(?:\.\d+)?成)', title)
    if cut:
        facts.append(f"缩减{cut.group(1)}成")
    # 产能
    cap = re.search(r'年产\s*(\d+(?:\.\d+)?\s*\w+)', title)
    if cap:
        facts.append(f"年产{cap.group(1)}")
    return facts


def _news_content(stock: dict) -> str:
    """合并实时快讯+本地库新闻，自己判情绪、抽取关键数字，生成有实质内容的消息面分析。"""
    code, name = stock.get("code", ""), stock.get("name", "")
    live = live_news.get_news_for_stock(code, name, limit=12)
    local = []
    try:
        from data import mysql_source
        local = mysql_source.get_news([code], since_days=90, limit=15)
    except Exception:
        pass

    # 统一字段 + 去重 + 合并
    norm = {}
    for n in live:
        norm[n["title"][:40]] = {
            "date": str(n.get("published_at", ""))[:16],
            "title": n["title"], "source": n.get("source", ""),
            "live": True, "summary": n.get("summary", ""),
        }
    for n in local:
        k = n["title"][:40]
        if k not in norm:  # 实时优先，本地不覆盖
            norm[k] = {
                "date": str(n.get("published_at", ""))[:16],
                "title": n["title"], "source": n.get("source", ""),
                "live": False, "summary": n.get("summary", ""),
            }
    all_news = sorted(norm.values(), key=lambda x: x["date"], reverse=True)
    for n in all_news:
        n["mood"] = _news_sentiment(n["title"])
        n["facts"] = _extract_facts(n["title"])

    live_cnt = sum(1 for n in all_news if n["live"])
    local_cnt = len(all_news) - live_cnt
    latest = all_news[0]["date"] if all_news else "无"
    lines = [f"### {name}（{code}）· 消息面梳理\n"]
    lines.append(f"> 共 {len(all_news)} 条（实时快讯 {live_cnt} · 资讯库 {local_cnt}），最新：{latest}。\n")

    # 关键数据提炼（有数字的新闻优先展示）——这是"实质内容"的核心
    fact_items = [n for n in all_news if n["facts"]]
    if fact_items:
        lines.append("**关键数据（从新闻标题提取）**")
        for n in fact_items[:5]:
            facts = " · ".join(n["facts"])
            lines.append(f"- `{n['date'][5:]}` **{facts}** — {n['title'][:44]}")
        lines.append("")

    pos = [n for n in all_news if n["mood"] == "positive"]
    neg = [n for n in all_news if n["mood"] == "negative"]
    neutral = [n for n in all_news if n["mood"] == "neutral"]

    lines.append("**积极信号**")
    if pos:
        for n in pos[:3]:
            lines.append(f"- `{n['date'][5:]}` {n['title'][:52]} — *{n['source']}*")
    else:
        lines.append("- 近期新闻中未识别到明确的积极表述。")

    lines.append("\n**需留意的风险点**")
    if neg:
        for n in neg[:3]:
            lines.append(f"- `{n['date'][5:]}` {n['title'][:52]} — *{n['source']}*")
    else:
        lines.append("- 近期新闻中未识别到明确的风险表述。")

    lines.append("\n**其他动态**（投资/扩产/融资等，方向中性）")
    if neutral:
        for n in neutral[:4]:
            lines.append(f"- `{n['date'][5:]}` {n['title'][:52]}")
    else:
        lines.append("- 无。")

    lines.append("\n> 情绪方向由标题关键词规则判定，关键数据从标题自动提取；资讯库新闻可能存在滞后，"
                 "请以公司公告等官方披露为准。本内容不构成投资建议。")
    return "\n".join(lines)


def _report_content(stock: dict) -> str:
    """基于真实K线指标生成投研速评。"""
    code, name = stock.get("code", ""), stock.get("name", "")
    close = _fmt(stock.get("close"))
    ret20 = _fmt(stock.get("ret20"), "%", 2)
    score = stock.get("score", "N/A")
    signals = "、".join(stock.get("signals", [])) or "暂无明显信号"
    ind = stock.get("indicators") or {}
    ma5, ma20, ma60 = _fmt(ind.get("ma5")), _fmt(ind.get("ma20")), _fmt(ind.get("ma60"))
    dif, dea, macd = _fmt(ind.get("dif")), _fmt(ind.get("dea")), _fmt(ind.get("macd"))
    rsi = _fmt(ind.get("rsi"))
    boll_up, boll_low = _fmt(ind.get("boll_up")), _fmt(ind.get("boll_low"))
    vol_ratio = _fmt(ind.get("vol_ratio"))
    sector = stock.get("sector", "所属板块")

    # 趋势判断：与 scorer 口径统一——MA20 在 MA60 下方时只算"短期转强"，不叫多头排列
    m5, m20, m60 = ind.get("ma5"), ind.get("ma20"), ind.get("ma60")
    if m5 and m20 and m60 and m5 > m20 > m60:
        trend = "均线多头排列（MA5>MA20>MA60）"
    elif m5 and m20 and m5 > m20:
        trend = "短期均线转强（MA5>MA20，但 MA20 仍在 MA60 下方，中期均线尚有压制）"
    else:
        trend = "均线尚未形成多头排列"
    macd_state = "DIF在DEA上方（多头）" if ind.get("dif") and ind.get("dea") and ind["dif"] > ind["dea"] else "DIF在DEA下方（空头）"
    rsi_state = "超买区(>70)" if (ind.get("rsi") or 0) > 70 else ("弱势区(<40)" if (ind.get("rsi") or 100) < 40 else "中性区域")
    vr = ind.get("vol_ratio")
    vol_state = "缩量" if vr and vr < 0.8 else ("放量" if vr and vr > 1.2 else "量能平稳")

    # 板块真实数据（当日涨跌幅/涨跌家数/领涨股）
    sector_line = ""
    try:
        from data import mysql_source as ms
        sec = next((s for s in ms.get_sector_rotation_mysql() if s["name"] == sector), None)
        if sec:
            leader = sec.get("leader") or "-"
            try:
                names = ms.get_stock_names([leader])
                leader = names.get(leader, leader)
            except Exception:
                pass
            sector_line = (f"- 板块联动：{sector} 当日 {sec['change_pct']:+.2f}%（{sec['trade_date']}），"
                           f"上涨{sec['up_count']}家/下跌{sec['down_count']}家，"
                           f"领涨股 {leader}（{sec.get('leader_change') or 0:+.2f}%）。")
    except Exception:
        pass
    if not sector_line:
        sector_line = f"- 板块联动：{sector}（本地板块库暂无该板块当日快照）。"

    # 最新一条真实新闻（标题+日期+来源）
    news_line = ""
    try:
        live = live_news.get_news_for_stock(code, name, limit=1)
        if live:
            n = live[0]
            news_line = f"- 最新消息：`{str(n.get('published_at',''))[:10]}` {n['title'][:48]} — *{n.get('source','')}*"
    except Exception:
        pass
    if not news_line:
        try:
            from data import mysql_source as ms
            loc = ms.get_news([code], since_days=90, limit=1)
            if loc:
                n = loc[0]
                news_line = f"- 最新资讯（本地库）：`{str(n.get('published_at',''))[:10]}` {n['title'][:48]} — *{n.get('source','')}*"
        except Exception:
            pass

    lines = [
        f"### {name}（{code}）· 技术面速评\n",
        f"> 板块：{sector} | 现价：¥{close} | 近20日：{ret20} | 技术评分：{score}/100\n",
        "**技术面解读**",
        f"- 趋势：{trend}。MA5={ma5}，MA20={ma20}，MA60={ma60}，现价{'站稳' if ind.get('close',0) >= (ind.get('ma20') or 0) else '低于'}20日线。",
        f"- 动量：MACD DIF={dif}、DEA={dea}、柱={macd}，{macd_state}；RSI={rsi}，处于{rsi_state}。",
        f"- 波动：布林上轨{boll_up}、下轨{boll_low}；量比{vol_ratio}（{vol_state}，较昨日）。",
        f"- 系统识别信号：{signals}。",
        "",
        "**逻辑与催化**",
        sector_line,
        news_line or "- 近3日快讯与资讯库均无该股消息，催化暂缺可验证线索。",
        "- 技术面是结果与资金行为的映射，需结合财报验证——基本面数据见「大师总结」价值派观点。",
        "",
        "**风险与证伪条件**",
        f"- 若跌破 MA20（{ma20}）且 MACD 转空，则转强形态失效；RSI 进入超买区后追高需谨慎。",
        f"- 当前近20日涨跌 {ret20}，波动放大时须严格控制仓位。",
        "",
        "> 本文基于公开行情、本地板块快照与新闻标题生成，为研究参考，不构成投资建议。",
    ]
    return "\n".join(lines)


def _yi(v) -> str:
    """元 → 亿元。"""
    try:
        return f"{float(v) / 1e8:.2f}亿"
    except Exception:
        return "N/A"


def _yoy(cur, prev) -> str | None:
    """同比百分比（带正负号），基期非正时返回 None。"""
    try:
        if prev and float(prev) > 0:
            return f"{(float(cur) / float(prev) - 1) * 100:+.1f}%"
    except Exception:
        pass
    return None


def _fundamental_digest(code: str, close=None) -> dict:
    """从本地 MySQL 取真实财报 + 研报一致预期，算好同比供圆桌引用。"""
    from data import mysql_source as ms
    out: dict = {"fin": None, "yoy": {}, "consensus": None, "ratings": [], "target": None}
    try:
        fin = ms.get_financial(code, limit=8)
        if fin:
            latest = fin[0]
            out["fin"] = latest
            # 找去年同期（同月日、年份-1），只比累计口径
            prev_date = f"{int(latest['report_date'][:4]) - 1}{latest['report_date'][4:]}"
            prev = next((f for f in fin if f["report_date"] == prev_date), None)
            if prev:
                for key in ("revenue", "net_profit", "roe", "gross_margin",
                            "debt_ratio", "operating_cashflow"):
                    out["yoy"][key] = _yoy(latest[key], prev[key]) if key in (
                        "revenue", "net_profit") else None
                # 比率类直接算百分点差
                for key in ("roe", "gross_margin", "debt_ratio"):
                    try:
                        out["yoy"][key + "_pct"] = f"{latest[key] - prev[key]:+.1f}pct"
                    except Exception:
                        pass
                # 现金流：绝对额变化更有意义
                try:
                    out["yoy"]["cashflow_delta"] = _yi(latest["operating_cashflow"] - prev["operating_cashflow"])
                except Exception:
                    pass
    except Exception:
        pass
    try:
        cons = ms.get_consensus(code, limit=8)
        out["consensus"] = next((c for c in cons if "一致预期" in c["broker"]), None)
        out["ratings"] = [c for c in cons if c["rating"]][:3]
        # 目标价合理性过滤：明显偏离现价的数据源异常值不展示（如 7.20 vs 现价 98）
        for c in cons:
            tp = c.get("target_price") or 0
            if tp > 0 and (not close or 0.5 * close <= tp <= 2.0 * close):
                out["target"] = (tp, c["broker"], c["report_date"])
                break
    except Exception:
        pass
    return out


def _council_content(stock: dict) -> str:
    """大师圆桌：三位角色各自引用真实财报 / K线 / 新闻数据发言。"""
    code, name = stock.get("code", ""), stock.get("name", "")
    ind = stock.get("indicators") or {}
    ret20 = _fmt(stock.get("ret20") if stock.get("ret20") else ind.get("ret20"), "%")
    rsi = _fmt(ind.get("rsi"))
    ma20 = _fmt(ind.get("ma20"))
    macd_dif = _fmt(ind.get("dif"))
    score = stock.get("score", "N/A")
    close = stock.get("close")
    live = live_news.get_news_for_stock(code, name, limit=5)
    news_hint = f"近3日有 {len(live)} 条相关资讯" if live else "近3日资讯曝光度低"

    f = _fundamental_digest(code, close)
    fin, yoy, cons = f["fin"], f["yoy"], f["consensus"]

    # 依据行：把财报期和机构预期都亮出来，用户可追溯
    basis = f"技术评分 {score}/100 | 近20日 {ret20} | RSI {rsi} | MA20 {ma20} | {news_hint}"
    if fin:
        basis += f" | 财报 {fin['report_date']}"
    if cons:
        basis += f" | {cons['broker']}"

    lines = [f"### {name}（{code}）· 大师圆桌\n", f"> 依据：{basis}\n"]

    # —— 价值投资者：只看财报和估值，用真实数字说话
    if fin:
        rev = f"营收{_yi(fin['revenue'])}"
        if yoy.get("revenue"):
            rev += f"（同比{yoy['revenue']}）"
        np_ = f"归母净利{_yi(fin['net_profit'])}"
        if yoy.get("net_profit"):
            np_ += f"（同比{yoy['net_profit']}）"
        gm = f"毛利率{_fmt(fin['gross_margin'])}%"
        if yoy.get("gross_margin_pct"):
            gm += f"（同比{yoy['gross_margin_pct']}）"
        value_view = f"看财报说话——{fin['report_date']} 报告期{rev}、{np_}、{gm}。"
        if yoy.get("roe_pct"):
            value_view += f"ROE {_fmt(fin['roe'])}%（{yoy['roe_pct']}）。"
        if cons and cons.get("eps_current"):
            pe = f"按现价 {_fmt(close)} 元对应今年 PE 约 {float(close) / cons['eps_current']:.0f} 倍" if close else ""
            value_view += f"{cons['broker']}给出的今年 EPS {cons['eps_current']:.2f} 元、明年 {cons['eps_next']:.2f} 元，{pe}。"
        if f["target"]:
            tp = f["target"][0]
            rel = f"，较现价 {(tp / close - 1) * 100:+.0f}%" if close else ""
            value_view += f"{f['target'][1]}目标价 {tp:.2f} 元{rel}（{f['target'][2]}）。"
        if not cons and not f["target"]:
            value_view += "本地库暂无该股的研报一致预期，估值缺乏锚点。"
    else:
        value_view = "本地财务表暂无该股数据，缺少盈利与估值依据——我不下结论。"
    lines += ["**价值投资者**：" + value_view, ""]

    # —— 趋势交易者：只用价格和指标
    trend = f"我尊重价格。近20日{ret20}，RSI {rsi}。"
    if ind.get("dif") is not None:
        trend += f"MACD DIF {macd_dif}"
        if ind.get("dea") is not None:
            trend += f" / DEA {_fmt(ind.get('dea'))}"
        trend += "。"
    trend += f"（{','.join(stock.get('signals', [])[:2]) or '无明显技术信号'}）。"
    trend += f"只要不失守 MA20（{ma20}），趋势就还在；破位我走，不含糊。"
    lines += ["**趋势交易者**：" + trend, ""]

    # —— 风险控制官：盯现金流、负债、消息面
    risk = ""
    if fin:
        cf = fin.get("operating_cashflow") or 0
        dr = fin.get("debt_ratio") or 0
        risk += f"财报里最刺眼的是现金流：经营现金流{_yi(cf)}"
        if yoy.get("cashflow_delta") and cf < 0:
            risk += f"（同比多流出{yoy['cashflow_delta'].lstrip('-')}）"
        risk += f"，资产负债率{_fmt(dr)}%"
        if yoy.get("debt_ratio_pct"):
            risk += f"（{yoy['debt_ratio_pct']}）"
        risk += "。利润表好看不等于现金回来了。"
    if f["ratings"]:
        risk += f"机构评级：{', '.join(r['broker'] + '·' + r['rating'] for r in f['ratings'][:2])}。"
    risk += news_hint + "。消息面若是短期脉冲，技术信号会很快反转——仓位比对错更重要。"
    lines += ["**风险控制官**：" + risk, ""]

    # —— 主持人：把分歧落到具体数字
    if fin:
        summary = (f"三方分歧落在同一组数字上：净利同比{yoy.get('net_profit', 'N/A')}、"
                   f"毛利率{'改善' if 'gross_margin_pct' in yoy and not yoy['gross_margin_pct'].startswith('-') else '走弱'}，"
                   f"但经营现金流{_yi(fin.get('operating_cashflow'))}。"
                   f"价值派看半年以上，趋势派看 MA20（{ma20}），风控盯现金与回撤。")
    else:
        summary = "三方分歧本质是周期不同：价值看半年以上，趋势看数周，风控看回撤。"
    summary += f"短线可跟趋势但设好止损，长线需回到财报验证。最终决策与责任在投资者本人。"
    lines += ["**主持人总结**：" + summary, "",
              "> 财务数据来自本地财报库，行情来自本地日线，新闻来自实时快讯+资讯库；均为研究参考，不构成投资建议。"]
    return "\n".join(lines)


def _arti_placeholder(stock: dict) -> str:
    """ARTi 独立研报 tab：本地不产出长文，只给一句引导；正文完全由 ARTi 联网生成后经轮询补齐。"""
    name = stock.get("name", "") or stock.get("code", "")
    return (f"> **{name} · ARTi 独立研报**\n>\n"
            "> 本页正文由 ARTi 联网检索实时数据后生成，正在获取中（通常 10~40 秒）。"
            "若服务端波动会显示重试按钮，可点「重试解读」重新获取。")


LOCAL_ENGINE = {
    "news": _news_content,
    "report": _report_content,
    "council": _council_content,
    "arti": _arti_placeholder,
}


def _arti_text(s: str) -> str:
    """清洗喂给 ARTi 的文本：把会被实体识别误判为证券代码的缩写/纯数字换成中文表述。

    实测 ARTi 会把 MACD/DIF/MA20 以及 6 位数字代码当成证券实体，
    返回“对应多个证券”拒答或 502。这里统一改写，保留可读的中文语义。
    """
    s = str(s or "")
    # 指标缩写 → 中文（长缩写先替换，避免 MA5 抢先命中 MA50 之类）
    for abbr, cn in (
        ("MACD", "动能指标"), ("DIF", "快线"), ("DEA", "慢线"),
        ("RSI", "强弱指标"), ("KDJ", "随机指标"), ("BOLL", "布林带"),
        ("MA60", "中期均线"), ("MA20", "短期均线"), ("MA10", "10日均线"), ("MA5", "5日均线"),
    ):
        s = s.replace(abbr, cn)
    # 6 位纯数字股票代码 → 去掉（股票已用中文名表达）
    s = re.sub(r"\b\d{6}\b", "", s)
    # 去 markdown 符号，减少噪声
    s = s.replace("#", "").replace("*", "").replace("`", "")
    return s


def _build_prompt(stock: dict, sector: str, kind: str, context: str = "") -> str:
    """构造喂给 ARTi 的 prompt——极简，只用「股票名 + 6位代码」。

    实测结论（2026-11-24）：
      - 之前把代码删掉、只留"一家中国A股上市公司"+ 堆一堆数据的写法，
        服务端稳定返回 502 或超时（"中国公司"等组合是触发点）。
      - 直接把证券代码交给 ARTi，它自己识别标的并联网取数，稳定返回
        2000~3000 字真实研报；带 6 位代码是识别关键，纯中文名反而超时。
    所以这里只问一句，artifin 自己认股票、自己查数据，不再塞本地素材。
    """
    code = str(stock.get("code", "") or "")
    name = str(stock.get("name", "") or "")
    if code:
        subject = f"{name}（{code}）" if name else code
    else:
        subject = name or "这只股票"
    asks = {
        "news": f"你怎么看{subject}？请结合最新消息面分析。",
        "report": f"你怎么看{subject}？请结合技术面和基本面写一份投研速评。",
        "council": f"你怎么看{subject}？请让价值投资者、趋势交易者、风险控制官三位角色发表观点并综合。",
        "arti": f"你怎么看{subject}？请做一份全面、独立的投研报告，覆盖基本面、消息面、技术面、估值与风险。",
    }
    return asks.get(kind, f"你怎么看{subject}？")


def _enrich_worker(cache_key: str, enriched: dict, sector: str, kind: str, context: str) -> None:
    """后台线程：调用 ARTi，成功则把解读写入独立缓存。前端通过 /arti 接口轮询。

    ARTi 服务端 502/超时是随机波动（同一 prompt 时通时不通），所以：
      - 用极简 prompt（股票名+代码），不带 system，不带堆砌数据；
      - 内部最多重试 3 次（间隔 2/4 秒），提高单次提交的成功概率；
      - 每次尝试都更新 attempts/reason，前端能实时看到"正在第N次重试"。
    """
    import time as _time
    last_reason = ""
    for attempt in range(1, 4):
        try:
            c = ARTIClient(mock=False, max_retries=0, timeout=40.0)
            ai = c.chat(_build_prompt(enriched, sector, kind, context), model="chat", fallback=False)
            # 拒答/脏数据不落缓存
            if ai and len(ai) > 80 and "对应多个证券" not in ai and "请确认" not in ai[:20]:
                _set_cached(cache_key, ai.strip(), "arti")
                with _arti_lock:
                    _arti_fail.pop(cache_key, None)   # 成功，清掉历史失败
                print(f"[ARTi增强] {enriched.get('code')} {kind} 完成 ({len(ai)}字, 第{attempt}次)")
                return
            last_reason = f"返回无效内容：{str(ai)[:40]}" if ai else "返回为空"
        except Exception as e:
            last_reason = f"{type(e).__name__}: {str(e)[:50]}"
        with _arti_lock:
            _arti_fail[cache_key] = {"reason": last_reason, "attempts": attempt, "running": True}
        print(f"[ARTi增强] {enriched.get('code')} {kind} 第{attempt}次失败: {last_reason}")
        if attempt < 3:
            _time.sleep(2 * attempt)  # 2s / 4s 退避，避开服务端抖动窗口
    with _arti_lock:
        _arti_fail[cache_key] = {"reason": last_reason, "attempts": 3, "running": False}
        _arti_running.discard(cache_key)


def arti_status(code: str, kind: str) -> dict:
    """供前端轮询：增强是否完成 / 是否失败及原因。cache_key 与主分析绑定同一 stock/kind。"""
    key = f"arti:{code}:{kind}:v1"
    cached = _get_cached(key, max_age_min=ARTI_CACHE_DAYS * 24 * 60)
    if cached:
        return {"ready": True, "content": cached, "running": False, "failed": False}
    with _arti_lock:
        running = key in _arti_running
        fail = dict(_arti_fail.get(key, {}))
    return {
        "ready": False, "running": running, "content": "",
        "failed": bool(fail) and not running,
        "reason": fail.get("reason", ""), "attempts": fail.get("attempts", 0),
    }


def _submit_enrich(code: str, kind: str, enriched: dict, sector: str, context: str) -> bool:
    """提交一个后台增强任务（去重：同一 code/kind 同时只跑一个）。返回是否新提交。"""
    c = get_client()
    if c.mock:
        return False
    arti_key = f"arti:{code}:{kind}:v1"
    with _arti_lock:
        if arti_key in _arti_running:
            return False
        _arti_running.add(arti_key)
    _arti_pool.submit(_enrich_worker, arti_key, dict(enriched), sector, kind, context)
    return True


def analyze_stock(
    stock: dict,
    sector: str,
    kind: str,
    client: ARTIClient | None = None,
    force: bool = False,
) -> dict:
    """生成某类分析。本地结果秒回；ARTi 增强在后台进行，可由 /arti 接口轮询补齐。

    返回 {kind, label, content, model, cached, source, arti_pending}
    """
    if kind not in PROMPTS:
        return {"kind": kind, "content": "未知分析类型", "cached": False}

    code = stock.get("code", "")
    cache_key = f"{code}:{kind}:v3"
    if force:
        # 用户主动重算：清掉旧的失败诊断，让状态从零开始
        with _arti_lock:
            _arti_fail.pop(f"arti:{code}:{kind}:v1", None)
    if not force:
        cached = _get_cached(cache_key)
        if cached:
            st = arti_status(code, kind)
            content = cached
            source = "本地数据(缓存)"
            if st["ready"] and "### ARTi 智能解读" not in cached:
                content = cached + "\n\n---\n\n### ARTi 智能解读\n\n" + st["content"]
                source = "本地数据 + ARTi(缓存)"
            # 关键修复：本地缓存命中但增强未成功时，若没有任务在跑，重新提交后台增强。
            # 之前命中即直接返回，导致首次失败后永远不再重试——这是"只有一只股票有解读"的主因。
            if not st["ready"] and not st["running"]:
                _submit_enrich(code, kind, stock, sector, cached.replace("#", "").replace("*", "")[:1800])
            return {
                "kind": kind, "label": LABELS[kind], "content": content,
                "model": "local", "cached": True, "source": source,
                "arti_pending": not st["ready"],
            }

    # 构造带指标信息的 stock 副本
    enriched = dict(stock)
    enriched["sector"] = sector
    try:
        from data import get_kline, add_indicators
        df = add_indicators(get_kline(code, days=120))
        if len(df):
            last = df.iloc[-1]
            prev = df.iloc[-2] if len(df) > 1 else last
            enriched["indicators"] = {
                "ma5": last.get("ma5"), "ma20": last.get("ma20"), "ma60": last.get("ma60"),
                "dif": last.get("dif"), "dea": last.get("dea"), "macd": last.get("macd"),
                "rsi": last.get("rsi"),
                "boll_up": last.get("boll_up"), "boll_low": last.get("boll_low"),
                "vol_ratio": (last.get("volume") / prev.get("volume")) if prev.get("volume") else None,
                "close": last.get("close"),
            }
            enriched["close"] = last.get("close", enriched.get("close"))
            if len(df) > 20:
                p = float(df.iloc[-21]["close"])
                c = float(last["close"])
                enriched["ret20"] = round((c / p - 1) * 100, 2) if p else 0
    except Exception:
        pass

    # 本地数据引擎（始终生成，作为立即可见的 base）—— 全部计算 <1s
    try:
        content = LOCAL_ENGINE[kind](enriched)
        source = "本地数据引擎"
    except Exception as e:
        content = f"> 本地分析生成失败（{type(e).__name__}: {str(e)[:100]}）"
        source = "错误"

    _set_cached(cache_key, content, "local")

    # ARTi 增强：提交后台线程，不阻塞响应
    c = client or get_client()
    pending = False
    if not c.mock:
        st = arti_status(code, kind)
        if st["ready"]:
            # 已有增强缓存，直接拼上
            content = content + "\n\n---\n\n### ARTi 智能解读\n\n" + st["content"]
            source = "本地数据 + ARTi"
        else:
            context = content.replace("#", "").replace("*", "")[:1800]
            submitted = _submit_enrich(code, kind, enriched, sector, context)
            # 已在跑也算 pending，让前端继续轮询
            pending = submitted or st["running"]

    return {
        "kind": kind, "label": LABELS[kind], "content": content,
        "model": "local" if c.mock else "local+arti(异步)", "cached": False,
        "source": source, "arti_pending": pending,
    }


def analyze_all(stock: dict, sector: str, client: ARTIClient | None = None) -> dict:
    # arti 是独立的纯 ARTi 联网研报 tab（本地仅为占位文案），不属于三维本地分析，不纳入 all
    return {kind: analyze_stock(stock, sector, kind, client)
            for kind in PROMPTS if kind != "arti"}
