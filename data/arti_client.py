"""
ARTi 数据接入模块
=================
ARTi 提供 OpenAI 兼容的投研 API，本模块封装三模型调用：
  - arti-chat    : 通用投研（行情/财报/个股研判）
  - arti-council : 大师圆桌（多角色辩论，偶发 502，带重试降级）
  - arti-news    : 新闻分析

认证：环境变量 ARTI_API_KEY（绝不硬编码）。
base_url：https://api.artifin.ai/functions/v1/public-api/v1

设计要点：
  1. 单点封装，业务层不感知 HTTP / SDK 细节
  2. 502 / 超时自动重试，council 失败降级到 chat
  3. 提供 mock 模式，无 key 也能跑通全流程（demo 友好）
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Iterable

# 加载项目根目录的 .env（若存在），让写进文件的 key / mock 开关自动生效
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
except ImportError:
    pass  # 未装 python-dotenv 时退回只读系统环境变量

BASE_URL = "https://api.artifin.ai/functions/v1/public-api/v1"

MODELS = {
    "chat": "arti-chat",
    "council": "arti-council",
    "news": "arti-news",
}


class ARTIClient:
    """ARTi OpenAI 兼容客户端，带重试与降级。"""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = BASE_URL,
        mock: bool = False,
        max_retries: int = 2,
        timeout: float = 120.0,
    ):
        self.api_key = api_key or os.getenv("ARTI_API_KEY", "")
        self.base_url = base_url
        self.mock = mock or not self.api_key
        self.max_retries = max_retries
        self.timeout = timeout
        self._client = None

        if not self.mock:
            try:
                from openai import OpenAI

                self._client = OpenAI(
                    api_key=self.api_key,
                    base_url=self.base_url,
                    timeout=self.timeout,
                    max_retries=0,  # 自己控制重试，便于降级
                )
            except ImportError as e:
                raise RuntimeError("需要 openai 库：pip install openai") from e

    def chat(
        self,
        prompt: str,
        *,
        model: str = "chat",
        system: str | None = None,
        stream: bool = False,
        fallback: bool = True,
    ) -> str:
        """发起一次投研对话，返回完整文本。

        model: chat / council / news 之一或原始模型名
        fallback: council 失败时降级到 chat
        """
        model_id = MODELS.get(model, model)

        if self.mock:
            return self._mock_reply(prompt, model_id, system)

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        last_err = None
        for attempt in range(self.max_retries + 1):
            try:
                if stream:
                    return self._stream(messages, model_id)
                resp = self._client.chat.completions.create(
                    model=model_id,
                    messages=messages,
                    stream=False,
                )
                return resp.choices[0].message.content or ""
            except Exception as e:
                last_err = e
                if attempt < self.max_retries:
                    time.sleep(1.5 * (attempt + 1))

        # 降级：council 不稳，失败转 chat
        if fallback and model_id != MODELS["chat"]:
            return self.chat(
                prompt, model="chat", system=system, stream=stream, fallback=False
            )
        raise RuntimeError(f"ARTi 调用失败: {type(last_err).__name__}: {last_err}")

    def _stream(self, messages: list, model_id: str) -> str:
        """流式聚合，按 delta 累加，返回完整文本。"""
        parts: list[str] = []
        stream = self._client.chat.completions.create(
            model=model_id, messages=messages, stream=True
        )
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta and getattr(delta, "content", None):
                parts.append(delta.content)
        return "".join(parts)

    def _mock_reply(self, prompt: str, model_id: str, system: str | None) -> str:
        """无 key 时的确定性 mock，保证 demo 可跑、样例稳定。"""
        sys_text = system or ""
        # 从 prompt 提取股票名，让 mock 内容更贴合
        name = "该股"
        if "请分析股票：" in prompt:
            name = prompt.split("请分析股票：", 1)[1].split("（", 1)[0].strip()

        # 持仓去留诊断（system 含"持仓决策/持仓去留/平仓"）
        if "持仓决策" in sys_text or "持仓去留" in sys_text or "平仓" in sys_text:
            return (
                f"### {name} · 持仓去留：继续持有（mock 示例）\n\n"
                "**为什么**：此处会结合你提供的真实异动数值给出去留依据。\n\n"
                "**怎么做**：跌破关键均线先减仓，站稳则维持。\n\n"
                "**盯什么**：MA20 / 成本价。\n\n"
                "> 当前为 mock 示例；配置 ARTI_API_KEY 后自动替换为真实大模型建议。不构成投资建议。"
            )
        # 大师圆桌（system 含"圆桌/大师/角色"）
        if "圆桌" in sys_text or "大师" in sys_text or "角色" in sys_text:
            return (
                f"【大师圆桌 · mock】针对 {name} 的三方观点：\n\n"
                "**价值投资者**：技术面走强不等于便宜。我会看估值分位和自由现金流，"
                "如果价格已经透支了乐观预期，再漂亮的均线也不是买入理由。\n\n"
                "**趋势交易者**：我尊重趋势。均线多头+MACD金叉+放量，这是标准的"
                "右侧信号，我会顺着做，但跌破20日线就走，不含糊。\n\n"
                "**风险控制官**：两人都有道理，但我提醒一句——技术信号在震荡市里"
                "假突破很多。仓位控制比方向判断更重要，别把单次信号当确定性。\n\n"
                "**主持人总结**：趋势与价值的分歧本质是持仓周期不同。"
                "短线可跟趋势设好止损，长线需回到估值与安全边际。"
                "最终决策与责任在投资者本人。"
            )
        # 新闻分析
        if model_id == MODELS["news"]:
            return (
                f"【新闻分析 · mock】{name} 近期消息面梳理：\n\n"
                "- **正面因素**：主营业务保持稳健增长，机构调研频次上升；"
                "行业景气度边际改善，龙头受益于集中度提升。\n"
                "- **负面因素**：板块整体估值承压，短期资金面出现分歧；"
                "原材料价格波动可能挤压毛利。\n"
                "- **需关注**：下一期财报的毛利率与订单数据、行业政策动向、"
                "大股东增减持公告。\n\n"
                "> 以上为示例内容（mock）。风险提示：消息面变化快，请以公司公告等"
                "官方披露为准，本内容不构成投资建议。"
            )
        # 投研报告（默认 chat）
        return (
            f"【投研速评 · mock】{name}\n\n"
            "## 技术面解读\n均线呈多头排列且 price 站稳20日线，MACD 金叉配合温和放量，"
            "属于典型的右侧转强形态。RSI 处于强势区但未超买，短期动能仍在。\n\n"
            "## 逻辑与催化\n技术信号背后通常对应资金对基本面预期的上修。"
            "需结合板块景气度与个股催化（业绩、订单、政策）验证，"
            "技术面只是结果，逻辑才是原因。\n\n"
            "## 风险与证伪条件\n若跌破20日线且 MACD 死叉，转强形态即告失效；"
            "放量滞涨或 RSI 顶背离是短线见顶信号。\n\n"
            "> 本文基于公开信息与技术信号生成，为研究参考，不构成投资建议。"
        )


def get_client(mock: bool | None = None) -> ARTIClient:
    """工厂方法：显式 mock 优先，否则看环境变量 ARTI_MOCK。"""
    if mock is None:
        mock = os.getenv("ARTI_MOCK", "").lower() in ("1", "true", "yes")
    return ARTIClient(mock=mock)
