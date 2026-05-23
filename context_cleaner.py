"""
上下文清洗 — 管家结果过长时自动摘要
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from astrbot.api import logger

if TYPE_CHECKING:
    from astrbot.api.star import Context


async def summarize_if_long(
    context: Context,
    text: str,
    max_tokens: int = 1500,
    summary_model: str = "",
    debug: bool = False,
) -> str:
    """若文本超过预估 token 数，则调用 LLM 进行摘要。"""
    # 粗略 token 估算（中文字符约 1.5 token/字，英文约 0.75 token/字）
    estimated_tokens = len(text.encode("utf-8")) // 2
    if estimated_tokens <= max_tokens:
        return text

    if debug:
        logger.info(f"[铃兰女仆] 🧹 管家结果过长 ({estimated_tokens} tokens > {max_tokens})，开始摘要…")

    summary_prompt = (
        "请将以下内容摘要为简洁的要点报告，保留所有关键信息和数据，去除冗余描述：\n\n"
        f"{text}"
    )

    try:
        result = await context.llm_generate(
            prompt=summary_prompt,
            system_prompt="你是一个专业的文本摘要助手。只输出摘要内容，不要多余的话。",
            provider_id=summary_model or None,
        )
        summary = result.completion_text.strip() if result and result.completion_text else text
        if debug:
            logger.info(f"[铃兰女仆] 🧹 摘要完成: {estimated_tokens} → ~{len(summary.encode('utf-8')) // 2} tokens")
        return summary
    except Exception as exc:
        logger.warning(f"[铃兰女仆] 🧹 摘要失败，使用原结果: {exc}")
        return text