"""
持仓去留诊断（ARTi 驱动 + 本地数据兜底）
=========================================
输入用户"我的持仓"里的一只异动股，喂给 ARTi 三样东西：
  1. 股票名称 / 代码
  2. 目前股票情况（现价、涨跌、技术评分、五维、近20日、板块联动、消息面、基本面）
  3. 持仓移动情况（这次触发的每一条异动 + 成本 / 盈亏 + 均线关键位）

输出一份明确的"该不该平仓"投资建议：结论(持有/减仓/平仓/加仓) + 理由 + 关键价位 + 操作要点 + 风险与证伪条件。

设计要点：
  - system prompt 要求 ARTi 只依据提供的数据、给明确结论、不得骑墙，中文 400 字内
  - 无 ARTI key（mock 模式）时，本地数据引擎同样产出一份可追溯规则的持仓研判，明确标注"本地规则"
  - 缓存 key 绑定"交易日+现价+盈亏+异动信号"，持仓状态一变就自动失效，避免旧结论误导
"""

from __future__ import annotations

from datetime import datetime

from data.arti_client import ARTIClient, get_client
from analysis import ai_analysis as _ai

# 注意：ARTi 对过长的 system prompt 偶发 502，这里保持简短；研判框架交给模型自身。
SYSTEM = "你是A股持仓研究助手。针对持仓给出该不该平仓的明确研判。仅输出四部分：结论(持有/减仓/平仓选一)、理由、关键价位、风险。全文250字内，中文，不要分一级标题。不构成投资建议。"


def _condense_arti(text: str) -> str:
    """从 ARTi 的完整研判里提取精简版：结论段 + 关键价位表/价位句。

    ARTi 自带固定模板且忽略长度要求，返回常达2000+字。这里按二级标题
    拆分，只保留「结论/研判」与「价位/风险」相关段落，去掉冗长的
    「关键数据」「简要分析」中间段，让用户一眼看到去留结论。
    """
    import re
    if not text or len(text) < 600:
        return text or ""
    # 按 markdown 标题切段
    parts = re.split(r"(?=^#{1,3}\s)", text, flags=re.M)
    keep, skip_kw = [], ("关键数据", "简要分析", "数据来源")
    for seg in parts:
        head = seg.strip().split("\n", 1)[0]
        if any(k in head for k in skip_kw):
            continue
        keep.append(seg.strip())
    cond = "\n\n".join(p for p in keep if p)
    # 开头"本次按A股…分析"这类套话去掉
    cond = re.sub(r"^本次按[^。\n]*。\s*", "", cond).strip()
    # 仍过长则硬截断到 700 字，保留结论完整性
    if len(cond) > 700:
        cond = cond[:700].rstrip() + "…\n\n> 内容较长，点「完整」查看 ARTi 全文。"
    return cond or text[:500]


def _fmt(v, suffix="", digits=2) -> str:
    try:
        if v is None:
            return "N/A"
        return f"{float(v):.{digits}f}{suffix}"
    except Exception:
        return "N/A"


def _current_state(holding: dict) -> str:
    """拼装'目前股票情况'：技术评分 + 五维 + 板块 + 消息面 + 基本面。"""
    code = holding.get("code", "")
    name = holding.get("name", code)
    lines = [
        f"{name}（{code}）现价 ¥{_fmt(holding.get('close'))}，"
        f"当日 {holding.get('change_pct', 0):+.2f}%，"
        f"近20日 {holding.get('ret20', 0):+.2f}%，"
        f"量比(对5日均量) {_fmt(holding.get('vol_ratio'), '倍')}。",
        f"技术评分 {holding.get('score', 'N/A')}/100，系统信号："
        f"{'、'.join(holding.get('signals', [])) or '无明显信号'}。",
    ]
    dims = holding.get("dims") or {}
    if dims:
        lines.append(
            "五维得分：" + " ".join(f"{k}{v}" for k, v in dims.items()) + "（趋势30/动量25/量能20/位置15/超跌10）。"
        )
    # 板块联动
    if holding.get("sector"):
        lines.append(
            f"所属板块 {holding['sector']} 当日 {holding.get('sector_change', 0):+.2f}%，"
            f"个股{holding.get('sector_relation', '表现')}。"
        )
    # 真实消息面（最多2条标题）
    try:
        from data import live_news
        news = live_news.get_news_for_stock(code, name, limit=2)
        if news:
            lines.append("近期消息：" + "；".join(
                f"{str(n.get('published_at',''))[:10]} {n['title'][:34]}" for n in news
            ))
    except Exception:
        pass
    # 真实基本面（营收/净利同比 + 研报目标价）
    try:
        f = _ai._fundamental_digest(code, holding.get("close"))
        fin, yoy = f["fin"], f["yoy"]
        if fin:
            seg = f"财报 {fin['report_date']}：营收{_ai._yi(fin['revenue'])}"
            if yoy.get("revenue"):
                seg += f"(同比{yoy['revenue']})"
            seg += f"，归母净利{_ai._yi(fin['net_profit'])}"
            if yoy.get("net_profit"):
                seg += f"(同比{yoy['net_profit']})"
            lines.append(seg + "。")
        if f["target"]:
            tp, brk, dt = f["target"]
            rel = f"，较现价{(tp / float(holding.get('close') or tp) - 1) * 100:+.0f}%" if holding.get("close") else ""
            lines.append(f"{brk}目标价 {_fmt(tp)} 元{rel}（{dt}）。")
    except Exception:
        pass
    return "\n".join(lines)


def _movement_state(holding: dict, key_levels: dict) -> str:
    """拼装'持仓移动情况'：每条异动 + 成本盈亏 + 关键价位。"""
    lines = ["【这次持仓移动】"]
    for c in holding.get("changes", []):
        lines.append(f"- {c.get('text','')}：{c.get('detail','')}")
    cost = holding.get("cost")
    if cost:
        lines.append(
            f"- 持仓成本 ¥{_fmt(cost)}，当前盈亏 {holding.get('pnl_pct', 0):+.2f}%。"
        )
    else:
        lines.append("- 未登记成本价，无法计算精确盈亏——给价位建议时以均线为准。")
    if key_levels:
        lines.append(
            f"- 关键价位（本地真实K线）：MA20 ¥{_fmt(key_levels.get('ma20'))}，"
            f"MA60 ¥{_fmt(key_levels.get('ma60'))}，"
            f"近20日最高 ¥{_fmt(key_levels.get('high20'))}，"
            f"近20日最低 ¥{_fmt(key_levels.get('low20'))}。"
        )
    return "\n".join(lines)


def _key_levels(code: str) -> dict:
    """从本地K线取真实均线/区间高低，作为止损止盈锚点。"""
    try:
        from data import get_kline, add_indicators
        df = add_indicators(get_kline(code, days=120))
        if df is None or len(df) < 21:
            return {}
        last = df.iloc[-1]
        return {
            "ma20": float(last.get("ma20", 0)) or 0,
            "ma60": float(last.get("ma60", 0)) or 0,
            "high20": float(df["high"].iloc[-20:].max()),
            "low20": float(df["low"].iloc[-20:].min()),
        }
    except Exception:
        return {}


def _local_advice(holding: dict, key_levels: dict) -> str:
    """无 ARTi key 时，基于真实数据给一份规则化持仓研判（明确标注本地规则）。"""
    name = holding.get("name", holding.get("code", ""))
    chg = holding.get("change_pct", 0) or 0
    ret20 = holding.get("ret20", 0) or 0
    score = holding.get("score", 0) or 0
    pnl = holding.get("pnl_pct")
    ma20 = key_levels.get("ma20") or 0
    close = float(holding.get("close") or 0)

    # 规则结论：先按"技术评分 + 盈亏 + 异动方向"判倾向
    if close and ma20 and close < ma20 and chg < 0:
        verdict, why = "减仓 / 平仓离场", "现价已跌破MA20且当日下跌，短期趋势转弱信号明确"
    elif ret20 <= -10 and score < 45:
        verdict, why = "减仓", "近20日跌幅较深且技术评分偏低，弱势未扭转"
    elif pnl is not None and pnl >= 15 and chg < 0:
        verdict, why = "减仓锁利", "持仓已有较厚浮盈但当日转跌，宜保护利润"
    elif score >= 60 and close and ma20 and close >= ma20:
        verdict, why = "继续持有", "技术评分较高且现价站在MA20上方，趋势未破"
    elif score >= 50:
        verdict, why = "继续持有（盯效）", "技术评分中性偏强，但需盯住关键均线"
    else:
        verdict, why = "观望 / 减仓", "技术评分偏低、缺乏明确转强信号，控制仓位为主"

    # 只取最重要的2条异动，每条截断并清理尾部标点，避免铺开和"。；"叠标点
    import re
    def _clip(s: str, n: int = 55) -> str:
        s = re.sub(r"\s+", " ", str(s or "")).strip().rstrip("。；;，, ")
        return s[:n].rstrip("，,、 ") + "…" if len(s) > n else s
    moves = holding.get("changes", [])[:2]
    move_text = "；".join(
        f"{_clip(c.get('text',''),20)}：{_clip(c.get('detail',''),55)}" for c in moves
    ) or "本次无明显异动"
    lines = [
        f"### {name} · 持仓去留：{verdict}\n",
        f"> 现价 ¥{_fmt(close)} · 当日 {chg:+.2f}% · 近20日 {ret20:+.2f}%"
        + (f" · 盈亏 {pnl:+.2f}%" if pnl is not None else "") + "\n",
        f"**为什么**：{move_text}。{why}。",
        f"**怎么做**：跌破 MA20（¥{_fmt(ma20)}）先减半仓，站稳则维持；反弹乏力可分批止盈。",
    ]
    watch = []
    if ma20:
        watch.append(f"MA20 ¥{_fmt(ma20)}")
    if holding.get("cost"):
        watch.append(f"成本 ¥{_fmt(holding['cost'])}")
    lines.append("**盯什么**：" + (" / ".join(watch) if watch else "关键均线") + "。")
    lines.append("\n> 依据真实行情与持仓规则研判，仅供参考，不构成投资建议。")
    return "\n".join(lines)


def advise_position(
    holding: dict,
    client: ARTIClient | None = None,
    force: bool = False,
) -> dict:
    """对一只异动持仓给出'该不该平仓'的投资建议。

    holding: /api/brief 返回的单只异动股（含 code/name/close/changes/cost 等）
    返回 {code, name, content, model, source, cached, generated_at}
    """
    code = holding.get("code", "")
    name = holding.get("name", code)
    close = holding.get("close")
    pnl = holding.get("pnl_pct")
    chg = holding.get("change_pct")
    sig = "|".join(c.get("text", "") for c in holding.get("changes", []))
    # 缓存绑定当前状态：交易日 + 现价 + 盈亏 + 异动信号，任一变化即失效
    today = datetime.now().strftime("%Y%m%d")
    key = f"position:v3:{code}:{today}:{close}:{pnl}:{sig}"

    if not force:
        cached = _ai._get_cached(key, max_age_min=120)
        if cached:
            return {
                "code": code, "name": name,
                "content": _condense_arti(cached), "full_content": cached,
                "model": "local", "source": "持仓诊断(缓存)", "cached": True,
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            }

    key_levels = _key_levels(code)
    c = client or get_client()

    # 本地规则引擎作为 base 和失败兜底（ARTi 模式不必算，省一次K线查询）
    if c.mock:
        content = _local_advice(holding, key_levels)
        source, model = "本地规则引擎", "local"
    else:
        # 精简 prompt：只给持仓核心事实。ARTi 自带行情研判能力，
        # 喂太多技术指标会触发它的实体识别误判(把MA/MACD当证券)或后端502。
        chg = holding.get("change_pct", 0)
        # 关键：ARTi 实体识别会把 MACD/DIF 等缩写当证券代码，触发502或拒答。
        # 异动标签全部改写成中文描述，且不出现任何大写指标缩写。
        move_map = {
            "MACD金叉": "动能转强", "MACD死叉": "动能转弱",
            "上穿20日线": "站上短期均线", "跌破20日线": "跌破短期均线",
            "站上60日线": "站上中期均线", "跌破60日线": "跌破中期均线",
        }
        raw_moves = [x.get("text", "") for x in holding.get("changes", [])[:2]]
        moves = "|".join(move_map.get(m, m) for m in raw_moves)
        for abbr, cn in (("MACD", "动能"), ("RSI", "强弱"), ("KDJ", "随机"),
                         ("BOLL", "布林"), ("MA20", "短期均线"), ("MA60", "中期均线")):
            moves = moves.replace(abbr, cn)
        pnl_part = f"，持仓盈亏{pnl:+.2f}%" if pnl is not None else ""
        prompt = (
            f"请研判 {name}（{code}）该不该平仓。"
            f"现价{holding.get('close')}元，当日{chg:+.2f}%，"
            f"近20日{holding.get('ret20', 0):+.2f}%{pnl_part}。"
            + (f"今日异动：{moves}。" if moves else "")
            + "请给出明确结论和关键价位。"
        )
        content, source, model = None, "本地规则引擎", "local"
        # 临时客户端：ARTi 响应常在30秒以上，timeout 需留足；3次重试应对偶发502
        probe = ARTIClient(api_key=c.api_key, mock=False, max_retries=1, timeout=120.0)
        for attempt in range(3):
            try:
                ai = probe.chat(prompt, model="chat", system=SYSTEM)
                if ai and len(ai) > 60 and "对应多个证券" not in ai:
                    content = ai
                    source, model = "ARTi arti-chat", "arti-chat"
                    break
            except Exception:
                pass
        if content is None:
            content = _local_advice(holding, key_levels) + "\n\n> ARTi 暂时不可用，以上为本地规则研判。"

    _ai._set_cached(key, content, model)
    # ARTi 全文较长，另给一份精简版供默认展示
    condensed = _condense_arti(content) if model == "arti-chat" else content
    return {
        "code": code, "name": name,
        "content": condensed,        # 前端默认展示精简版
        "full_content": content,     # 展开时看 ARTi 全文
        "model": model, "source": source, "cached": False,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
