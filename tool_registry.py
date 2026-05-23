"""
工具注册表 — 从全局工具管理器中匹配管家所需工具
用法：
- tool_names 为空或包含 "*"：获取所有全局 LLM 工具 + 框架内置工具（自动排除 transfer_to_maid 防递归）
- 否则按名称匹配（同时搜索插件工具和内置工具）
"""
from __future__ import annotations
from functools import partial
from typing import TYPE_CHECKING, Any, Callable, Awaitable

from astrbot.api import logger
from astrbot.core.agent.tool import FunctionTool

if TYPE_CHECKING:
    from astrbot.api.star import Context

# ★ 管家不应使用的工具（防递归调度）
# send_message_to_user 已移除 — 管家需要用它发送媒体，由虚拟对话拦截
_BLOCKLIST = {"transfer_to_maid"}


def resolve_maid_tools(context: Context, tool_names: list[str], debug: bool = False):
    """从全局工具管理器匹配工具名，返回工具实例列表（包含插件工具和内置工具）。"""
    mgr = context.get_llm_tool_manager()

    # 空列表或通配符 → 获取所有插件工具 + 内置工具
    if not tool_names or "*" in tool_names:
        # 插件注册的工具
        all_plugin_tools = list(mgr.func_list)
        resolved = [
            t for t in all_plugin_tools
            if getattr(t, "active", True) and t.name not in _BLOCKLIST
        ]

        # 框架内置工具
        for bt in mgr.iter_builtin_tools():
            if getattr(bt, "active", True) and bt.name not in _BLOCKLIST:
                resolved.append(bt)

        blocked = [t.name for t in all_plugin_tools if t.name in _BLOCKLIST]
        if debug:
            logger.info(
                f"[铃兰女仆] 🔧 管家工具解析完成: 模式=全部(插件+内置), "
                f"命中={len(resolved)}个({[t.name for t in resolved]}), 屏蔽={blocked}"
            )
        return resolved

    # 按名称匹配
    resolved = []
    missing = []
    for name in tool_names:
        name = name.strip()
        # 先尝试插件工具
        tool = mgr.get_func(name)
        if tool is not None and getattr(tool, "active", True) and name not in _BLOCKLIST:
            resolved.append(tool)
            continue
        # 再尝试内置工具
        if mgr.is_builtin_tool(name) and name not in _BLOCKLIST:
            try:
                bt = mgr.get_builtin_tool(name)
                if getattr(bt, "active", True):
                    resolved.append(bt)
                    continue
            except KeyError:
                pass
        missing.append(name)

    if missing and debug:
        logger.warning(f"[铃兰女仆] 🟡 以下管家工具未在全局注册表中找到或已禁用: {missing}")

    if debug:
        logger.info(f"[铃兰女仆] 🔧 管家工具解析完成: 请求={tool_names}, 命中={[t.name for t in resolved]}, 缺失={missing}")

    return resolved


def create_task_tools(base_tools: list[FunctionTool], workdir: str) -> list[FunctionTool]:
    """为每个任务创建工具副本，只包装 handler 不为 None 且可调用的工具。"""
    task_tools = []
    for tool in base_tools:
        original_handler = getattr(tool, 'handler', None)
        if original_handler is not None and callable(original_handler):
            new_handler = partial(_wrap_handler, original_handler, workdir)
        else:
            new_handler = original_handler
        if original_handler is None:
            logger.debug(f"[铃兰女仆] ⏭️ 工具 {tool.name} 的 handler 为 None，跳过包装")

        new_tool = FunctionTool(
            name=tool.name,
            handler=new_handler,
            description=tool.description,
            parameters=tool.parameters,
            active=getattr(tool, 'active', True),
            is_background_task=getattr(tool, 'is_background_task', False),
            handler_module_path=getattr(tool, 'handler_module_path', None),
        )
        task_tools.append(new_tool)
    return task_tools


async def _wrap_handler(
    original_handler: Callable[..., Awaitable[str]],
    workdir: str,
    event,
    **kwargs: Any
):
    """包装工具 handler，自动改写输出路径参数到任务工作目录。"""
    for param in ('output_path', 'save_path'):
        if param in kwargs and not kwargs[param]:
            kwargs[param] = workdir
    return await original_handler(event, **kwargs)
