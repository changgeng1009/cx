"""任务状态机与断点持久化。

状态机的价值在于**拒绝非法流转**。用户要求"不隐藏失败"，而失败被隐藏
的常见方式就是状态乱跳：`failed` 悄悄变回 `running`，或者 `blocked`
被当成 `failed` 进而触发自动重试（在风控场景下这会加重风控）。

所以这里把合法流转写成显式表，非法流转直接抛异常。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .models import (
    TERMINAL_STATES,
    TaskState,
    TaskType,
    now_iso,
)

ALLOWED_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.PENDING: frozenset({TaskState.RUNNING, TaskState.CANCELLED}),
    TaskState.RUNNING: frozenset(
        {
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.BLOCKED,
            TaskState.NEEDS_MANUAL_ACTION,
            TaskState.PAUSED,
            TaskState.CANCELLED,
        }
    ),
    # 暂停 → 恢复
    TaskState.PAUSED: frozenset({TaskState.RUNNING, TaskState.CANCELLED}),
    # 失败 → 重试（重新入队）
    TaskState.FAILED: frozenset({TaskState.PENDING, TaskState.CANCELLED}),
    # 风控 → 人工冷却后恢复
    TaskState.BLOCKED: frozenset({TaskState.RUNNING, TaskState.CANCELLED}),
    # 需人工 → 人工处理完恢复
    TaskState.NEEDS_MANUAL_ACTION: frozenset({TaskState.RUNNING, TaskState.CANCELLED}),
    TaskState.COMPLETED: frozenset(),
    TaskState.CANCELLED: frozenset(),
}


class InvalidTransition(Exception):
    def __init__(self, src: TaskState, dst: TaskState) -> None:
        super().__init__(f"非法状态流转：{src} -> {dst}")
        self.src = src
        self.dst = dst


def is_valid_transition(src: TaskState, dst: TaskState) -> bool:
    return dst in ALLOWED_TRANSITIONS.get(src, frozenset())


def is_terminal(state: TaskState) -> bool:
    return state in TERMINAL_STATES


TransitionHook = Callable[[TaskState, TaskState, dict[str, Any]], None]


class TaskStateMachine:
    """单次请求的状态机。"""

    def __init__(
        self,
        initial: TaskState = TaskState.PENDING,
        on_transition: TransitionHook | None = None,
    ) -> None:
        self._state = initial
        self._on_transition = on_transition
        self.history: list[dict[str, Any]] = []

    @property
    def state(self) -> TaskState:
        return self._state

    @property
    def is_terminal(self) -> bool:
        return is_terminal(self._state)

    def can(self, target: TaskState) -> bool:
        return is_valid_transition(self._state, target)

    def transition(self, target: TaskState, **fields: Any) -> TaskState:
        if not self.can(target):
            raise InvalidTransition(self._state, target)
        previous, self._state = self._state, target
        record = {
            "state_from": str(previous),
            "state_to": str(target),
            "at": now_iso(),
            **fields,
        }
        self.history.append(record)
        if self._on_transition is not None:
            self._on_transition(previous, target, fields)
        return self._state

    def try_transition(self, target: TaskState, **fields: Any) -> bool:
        """不抛异常的版本，用于清理/收尾路径。"""
        if not self.can(target):
            return False
        self.transition(target, **fields)
        return True


# --------------------------------------------------------------------------
# 断点
# --------------------------------------------------------------------------


@dataclass
class FailedTaskPoint:
    task_point_id: str
    task_type: str = "unknown"
    attempt: int = 1
    last_error_code: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.task_point_id,
            "type": self.task_type,
            "attempt": self.attempt,
            "last_error_code": self.last_error_code,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "FailedTaskPoint":
        return cls(
            task_point_id=str(raw.get("id", "")),
            task_type=str(raw.get("type", "unknown")),
            attempt=int(raw.get("attempt", 1)),
            last_error_code=str(raw.get("last_error_code", "")),
            reason=str(raw.get("reason", "")),
        )


@dataclass
class Checkpoint:
    """可恢复的执行断点。

    `completed_task_points` 是恢复的核心依据：学习通本身会跳过已完成的
    任务点，所以 resume 时只需重新进入课程，平台会自动跳过——这让
    "协作式暂停"（第三方项目均不支持进程内暂停）成为可行方案。
    """

    request_id: str
    account_id: str
    command: str = ""
    state: TaskState = TaskState.PENDING
    course_id: str = ""
    chapter_id: str = ""
    target_types: list[str] = field(default_factory=list)
    completed_task_points: list[str] = field(default_factory=list)
    failed_task_points: list[FailedTaskPoint] = field(default_factory=list)
    skipped_task_points: list[dict[str, Any]] = field(default_factory=list)
    cursor: dict[str, str] = field(default_factory=dict)
    adapter: str = ""
    updated_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "account_id": self.account_id,
            "command": self.command,
            "state": str(self.state),
            "course_id": self.course_id,
            "chapter_id": self.chapter_id,
            "target_types": list(self.target_types),
            "completed_task_points": list(self.completed_task_points),
            "failed_task_points": [f.to_dict() for f in self.failed_task_points],
            "skipped_task_points": list(self.skipped_task_points),
            "cursor": dict(self.cursor),
            "adapter": self.adapter,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Checkpoint":
        return cls(
            request_id=str(raw.get("request_id", "")),
            account_id=str(raw.get("account_id", "")),
            command=str(raw.get("command", "")),
            state=TaskState(str(raw.get("state", "pending"))),
            course_id=str(raw.get("course_id", "")),
            chapter_id=str(raw.get("chapter_id", "")),
            target_types=[str(t) for t in raw.get("target_types") or []],
            completed_task_points=[
                str(t) for t in raw.get("completed_task_points") or []
            ],
            failed_task_points=[
                FailedTaskPoint.from_dict(f)
                for f in raw.get("failed_task_points") or []
            ],
            skipped_task_points=list(raw.get("skipped_task_points") or []),
            cursor=dict(raw.get("cursor") or {}),
            adapter=str(raw.get("adapter", "")),
            updated_at=str(raw.get("updated_at", now_iso())),
        )

    def mark_completed(self, task_point_id: str) -> None:
        if task_point_id not in self.completed_task_points:
            self.completed_task_points.append(task_point_id)
        self.updated_at = now_iso()

    def mark_skipped(self, task_point_id: str, task_type: str, reason: str) -> None:
        # 幂等：resume/retry 会反复经过同一批任务点，若不去重，断点里的
        # skipped 列表会随重试次数线性膨胀，最后既看不懂也没法统计。
        for item in self.skipped_task_points:
            if str(item.get("id")) == task_point_id and str(item.get("reason")) == reason:
                return
        self.skipped_task_points.append(
            {"id": task_point_id, "type": task_type, "reason": reason}
        )
        self.updated_at = now_iso()

    def mark_failed(
        self, task_point_id: str, task_type: str, error_code: str, reason: str = ""
    ) -> None:
        for item in self.failed_task_points:
            if item.task_point_id == task_point_id:
                item.attempt += 1
                item.last_error_code = error_code
                item.reason = reason
                break
        else:
            self.failed_task_points.append(
                FailedTaskPoint(
                    task_point_id=task_point_id,
                    task_type=task_type,
                    last_error_code=error_code,
                    reason=reason,
                )
            )
        self.updated_at = now_iso()

    def remaining_from(self, ordered_ids: Iterable[str]) -> list[str]:
        """给定完整任务点顺序，返回还没完成的那些。

        被 skip 的不算"未完成"——它们不是本次执行的目标。
        """
        done = set(self.completed_task_points)
        skipped = {str(s.get("id")) for s in self.skipped_task_points}
        return [tid for tid in ordered_ids if tid not in done and tid not in skipped]


class StateStore:
    """断点的 JSON 持久化。

    用 JSON 而不是 SQLite：状态量极小，JSON 便于人工查看和手工修复——
    当事务卡在 `needs_manual_action` 时，能直接编辑文件放行。
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def path_for(self, account_id: str, request_id: str) -> Path:
        return self.root / account_id / "state" / f"{request_id}.json"

    def save(self, checkpoint: Checkpoint) -> Path:
        path = self.path_for(checkpoint.account_id, checkpoint.request_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        checkpoint.updated_at = now_iso()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(checkpoint.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)
        return path

    def load(self, account_id: str, request_id: str) -> Checkpoint | None:
        path = self.path_for(account_id, request_id)
        if not path.is_file():
            return None
        return Checkpoint.from_dict(
            json.loads(path.read_text(encoding="utf-8"))
        )

    def list_for_account(self, account_id: str) -> list[Checkpoint]:
        directory = self.root / account_id / "state"
        if not directory.is_dir():
            return []
        items = [
            Checkpoint.from_dict(json.loads(p.read_text(encoding="utf-8")))
            for p in sorted(directory.glob("*.json"))
        ]
        items.sort(key=lambda c: c.updated_at, reverse=True)
        return items

    def latest(self, account_id: str) -> Checkpoint | None:
        items = self.list_for_account(account_id)
        return items[0] if items else None


def filter_by_types(
    task_point_ids_and_types: dict[str, str], target_types: Iterable[TaskType | str]
) -> list[str]:
    """按任务类型过滤任务点。

    这个函数是 `run_video_tasks` / `run_reading_tasks` 能存在的原因：
    第三方项目**没有按类型单跑**的入口，所以类型过滤只能由统一层做，
    而过滤依赖 `scan_tasks` 的输出 —— 这就是"`scan_tasks` 是一等公民"
    的具体体现（docs/02 §4）。
    """
    wanted = {str(t) for t in target_types}
    if not wanted:
        return list(task_point_ids_and_types)
    return [
        tid for tid, ttype in task_point_ids_and_types.items() if str(ttype) in wanted
    ]
