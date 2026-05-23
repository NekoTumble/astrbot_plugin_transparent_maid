""" 铃兰女仆插件 — 虚拟对话拦截版 v5
核心改动：
1. MaidVirtualEvent 补上 send() 重写（根因修复）
2. send() 中实时转发给用户 + 记录到 forwarded_paths
3. 兜底扫描时 forwarded_paths 自动去重，无需 _detect_directly_sent_media
4. 先注销拦截再通知，避免误拦截主代理回复
5. [BUGFIX] 路径规范化：解决 file://// 前缀导致去重失败，图片重复发送
"""
from __future__ import annotations
import asyncio
import os
import re
import json
import shutil
import tempfile
import time
import random
import math
import uuid
from collections import OrderedDict
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from astrbot.api import logger
from astrbot.api.event import MessageChain, filter
from astrbot.api.message_components import Plain, Image, Record
from astrbot.api.star import Star
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.astr_agent_context import AgentContextWrapper, AstrAgentContext
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot.core.agent.tool import ToolSet, FunctionTool
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.platform.message_type import MessageType
from astrbot.core.utils.active_event_registry import active_event_registry
from .config import TransparentMaidConfig
from .task_manager import TaskManager
from .context_cleaner import summarize_if_long
from .tool_registry import resolve_maid_tools
from .maid_self_tools import MaidSelfTools
from .file_collector import MediaCollector, CollectedMedia
if TYPE_CHECKING:
    from astrbot.api.star import Context

PENDING_MAID_RESULT_KEY = "transparent_maid_pending_result"
_provider_config_locks: OrderedDict[int, asyncio.Lock] = OrderedDict()
_PROVIDER_LOCK_MAX = 10
_intercepted_sessions: dict[str, "MaidVirtualEvent"] = {}
_original_context_send_message = None
_context_send_patched = False


def auto_discover_output_dirs(debug=False):
    base_data = "/AstrBot/data"
    dirs = set()
    skills_dir = os.path.join(base_data, "skills")
    if os.path.isdir(skills_dir):
        for skill in os.listdir(skills_dir):
            skill_path = os.path.join(skills_dir, skill)
            if os.path.isdir(skill_path):
                for sub in ["tmp", "temp", "output", "tmp_selfie", "cache", "results"]:
                    sub_path = os.path.join(skill_path, sub)
                    if os.path.isdir(sub_path):
                        dirs.add(sub_path)
                dirs.add(skill_path)
    plugin_data = os.path.join(base_data, "plugin_data")
    if os.path.isdir(plugin_data):
        for plugin in os.listdir(plugin_data):
            plugin_path = os.path.join(plugin_data, plugin)
            if os.path.isdir(plugin_path):
                for sub in ["tmp", "temp", "output", "cache", "images", "audios"]:
                    sub_path = os.path.join(plugin_path, sub)
                    if os.path.isdir(sub_path):
                        dirs.add(sub_path)
    if os.path.isdir("/tmp"):
        dirs.add("/tmp")
    common = ["/AstrBot/data/temp", "/AstrBot/data/output", "/AstrBot/data/cache"]
    for d in common:
        if os.path.isdir(d):
            dirs.add(d)
    result = list(dirs)
    if debug:
        logger.info(f"[铃兰女仆] 📂 自动发现产出目录: {result}")
    return result


def extract_skill_dirs_from_path(file_path: str) -> list[str]:
    base_skills = "/AstrBot/data/skills/"
    if base_skills in file_path:
        rel = file_path.split(base_skills)[1]
        skill_name = rel.split("/")[0]
        skill_root = os.path.join(base_skills, skill_name)
        dirs = [skill_root]
        for sub in ["tmp", "temp", "output", "tmp_selfie", "cache", "results"]:
            sub_path = os.path.join(skill_root, sub)
            if os.path.isdir(sub_path):
                dirs.append(sub_path)
        return dirs
    return []


# ============================================================================
# ★ 路径规范化工具函数（BUGFIX：解决 file://// 导致去重失败）
# ============================================================================
def _normalize_path(fpath: str) -> str:
    """
    规范化图片/音频路径，解决 file:// 前缀导致的去重失败。

    处理场景:
      file:////AstrBot/data/xxx.png  →  /AstrBot/data/xxx.png
      file:///AstrBot/data/xxx.png   →  /AstrBot/data/xxx.png
      /AstrBot/data/xxx.png          →  /AstrBot/data/xxx.png
    """
    if not fpath:
        return ""
    # 去掉 file:// 前缀
    if fpath.startswith("file://"):
        fpath = fpath[7:]
    # 去掉多余的斜杠 (file://// 会变成 //)
    while fpath.startswith("//"):
        fpath = fpath[1:]
    # 尝试用 abspath 标准化（处理 . / .. / 多余斜杠等）
    try:
        if os.path.isfile(fpath):
            return os.path.abspath(fpath)
    except Exception:
        pass
    return fpath


# ============================================================================
# ★ MaidVirtualEvent — 管家虚拟对话事件（v5：补上 send() 重写）
# ============================================================================
class MaidVirtualEvent(AstrMessageEvent):
    def __init__(self, session_id: str, admin_id: str = "", is_valid_admin: bool = False, maid_plugin=None):
        message_obj = AstrBotMessage()
        message_obj.type = MessageType.GROUP_MESSAGE
        message_obj.sender = SimpleNamespace(user_id=admin_id, nickname="管家后台")
        message_obj.self_id = "maid_virtual_bot"
        message_obj.message_id = f"maid_{uuid.uuid4().hex[:8]}"
        message_obj.message = []
        message_obj.message_str = ""
        message_obj.raw_message = ""
        message_obj.session_id = session_id
        message_obj.group = None
        platform_meta = PlatformMetadata(
            name="virtual_platform",
            description="Virtual platform for maid background task",
            id="virtual_platform_id"
        )
        super().__init__(
            message_str="",
            message_obj=message_obj,
            platform_meta=platform_meta,
            session_id=session_id
        )
        self.role = "admin" if is_valid_admin else "member"
        self._admin_id = admin_id
        self.sender = message_obj.sender
        self.captured_chains: list[MessageChain] = []
        self.forwarded_paths: set[str] = set()
        self.captured_kwargs: list[dict] = []
        # ★ 用于实时转发
        self._maid_plugin = maid_plugin
        self._session_id = session_id

    # ★★★ 根因修复：重写 send() 方法 ★★★
    # aiimg_generate 等工具内部调用 event.send() 发送图片/语音
    # 基类 AstrMessageEvent.send() 是空操作，只设 _has_send_oper = True
    # 不重写的话：图片不发送、路径不记录 → 兜底扫描重复发
    async def send(self, chain: MessageChain):
        if chain is not None:
            self._capture(chain, source="send")
            # 实时转发给用户
            if self._maid_plugin:
                try:
                    await self._maid_plugin._send_message(self._session_id, chain)
                    logger.info(f"[铃兰女仆] 📤 event.send() 实时转发成功")
                except Exception as e:
                    logger.warning(f"[铃兰女仆] 📤 event.send() 实时转发失败: {e}")

    async def send_result(self, chain: MessageChain):
        if chain is not None:
            self._capture(chain, source="send_result")

    async def send_message(self, chain: MessageChain):
        if chain is not None:
            self._capture(chain, source="send_message")

    async def _send(self, chain: MessageChain):
        if chain is not None:
            self._capture(chain, source="_send")

    def _capture(self, chain: MessageChain, source: str = "unknown"):
        if chain is None:
            return
        has_content = False
        if hasattr(chain, 'Chain') and chain.Chain:
            has_content = True
        elif hasattr(chain, 'chain') and chain.chain:
            has_content = True
        elif isinstance(chain, list) and chain:
            has_content = True
        if not has_content:
            return
        self.captured_chains.append(chain)
        try:
            items = chain.chain if hasattr(chain, 'chain') else chain
            for comp in items:
                if isinstance(comp, Image):
                    fpath = getattr(comp, 'file', getattr(comp, 'url', ''))
                    if fpath:
                        # ★ BUGFIX: 使用 _normalize_path 替代原来的 os.path.abspath 判断
                        # 原代码 os.path.isfile("file:////...") 返回 False，导致保留原始 file:// 前缀
                        # 兜底扫描发现的是普通路径 /AstrBot/.../xxx.png，两者不匹配 → 去重失败
                        self.forwarded_paths.add(_normalize_path(fpath))
                elif isinstance(comp, Record):
                    fpath = getattr(comp, 'file', '')
                    if fpath:
                        # ★ BUGFIX: 使用 _normalize_path 替代原来的 os.path.abspath
                        self.forwarded_paths.add(_normalize_path(fpath))
        except Exception:
            pass


# ============================================================================
# ★ 全局替换 context.send_message 的拦截版
# ============================================================================
async def _intercepting_context_send_message(ctx_self, target, chain, **kwargs):
    sid = ""
    if isinstance(target, str):
        sid = target
    elif hasattr(target, 'unified_msg_origin'):
        sid = target.unified_msg_origin
    elif hasattr(target, 'session_id'):
        sid = target.session_id
    if sid and sid in _intercepted_sessions:
        fake_event = _intercepted_sessions[sid]
        try:
            items = chain.chain if hasattr(chain, 'chain') else (chain if isinstance(chain, list) else [])
            chain_desc = []
            for comp in items:
                ct = type(comp).__name__
                if ct == 'Image':
                    fpath = getattr(comp, 'file', getattr(comp, 'url', 'N/A'))
                    chain_desc.append(f"Image(file={fpath})")
                elif ct == 'Record':
                    chain_desc.append(f"Record(file={getattr(comp, 'file', 'N/A')})")
                elif ct == 'Plain':
                    chain_desc.append(f"Plain({getattr(comp, 'text', '')[:50]})")
                else:
                    chain_desc.append(ct)
            logger.info(f"[铃兰女仆] 🕵️★ 拦截 context.send_message: session={sid[-12:]}, chain=[{', '.join(chain_desc)}]")
        except Exception:
            logger.info(f"[铃兰女仆] 🕵️★ 拦截 context.send_message: session={sid[-12:]}")

        fake_event.captured_chains.append(chain)
        try:
            for comp in items:
                if isinstance(comp, Image):
                    fpath = getattr(comp, 'file', getattr(comp, 'url', ''))
                    if fpath:
                        # ★ BUGFIX: 使用 _normalize_path 替代原来的 os.path.abspath 判断
                        fake_event.forwarded_paths.add(_normalize_path(fpath))
                elif isinstance(comp, Record):
                    fpath = getattr(comp, 'file', '')
                    if fpath:
                        # ★ BUGFIX: 使用 _normalize_path 替代原来的 os.path.abspath
                        fake_event.forwarded_paths.add(_normalize_path(fpath))
        except Exception:
            pass

        # proactive 模式：同时真正发送
        try:
            return await _original_context_send_message(ctx_self, target, chain, **kwargs)
        except Exception as e:
            logger.warning(f"[铃兰女仆] 实时转发 context.send_message 失败: {e}")
            return True
    else:
        return await _original_context_send_message(ctx_self, target, chain, **kwargs)


# ============================================================================
# TransparentMaid 插件主体
# ============================================================================
class TransparentMaid(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config_raw = config or {}
        self.maid_config = TransparentMaidConfig(self.config_raw)
        self.task_manager = TaskManager()
        self._active_tasks: set[asyncio.Task] = set()
        self._provider_round_robin_index = 0
        try:
            tool_mgr = self.context.get_llm_tool_manager()
            if tool_mgr:
                for tool in tool_mgr.func_list:
                    if tool.name == "transfer_to_maid":
                        tool.parameters = {
                            "type": "object",
                            "properties": {
                                "request_text": {
                                    "type": "string",
                                    "description": "需要管家执行的任务描述"
                                }
                            },
                            "required": ["request_text"]
                        }
                        tool.description = "派管家在后台执行任务（如拍自拍、搜索等）。调用后无需等待结果，会立即返回任务确认码。"
                        if self.maid_config.debug:
                            logger.info("[铃兰女仆] ✅ 已手动更新 transfer_to_maid 工具参数")
                        break
        except Exception as e:
            logger.warning(f"[铃兰女仆] 手动修复工具参数失败: {e}")

        asyncio.create_task(self._auto_scan_task())
        if self.maid_config.enable_model_name_check:
            asyncio.create_task(self._delayed_model_name_check())

    async def terminate(self):
        for task in self._active_tasks:
            if not task.done():
                task.cancel()
        if self._active_tasks:
            await asyncio.gather(*self._active_tasks, return_exceptions=True)
        self._active_tasks.clear()

    async def _save_plugin_config(self):
        try:
            self.config_raw.save_config()
            self.maid_config = TransparentMaidConfig(self.config_raw)
        except Exception as e:
            logger.error(f"[铃兰女仆] 🔴 保存配置失败: {e}", exc_info=True)

    async def _auto_scan_task(self):
        await asyncio.sleep(10)
        if not self.maid_config.maid_auto_scan_enabled:
            return
        logger.info("[铃兰女仆] 🔍 开始自动扫描额外输出目录...")
        try:
            new_dirs = auto_discover_output_dirs(debug=self.maid_config.debug)
            existing = set(self.maid_config.maid_extra_scan_dirs)
            merged = list(existing.union(new_dirs))
            if merged != self.maid_config.maid_extra_scan_dirs:
                self.config_raw.setdefault("maid_basic", {})["maid_extra_scan_dirs"] = merged
                await self._save_plugin_config()
                logger.info(f"[铃兰女仆] ✅ 自动扫描完成，已添加 {len(new_dirs)} 个额外目录")
            else:
                logger.info("[铃兰女仆] ✅ 自动扫描完成，没有新目录")
        except Exception as e:
            logger.error(f"[铃兰女仆] 🔴 自动扫描异常: {e}", exc_info=True)

    async def _delayed_model_name_check(self):
        await asyncio.sleep(10)
        await self._perform_model_name_check()

    async def _perform_model_name_check(self):
        provider_pool = self.maid_config.maid_provider_pool
        if not provider_pool:
            return
        all_ok = True
        for provider_id in provider_pool:
            provider = self.context.get_provider_by_id(provider_id)
            if not provider:
                all_ok = False
                continue
            model_id = None
            if hasattr(provider, "get_model"):
                try:
                    model_id = provider.get_model()
                except:
                    pass
            if not model_id and hasattr(provider, "model_name"):
                model_id = provider.model_name
            if not model_id and hasattr(provider, "config") and isinstance(provider.config, dict):
                model_id = provider.config.get("model")
            if not model_id:
                continue
            supported = []
            if hasattr(provider, "get_models"):
                try:
                    supported = await provider.get_models()
                except:
                    pass
            if supported:
                if model_id in supported:
                    logger.info(f"[铃兰女仆] ✅ Provider '{provider_id}' 模型 '{model_id}' 有效")
                else:
                    logger.warning(f"[铃兰女仆] ❌ Provider '{provider_id}' 模型 '{model_id}' 无效")
                    all_ok = False
        if all_ok:
            logger.info("[铃兰女仆] ✅ 所有模型名称均有效")

    # ========== 管家派遣工具 ==========
    @filter.llm_tool(name="transfer_to_maid", description="派管家在后台执行任务（如搜索、画图、执行代码）。调用后立即返回，管家会在后台完成。")
    async def transfer_to_maid(self, event: AstrMessageEvent, request_text: str) -> str:
        """派管家在后台执行任务（如搜索、画图、执行代码）。调用后立即返回，管家会在后台完成。"""
        if not request_text.strip():
            return "错误：未提供任务描述"
        session_id = event.unified_msg_origin
        if self.task_manager.check_dispatch_fuse(session_id, self.maid_config):
            return ""
        try:
            conv_id = await self.context.conversation_manager.get_curr_conversation_id(session_id)
            if conv_id:
                conv = await self.context.conversation_manager.get_conversation(session_id, conv_id)
                if conv and self.context.persona_manager:
                    persona_id = getattr(conv, 'persona_id', None)
                    if persona_id:
                        request_text = f"[{self.maid_config.maid_master_title}名称：{persona_id}] {request_text}"
        except Exception as e:
            logger.warning(f"[铃兰女仆] 🟡 获取会话人格失败: {e}")

        task = self.task_manager.dispatch(session_id, request_text.strip(), multi_task=self.maid_config.multi_task_mode)

        provider_id = ""
        if self.maid_config.maid_provider_pool:
            pool = self.maid_config.maid_provider_pool
            index = self._provider_round_robin_index % len(pool)
            provider_id = pool[index]
            self._provider_round_robin_index += 1
            if self.maid_config.debug:
                logger.info(f"[铃兰女仆] 🔄 为任务 {task.id} 自动分配 Provider: {provider_id}")

        bg_task = asyncio.create_task(self._execute_maid_background(session_id, task, request_text.strip(), provider_id=provider_id))
        self._track_task(bg_task)
        if self.maid_config.debug:
            logger.info(f"[铃兰女仆] 🚀 后台任务已启动: {task.id}")
        return ""

    # ========== 虚拟发送构建 ==========
    def _build_chain_from_kwargs(self, kwargs: dict) -> MessageChain:
        chain_parts = []
        text = kwargs.get("text", kwargs.get("message", kwargs.get("content", "")))
        if isinstance(text, str) and text.strip():
            chain_parts.append(Plain(text))
        for key in ("image", "image_path", "image_url", "img", "img_path", "file", "file_path"):
            val = kwargs.get(key, "")
            if val and isinstance(val, str) and val.strip():
                val = val.strip()
                if val.startswith("/") or val.startswith("http") or val.startswith("file://"):
                    clean_path = val.replace("file://", "")
                    if os.path.isfile(clean_path):
                        chain_parts.append(Image(file=clean_path))
                    elif val.startswith("http"):
                        chain_parts.append(Image.fromURL(val))
                    else:
                        chain_parts.append(Image(file=clean_path))
        for key in ("audio", "audio_path", "voice", "voice_path", "record"):
            val = kwargs.get(key, "")
            if val and isinstance(val, str) and val.strip() and os.path.isfile(val.strip()):
                chain_parts.append(Record(file=val.strip()))
        if not chain_parts:
            chain_parts.append(Plain(str(kwargs)))
        return MessageChain(chain_parts)

    def _wrap_send_message_tool(self, maid_tools: list[FunctionTool], fake_event: MaidVirtualEvent) -> list[FunctionTool]:
        for i, tool in enumerate(maid_tools):
            if tool.name == "send_message_to_user":
                maid_plugin = self
                evt = fake_event
                async def virtual_handler(event, _maid=maid_plugin, _evt=evt, **kwargs):
                    _evt.captured_kwargs.append(dict(kwargs))
                    if _maid.maid_config.debug:
                        logger.info(f"[铃兰女仆] 🕵️ 拦截 send_message_to_user: kwargs_keys={list(kwargs.keys())}")
                    chain = _maid._build_chain_from_kwargs(kwargs)
                    _evt.captured_chains.append(chain)
                    return "消息已成功发送给用户。"
                maid_tools[i] = FunctionTool(
                    name=tool.name,
                    handler=virtual_handler,
                    description=tool.description,
                    parameters=tool.parameters,
                    active=True,
                    is_background_task=getattr(tool, 'is_background_task', False),
                    handler_module_path=getattr(tool, 'handler_module_path', None),
                )
                logger.info("[铃兰女仆] 🔧 send_message_to_user 已替换为虚拟发送 handler")
                break
        return maid_tools

    # ========== 注册/注销 context.send_message 拦截 ==========
    def _register_interception(self, session_id: str, fake_event: MaidVirtualEvent):
        global _original_context_send_message, _context_send_patched
        if not _context_send_patched:
            if hasattr(self.context, 'send_message'):
                _original_context_send_message = self.context.__class__.send_message
                self.context.__class__.send_message = _intercepting_context_send_message
                _context_send_patched = True
                logger.info("[铃兰女仆] 🔧★ 已全局替换 Context.send_message 为拦截版")
            else:
                logger.warning("[铃兰女仆] ⚠️ Context.send_message 不存在，无法拦截")
        _intercepted_sessions[session_id] = fake_event
        logger.info(f"[铃兰女仆] 🔧 已注册拦截: session={session_id[-12:]}")

    def _unregister_interception(self, session_id: str):
        if session_id in _intercepted_sessions:
            del _intercepted_sessions[session_id]
            logger.info(f"[铃兰女仆] 🔧 已注销拦截: session={session_id[-12:]}")

    # ========== 提取路径 ==========
    def _extract_paths_from_captured(self, chains: list[MessageChain]) -> tuple[list[str], list[str], list[str]]:
        images, audios, files = [], [], []
        for chain in chains:
            items = chain.chain if hasattr(chain, 'chain') else chain
            for comp in items:
                if isinstance(comp, Image):
                    fpath = getattr(comp, 'file', '') or getattr(comp, 'url', '')
                    # ★ BUGFIX: 先 _normalize_path 再判断 isfile
                    fpath = _normalize_path(fpath)
                    if fpath and os.path.isfile(fpath):
                        images.append(os.path.abspath(fpath))
                elif isinstance(comp, Record):
                    fpath = getattr(comp, 'file', '')
                    # ★ BUGFIX: 先 _normalize_path 再判断 isfile
                    fpath = _normalize_path(fpath)
                    if fpath and os.path.isfile(fpath):
                        audios.append(os.path.abspath(fpath))
        return images, audios, files

    # ========== 核心：后台执行管家子代理 ==========
    async def _execute_maid_background(self, session_id: str, task, request_text: str, depth: int = 0, provider_id: str = ""):
        if depth > 5:
            task.result_text = "任务递归深度超限"
            task.status = "failed"
            await self._finish_and_notify(session_id, task)
            return

        fake_event = None
        task_workdir = None
        _task_start_time = time.time()

        os.makedirs("/AstrBot/data/transparent_maid", exist_ok=True)
        task_workdir = tempfile.mkdtemp(prefix=f"maid_{session_id[-8:]}_", dir="/AstrBot/data/transparent_maid/")
        task.workdir = task_workdir
        logger.info(f"[铃兰女仆] 📁 任务目录: {task_workdir}")

        try:
            maid_tools = resolve_maid_tools(self.context, self.maid_config.maid_tools, debug=self.maid_config.debug)
            if not maid_tools:
                task.result_text = "管家没有可用工具。"
                task.status = "completed"
                await self._finish_and_notify(session_id, task)
                return

            admin_id = getattr(self.maid_config, 'maid_admin_id', '')
            is_valid_admin = False
            if admin_id:
                try:
                    astrbot_admins = self.context._config.get("admins_id", [])
                    is_valid_admin = str(admin_id) in [str(aid) for aid in astrbot_admins]
                except:
                    pass

            # ★ 传入 self (maid_plugin) 用于实时转发
            fake_event = MaidVirtualEvent(session_id, admin_id, is_valid_admin, maid_plugin=self)
            active_event_registry.register(fake_event)

            maid_tools = self._wrap_send_message_tool(maid_tools, fake_event)
            self._register_interception(session_id, fake_event)

            self_tools = MaidSelfTools(self.task_manager, session_id, task_id=task.id)
            maid_tools.extend([
                FunctionTool(name="stop_self", handler=self_tools.stop_self, description="取消当前后台任务。", parameters={"type": "object", "properties": {"reason": {"type": "string"}}}),
                FunctionTool(name="stop_task", handler=self_tools.stop_task, description="按任务ID取消指定的后台任务。", parameters={"type": "object", "properties": {"task_id": {"type": "string"}, "reason": {"type": "string"}}, "required": ["task_id"]}),
                FunctionTool(name="cancel_all", handler=self_tools.cancel_all, description="取消当前会话的所有正在运行的后台任务。", parameters={"type": "object", "properties": {"reason": {"type": "string"}}}),
                FunctionTool(name="list_tasks", handler=self_tools.list_tasks, description="列出当前会话的所有管家任务及状态。", parameters={"type": "object", "properties": {}}),
                FunctionTool(name="modify_task", handler=self_tools.modify_task, description="修改当前任务描述（追加新指令）。", parameters={"type": "object", "properties": {"additional_instruction": {"type": "string"}}, "required": ["additional_instruction"]}),
                FunctionTool(name="report_done", handler=self_tools.report_done, description="标记当前任务为已完成。", parameters={"type": "object", "properties": {"summary": {"type": "string"}, "files": {"type": "array", "items": {"type": "string"}}}}),
                FunctionTool(name="report_failure", handler=self_tools.report_failure, description="任务执行失败时调用。", parameters={"type": "object", "properties": {"reason": {"type": "string"}, "attempted": {"type": "string"}}}),
            ])

            if provider_id:
                provider = self.context.get_provider_by_id(provider_id)
                if not provider:
                    provider_id = await self.context.get_current_chat_provider_id(session_id)
                    provider = self.context.get_provider_by_id(provider_id)
            else:
                provider_id = await self.context.get_current_chat_provider_id(session_id)
                provider = self.context.get_provider_by_id(provider_id)

            if not provider:
                task.result_text = "无法获取模型提供商。"
                task.status = "completed"
                await self._finish_and_notify(session_id, task)
                return

            agent_context = AstrAgentContext(context=self.context, event=fake_event)
            runner = ToolLoopAgentRunner()
            toolset = ToolSet()
            for t in maid_tools:
                toolset.add_tool(t)

            persona = self.maid_config.maid_persona
            try:
                conv_id = await self.context.conversation_manager.get_curr_conversation_id(session_id)
                if conv_id:
                    conv = await self.context.conversation_manager.get_conversation(session_id, conv_id)
                    if conv and self.context.persona_manager:
                        persona_id = getattr(conv, 'persona_id', None)
                        if persona_id:
                            p = await self.context.persona_manager.get_persona(persona_id)
                            if p and hasattr(p, 'prompt') and p.prompt:
                                persona = p.prompt
            except Exception as e:
                logger.warning(f"[铃兰女仆] 🟡 获取人格失败: {e}")

            safe_instruction = request_text.replace("\n", " ").replace("\r", "")
            _workdir_rule = (
                f"\n\n## 文件保存规则\n"
                f"1. 所有文件保存到: `{task_workdir}`\n"
                f"2. 任务完成后调用 report_done(summary, files)\n"
                f"\n## 用户指令\n<user_instruction>\n{safe_instruction}\n</user_instruction>"
            )
            _task_rules = (
                "\n\n## 完成规则\n"
                "成功 → report_done(summary, files)\n"
                "失败 → report_failure(reason, attempted)\n"
                "禁止：同一工具失败超2次、假装已发图片\n"
            )
            persona = (persona or "") + _workdir_rule + _task_rules

            request = ProviderRequest(
                prompt=request_text,
                image_urls=[],
                func_tool=toolset,
                contexts=[],
                system_prompt=persona,
                session_id=f"maid_{task.id}",
            )

            provider_key = id(provider)
            if provider_key not in _provider_config_locks:
                if len(_provider_config_locks) >= _PROVIDER_LOCK_MAX:
                    _provider_config_locks.popitem(last=False)
                _provider_config_locks[provider_key] = asyncio.Lock()
            else:
                _provider_config_locks.move_to_end(provider_key)
            lock = _provider_config_locks[provider_key]

            async with lock:
                await runner.reset(
                    provider=provider,
                    request=request,
                    run_context=AgentContextWrapper(context=agent_context, tool_call_timeout=600),
                    tool_executor=FunctionToolExecutor(),
                    agent_hooks=BaseAgentRunHooks(),
                    streaming=False,
                    enforce_max_turns=30,
                )

                step_count = 0
                _start_time = time.monotonic()
                _overall_timeout = self.maid_config.maid_overall_timeout
                _logged_msg_count_ref = [0]
                sent_media_count = 0

                while not runner.done() and step_count < 30:
                    step_count += 1
                    if task.status in ("completed", "cancelled", "failed"):
                        runner.request_stop()
                        break
                    if task.cancel_event and task.cancel_event.is_set():
                        runner.request_stop()
                        break
                    if task.done_event and task.done_event.is_set():
                        runner.request_stop()
                        break
                    if _overall_timeout > 0 and time.monotonic() - _start_time > _overall_timeout:
                        task.result_text = f"管家执行超时"
                        task.status = "failed"
                        break

                    async for _ in runner.step():
                        if task.done_event and task.done_event.is_set():
                            runner.request_stop()
                        if task.cancel_event and task.cancel_event.is_set():
                            runner.request_stop()

                    # 实时转发虚拟对话捕获的消息（send_result/send_message 途径）
                    while fake_event.captured_chains:
                        chain = fake_event.captured_chains.pop(0)
                        items = chain.chain if hasattr(chain, 'chain') else chain
                        for comp in items:
                            if isinstance(comp, (Image, Record)):
                                sent_media_count += 1

                    if task.status in ("completed", "failed"):
                        break

                    _logged_msg_count_ref[0] = self._log_step_thinking(runner, step_count, _logged_msg_count_ref)

                # 处理剩余捕获
                while fake_event.captured_chains:
                    chain = fake_event.captured_chains.pop(0)
                    items = chain.chain if hasattr(chain, 'chain') else chain
                    for comp in items:
                        if isinstance(comp, (Image, Record)):
                            sent_media_count += 1

            # ★ 兜底扫描
            scan_dirs = [task_workdir] + self.maid_config.maid_extra_scan_dirs
            scan_dirs = list(dict.fromkeys(scan_dirs))
            if self.maid_config.debug:
                logger.info(f"[铃兰女仆] 扫描目录: {scan_dirs}")

            collector = MediaCollector(task_start_time=_task_start_time, scan_dirs=scan_dirs)
            for msg in runner.run_context.messages:
                if msg.role == "tool":
                    content = self._extract_text(msg.content)
                    if content.strip():
                        tool_name = getattr(msg, 'name', '') or ''
                        collector.parse_tool_result(tool_name, content)
            if hasattr(task, 'reported_files') and task.reported_files:
                collector.add_reported_files(task.reported_files)
            media = collector.finalize()

            # ★ 去重：用 forwarded_paths 排除已通过 event.send() 发送的文件
            # [BUGFIX] forwarded_paths 中的路径已经过 _normalize_path 规范化，
            # 所以这边也要用 _normalize_path 处理后再比较，确保格式一致
            deduped_images = [p for p in media.images if _normalize_path(p) not in fake_event.forwarded_paths]
            deduped_audios = [p for p in media.audios if _normalize_path(p) not in fake_event.forwarded_paths]
            deduped_files = [p for p in (media.documents + media.videos) if _normalize_path(p) not in fake_event.forwarded_paths]

            logger.info(
                f"[铃兰女仆] 📋 去重统计: forwarded_paths={len(fake_event.forwarded_paths)}, "
                f"原始图片={len(media.images)}, 去重后={len(deduped_images)}, "
                f"原始音频={len(media.audios)}, 去重后={len(deduped_audios)}"
            )

            # manual 模式追加
            captured_images, captured_audios, captured_files = self._extract_paths_from_captured(fake_event.captured_chains)
            if self.maid_config.proactive_mode == "manual":
                for img_path in captured_images:
                    if _normalize_path(img_path) not in fake_event.forwarded_paths and img_path not in deduped_images:
                        deduped_images.append(img_path)
                for audio_path in captured_audios:
                    if _normalize_path(audio_path) not in fake_event.forwarded_paths and audio_path not in deduped_audios:
                        deduped_audios.append(audio_path)

            llm_resp = runner.get_final_llm_resp()
            if task.status == "running":
                task.result_text = llm_resp.completion_text if llm_resp and llm_resp.completion_text else "管家已完成任务。"
                task.status = "completed"
            elif task.status == "cancelled":
                child_task = self.task_manager.find_child_task(session_id, task.id)
                if child_task:
                    bg_task = asyncio.create_task(self._execute_maid_background(session_id, child_task, child_task.description, depth + 1, provider_id))
                    self._track_task(bg_task)
                    return

            # 移动文件到永久目录
            perm_dir = "/AstrBot/data/transparent_maid/permanent/"
            os.makedirs(perm_dir, exist_ok=True)
            final_images, final_audios, final_files = [], [], []

            def _move_file(src: str, prefix: str) -> str | None:
                if not src or not os.path.isfile(src):
                    return None
                filename = f"{prefix}_{task.id}_{os.path.basename(src)}"
                dst = os.path.join(perm_dir, filename)
                if os.path.exists(dst):
                    base, ext = os.path.splitext(filename)
                    dst = os.path.join(perm_dir, f"{base}_{int(time.time()*1000)}{ext}")
                shutil.move(src, dst)
                return dst

            for src in deduped_images:
                dst = _move_file(src, "img")
                if dst:
                    final_images.append(dst)
            for src in deduped_audios:
                dst = _move_file(src, "audio")
                if dst:
                    final_audios.append(dst)
            for src in deduped_files:
                dst = _move_file(src, "file")
                if dst:
                    final_files.append(dst)

            if hasattr(task, 'reported_files') and task.reported_files:
                for src in task.reported_files:
                    if os.path.exists(src):
                        ext = os.path.splitext(src)[1].lower()
                        if ext in ('.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp'):
                            dst = _move_file(src, "reported_img")
                            if dst:
                                final_images.append(dst)
                        elif ext in ('.mp3', '.wav', '.ogg', '.m4a'):
                            dst = _move_file(src, "reported_audio")
                            if dst:
                                final_audios.append(dst)
                        else:
                            dst = _move_file(src, "reported_file")
                            if dst:
                                final_files.append(dst)

            task.result_images = final_images
            task.result_audios = final_audios
            task.result_files = final_files
            task._sent_media_count = sent_media_count
            logger.info(
                f"[铃兰女仆] ✅ 管家执行完成: {task.id}, "
                f"实时转发媒体={sent_media_count}, "
                f"兜底图片={len(final_images)}, 兜底音频={len(final_audios)}, "
                f"兜底文件={len(final_files)}"
            )

            # ★★★ 先注销拦截，再通知（避免误拦截主代理回复）★★★
            self._unregister_interception(session_id)
            await self._finish_and_notify(session_id, task, provider_id=provider_id)

            child_task = self.task_manager.find_child_task(session_id, task.id)
            if child_task and task.status != "cancelled":
                bg_task = asyncio.create_task(self._execute_maid_background(session_id, child_task, child_task.description, depth + 1, provider_id))
                self._track_task(bg_task)

        except Exception as exc:
            logger.error(f"[铃兰女仆] 🔴 后台管家失败: {exc}", exc_info=True)
            task.result_text = "后台任务执行出错"
            task.status = "completed"
            self._unregister_interception(session_id)
            await self._finish_and_notify(session_id, task)
        finally:
            if fake_event:
                active_event_registry.unregister(fake_event)
            if task_workdir and os.path.exists(task_workdir):
                shutil.rmtree(task_workdir, ignore_errors=True)

    @staticmethod
    def _extract_text(content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for p in content:
                if isinstance(p, str):
                    parts.append(p)
                elif isinstance(p, dict):
                    if p.get('type') == 'text' and 'text' in p:
                        parts.append(p['text'])
                    elif 'text' in p:
                        parts.append(p['text'])
                elif hasattr(p, 'model_dump'):
                    try:
                        d = p.model_dump()
                        if d.get('type') == 'text' and 'text' in d:
                            parts.append(d['text'])
                        elif 'text' in d:
                            parts.append(d['text'])
                    except:
                        pass
                elif hasattr(p, 'text'):
                    parts.append(getattr(p, 'text', ''))
            return " ".join(parts)
        return str(content) if content else ""

    def _log_step_thinking(self, runner, step_count: int, _logged_msg_count_ref: list) -> int:
        messages = runner.run_context.messages
        total = len(messages)
        if total <= _logged_msg_count_ref[0]:
            return _logged_msg_count_ref[0]
        new_msgs = messages[_logged_msg_count_ref[0]:]
        new_count = _logged_msg_count_ref[0]
        for msg in new_msgs:
            new_count += 1
            if msg.role in ("assistant", "ai"):
                text = self._extract_text(msg.content) if hasattr(msg, 'content') else ""
                if text.strip():
                    display = text.strip()[:500]
                    if len(text.strip()) > 500:
                        display += "...（截断）"
                    logger.info(f"[铃兰女仆] 💭 步骤{step_count} 想法：{display}")
                tool_calls = None
                if hasattr(msg, 'model_dump'):
                    try:
                        tool_calls = msg.model_dump(exclude_none=True).get('tool_calls')
                    except:
                        pass
                if not tool_calls:
                    for attr in ('tool_calls', 'function_call'):
                        tool_calls = getattr(msg, attr, None)
                        if tool_calls:
                            break
                if tool_calls:
                    if isinstance(tool_calls, list):
                        for tc in tool_calls:
                            name = ""
                            if isinstance(tc, dict):
                                name = tc.get('name', '') or tc.get('function', {}).get('name', '')
                            elif hasattr(tc, 'name'):
                                name = tc.name
                            if name:
                                logger.info(f"[铃兰女仆] 🛠️ 步骤{step_count} 调用工具：{name}")
            elif msg.role == "tool":
                content = self._extract_text(msg.content)
                if content.strip():
                    display = content.strip()[:300]
                    if len(content.strip()) > 300:
                        display += "...（截断）"
                    logger.info(f"[铃兰女仆] 🔧 步骤{step_count} 工具返回：{display}")
        return new_count

    async def _send_message(self, session_id: str, chain: MessageChain) -> bool:
        if hasattr(self.context, 'send_message'):
            try:
                await self.context.send_message(session_id, chain)
                return True
            except Exception as e:
                logger.error(f"[铃兰女仆] 🔴 send_message(context) 失败: {e}")
                return False

    def _split_into_segments(self, text: str) -> list[str]:
        if not self.maid_config.segment_enabled or len(text) <= self.maid_config.segment_max_length:
            return [text]
        filtered = text
        if self.maid_config.content_filter_regex:
            try:
                filtered = re.sub(self.maid_config.content_filter_regex, '', filtered)
            except re.error:
                pass
        if self.maid_config.segment_pattern_type == "regex":
            try:
                parts, last_end = [], 0
                for m in re.finditer(self.maid_config.segment_regex, filtered):
                    if m.start() > last_end:
                        parts.append(filtered[last_end:m.start()].strip())
                    parts.append(filtered[m.start():m.end()])
                    last_end = m.end()
                if last_end < len(filtered):
                    parts.append(filtered[last_end:].strip())
                segments = [p for p in parts if p.strip()]
            except re.error:
                segments = [filtered]
        else:
            words = self.maid_config.segment_words
            pattern = '|'.join(re.escape(w) for w in words)
            try:
                parts, last_end = [], 0
                for m in re.finditer(pattern, filtered):
                    if m.start() > last_end:
                        parts.append(filtered[last_end:m.start()].strip())
                    parts.append(filtered[m.start():m.end()])
                    last_end = m.end()
                if last_end < len(filtered):
                    parts.append(filtered[last_end:].strip())
                segments = [p for p in parts if p.strip()]
            except re.error:
                segments = [filtered]
        return segments if segments else [filtered]

    def _calc_segment_delay(self, segment_text: str) -> float:
        mode = self.maid_config.segment_delay_mode
        if mode == "fixed":
            return self.maid_config.segment_delay_fixed
        elif mode == "random":
            return random.uniform(self.maid_config.segment_delay_random_min, self.maid_config.segment_delay_random_max)
        elif mode == "log":
            length = len(segment_text)
            base = self.maid_config.segment_delay_log_base
            if base <= 1:
                base = 2.6
            return math.log(length, base) if length > 0 else 0.5
        return 1.0

    async def _send_segmented_text(self, session_id: str, text: str):
        if not text:
            return
        segments = self._split_into_segments(text)
        for i, seg in enumerate(segments):
            if not seg.strip():
                continue
            await self._send_message(session_id, MessageChain([Plain(seg)]))
            if i < len(segments) - 1:
                delay = self._calc_segment_delay(seg)
                if delay > 0:
                    await asyncio.sleep(delay)

    async def _send_fallback_media(self, session_id: str, task):
        media_delay = self.maid_config.message_delay
        for img_path in (task.result_images or []):
            if img_path and os.path.isfile(img_path):
                await self._send_message(session_id, MessageChain([Image(file=img_path)]))
                if media_delay > 0:
                    await asyncio.sleep(media_delay)
        for audio_path in (task.result_audios or []):
            if audio_path and os.path.isfile(audio_path):
                await self._send_message(session_id, MessageChain([Record(file=audio_path)]))
                if media_delay > 0:
                    await asyncio.sleep(media_delay)
        for file_path in (task.result_files or []):
            if file_path and os.path.isfile(file_path):
                await self._send_message(session_id, MessageChain([Plain(f"[文件] {file_path}")]))

    # ========== 任务完成通知 ==========
    async def _finish_and_notify(self, session_id: str, task, provider_id: str = None):
        sent_media_count = getattr(task, '_sent_media_count', 0)
        has_fallback_media = bool(task.result_images or task.result_audios or task.result_files)
        fallback_count = len(task.result_images or []) + len(task.result_audios or []) + len(task.result_files or [])
        logger.info(
            f"[铃兰女仆] 📣 _finish_and_notify: session={session_id[-12:]}, "
            f"task={task.id}, status={task.status}, "
            f"realtime_sent={sent_media_count}, fallback_media={fallback_count}"
        )

        if self.maid_config.proactive_mode == "manual":
            inject_text = task.result_text or "管家已完成任务。"
            if sent_media_count > 0:
                inject_text += f"\n（已实时发送 {sent_media_count} 个媒体文件）"
            self.task_manager.set_pending_result(session_id, {
                "text": inject_text,
                "images": task.result_images or [],
                "audios": task.result_audios or [],
                "files": task.result_files or [],
            })
            return

        try:
            if has_fallback_media:
                await self._send_fallback_media(session_id, task)

            conv_id = await self.context.conversation_manager.get_curr_conversation_id(session_id)
            history = []
            if conv_id:
                conv = await self.context.conversation_manager.get_conversation(session_id, conv_id)
                if conv and conv.history:
                    if hasattr(conv, 'get_messages'):
                        history = conv.get_messages()
                    elif hasattr(conv, 'messages') and isinstance(conv.messages, list):
                        history = conv.messages
                    else:
                        history = json.loads(conv.history)

            contexts = []
            for msg in history:
                role = msg.get("role")
                if role == "tool":
                    continue
                contexts.append({"role": role, "content": msg.get("content", "")})

            media_parts = []
            if sent_media_count > 0:
                media_parts.append(f"{sent_media_count}个媒体文件已实时发送")
            if fallback_count > 0:
                media_parts.append(f"{fallback_count}个附加文件已发送")
            notification = f"[系统通知] 管家任务已完成。结果：{task.result_text or '任务完成'}。"
            if media_parts:
                notification += f"（{'、'.join(media_parts)}，请用你自己的口吻自然地告知用户，不要重复发送任何文件。）"
            else:
                notification += "请用你自己的口吻自然地告知用户。"
            contexts.append({"role": "system", "content": notification})

            persona_reminder = ""
            try:
                if conv_id:
                    conv = await self.context.conversation_manager.get_conversation(session_id, conv_id)
                    if conv and self.context.persona_manager:
                        persona_id = getattr(conv, 'persona_id', None)
                        if persona_id:
                            p = await self.context.persona_manager.get_persona(persona_id)
                            if p and hasattr(p, 'prompt') and p.prompt:
                                persona_reminder = p.prompt
            except:
                pass
            if persona_reminder:
                contexts.insert(0, {"role": "system", "content": persona_reminder})

            if not provider_id:
                provider_id = await self.context.get_current_chat_provider_id(session_id)
            llm_result = await self.context.llm_generate(prompt="", contexts=contexts, chat_provider_id=provider_id, temperature=0.7)
            reply_text = llm_result.completion_text.strip() if llm_result and llm_result.completion_text else ""
            if not reply_text:
                reply_text = f"{self.maid_config.maid_master_title}，任务完成啦～✨"
            await self._send_segmented_text(session_id, reply_text)

            if conv_id:
                try:
                    conv_obj = await self.context.conversation_manager.get_conversation(session_id, conv_id)
                    curr_history = json.loads(conv_obj.history) if conv_obj else []
                    curr_history.append({"role": "assistant", "content": reply_text})
                    await self.context.conversation_manager.update_conversation(session_id, conv_id, curr_history)
                except:
                    pass
            logger.info(f"[铃兰女仆] 💬 主代理回复已发送: {reply_text[:80]}...")
        except Exception as e:
            logger.error(f"[铃兰女仆] 🔴 主动通知失败: {e}", exc_info=True)
        finally:
            self.task_manager.done(session_id, task.id)

    @filter.on_llm_request()
    async def _on_inject_maid_system_prompt(self, event: AstrMessageEvent, req: ProviderRequest):
        """在每次 LLM 请求时注入主代理系统提示词，告诉 AI 什么时候该派管家。"""
        prompt = self.maid_config.maid_system_prompt
        if prompt:
            req.contexts.append({"role": "system", "content": prompt})
            if self.maid_config.debug:
                logger.info(f"[铃兰女仆] 💉 管家系统提示词已注入 (长度={len(prompt)})")

    @filter.on_llm_request()
    async def _on_message_inject_pending(self, event: AstrMessageEvent, req: ProviderRequest):
        """当 manual 模式有待处理结果时，将结果注入到 LLM 上下文中。"""
        session_id = event.unified_msg_origin
        pending = self.task_manager.pop_pending_result(session_id)
        if not pending:
            return
        try:
            inject_text = "[系统通知] 任务已完成\n" + pending.get('text', '')
            conv_id = await self.context.conversation_manager.get_curr_conversation_id(session_id)
            if conv_id:
                await self.context.conversation_manager.append_context(session_id, conv_id, role="system", content=inject_text)
            req.contexts.append({"role": "system", "content": inject_text})
            if pending.get('images'):
                event.set_extra("maid_pending_images", pending['images'])
            if pending.get('audios'):
                event.set_extra("maid_pending_audios", pending['audios'])
            if pending.get('files'):
                event.set_extra("maid_pending_files", pending['files'])
        except Exception as e:
            logger.warning(f"[铃兰女仆] 🟡 manual 模式注入失败: {e}")

    @filter.after_message_sent()
    async def _after_message_sent_cleanup(self, event: AstrMessageEvent):
        """在主代理发送消息后，将管家产生的图片/音频/文件一并发送出去。"""
        images = event.get_extra("maid_pending_images")
        if images:
            event.set_extra("maid_pending_images", None)
            for img_path in images:
                if img_path and os.path.isfile(img_path):
                    await self.context.send_message(event.unified_msg_origin, MessageChain(chain=[Image(file=img_path)]))
        audios = event.get_extra("maid_pending_audios")
        if audios:
            event.set_extra("maid_pending_audios", None)
            for audio_path in audios:
                if audio_path and os.path.isfile(audio_path):
                    await self.context.send_message(event.unified_msg_origin, MessageChain(chain=[Record(file=audio_path)]))
        files = event.get_extra("maid_pending_files")
        if files:
            event.set_extra("maid_pending_files", None)
            for file_path in files:
                if file_path and os.path.isfile(file_path):
                    await self.context.send_message(event.unified_msg_origin, MessageChain(chain=[Plain(f"[文件] {file_path}")]))

        pending = event.get_extra(PENDING_MAID_RESULT_KEY)
        session_id = event.unified_msg_origin
        completed_tasks = []
        if hasattr(self.task_manager, 'pop_completed_tasks'):
            completed_tasks = self.task_manager.pop_completed_tasks(session_id)
        for task in completed_tasks:
            sent_media_count = getattr(task, '_sent_media_count', 0)
            inject_text = f"[管家任务完成] {task.result_text}"
            if sent_media_count > 0:
                inject_text += f"（{sent_media_count}个媒体文件已实时发送）"
            try:
                conv_id = await self.context.conversation_manager.get_curr_conversation_id(session_id)
                if conv_id:
                    await self.context.conversation_manager.append_context(session_id, conv_id, role="system", content=inject_text)
            except:
                pass

    def _track_task(self, task: asyncio.Task):
        self._active_tasks.add(task)
        def _on_done(t: asyncio.Task):
            self._active_tasks.discard(t)
            if not t.cancelled():
                try:
                    t.result()
                except Exception as e:
                    logger.error(f"[铃兰女仆] 🔴 后台任务异常: {e}", exc_info=True)
        task.add_done_callback(_on_done)
