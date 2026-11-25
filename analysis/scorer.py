"""
技术选股引擎
============
对每个板块的成分股做多维技术打分，选 3-5 只，并输出可解释信号。

评分维度（总分100）：
  - 趋势 (30)：均线多头排列、价格站上MA20/MA60
  - 动量 (25)：MACD金叉/红柱放大、RSI处于强势区
  - 量能 (20)：近期放量（量比>1）
  - 位置 (15)：布林带位置、距近期高低点
  - 超跌反弹 (10)：KDJ低位金叉

设计理念：
  - 不是"预测涨跌"，而是"识别技术形态+给出可解释信号"
  - 每只股票附带触发的信号标签，让用户看得懂为什么被选中
  - 明确风险提示，不构成投资建议
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

from data import get_sector_constituents, get_kline, add_indicators


def _vol_ratio(df: pd.DataFrame) -> float:
    """当日量比 = 当日成交量 / 前5日均量。"""
    vol = float(df["volume"].iloc[-1])
    base = df["volume"].iloc[-6:-1].mean() if len(df) >= 6 else vol
    return vol / float(base) if float(base) > 0 else 1.0


def _rank_percentile(values: list[float]) -> list[float]:
    """把一组数值转成 0~1 分位（越大越强），并列取平均名次。"""
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [0.5]
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg_rank = (i + j) / 2
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank / (n - 1)
        i = j + 1
    return ranks


def _clean_kline(df: pd.DataFrame) -> pd.DataFrame | None:
    """清洗K线，剔除脏数据，返回可用于评分的干净副本。

    两类脏数据（本地库实测存在）：
      1) 停牌僵尸股：近20日成交量几乎全0、收盘价连续多日完全相同 → 整只剔除，
         否则会算出 RSI=100、20日0%、均线1.0>0.3 的虚假高分。
      2) 当日占位行：最后一行 volume=0 且价格=前收（日线未更新完）→ 截掉，
         用最新有成交的交易日做评分，避免"今天0成交"污染量比/涨跌。
    """
    if df is None or len(df) < 30:
        return None
    d = df.copy()
    vol = pd.to_numeric(d["volume"], errors="coerce").fillna(0)

    # —— 闸门1：停牌僵尸股 ——
    recent = d.iloc[-20:]
    zero_days = int((pd.to_numeric(recent["volume"], errors="coerce").fillna(0) == 0).sum())
    close_arr = pd.to_numeric(recent["close"], errors="coerce")
    # 最近连续完全相同收盘价的天数
    same_streak = 1
    for i in range(len(close_arr) - 1, 0, -1):
        if abs(float(close_arr.iloc[i]) - float(close_arr.iloc[i - 1])) < 1e-9:
            same_streak += 1
        else:
            break
    if zero_days >= 18 or same_streak >= 10:
        return None

    # —— 闸门2：当日占位行 ——
    if len(d) >= 2:
        last, prev = d.iloc[-1], d.iloc[-2]
        last_vol = float(vol.iloc[-1])
        unchanged = abs(float(last["close"]) - float(prev["close"])) < 1e-9
        if last_vol == 0 and unchanged:
            d = d.iloc[:-1].reset_index(drop=True)

    if len(d) < 30 or float(d["close"].iloc[-1]) <= 0:
        return None
    return d


def _load_one(c: dict) -> tuple[dict, pd.DataFrame] | None:
    """并发加载单只股票K线（评分在主线程做，避免重复拉数据）。"""
    try:
        raw = get_kline(c["code"], days=120)
        clean = _clean_kline(raw)
        if clean is None:
            return None
        df = add_indicators(clean)
        if df is None or len(df) < 30:
            return None
        return c, df
    except Exception as e:
        print(f"[选股] {c['code']} K线加载失败: {type(e).__name__}")
        return None


def score_stock(df: pd.DataFrame, vol_percentile: float | None = None) -> dict:
    """对单只股票的技术面打分。vol_percentile 为板块内量比分位(0~1)。"""
    """对单只股票的技术面打分。df 需含 add_indicators 后的所有列。"""
    if df is None or len(df) < 30:
        return {"total": 0, "signals": [], "dims": {}, "recommend": False}

    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else last
    signals: list[str] = []
    dims: dict[str, float] = {}

    close = float(last["close"])
    ma5, ma10, ma20, ma60 = (float(last[f"ma{n}"]) for n in (5, 10, 20, 60))

    # --- 趋势 (30) ---
    trend = 0.0
    if ma5 > ma10 > ma20:
        trend += 12
        # MA20 仍在 MA60 下方时只是短期转强，不能叫"多头排列"（与报告口径统一）
        signals.append("均线多头排列" if ma20 > ma60 else "短期均线转强")
    elif ma5 > ma20:
        trend += 6
    if close > ma20:
        trend += 10
        signals.append("站上20日线")
    if close > ma60:
        trend += 8
    dims["趋势"] = min(30.0, trend)

    # --- 动量 (25) ---
    mom = 0.0
    dif, dea, macd = float(last["dif"]), float(last["dea"]), float(last["macd"])
    prev_dif, prev_dea = float(prev["dif"]), float(prev["dea"])
    if prev_dif <= prev_dea and dif > dea:
        mom += 15
        signals.append("MACD金叉")
    elif dif > dea and macd > 0:
        mom += 10
        signals.append("MACD红柱")
    rsi = float(last["rsi"])
    if 50 <= rsi <= 70:
        mom += 10
        signals.append("RSI强势区")
    elif rsi > 70:
        mom += 4
        signals.append("RSI超买")
    dims["动量"] = min(25.0, mom)

    # --- 量能 (20)：优先板块内相对分位，单股场景退回绝对阈值 ---
    vol_ratio = _vol_ratio(df)
    if vol_percentile is not None:
        pct = min(1.0, max(0.0, float(vol_percentile)))
        dims["量能"] = round(2 + 18 * pct, 1)  # 2~20，保证每天都能拉开差距
        signals.append("板块量能前30%" if pct >= 0.7 else "板块量能后30%" if pct < 0.3 else "量能居中")
    elif vol_ratio >= 1.5:
        dims["量能"] = 20.0
        signals.append(f"放量{vol_ratio:.1f}倍")
    elif vol_ratio >= 1.2:
        dims["量能"] = 13.0
        signals.append("温和放量")
    else:
        dims["量能"] = 5.0

    # --- 位置 (15) ---
    boll_up, boll_low, boll_mid = (float(last[c]) for c in ("boll_up", "boll_low", "boll_mid"))
    boll_pos = (close - boll_low) / (boll_up - boll_low) if boll_up > boll_low else 0.5
    pos = 0.0
    if boll_mid < close <= boll_up:
        pos += 8
        signals.append("布林中上轨")
    hi20 = df["high"].iloc[-20:].max()
    if close >= hi20 * 0.95:
        pos += 7
        signals.append("接近20日高点")
    dims["位置"] = min(15.0, pos)

    # --- 超跌反弹 (10) ---
    k, d = float(last["k"]), float(last["d"])
    prev_k, prev_d = float(prev["k"]), float(prev["d"])
    if prev_k <= prev_d and k > d and k < 50:
        dims["超跌反弹"] = 10.0
        signals.append("KDJ低位金叉")
    else:
        dims["超跌反弹"] = 0.0

    total = round(sum(dims.values()), 1)
    # 近20日涨跌幅
    base20 = float(df["close"].iloc[-21]) if len(df) >= 21 else 0.0
    ret20 = (close / base20 - 1) * 100 if base20 > 0 else 0.0

    # 差异化标签：每个都带真实数值，避免所有股票都显示同一组共性信号
    if ma5 > ma20 > ma60:
        trend_tag = f"多头排列 MA20{ma20:.1f}>MA60{ma60:.1f}"
    elif ma5 > ma20:
        trend_tag = f"短期转强 MA20{ma20:.1f}<MA60{ma60:.1f}"
    else:
        trend_tag = f"均线偏弱 MA5{ma5:.1f}<MA20{ma20:.1f}"
    if vol_percentile is not None:
        pct = min(1.0, max(0.0, float(vol_percentile)))
        vol_tag = f"量比{vol_ratio:.2f}·板块前{(1-pct)*100:.0f}%"
    else:
        vol_tag = f"{'放量' if vol_ratio >= 1.2 else '缩量' if vol_ratio < 0.8 else '平量'}{vol_ratio:.2f}倍"
    hi_gap = (close / hi20 - 1) * 100 if hi20 else 0
    pos_tag = f"距20日高{hi_gap:+.1f}%"
    highlights = [trend_tag, vol_tag, f"RSI{rsi:.1f}", pos_tag, f"20日{ret20:+.1f}%"]

    return {
        "total": total,
        "signals": signals[:5],  # 最多5个信号标签
        "highlights": highlights,
        "dims": {k: round(v, 1) for k, v in dims.items()},
        "recommend": total >= 55,
        "close": close,
        "change_pct": round((close / float(prev["close"]) - 1) * 100, 2) if float(prev["close"]) else 0,
        "ret20": round(ret20, 2),
        "rsi": rsi,
        "source": str(last.get("source", "")),
    }


def pick_sector_stocks(sector: str, top_n: int = 4) -> dict:
    """为一个板块挑选 top_n 只技术面最优股票（并发拉K线 + 板块内量能分位）。"""
    cons = get_sector_constituents(sector)[:12]
    # 阶段一：并发把K线一次拉齐，评分在主线程做，避免重复请求
    loaded: list[tuple[dict, pd.DataFrame]] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for f in as_completed([pool.submit(_load_one, c) for c in cons]):
            item = f.result()
            if item:
                loaded.append(item)
    # 阶段二：按板块内量比排名算分位，让量能维度每天都有区分度
    pcts = _rank_percentile([_vol_ratio(df) for _, df in loaded])
    results = []
    for (c, df), pct in zip(loaded, pcts):
        try:
            scored = score_stock(df, vol_percentile=pct)
            if not scored["total"]:
                continue
            results.append({
                "code": c["code"], "name": c["name"], "score": scored["total"],
                "signals": scored["signals"], "highlights": scored.get("highlights", []),
                "dims": scored["dims"], "close": scored["close"],
                "change_pct": c.get("change_pct") or scored["change_pct"],
                "ret20": scored["ret20"], "rsi": scored["rsi"],
                "kline_source": scored["source"],
            })
        except Exception as e:
            print(f"[选股] {c['code']} 评分失败: {type(e).__name__}")

    results.sort(key=lambda x: -x["score"])
    picked = [r for r in results if r["score"] >= 45][:top_n]
    if not picked:  # 没有达标股票时至少给分数最高的
        picked = results[:top_n]
    data_source = picked[0]["kline_source"] if picked else "无"
    return {
        "sector": sector,
        "stocks": picked,
        "evaluated": len(results),
        "data_source": data_source,
    }


def generate_morning_brief(sectors: list[dict], picks: dict[str, dict]) -> str:
    """生成晨报摘要文本（规则生成，不消耗ARTi credits）。"""
    lines = ["# 每日投研雷达 · 晨报摘要", ""]
    top_sectors = sorted(sectors, key=lambda x: -x.get("momentum_score", 0))[:3]
    lines.append("## 板块轮动 TOP3")
    for i, s in enumerate(top_sectors, 1):
        flow = "净流入" if s.get("net_inflow", 0) >= 0 else "净流出"
        lines.append(
            f"{i}. **{s['name']}** 轮动评分 {s.get('momentum_score', 0)}｜"
            f"涨跌 {s.get('change_pct', 0):+.2f}%｜{flow} {abs(s.get('net_inflow', 0)):.1f}亿"
            + (f"｜领涨 {s['leader']}" if s.get("leader") else "")
        )
    lines.append("")
    lines.append("## 技术面精选")
    total = 0
    for sector, pick in picks.items():
        stocks = pick.get("stocks", [])
        if not stocks:
            continue
        total += len(stocks)
        names = "、".join(f"{s['name']}({s['score']}分)" for s in stocks[:3])
        lines.append(f"- **{sector}**：{names}")
    lines.append("")
    lines.append(f"> 共扫描 {len(picks)} 个板块、精选 {total} 只个股。"
                 "技术信号仅作研究参考，不构成投资建议。")
    return "\n".join(lines)
