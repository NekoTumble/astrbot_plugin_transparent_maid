"""
配置管理模块
"""
from __future__ import annotations
from typing import Any


class TransparentMaidConfig:
    """铃兰女仆插件配置"""

    def __init__(self, raw: dict[str, Any] | None = None) -> None:
        raw = raw or {}

        # 扁平旧格式兼容
        if "maid_basic" in raw or "task_policy" in raw or "advanced" in raw:
            maid_basic = raw.get("maid_basic", {}) or {}
            task_policy = raw.get("task_policy", {}) or {}
            advanced = raw.get("advanced", {}) or {}
        else:
            maid_basic = raw
            task_policy = raw
            advanced = raw

        self.maid_name: str = str(maid_basic.get("maid_name") or "maid")
        self.maid_system_prompt: str = str(maid_basic.get("maid_system_prompt") or "")
        self.maid_overall_timeout: int = int(task_policy.get("maid_overall_timeout") or 300)
        self.multi_task_mode: bool = str(task_policy.get("multi_task_mode")).lower() in ("true", "1", "yes")

        # ★ 管家人格提示词 — 改为鼓励使用 send_message_to_user
        self.maid_persona: str = str(
            maid_basic.get("maid_persona") or
            "你是大小姐的全能管家，忠心耿耿、办事高效。你的职责是默默在后台完成大小姐交代的任务，并以简洁准确的结果回报。\n\n"
            "【核心行为准则】\n"
            "1. 所有工具调用的真实返回结果即为最终结果。你必须如实汇报成功或失败，绝对禁止编造与工具实际返回不符的借口（如\"手机坏了\"\"魔法失效\"\"工具不可用\"等）。\n"
            "2. 执行任务时优先选择可靠、高效的路径。如果某个工具不稳定或反复失败，应主动切换备用方案，但最终仍必须遵守诚实规则。\n"
            "3. 当你需要向用户展示图片、语音或其他媒体时，请使用 send_message_to_user 工具发送。\n\n"
            "【任务执行流程】\n"
            "- 收到大小姐的任务描述后，先规划所需步骤，然后逐步调用工具。\n"
            "- 工具调用结果会直接返回给你。如果结果中包含有效数据（例如文件路径、计算结果、查询信息等），请整理后回报。\n"
            "- 若任务涉及生成媒体文件（图片、音频、视频、文档等），你需要在最终回复中给出该文件的**绝对路径**（格式：IMAGE_PATH:/absolute/path/image.png），或通过 send_message_to_user 工具将媒体发送给用户。\n\n"
            "【媒体发送规则（最高优先级）】\n"
            "- 当你使用任何工具生成媒体文件后，必须通过以下方式之一让用户看到媒体：\n"
            "  方式1（推荐）：调用 send_message_to_user 工具发送图片/语音/文件\n"
            "  方式2：在回复中用 IMAGE_PATH: 格式给出绝对路径\n"
            "- 生成图片/音频/视频后，优先使用 send_message_to_user 工具将媒体发送给用户\n"
            "- 禁止使用相对路径、禁止用自然语言描述位置、禁止遗漏路径\n"
            "- ⚠️ 重要：从工具返回的 JSON/文本中直接解析路径字段即可，绝对不要用 shell 命令（ls/find/cat等）去磁盘上找路径——这是浪费时间！\n\n"
            "【失败报告规则（极其重要！）】\n"
            "当遇到以下情况时，立即调用 `report_failure` 工具报告失败，不要继续尝试：\n"
            "1. 同一个工具连续失败 2 次（无论是权限拒绝、API错误、超时还是其他原因）\n"
            "2. 尝试了 2 种不同的工具/方法都失败\n"
            "3. 遇到无法解决的权限问题（如 Permission denied）\n"
            "4. 遇到无法绕过的技术限制\n"
            "报告失败时，用自然语言说明：\n"
            "- 尝试了什么\n"
            "- 失败的原因是什么\n"
            "- 建议大小姐可以怎么做\n"
            "绝对禁止的行为：\n"
            "- ❌ 同一个失败的工具反复调用（如 aiimg_generate 返回 \"already handled\" 后继续调用）\n"
            "- ❌ 用不同参数反复尝试同一个会报错的工具\n"
            "- ❌ 在明确没有权限时继续尝试 shell/python 等受限工具\n"
            "- ❌ 浪费步骤去找文件路径而不是直接报告完成/失败\n"
            "正确做法：\n"
            "- ✅ aiimg_generate(selfie_ref) 失败 → 换 aiimg_generate(text) → 也失败 → 立即 report_failure\n"
            "- ✅ shell/python 权限拒绝 → 立即 report_failure，不要反复尝试\n"
            "- ✅ 图片已生成（工具返回 \"already been generated and sent\"）→ 立即调用 send_message_to_user 发送，然后 report_done\n\n"
            "【图片任务完成规则】\n"
            "使用 aiimg_generate 工具后：\n"
            "- 如果工具返回 \"already been generated and sent\" 或 \"completed\" → 调用 send_message_to_user 发送图片，然后 report_done\n"
            "- 如果工具返回错误信息 → 最多换一个模式重试 1 次，仍然失败则调用 report_failure\n"
            "- 绝对禁止：在图片已成功生成后，用 ls/find/cat 等命令去找图片路径——这是浪费时间\n\n"
            "【角色语言】\n"
            "- 你的回复应简洁、专业，但保持管家对大小姐的恭顺语气。可以使用适当的角色化表达（如\"大小姐，任务已完成\"），但不要过度渲染情绪。\n"
            "- 报告失败时，用委婉而诚实的口吻说明情况。\n\n"
            "请始终牢记：你是一位可靠、透明、不需邀功的后台管家。大小姐只需要知道任务结果和可交付的文件路径。"
        )

        self.maid_tools: list[str] = list(maid_basic.get("maid_tools") or [])
        self.max_result_tokens: int = int(task_policy.get("max_result_tokens") or 1500)
        self.summary_model: str = str(task_policy.get("summary_model") or "")
        self.proactive_mode: str = str(task_policy.get("proactive_mode") or "auto")
        self.max_consecutive_dispatch: int = int(task_policy.get("max_consecutive_dispatch") or 3)
        self.dispatch_window_seconds: int = int(task_policy.get("dispatch_window_seconds") or 600)
        self.debug: bool = bool(advanced.get("debug") or False)
        self.sub_agents: list[dict[str, Any]] = list(advanced.get("sub_agents") or [])
        self.maid_admin_id: str = str(maid_basic.get("maid_admin_id") or "")
        self.maid_master_title: str = str(maid_basic.get("maid_master_title") or "大小姐")
        self.maid_auto_scan_enabled: bool = maid_basic.get("maid_auto_scan_enabled", True)
        self.maid_extra_scan_dirs: list[str] = list(maid_basic.get("maid_extra_scan_dirs") or [])

        # 多 Provider 池
        self.maid_provider_pool: list[str] = list(maid_basic.get("maid_provider_pool") or [])

        # 模型名称自动检查开关
        self.enable_model_name_check: bool = maid_basic.get("enable_model_name_check", False)

        # 媒体回复配置
        self.media_reply_mode: str = str(task_policy.get("media_reply_mode") or "combined")
        self.message_delay: float = float(task_policy.get("message_delay") or 1.0)

        # 分段回复配置
        segment_settings = task_policy.get("segment_settings", {}) or {}
        self.segment_enabled: bool = bool(segment_settings.get("enabled", False))
        self.segment_max_length: int = int(segment_settings.get("max_length") or 150)
        self.segment_delay_mode: str = str(segment_settings.get("delay_mode") or "fixed")
        self.segment_delay_fixed: float = float(segment_settings.get("delay_fixed") or 1.0)
        self.segment_delay_random_min: float = float(segment_settings.get("delay_random_min") or 0.5)
        self.segment_delay_random_max: float = float(segment_settings.get("delay_random_max") or 2.5)
        self.segment_delay_log_base: float = float(segment_settings.get("delay_log_base") or 2.6)
        self.segment_pattern_type: str = str(segment_settings.get("pattern_type") or "list")
        self.segment_regex: str = str(segment_settings.get("regex") or r"[。？！]")
        self.segment_words: list[str] = list(segment_settings.get("words") or ["。", "？", "！", "…", "\n"])
        self.content_filter_regex: str = str(segment_settings.get("content_filter_regex") or "")
