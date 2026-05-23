"""
任务管理器 — 维护活跃管家任务的生命周期
v2 支持多任务并行：同一 session 可同时执行多个管家任务。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from astrbot.api import logger


@dataclass
class Task:
    """单个管家任务"""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    description: str = ""
    status: str = "running"  # running | completed | failed | cancelled
    created_at: float = field(default_factory=time.time)
    parent_task_id: Optional[str] = None
    cancel_event: Optional[asyncio.Event] = None
    done_event: Optional[asyncio.Event] = None
    session_id: str = ""

    # 多媒体结果支持
    result_text: str = ""
    result_images: list[str] = field(default_factory=list)
    result_audios: list[str] = field(default_factory=list)
    result_files: list[str] = field(default_factory=list)

    # 管家通过 report_done 报告的文件路径
    reported_files: list[str] = field(default_factory=list)


class TaskManager:
    """管家任务管理器（支持单任务/多任务两种模式）"""

    def __init__(self) -> None:
        # _tasks[session_id] = {task_id: Task, ...}
        self._tasks: dict[str, dict[str, Task]] = {}
        # 已完成但暂未汇报的任务ID索引（用于钩子检查）
        self._completed_tasks: dict[str, set[str]] = {}
        # manual 降级模式的待处理结果（不依赖 event extra）
        self._pending_manual_results: dict[str, dict] = {}

    # ========== 查询 ==========

    def _get_tasks_map(self, session_id: str) -> dict[str, Task]:
        """获取会话的任务字典（不存在则自动创建）"""
        if session_id not in self._tasks:
            self._tasks[session_id] = {}
        return self._tasks[session_id]

    def get(self, session_id: str) -> Optional[Task]:
        """
        获取最新活跃任务（兼容旧版单任务模式）。
        多任务模式下返回第一个 running 的任务，无则返回最新 created 的任务。
        """
        tasks_map = self._tasks.get(session_id)
        if not tasks_map:
            return None
        # 先找 running 的
        for task in tasks_map.values():
            if task.status == "running":
                return task
        # 没有 running 的，返回最新创建的
        sorted_tasks = sorted(tasks_map.values(), key=lambda t: t.created_at, reverse=True)
        return sorted_tasks[0] if sorted_tasks else None

    def get_by_id(self, session_id: str, task_id: str) -> Optional[Task]:
        """通过任务ID获取指定任务"""
        tasks_map = self._tasks.get(session_id)
        if not tasks_map:
            return None
        return tasks_map.get(task_id)

    def list_tasks(self, session_id: str) -> list[dict]:
        """列出当前会话的所有任务（用于内控工具）"""
        tasks_map = self._tasks.get(session_id, {})
        return [
            {
                "id": t.id,
                "description": t.description[:50] + ("..." if len(t.description) > 50 else ""),
                "status": t.status,
                "created_at": time.strftime("%H:%M:%S", time.localtime(t.created_at)),
            }
            for t in sorted(tasks_map.values(), key=lambda x: x.created_at)
        ]

    def has_active(self, session_id: str) -> bool:
        """检查会话是否有活跃任务"""
        tasks_map = self._tasks.get(session_id)
        if not tasks_map:
            return False
        return any(t.status == "running" for t in tasks_map.values())

    def count_active(self, session_id: str) -> int:
        """统计活跃任务数量"""
        tasks_map = self._tasks.get(session_id)
        if not tasks_map:
            return 0
        return sum(1 for t in tasks_map.values() if t.status == "running")

    # ========== 派遣与取消 ==========

    def dispatch(self, session_id: str, description: str = "", multi_task: bool = False) -> Task:
        """
        创建新任务。
        Args:
            session_id: 会话ID
            description: 任务描述
            multi_task: 是否多任务模式。True=追加新任务，False=取消旧任务再创建
        Returns:
            新创建的 Task
        """
        tasks_map = self._get_tasks_map(session_id)

        # 单任务模式：取消旧任务
        if not multi_task:
            for tid, old in list(tasks_map.items()):
                if old.status == "running":
                    old.status = "cancelled"
                    if old.cancel_event:
                        old.cancel_event.set()

        # 创建新任务
        task = Task(session_id=session_id, description=description)
        task.cancel_event = asyncio.Event()
        task.done_event = asyncio.Event()
        tasks_map[task.id] = task
        return task

    def stop(self, session_id: str, task_id: str = "") -> bool:
        """
        软取消指定任务。
        Args:
            session_id: 会话ID
            task_id: 任务ID，为空则取消最新创建的那个
        Returns:
            是否成功取消
        """
        tasks_map = self._tasks.get(session_id)
        if not tasks_map:
            return False

        if task_id:
            task = tasks_map.get(task_id)
        else:
            # 找最新的 running 任务
            running = [t for t in tasks_map.values() if t.status == "running"]
            if not running:
                return False
            task = max(running, key=lambda t: t.created_at)

        if task and task.status == "running":
            task.status = "cancelled"
            if task.cancel_event:
                task.cancel_event.set()
            return True
        return False

    def stop_by_id(self, session_id: str, task_id: str) -> bool:
        """按ID取消任务（给内控工具用）"""
        return self.stop(session_id, task_id)

    def cancel_all(self, session_id: str) -> int:
        """取消会话所有活跃任务，返回取消数量"""
        tasks_map = self._tasks.get(session_id)
        if not tasks_map:
            return 0
        count = 0
        for task in tasks_map.values():
            if task.status == "running":
                task.status = "cancelled"
                if task.cancel_event:
                    task.cancel_event.set()
                count += 1
        return count

    def done(self, session_id: str, task_id: str = "") -> None:
        """
        标记任务完成。
        Args:
            session_id: 会话ID
            task_id: 任务ID。为空则标记最新创建的任务
        """
        tasks_map = self._tasks.get(session_id)
        if not tasks_map:
            return

        if task_id:
            task = tasks_map.get(task_id)
        else:
            # 找最新的 running 任务
            running = [t for t in tasks_map.values() if t.status == "running"]
            if running:
                task = max(running, key=lambda t: t.created_at)
            else:
                # 如果没有 running 的，找最新的 cancelled
                cancelled = [t for t in tasks_map.values() if t.status == "cancelled"]
                task = max(cancelled, key=lambda t: t.created_at) if cancelled else None

        if task:
            if task.status in ("running", "failed"):
                task.status = "completed"
            logger.debug(f"[铃兰女仆] ✅ 任务完成: session={session_id[-12:]}, task={task.id}")

            # 清理已完成的旧任务（保留最近的10个）
            all_tasks = sorted(tasks_map.values(), key=lambda t: t.created_at, reverse=True)
            if len(all_tasks) > 10:
                for t in all_tasks[10:]:
                    if t.status in ("completed", "cancelled", "failed"):
                        tasks_map.pop(t.id, None)

    def complete_task(self, session_id: str, task_id: str) -> None:
        """标记指定任务为完成状态（用于通知已发送后的清理）"""
        if not task_id:
            return
        tasks_map = self._tasks.get(session_id)
        if not tasks_map:
            return
        task = tasks_map.get(task_id)
        if task:
            task.status = "completed"
        self._completed_tasks.setdefault(session_id, set()).add(task_id)

    def pop_completed_tasks(self, session_id: str) -> list[Task]:
        """取出并清除本会话已完成的任务对象列表（用于注入主代理上下文）"""
        completed_ids = self._completed_tasks.pop(session_id, set())
        tasks_map = self._tasks.get(session_id, {})
        return [tasks_map[tid] for tid in completed_ids if tid in tasks_map]

    # ========== Manual 降级模式 ==========

    def set_pending_result(self, session_id: str, result: dict) -> None:
        """存储 manual 降级模式的待处理结果（下一轮消息时注入）"""
        self._pending_manual_results[session_id] = result

    def pop_pending_result(self, session_id: str) -> dict | None:
        """取出并清除 manual 降级模式的待处理结果"""
        return self._pending_manual_results.pop(session_id, None)

    # ========== 废弃清理 ==========

    def steer(self, session_id: str, additional_description: str, multi_task: bool = False) -> Optional[Task]:
        """
        追加指令：取消旧任务并创建新任务（合并新旧指令）。
        Args:
            session_id: 会话ID
            additional_description: 要追加的指令
            multi_task: 是否多任务模式
        Returns:
            新创建的 Task，如果没有旧任务则返回 None
        """
        tasks_map = self._tasks.get(session_id)
        if not tasks_map:
            return None

        # 找最新的 running 任务
        running = [t for t in tasks_map.values() if t.status == "running"]
        if not running:
            return None
        old = max(running, key=lambda t: t.created_at)
        parent_id = old.id
        old_desc = old.description

        # 单任务模式：取消旧任务
        if not multi_task:
            if old.status == "running":
                old.status = "cancelled"
                if old.cancel_event:
                    old.cancel_event.set()

        # 创建新任务
        task = Task(
            session_id=session_id,
            description=old_desc + "\n\n【追加指令】" + additional_description,
            parent_task_id=parent_id,
        )
        task.cancel_event = asyncio.Event()
        task.done_event = asyncio.Event()
        tasks_map[task.id] = task
        return task

    def check_dispatch_fuse(self, session_id: str, config) -> bool:
        """熔断检测：遍历当前会话所有任务，不限 parent 链。"""
        now = time.time()
        window_start = now - config.dispatch_window_seconds
        tasks_map = self._tasks.get(session_id, {})
        recent_count = sum(
            1 for t in tasks_map.values()
            if t.created_at >= window_start and t.status == "running"
        )
        if recent_count >= config.max_consecutive_dispatch:
            logger.warning(f"[铃兰女仆] 🔴 熔断触发: session={session_id[-12:]}, 近{config.dispatch_window_seconds}秒有{recent_count}个运行中任务")
            return True
        return False

    def find_child_task(self, session_id: str, parent_task_id: str):
        """查找指定父任务的子任务（用于 steer 产生的任务自动执行）"""
        tasks_map = self._tasks.get(session_id, {})
        for t in tasks_map.values():
            if t.parent_task_id == parent_task_id and t.status == "running":
                return t
        return None

    def cleanup_stale(self, max_age_seconds: float = 600) -> int:
        """清理过期任务（>10分钟）"""
        now = time.time()
        stale_count = 0
        for session_id, tasks_map in list(self._tasks.items()):
            stale_ids = [
                tid for tid, task in tasks_map.items()
                if now - task.created_at > max_age_seconds
            ]
            for tid in stale_ids:
                tasks_map.pop(tid, None)
                stale_count += 1
            if not tasks_map:
                self._tasks.pop(session_id, None)
        return stale_count
