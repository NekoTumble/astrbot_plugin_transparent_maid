"""
管家内控工具 — 让管家能自主管理任务生命周期
v2 支持多任务并行管理：stop_task、cancel_all、list_tasks
v3 支持 report_done(files=...) 和 report_failure
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from astrbot.api import logger

if TYPE_CHECKING:
    from .task_manager import TaskManager


class MaidSelfTools:
    """管家专属内控工具集"""

    def __init__(self, task_manager: TaskManager, session_id: str, task_id: str = ""):
        self.tm = task_manager
        self.session_id = session_id
        self.task_id = task_id  # 当前管家自己的任务ID，用于 stop_self 精确定位

    # ========== 工具函数 ==========

    async def stop_self(self, reason: str = "") -> str:
        """
        取消当前任务。当任务无法完成或出现不可恢复的错误时调用。

        Args:
            reason(string): 取消原因（可选）
        """
        if self.task_id:
            task = self.tm.get_by_id(self.session_id, self.task_id)
        else:
            task = self.tm.get(self.session_id)

        if task is None or task.status != "running":
            return "当前没有可取消的任务。"

        self.tm.stop_by_id(self.session_id, task.id)
        logger.info(f"[管家内控] 🤖 任务已取消: {task.id}, 原因: {reason or '管家主动取消'}")
        return f"任务已取消：{reason or '管家主动取消'}"

    async def stop_task(self, task_id: str, reason: str = "") -> str:
        """
        按任务ID取消指定的后台任务。可取消任意任务（不限于当前上下文）。

        Args:
            task_id(string): 要取消的任务ID（通过 list_tasks 查看）
            reason(string): 取消原因（可选）
        """
        success = self.tm.stop_by_id(self.session_id, task_id)
        if success:
            logger.info(f"[管家内控] 🤖 已按ID取消任务: {task_id}, 原因: {reason or '主动取消'}")
            return f"任务 {task_id[:12]} 已取消：{reason or '主动取消'}"
        else:
            return f"未找到运行中的任务: {task_id[:12]}"

    async def cancel_all(self, reason: str = "") -> str:
        """
        取消当前会话的所有正在运行的任务。

        Args:
            reason(string): 取消原因（可选）
        """
        count = self.tm.cancel_all(self.session_id)
        if count > 0:
            logger.info(f"[管家内控] 🤖 已取消全部 {count} 个任务, 原因: {reason or '主动取消'}")
            return f"已取消全部 {count} 个任务：{reason or '主动取消'}"
        else:
            return "当前没有运行中的任务可取消。"

    async def list_tasks(self) -> str:
        """
        列出当前会话的所有管家任务及状态。
        返回格式：任务ID | 描述（截断） | 状态 | 创建时间
        """
        tasks = self.tm.list_tasks(self.session_id)
        if not tasks:
            return "当前没有任务记录。"
        lines = ["📋 当前会话任务列表："]
        for t in tasks:
            status_icon = "🟢" if t["status"] == "running" else ("✅" if t["status"] == "completed" else "❌")
            lines.append(f" {status_icon} `{t['id']}` | {t['description']} | {t['status']} | {t['created_at']}")
        return "\n".join(lines)

    async def modify_task(self, event, additional_instruction: str) -> str:
        """
        追加新指令到当前任务。旧任务会被取消，新任务包含原始指令 + 追加内容。

        Args:
            event: AstrBot 事件
            additional_instruction(string): 要追加的指令内容
        """
        task = self.tm.steer(self.session_id, additional_instruction)
        if task is None:
            return "当前没有可修改的任务。"
        logger.info(f"[管家内控] 🤖 任务已修改: {task.id}, 新描述: {task.description[:80]}...")
        return f"任务已更新，新指令已追加。"

    async def report_done(self, event, summary: str = "", files: list = None) -> str:
        """
        标记当前会话任务为已完成。管家完成全部工作后调用此工具收尾。

        Args:
            event: AstrBot 事件
            summary(string): 任务完成摘要
            files(array of string): 产出的文件绝对路径列表（图片、音频、视频、文档均可，可选）
        """
        if self.task_id:
            task = self.tm.get_by_id(self.session_id, self.task_id)
        else:
            task = self.tm.get(self.session_id)

        if task is None or task.status != "running":
            return "当前没有运行中的任务。"

        task.result_text = summary or task.result_text or "管家已完成任务。"
        task.status = "completed"

        # 保存报告的文件路径到 task 对象
        if files:
            task.reported_files = files
            logger.info(f"[管家内控] 🤖 任务标记完成: {task.id}，报告文件: {files}")
        else:
            task.reported_files = []
            logger.info(f"[管家内控] 🤖 任务标记完成: {task.id}")

        if task.done_event and not task.done_event.is_set():
            task.done_event.set()
        if task.cancel_event and not task.cancel_event.is_set():
            task.cancel_event.set()

        return "任务已标记为完成。"

    async def report_failure(self, event, reason: str = "", attempted: str = "") -> str:
        """
        任务执行失败时调用。报告失败原因，停止执行。
        适用场景：工具连续失败2次、权限不足、遇到无法解决的问题。

        Args:
            event: AstrBot 事件
            reason(string): 失败原因，用自然语言描述
            attempted(string): 你尝试了什么方法（可选）
        """
        if self.task_id:
            task = self.tm.get_by_id(self.session_id, self.task_id)
        else:
            task = self.tm.get(self.session_id)

        if task is None:
            return "当前没有活跃任务。"

        detail = reason or "管家执行任务时遇到问题"
        if attempted:
            detail = f"尝试了：{attempted}。失败原因：{reason}"

        task.result_text = detail
        task.status = "failed"
        task.reported_files = []

        if task.done_event and not task.done_event.is_set():
            task.done_event.set()
        if task.cancel_event and not task.cancel_event.is_set():
            task.cancel_event.set()

        logger.info(f"[管家内控] ❌ 任务失败: {task.id} - {detail}")
        return "已报告任务失败。请停止尝试，等待指示。"
