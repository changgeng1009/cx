"""统一层数据模型。

设计要点：
1. 所有对外结构都能 `to_dict()` 成可 JSON 序列化的形式。
2. 状态/类型用 StrEnum，保证 JSON 输出是字符串而非数字。
3. Envelope 是唯一的对外响应形态，包含 `fallback_trace`——这是
   "不隐藏失败"的机制保证，而不是靠人工自觉。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Callable

CST = timezone(timedelta(hours=8))


def now() -> datetime:
    """当前时间（东八区）。"""
    return datetime.now(CST)


def to_iso(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds")


def now_iso() -> str:
    return to_iso(now())


# --------------------------------------------------------------------------
# 状态机
# --------------------------------------------------------------------------


class TaskState(StrEnum):
    """统一任务状态。

    前 6 个是用户明确要求的；`paused` / `cancelled` 是补充的，原因见
    docs/03 §4：没有它们，`pause`/`stop` 命令无处安放——映射到 pending
    会误报"排队中"，映射到 failed 会污染失败统计并触发无意义的 retry。
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    NEEDS_MANUAL_ACTION = "needs_manual_action"
    PAUSED = "paused"
    CANCELLED = "cancelled"


TERMINAL_STATES: frozenset[TaskState] = frozenset(
    {TaskState.COMPLETED, TaskState.CANCELLED}
)


class TaskType(StrEnum):
    VIDEO = "video"
    AUDIO = "audio"
    DOCUMENT = "document"
    PPT = "ppt"
    READING = "reading"
    LIVE = "live"
    DISCUSSION = "discussion"
    QUIZ = "quiz"
    UNKNOWN = "unknown"


#: `run_reading_tasks` 命令覆盖的任务类型
READING_TASK_TYPES: frozenset[TaskType] = frozenset(
    {TaskType.DOCUMENT, TaskType.PPT, TaskType.READING}
)

#: `run_video_tasks` 命令覆盖的任务类型
VIDEO_TASK_TYPES: frozenset[TaskType] = frozenset({TaskType.VIDEO})


class TaskPointStatus(StrEnum):
    DONE = "done"
    TODO = "todo"
    LOCKED = "locked"
    UNKNOWN = "unknown"


class TicketState(StrEnum):
    """Agent 答题工单状态。"""

    PENDING = "pending"
    ANSWERED = "answered"
    TIMEOUT = "timeout"
    DROPPED = "dropped"


# --------------------------------------------------------------------------
# 领域实体
# --------------------------------------------------------------------------


@dataclass
class Course:
    course_id: str
    clazz_id: str = ""
    cpi: str = ""
    name: str = ""
    teacher: str = ""
    fid: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "course_id": self.course_id,
            "clazz_id": self.clazz_id,
            "cpi": self.cpi,
            "name": self.name,
            "teacher": self.teacher,
            "fid": self.fid,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Course":
        return cls(
            course_id=str(raw.get("course_id", "")),
            clazz_id=str(raw.get("clazz_id", "")),
            cpi=str(raw.get("cpi", "")),
            name=str(raw.get("name", "")),
            teacher=str(raw.get("teacher", "")),
            fid=str(raw.get("fid", "")),
        )


@dataclass
class Chapter:
    chapter_id: str
    name: str = ""
    index: int = 0
    parent_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "chapter_id": self.chapter_id,
            "name": self.name,
            "index": self.index,
            "parent_id": self.parent_id,
        }


@dataclass
class TaskPoint:
    task_point_id: str
    task_type: TaskType = TaskType.UNKNOWN
    title: str = ""
    chapter_id: str = ""
    chapter_name: str = ""
    status: TaskPointStatus = TaskPointStatus.UNKNOWN
    #: 该任务点是否需要读图作答（用于把课程路由到 A2）
    needs_image: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_point_id": self.task_point_id,
            "type": str(self.task_type),
            "title": self.title,
            "chapter_id": self.chapter_id,
            "chapter_name": self.chapter_name,
            "status": str(self.status),
            "needs_image": self.needs_image,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TaskPoint":
        return cls(
            task_point_id=str(raw.get("task_point_id", raw.get("id", ""))),
            task_type=TaskType(str(raw.get("type", "unknown"))),
            title=str(raw.get("title", "")),
            chapter_id=str(raw.get("chapter_id", "")),
            chapter_name=str(raw.get("chapter_name", "")),
            status=TaskPointStatus(str(raw.get("status", "unknown"))),
            needs_image=bool(raw.get("needs_image", False)),
        )


@dataclass
class HomeworkItem:
    course_id: str
    index: int
    title: str
    submitted: bool = False
    progress: str = ""
    due_at: str | None = None
    score: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "course_id": self.course_id,
            "index": self.index,
            "title": self.title,
            "submitted": self.submitted,
            "progress": self.progress,
            "due_at": self.due_at,
            "score": self.score,
        }


# --------------------------------------------------------------------------
# 运行期上下文
# --------------------------------------------------------------------------


@dataclass
class AccountContext:
    """一个账号的独立工作区。凭据只在这里流转，绝不进日志。"""

    account_id: str
    workdir: str
    phone_masked: str = ""
    credentials: dict[str, str] = field(default_factory=dict, repr=False)

    def __repr__(self) -> str:  # 防止凭据意外出现在排障输出里
        return (
            f"AccountContext(account_id={self.account_id!r}, "
            f"workdir={self.workdir!r}, phone_masked={self.phone_masked!r})"
        )


@dataclass
class TaskContext:
    """一次命令执行的上下文。Adapter 通过它报告进度、检查取消。"""

    request_id: str
    account_id: str
    course: Course | None = None
    target_types: tuple[TaskType, ...] = ()
    dry_run: bool = False
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    pause_event: threading.Event = field(default_factory=threading.Event, repr=False)
    progress_sink: Callable[[str, dict[str, Any]], None] | None = field(
        default=None, repr=False
    )

    def report(self, event: str, **fields: Any) -> None:
        if self.progress_sink is not None:
            self.progress_sink(event, fields)

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    @property
    def pause_requested(self) -> bool:
        return self.pause_event.is_set()


# --------------------------------------------------------------------------
# 结果与信封
# --------------------------------------------------------------------------


@dataclass
class ProbeResult:
    healthy: bool
    latency_ms: int = 0
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "healthy": self.healthy,
            "latency_ms": self.latency_ms,
            "detail": self.detail,
        }


@dataclass
class AdapterResult:
    """Adapter 的原始返回。`raw_output` 必须原样保留，用于排障。"""

    ok: bool
    data: Any = None
    error: Any = None  # errors.AdapterError
    raw_output: str = ""
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def success(
        cls, data: Any, raw_output: str = "", warnings: list[str] | None = None
    ) -> "AdapterResult":
        return cls(
            ok=True,
            data=data,
            raw_output=raw_output,
            warnings=list(warnings or []),
        )

    @classmethod
    def failure(cls, error: Any, raw_output: str = "") -> "AdapterResult":
        return cls(ok=False, error=error, raw_output=raw_output)


@dataclass
class Envelope:
    """统一响应信封。所有命令都返回这个结构。"""

    ok: bool
    command: str
    request_id: str
    account: str
    state: str
    started_at: str
    finished_at: str
    duration_ms: int
    adapter: str | None = None
    adapter_version: str | None = None
    data: Any = None
    warnings: list[str] = field(default_factory=list)
    error: dict[str, Any] | None = None
    fallback_trace: list[dict[str, Any]] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "command": self.command,
            "request_id": self.request_id,
            "account": self.account,
            "adapter": self.adapter,
            "adapter_version": self.adapter_version,
            "state": self.state,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "data": self.data,
            "warnings": self.warnings,
            "error": self.error,
            "fallback_trace": self.fallback_trace,
            "next_actions": self.next_actions,
        }
