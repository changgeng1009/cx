"""统一命令层：把 Router / 状态机 / 断点 / 控制通道 / 工单队列串成命令。

这一层做三件 Router 不该做的事：
1. **参数校验与业务语义**（例如 `scan_tasks` 的类型过滤、`run_video_tasks`
   只跑视频）——这类过滤上游项目没有入口，只能在这里做。
2. **断点与状态的编排**：把 Adapter 上报的进度落到 `Checkpoint`。
3. **输出形状**：把各 Adapter 各不相同的返回，统一成 Envelope 的 `data`。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import fixtures
from .answer_broker import AnswerBroker, parse_answers
from .capabilities import CAPABILITIES, GROUP_ORDER, by_group
from .cdp import CdpError
from .cookies import CHAOXING_DOMAIN_SUFFIXES, CookieJar, CookieStore
from .control import ControlChannel
from .errors import AdapterError, Codes, ErrorCategory, invalid_param
from .models import (
    Course,
    Envelope,
    READING_TASK_TYPES,
    TaskContext,
    TaskPoint,
    TaskState,
    TaskType,
    TicketState,
    VIDEO_TASK_TYPES,
    now,
    now_iso,
    to_iso,
)
from .registry import CapabilityRegistry, Manifest, load_manifests
from .redact import SENSITIVE_KEYS
from .router import RouterOutcome, TaskRouter
from .session import AccountManager
from .state import Checkpoint, StateStore
from .structured_log import Event, StructuredLogger


def _public_params(params: dict[str, Any]) -> dict[str, Any]:
    """日志用的参数视图：凭据类字段一律不落盘。

    日志是最容易被忽略的凭据泄漏渠道——一次 verbose 排障就可能把手机号
    和密码写进 runs/*.jsonl。所以这里做的是**白名单式剔除**，而不是
    指望调用方自觉。
    """
    return {
        key: value
        for key, value in params.items()
        if str(key).lower() not in SENSITIVE_KEYS
    }


@dataclass(frozen=True)
class CommandSpec:
    name: str
    capability: str | None
    kind: str
    summary: str
    needs_confirmation: bool = False


COMMANDS: dict[str, CommandSpec] = {
    # 读命令
    "list_courses": CommandSpec("list_courses", "C06", "read", "列出账号下全部课程"),
    "get_course": CommandSpec("get_course", "C07", "read", "单门课元数据"),
    "get_progress": CommandSpec("get_progress", "C11", "read", "学习进度（可指定课程）"),
    "scan_tasks": CommandSpec("scan_tasks", "C09", "read", "扫描任务点并按类型汇总"),
    "get_homework": CommandSpec("get_homework", "C29", "read", "作业列表与截止看板"),
    "get_notices": CommandSpec("get_notices", "C31", "read", "通知中心（客户端过滤）"),
    "get_schedule": CommandSpec("get_schedule", "C33", "read", "课表"),
    "list_exams": CommandSpec("list_exams", "C30", "read", "考试安排"),
    "list_materials": CommandSpec("list_materials", "C08", "read", "课程章节与资料"),
    "download_material": CommandSpec("download_material", "C32", "read", "下载课程资料"),
    # 写命令
    "run_course": CommandSpec("run_course", "C18", "write", "跑完一门课", True),
    "run_chapter": CommandSpec("run_chapter", "C18", "write", "只跑指定章节", True),
    "run_video_tasks": CommandSpec("run_video_tasks", "C12", "write", "只跑视频任务点", True),
    "run_reading_tasks": CommandSpec("run_reading_tasks", "C14", "write", "只跑阅读/文档任务点", True),
    # 控制命令
    "status": CommandSpec("status", None, "control", "查询任务状态"),
    "pause": CommandSpec("pause", None, "control", "在任务点边界暂停"),
    "resume": CommandSpec("resume", None, "control", "从断点继续"),
    "retry": CommandSpec("retry", None, "control", "重放未完成/失败的任务点"),
    "stop": CommandSpec("stop", None, "control", "终止并保留断点"),
    # 辅助命令
    "adapters": CommandSpec("adapters", None, "aux", "列出 Adapter 与能力声明"),
    "probe": CommandSpec("probe", None, "aux", "探活全部 Adapter"),
    "accounts": CommandSpec("accounts", None, "aux", "列出账号与会话状态"),
    "capabilities": CommandSpec("capabilities", None, "aux", "列出能力注册表与覆盖情况"),
    # Agent 答题链路
    "answer_pending": CommandSpec("answer_pending", "C43", "agent", "取出待答工单"),
    "answer_submit": CommandSpec("answer_submit", "C44", "agent", "回填 Agent 答案"),
    "answer_stats": CommandSpec("answer_stats", None, "agent", "工单队列统计"),
    "shim_config": CommandSpec("shim_config", "C46", "agent", "输出上游所需配置片段"),
    # 签到
    "sign_in": CommandSpec("sign_in", "C48", "sign", "执行签到", True),
    "sign_status": CommandSpec("sign_status", "C48", "sign", "查询签到状态"),
    "sign_watch": CommandSpec("sign_watch", "C50", "sign", "轮询监测新签到"),
    # Cookie 管理（统一层自建，不经 Adapter）
    "cookies": CommandSpec("cookies", None, "cookie", "查看已落盘的 cookie 与体检报告"),
    "cookies_import": CommandSpec("cookies_import", None, "cookie", "从字符串/文件导入 cookie"),
    "cookies_extract": CommandSpec("cookies_extract", None, "cookie", "通过 CDP 从独立浏览器提取 cookie"),
    "cookies_clear": CommandSpec("cookies_clear", None, "cookie", "清除已落盘的 cookie"),
    "cookies_login": CommandSpec(
        "cookies_login", None, "cookie",
        "一站式登录：起浏览器 → 等你登录 → 自动提取落盘",
    ),
    "cookies_verify": CommandSpec(
        "cookies_verify", None, "cookie",
        "用已落盘 cookie 真实请求一次平台，验证会话是否有效",
    ),
}


class Orchestrator:
    def __init__(
        self,
        registry: CapabilityRegistry,
        accounts: AccountManager,
        state_store: StateStore,
        control: ControlChannel,
        broker: AnswerBroker,
        run_dir: str | Path,
        cookie_store: CookieStore | None = None,
        echo: Callable[[str], None] | None = None,
        router_factory: Callable[[StructuredLogger], TaskRouter] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.registry = registry
        self.accounts = accounts
        self.state_store = state_store
        self.control = control
        self.broker = broker
        self.run_dir = Path(run_dir)
        self.cookie_store = cookie_store or CookieStore(
            Path(run_dir).parent / "accounts"
        )
        self.echo = echo
        self.sleeper = sleeper
        self._router_factory = router_factory
        #: 需要在"写命令"上显式确认，避免误触发真实写操作
        self.confirmed = False

    # ------------------------------------------------------------------
    def new_request_id(self) -> str:
        stamp = now().strftime("%Y%m%d_%H%M%S")
        return f"req_{stamp}_{uuid.uuid4().hex[:4]}"

    def _make_router(self, logger: StructuredLogger) -> TaskRouter:
        if self._router_factory is not None:
            return self._router_factory(logger)
        return TaskRouter(self.registry, logger=logger)

    # ------------------------------------------------------------------
    def run(
        self,
        command: str,
        params: dict[str, Any] | None = None,
        account_id: str | None = None,
        request_id: str | None = None,
        resume_from: Checkpoint | None = None,
    ) -> Envelope:
        params = dict(params or {})
        spec = COMMANDS.get(command)
        started = now()
        request_id = request_id or self.new_request_id()
        account = self.accounts.get(account_id or self.accounts.default_account_id())
        logger = StructuredLogger(
            self.run_dir, request_id, echo=self.echo, write_jsonl=True
        ).bind(account=account.account_id, command=command)

        logger.emit(Event.REQUEST_RECEIVED, command=command, params=_public_params(params))

        try:
            # 未知命令的判定必须放在 try 内：否则 logger 的文件句柄不会
            # 被 finally 关闭，长时间运行的进程会耗尽句柄。
            if spec is None:
                return self._finish(
                    request_id, command, account.account_id, started, logger,
                    ok=False,
                    error=invalid_param(f"未知命令：{command}"),
                    next_actions=[f"可用命令：{', '.join(sorted(COMMANDS))}"],
                )

            # 注意顺序：scan_tasks 虽然 kind=read，但它需要统一层做类型过滤
            # 与汇总（这是它成为"一等公民"的原因），所以必须排在通用读分支之前。
            if command == "scan_tasks":
                return self._scan_tasks(
                    request_id, params, account.account_id, started, logger
                )
            if spec.kind == "read":
                outcome = self._run_simple(spec, params, account.account_id, logger, request_id)
                return self._finish_from_outcome(
                    request_id, command, account.account_id, started, logger, outcome
                )
            if spec.kind == "write":
                return self._run_write(
                    request_id, command, spec, params, account.account_id, started,
                    logger, resume_from,
                )
            if spec.kind == "control":
                return self._run_control(
                    request_id, command, params, account.account_id, started, logger
                )
            if spec.kind == "aux":
                return self._run_aux(
                    request_id, command, params, account.account_id, started, logger
                )
            if spec.kind == "agent":
                return self._run_agent(
                    request_id, command, spec, params, account.account_id, started, logger
                )
            if spec.kind == "sign":
                return self._run_sign(
                    request_id, command, spec, params, account.account_id, started, logger
                )
            if spec.kind == "cookie":
                return self._run_cookies(
                    request_id, command, params, account.account_id, started, logger
                )
        finally:
            logger.close()

        return self._finish(
            request_id, command, account.account_id, started, logger,
            ok=False, error=AdapterError(
                code=Codes.INTERNAL_ERROR,
                category=ErrorCategory.INTERNAL,
                message=f"命令 {command} 没有对应的处理分支",
            ),
        )

    # ------------------------------------------------------------------
    # 读命令
    # ------------------------------------------------------------------
    def _run_simple(
        self,
        spec: CommandSpec,
        params: dict[str, Any],
        account_id: str,
        logger: StructuredLogger,
        request_id: str,
    ) -> RouterOutcome:
        assert spec.capability is not None
        ctx = TaskContext(request_id=request_id, account_id=account_id)
        router = self._make_router(logger)
        return router.execute(spec.capability, params, ctx, command=spec.name)

    def _scan_tasks(
        self,
        request_id: str,
        params: dict[str, Any],
        account_id: str,
        started: Any,
        logger: StructuredLogger,
    ) -> Envelope:
        """扫描任务点。

        `scan_tasks` 是一等公民（docs/02 §4）：`run_video_tasks` /
        `run_reading_tasks` 的类型过滤只能由统一层完成，而过滤依赖本命令
        的输出。所以这里承担"把 Adapter 的原始任务点列表规范化 + 汇总 +
        标注能力缺口"三件事。
        """
        ctx = TaskContext(request_id=request_id, account_id=account_id)
        router = self._make_router(logger)
        outcome = router.execute("C09", params, ctx, command="scan_tasks")

        if not outcome.ok:
            return self._finish_from_outcome(
                request_id, "scan_tasks", account_id, started, logger, outcome
            )

        raw = outcome.data or {}
        points = [
            TaskPoint.from_dict(item) for item in (raw.get("task_points") or [])
        ]
        wanted = {str(t) for t in (params.get("types") or [])}
        if wanted:
            points = [p for p in points if str(p.task_type) in wanted]

        chapter_filter = params.get("chapter_id")
        if chapter_filter:
            points = [p for p in points if p.chapter_id == str(chapter_filter)]

        by_type: dict[str, int] = {}
        by_status: dict[str, int] = {}
        for point in points:
            by_type[str(point.task_type)] = by_type.get(str(point.task_type), 0) + 1
            by_status[str(point.status)] = by_status.get(str(point.status), 0) + 1

        warnings = list(outcome.warnings)
        image_points = [p.task_point_id for p in points if p.needs_image]
        if image_points:
            handler = self._route_hint("C24")
            warnings.append(
                f"{len(image_points)} 个任务点含图片题，需要 C24（图片题识别）；"
                f"当前 C24 的可选实现：{handler}"
            )

        todo_quiz = [
            p for p in points
            if p.task_type == TaskType.QUIZ and p.status.value == "todo"
        ]
        if todo_quiz:
            warnings.append(
                f"{len(todo_quiz)} 个测验任务点将通过 Agent 答题链路（C46 本地代理）处理；"
                "请确保操控 Agent 在线，否则按 submit=false 只保存不提交"
            )

        data = {
            "course_id": params.get("course_id"),
            "chapters": raw.get("chapters") or [],
            "task_points": [p.to_dict() for p in points],
            "summary": {
                "total": len(points),
                "by_type": by_type,
                "by_status": by_status,
                "needs_image": image_points,
            },
            "filter": {"types": sorted(wanted), "chapter_id": chapter_filter},
            "capability_hint": {
                "task_point_detail": self._capability_level_note("C09"),
                "image_questions": self._capability_level_note("C24"),
            },
        }
        return self._finish_from_outcome(
            request_id, "scan_tasks", account_id, started, logger, outcome, data=data,
            warnings=warnings,
        )

    def _capability_level_note(self, capability_id: str) -> dict[str, Any]:
        candidates = self.registry.candidates(capability_id)
        return {
            "capability": capability_id,
            "providers": [a.manifest.id for a in candidates],
            "level": (
                candidates[0].manifest.level(capability_id) if candidates else "none"
            ),
        }

    def _route_hint(self, capability_id: str) -> str:
        candidates = self.registry.candidates(capability_id)
        if not candidates:
            return "（无可用 Adapter，该能力仍为缺口）"
        return " / ".join(a.manifest.id for a in candidates)

    # ------------------------------------------------------------------
    # 写命令
    # ------------------------------------------------------------------
    def _run_write(
        self,
        request_id: str,
        command: str,
        spec: CommandSpec,
        params: dict[str, Any],
        account_id: str,
        started: Any,
        logger: StructuredLogger,
        resume_from: Checkpoint | None,
    ) -> Envelope:
        if not params.get("course_id"):
            return self._finish(
                request_id, command, account_id, started, logger,
                ok=False, error=invalid_param(f"{command} 需要参数 course_id"),
            )
        if spec.needs_confirmation and not params.get("dry_run") and not self.confirmed:
            return self._finish(
                request_id, command, account_id, started, logger,
                ok=False,
                error=AdapterError(
                    code=Codes.INVALID_PARAM,
                    category=ErrorCategory.INPUT,
                    message=f"{command} 是写操作，需要 --confirm（或先用 --dry-run 预览）",
                ),
                next_actions=[
                    f"先跑 `orchestrator {command} --course-id ... --dry-run` 预览",
                    f"确认无误后加 --confirm 执行",
                ],
            )

        dry_run = bool(params.get("dry_run", False))
        target_types = self._target_types(command, params)
        course_id = str(params["course_id"])

        checkpoint = resume_from or Checkpoint(
            request_id=request_id,
            account_id=account_id,
            command=command,
            course_id=course_id,
            chapter_id=str(params.get("chapter_id", "") or ""),
            target_types=[str(t) for t in target_types],
        )
        checkpoint.state = TaskState.RUNNING
        skipped_ids = set(checkpoint.completed_task_points) | {
            str(s.get("id")) for s in checkpoint.skipped_task_points
        }

        driver = _CheckpointDriver(checkpoint, logger)
        driver.to(TaskState.PENDING if dry_run else TaskState.RUNNING)

        ctx = TaskContext(
            request_id=request_id,
            account_id=account_id,
            target_types=target_types,
            dry_run=dry_run,
        )
        ctx.progress_sink = self._make_progress_sink(
            checkpoint, ctx, logger, request_id
        )

        router = self._make_router(logger)
        call_params = {
            **params,
            "course_id": course_id,
            "target_types": [str(t) for t in target_types],
            "skip_task_point_ids": sorted(skipped_ids),
            "dry_run": dry_run,
        }
        outcome = router.execute(
            spec.capability or "C18", call_params, ctx, command=command
        )

        stopped_reason = ""
        if outcome.ok and isinstance(outcome.data, dict):
            stopped_reason = str(outcome.data.get("stopped_reason", ""))

        final_state = outcome.state
        if outcome.ok:
            if stopped_reason == "paused":
                final_state = TaskState.PAUSED
            elif stopped_reason == "cancelled":
                final_state = TaskState.CANCELLED
            else:
                final_state = TaskState.COMPLETED
        driver.to(final_state)
        checkpoint.state = final_state

        if not dry_run:
            self.state_store.save(checkpoint)
            if final_state in (TaskState.PAUSED, TaskState.CANCELLED):
                self.control.clear(request_id)

        logger.emit(
            Event.COURSE_FINISHED if not dry_run else Event.COURSE_STARTED,
            course={"id": course_id, "name": ""},
            state_from="running",
            state_to=str(final_state),
            completed=len(checkpoint.completed_task_points),
            failed=len(checkpoint.failed_task_points),
            skipped=len(checkpoint.skipped_task_points),
            stopped_reason=stopped_reason,
        )

        warnings = list(outcome.warnings)
        if dry_run:
            warnings.append("dry-run：未产生任何写操作")
        if final_state == TaskState.PAUSED:
            warnings.append(
                f"已在任务点边界暂停；用 `orchestrator resume --request-id {request_id}` 继续"
            )

        data = outcome.data if outcome.ok else None
        if isinstance(data, dict):
            data = {
                **data,
                "request_id": request_id,
                "checkpoint": {
                    "completed": len(checkpoint.completed_task_points),
                    "failed": [f.to_dict() for f in checkpoint.failed_task_points],
                    "skipped": len(checkpoint.skipped_task_points),
                },
            }

        return self._finish_from_outcome(
            request_id, command, account_id, started, logger, outcome,
            data=data, warnings=warnings, state=final_state,
        )

    @staticmethod
    def _target_types(command: str, params: dict[str, Any]) -> tuple[TaskType, ...]:
        explicit = params.get("types") or params.get("target_types")
        if explicit:
            return tuple(TaskType(str(t)) for t in explicit)
        if command == "run_video_tasks":
            return tuple(sorted(VIDEO_TASK_TYPES, key=lambda t: t.value))
        if command == "run_reading_tasks":
            return tuple(sorted(READING_TASK_TYPES, key=lambda t: t.value))
        return ()

    def _make_progress_sink(
        self,
        checkpoint: Checkpoint,
        ctx: TaskContext,
        logger: StructuredLogger,
        request_id: str,
    ) -> Callable[[str, dict[str, Any]], None]:
        """把 Adapter 上报的进度变成断点更新 + 结构化日志 + 控制信号检查。

        这是"A1 类 Adapter 靠解析 stdout 报进度"在统一层的对端实现：
        Adapter 只管上报事件，落库与日志由这里统一负责。
        """

        #: 任务点 id -> 开始时间。用于在结束事件上补出"开始/结束/耗时"，
        #: 这三点是排障时唯一能判断"是卡住了还是快速失败了"的依据。
        started_at: dict[str, str] = {}

        def sink(event: str, fields: dict[str, Any]) -> None:
            signal = self.control.read(request_id)
            if signal is not None:
                if signal.cancel and not ctx.cancel_event.is_set():
                    ctx.cancel_event.set()
                    logger.emit(
                        Event.PAUSE_REQUESTED,
                        action="cancel",
                        note=signal.note,
                        task_point_id=fields.get("task_point_id", ""),
                    )
                elif signal.pause and not ctx.pause_event.is_set():
                    ctx.pause_event.set()
                    logger.emit(
                        Event.PAUSE_REQUESTED,
                        action="pause",
                        note=signal.note,
                        task_point_id=fields.get("task_point_id", ""),
                    )

            payload = dict(fields)
            task_point_id = str(payload.pop("task_point_id", ""))
            stamp = now_iso()

            if event == Event.TASK_POINT_STARTED:
                started_at[task_point_id] = stamp

            if event == Event.TASK_POINT_COMPLETED:
                checkpoint.mark_completed(task_point_id)
            elif event == Event.TASK_POINT_SKIPPED:
                checkpoint.mark_skipped(
                    task_point_id,
                    str(payload.get("task_type", "unknown")),
                    str(payload.get("reason", "")),
                )
            elif event == Event.TASK_POINT_FAILED:
                error = payload.get("error") or {}
                checkpoint.mark_failed(
                    task_point_id,
                    str(payload.get("task_type", "unknown")),
                    str(error.get("code", "")),
                    str(error.get("category", "")),
                )

            record: dict[str, Any] = {
                "task_point_id": task_point_id,
                "course": {"id": checkpoint.course_id, "name": ""},
                **payload,
            }
            if event in (
                Event.TASK_POINT_COMPLETED,
                Event.TASK_POINT_FAILED,
                Event.TASK_POINT_SKIPPED,
            ):
                begin = started_at.pop(task_point_id, None)
                record["started_at"] = begin
                record["finished_at"] = stamp
                record["ok"] = event == Event.TASK_POINT_COMPLETED
                if event == Event.TASK_POINT_FAILED:
                    record["error"] = payload.get("error")

            logger.emit(event, **record)

        return sink

    # ------------------------------------------------------------------
    # 控制命令
    # ------------------------------------------------------------------
    def _run_control(
        self,
        request_id: str,
        command: str,
        params: dict[str, Any],
        account_id: str,
        started: Any,
        logger: StructuredLogger,
    ) -> Envelope:
        target = params.get("request_id") or request_id

        if command == "status":
            checkpoints = self.state_store.list_for_account(account_id)
            if params.get("all"):
                data = {
                    "checkpoints": [c.to_dict() for c in checkpoints],
                    "count": len(checkpoints),
                }
            else:
                latest = self.state_store.load(account_id, target) or (
                    checkpoints[0] if checkpoints else None
                )
                data = {
                    "checkpoint": latest.to_dict() if latest else None,
                    "count": len(checkpoints),
                }
            data["control"] = self.control.list_active()
            return self._finish(
                request_id, command, account_id, started, logger, ok=True,
                state=TaskState.COMPLETED, data=data,
            )

        if command == "pause":
            self.control.request_pause(
                target, note=str(params.get("note", "用户请求暂停"))
            )
            logger.emit(Event.PAUSE_REQUESTED, target_request_id=target)
            return self._finish(
                request_id, command, account_id, started, logger, ok=True,
                state=TaskState.PAUSED,
                data={
                    "requested": True,
                    "target_request_id": target,
                    "note": "暂停标志已置；执行方会在下一个任务点边界优雅停止",
                },
            )

        if command == "stop":
            self.control.request_cancel(
                target, note=str(params.get("note", "用户请求终止"))
            )
            checkpoint = self.state_store.load(account_id, target)
            if checkpoint is not None:
                checkpoint.state = TaskState.CANCELLED
                self.state_store.save(checkpoint)
            return self._finish(
                request_id, command, account_id, started, logger, ok=True,
                state=TaskState.CANCELLED,
                data={"requested": True, "target_request_id": target, "kept_checkpoint": True},
            )

        if command in ("resume", "retry"):
            # resume / retry 同样是写操作（会在平台上真实推进任务点），
            # 所以要和 run_* 享受同一道确认门禁 —— 否则就存在一条
            # "不经确认就能触发真实写操作"的旁路。
            if not self.confirmed:
                return self._finish(
                    request_id, command, account_id, started, logger, ok=False,
                    error=AdapterError(
                        code=Codes.INVALID_PARAM,
                        category=ErrorCategory.INPUT,
                        message=f"{command} 会在平台产生真实写操作，需要 --confirm",
                    ),
                    next_actions=[
                        f"确认要恢复该任务后执行：orchestrator {command} "
                        f"--request-id {target} --confirm",
                    ],
                )

            checkpoint = self.state_store.load(account_id, target)
            if checkpoint is None:
                return self._finish(
                    request_id, command, account_id, started, logger, ok=False,
                    error=AdapterError(
                        code=Codes.INTERNAL_ERROR,
                        category=ErrorCategory.INPUT,
                        message=f"找不到断点：{target}",
                    ),
                    next_actions=["运行 `orchestrator status --all` 查看已有断点"],
                )
            self.control.clear(target)
            checkpoint.state = TaskState.RUNNING
            self.state_store.save(checkpoint)

            # 快照必须在重入之前取：否则 resumed_from 会显示运行**之后**的
            # checkpoint（对象是同一个引用），读起来像是"恢复前就已经完成了"。
            snapshot_before = checkpoint.to_dict()

            # 复用原命令重入：已完成/已跳过的任务点作为 skip 集合传下去，
            # 因此"重试"天然只跑剩余与失败的部分 —— 不需要上游支持细粒度重试。
            resume_params = {
                "course_id": checkpoint.course_id,
                "chapter_id": checkpoint.chapter_id or None,
                "types": checkpoint.target_types or None,
            }
            sub = self.run(
                checkpoint.command,
                resume_params,
                account_id=account_id,
                request_id=target,
                resume_from=checkpoint,
            )
            sub.command = command
            sub.data = {
                "resumed_from": snapshot_before,
                "result": sub.data,
            }
            return sub

        return self._finish(
            request_id, command, account_id, started, logger, ok=False,
            error=invalid_param(f"未实现的控制命令：{command}"),
        )

    # ------------------------------------------------------------------
    # 辅助命令
    # ------------------------------------------------------------------
    def _run_aux(
        self,
        request_id: str,
        command: str,
        params: dict[str, Any],
        account_id: str,
        started: Any,
        logger: StructuredLogger,
    ) -> Envelope:
        if command == "accounts":
            return self._finish(
                request_id, command, account_id, started, logger, ok=True,
                state=TaskState.COMPLETED, data={"accounts": self.accounts.summary()},
            )

        if command == "adapters":
            # 同时展示"已注册"与"已声明未接入"，让路线图可见
            declared = {m.id: m for m in load_manifests()}
            registered = {a.manifest.id for a in self.registry.all()}
            rows = []
            for manifest in declared.values():
                rows.append(
                    {
                        "adapter": manifest.id,
                        "name": manifest.name,
                        "kind": manifest.kind,
                        "license": manifest.license,
                        "enabled": manifest.enabled,
                        "registered": manifest.id in registered,
                        "priority": manifest.priority,
                        "supported_count": len(manifest.supported_ids()),
                        "supported": manifest.supported_ids(),
                    }
                )
            rows.sort(key=lambda r: (r["priority"], r["adapter"]))
            return self._finish(
                request_id, command, account_id, started, logger, ok=True,
                state=TaskState.COMPLETED,
                data={"adapters": rows, "registered": sorted(registered)},
            )

        if command == "capabilities":
            coverage = self.registry.coverage()
            grouped = {
                group: [c.to_dict() for c in items]
                for group, items in by_group().items()
            }
            return self._finish(
                request_id, command, account_id, started, logger, ok=True,
                state=TaskState.COMPLETED,
                data={
                    "group_order": list(GROUP_ORDER),
                    "groups": grouped,
                    "coverage": coverage,
                    "matrix": self.registry.matrix(),
                },
            )

        if command == "probe":
            router = self._make_router(logger)
            rows = router.probe_all(include_disabled=False)
            # 只探活已注册的会遗漏"已声明但未实现"的 Adapter —— 那恰恰是
            # 路线图上最重要的信息（哪些能力还没有真正的实现）。
            registered = {a.manifest.id for a in self.registry.all()}
            for manifest in load_manifests():
                if manifest.id in registered:
                    continue
                rows.append(
                    {
                        "adapter": manifest.id,
                        "name": manifest.name,
                        "kind": manifest.kind,
                        "license": manifest.license,
                        "enabled": manifest.enabled,
                        "healthy": False,
                        "detail": "已声明但未接入（manifest.enabled = false）",
                        "declared_capabilities": len(manifest.supported_ids()),
                    }
                )
            for row in rows:
                logger.emit(Event.ADAPTER_PROBE, **row)
            return self._finish(
                request_id, command, account_id, started, logger, ok=True,
                state=TaskState.COMPLETED, data={"adapters": rows},
            )

        return self._finish(
            request_id, command, account_id, started, logger, ok=False,
            error=invalid_param(f"未实现的辅助命令：{command}"),
        )

    # ------------------------------------------------------------------
    # Agent 答题链路
    # ------------------------------------------------------------------
    def _run_agent(
        self,
        request_id: str,
        command: str,
        spec: CommandSpec,
        params: dict[str, Any],
        account_id: str,
        started: Any,
        logger: StructuredLogger,
    ) -> Envelope:
        if command == "answer_stats":
            return self._finish(
                request_id, command, account_id, started, logger, ok=True,
                state=TaskState.COMPLETED, data=self.broker.stats(),
            )

        if command == "answer_pending":
            limit = int(params.get("limit", 10) or 10)
            tickets = self.broker.pending(limit=limit)
            for ticket in tickets:
                logger.emit(
                    Event.TICKET_CREATED,
                    ticket_id=ticket.ticket_id,
                    question_count=len(ticket.questions),
                )
            return self._finish(
                request_id, command, account_id, started, logger, ok=True,
                state=TaskState.COMPLETED,
                data={
                    "tickets": [t.to_dict() for t in tickets],
                    "count": len(tickets),
                    "how_to_answer": (
                        "对每个工单运行 "
                        "`orchestrator answer_submit --ticket-id <id> --answers \"A\\nB\\nA\"`"
                    ),
                },
            )

        if command == "answer_submit":
            ticket_id = str(params.get("ticket_id", ""))
            if not ticket_id:
                return self._finish(
                    request_id, command, account_id, started, logger, ok=False,
                    error=invalid_param("answer_submit 需要 --ticket-id"),
                )
            answers = params.get("answers")
            if isinstance(answers, str):
                answers = parse_answers(answers)
            if not isinstance(answers, list) or not answers:
                return self._finish(
                    request_id, command, account_id, started, logger, ok=False,
                    error=invalid_param("answer_submit 需要非空 --answers"),
                )
            try:
                ticket = self.broker.submit(
                    ticket_id, [str(a) for a in answers],
                    answered_by=str(params.get("answered_by", "agent")),
                )
            except KeyError:
                return self._finish(
                    request_id, command, account_id, started, logger, ok=False,
                    error=AdapterError(
                        code=Codes.TICKET_NOT_FOUND,
                        category=ErrorCategory.INPUT,
                        message=f"工单不存在：{ticket_id}",
                    ),
                )
            logger.emit(
                Event.TICKET_ANSWERED,
                ticket_id=ticket_id,
                answer_count=len(ticket.answers or []),
            )
            return self._finish(
                request_id, command, account_id, started, logger, ok=True,
                state=TaskState.COMPLETED, data={"ticket": ticket.to_dict()},
            )

        if command == "shim_config":
            from .openai_shim import DEFAULT_PORT, config_snippet

            port = int(params.get("port", DEFAULT_PORT) or DEFAULT_PORT)
            return self._finish(
                request_id, command, account_id, started, logger, ok=True,
                state=TaskState.COMPLETED,
                data={
                    "config_ini_snippet": config_snippet(port=port),
                    "endpoint": f"http://127.0.0.1:{port}/v1",
                    "explanation": (
                        "把这段写进 chaoxing-cli 的 config.ini，上游会把题目发到"
                        "本地代理，再由操控 Agent 作答 —— 上游零改动"
                    ),
                },
            )

        return self._finish(
            request_id, command, account_id, started, logger, ok=False,
            error=invalid_param(f"未实现的 Agent 命令：{command}"),
        )

    # ------------------------------------------------------------------
    # 签到
    # ------------------------------------------------------------------
    def _run_sign(
        self,
        request_id: str,
        command: str,
        spec: CommandSpec,
        params: dict[str, Any],
        account_id: str,
        started: Any,
        logger: StructuredLogger,
    ) -> Envelope:
        ctx = TaskContext(request_id=request_id, account_id=account_id)
        router = self._make_router(logger)
        capability = "C48" if command in ("sign_in", "sign_status") else "C50"
        outcome = router.execute(capability, params, ctx, command=command)

        warnings = list(outcome.warnings)
        if outcome.ok and command == "sign_in":
            if not self.confirmed and not params.get("dry_run"):
                # 签到是写操作，但没有 --confirm 时只提示不拦截：
                # 签到窗口很短，多一次确认可能就意味着错过。
                warnings.append("签到未加 --confirm；如为真实签到请确认账号与课程无误")
            logger.emit(
                Event.SIGN_COMPLETED,
                course={"id": str(params.get("course_id", "")), "name": ""},
                sign_type=str(params.get("type", "normal")),
            )
        if command == "sign_watch" and not params.get("interval"):
            warnings.append("sign_watch 在 M0 为一次性轮询；持续监测需在 CLI 外层循环或 M6 实现")

        return self._finish_from_outcome(
            request_id, command, account_id, started, logger, outcome,
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    # Cookie 管理
    # ------------------------------------------------------------------
    def _run_cookies(
        self,
        request_id: str,
        command: str,
        params: dict[str, Any],
        account_id: str,
        started: Any,
        logger: StructuredLogger,
    ) -> Envelope:
        if command == "cookies":
            meta = self.cookie_store.meta(account_id)
            return self._finish(
                request_id, command, account_id, started, logger, ok=True,
                state=TaskState.COMPLETED, data=meta,
            )

        if command == "cookies_clear":
            removed = self.cookie_store.clear(account_id)
            logger.emit(
                Event.REQUEST_RECEIVED, action="cookies_clear", removed=len(removed)
            )
            return self._finish(
                request_id, command, account_id, started, logger, ok=True,
                state=TaskState.COMPLETED,
                data={"removed": removed, "count": len(removed)},
            )

        if command == "cookies_import":
            return self._cookies_import(
                request_id, params, account_id, started, logger
            )

        if command == "cookies_extract":
            return self._cookies_extract(
                request_id, params, account_id, started, logger
            )

        if command == "cookies_login":
            return self._cookies_login(
                request_id, params, account_id, started, logger
            )

        if command == "cookies_verify":
            return self._cookies_verify(
                request_id, params, account_id, started, logger
            )

        return self._finish(
            request_id, command, account_id, started, logger, ok=False,
            error=invalid_param(f"未实现的 cookie 命令：{command}"),
        )

    def _cookies_import(
        self,
        request_id: str,
        params: dict[str, Any],
        account_id: str,
        started: Any,
        logger: StructuredLogger,
    ) -> Envelope:
        """从字符串或文件导入 cookie。

        这是**零依赖、零风险**的入口：不需要浏览器、不需要网络。
        想手动从 DevTools 复制 cookie 的人走这条路。
        """
        text = params.get("header") or params.get("text")
        file_path = params.get("file")
        fmt = str(params.get("format", "") or "").lower()

        if not text and not file_path:
            return self._finish(
                request_id, "cookies_import", account_id, started, logger, ok=False,
                error=invalid_param(
                    "cookies_import 需要 --header（cookie 字符串）或 --file（文件路径）"
                ),
                next_actions=[
                    '示例：orchestrator cookies_import --header "UID=xxx; _d=yyy"',
                    "从浏览器 DevTools 的 Network 面板复制 Cookie 请求头的值即可",
                ],
            )

        if file_path:
            path = Path(str(file_path)).expanduser()
            if not path.is_file():
                return self._finish(
                    request_id, "cookies_import", account_id, started, logger, ok=False,
                    error=invalid_param(f"文件不存在：{path}"),
                )
            text = path.read_text(encoding="utf-8")
            if not fmt:
                fmt = "netscape" if path.suffix.lower() == ".txt" else "json"

        assert text is not None
        try:
            if fmt == "netscape" or text.lstrip().startswith("# Netscape"):
                jar = CookieJar.from_netscape(text)
            elif fmt == "json" or text.lstrip().startswith(("{", "[")):
                jar = CookieJar.from_json(text)
            else:
                jar = CookieJar.from_header(text)
        except (ValueError, json.JSONDecodeError) as exc:
            return self._finish(
                request_id, "cookies_import", account_id, started, logger, ok=False,
                error=invalid_param(f"cookie 解析失败：{exc}"),
            )

        jar.source = str(params.get("source", "manual-import"))
        # 显式给了 --domain 就以它为准做过滤：调用方已经说明了这批 cookie
        # 属于哪个域名，再拿默认的学习通后缀去筛会把它们全丢掉。
        domain_override = str(params.get("domain") or "") or None
        saved = self.cookie_store.save(
            account_id,
            jar,
            only_chaoxing=not bool(params.get("keep_all")),
            default_domain=domain_override or ".chaoxing.com",
            domain_filter=(domain_override,) if domain_override else None,
        )
        logger.emit(
            Event.REQUEST_RECEIVED,
            action="cookies_import",
            parsed=len(jar),
            kept=saved["kept"],
        )

        warnings: list[str] = []
        if saved["kept"] == 0 and len(jar) > 0:
            warnings.append(
                f"解析到 {len(jar)} 条 cookie，但都不属于学习通域名"
                f"（{'/'.join(CHAOXING_DOMAIN_SUFFIXES)}），已全部丢弃。"
                "如果你确认要保留，请加 --keep-all。"
            )
        expired = saved["diagnose"]["expired"]
        if expired:
            warnings.append(f"其中有 {expired} 条已过期，可能导致登录态无效")

        return self._finish(
            request_id, "cookies_import", account_id, started, logger, ok=True,
            state=TaskState.COMPLETED, data=saved, warnings=warnings,
        )

    def _cookies_extract(
        self,
        request_id: str,
        params: dict[str, Any],
        account_id: str,
        started: Any,
        logger: StructuredLogger,
    ) -> Envelope:
        """通过 CDP 从**独立浏览器实例**提取 cookie。

        为什么走 CDP 而不是直读 profile 数据库：新版浏览器启用了
        App-Bound Encryption，外部进程无法解密（详见 `cdp.py`）。
        让浏览器自己把 cookie 交出来是最短路径，也不涉及任何破解。
        """
        from . import browser as browser_mod
        from . import cdp as cdp_mod

        port = int(params.get("port") or browser_mod.debug_port())

        profile = browser_mod.profile_dir()
        guard_error: str | None = None
        if not browser_mod.is_isolated(profile):
            guard_error = (
                f"profile {profile} 被隔离守卫拒绝。提取 cookie 只能针对"
                f"项目内的独立实例，不能针对系统默认 profile。"
            )
            return self._finish(
                request_id, "cookies_extract", account_id, started, logger, ok=False,
                error=AdapterError(
                    code=Codes.INVALID_PARAM,
                    category=ErrorCategory.INPUT,
                    message=guard_error,
                ),
            )

        try:
            raw_cookies = cdp_mod.get_all_cookies(port)
        except CdpError as exc:
            return self._finish(
                request_id, "cookies_extract", account_id, started, logger, ok=False,
                error=AdapterError(
                    code=Codes.ADAPTER_NOT_READY,
                    category=ErrorCategory.NOT_SUPPORTED,
                    message=f"无法通过 CDP 连接独立浏览器（端口 {port}）：{exc}",
                ),
                next_actions=[
                    "先启动独立浏览器：python -m orchestrator.browser --launch "
                    "--url https://i.chaoxing.com",
                    "在那个独立窗口里登录学习通（不是你的日常 Chrome）",
                    "登录成功后重新执行本命令",
                    f"若浏览器已启动但仍失败，用 `python -m orchestrator.browser "
                    f"--status` 确认 {port} 端口在线",
                ],
            )

        jar = CookieJar.from_cdp(raw_cookies)
        saved = self.cookie_store.save(
            account_id, jar, only_chaoxing=not bool(params.get("keep_all"))
        )
        logger.emit(
            Event.REQUEST_RECEIVED,
            action="cookies_extract",
            port=port,
            total=len(jar),
            kept=saved["kept"],
        )

        warnings: list[str] = []
        if saved["kept"] == 0:
            warnings.append(
                "没有取到任何学习通 cookie —— 大概率是还没有在**那个独立窗口**里"
                "登录学习通。请在该窗口打开 https://i.chaoxing.com 登录后重试。"
            )
        else:
            expired = saved["diagnose"]["expired"]
            if expired:
                warnings.append(f"有 {expired} 条已过期，登录态可能不完整")

        return self._finish(
            request_id, "cookies_extract", account_id, started, logger, ok=True,
            state=TaskState.COMPLETED, data={**saved, "port": port}, warnings=warnings,
        )

    def _cookies_login(
        self,
        request_id: str,
        params: dict[str, Any],
        account_id: str,
        started: Any,
        logger: StructuredLogger,
    ) -> Envelope:
        """一站式登录：起浏览器 → 等你登录 → 自动提取落盘。

        检测方式是**基线对比**：先记下当前学习通 cookie 条数，然后轮询，
        当条数相对基线增长到阈值就认为登录完成。

        为什么用"增长量"而不是"某个 cookie 名"：cookie 名是平台内部实现，
        没有权威文档，硬编码某个名字（比如 `_uid`）在平台改版后会静默失效。
        增长量是行为层面的信号，更稳。

        但必须说清楚：**这是弱信号**。真正确认登录态的是 `cookies_verify`
        （它会真的请求一次平台）。所以本命令结束时永远会提示去跑 verify。
        """
        from . import browser as browser_mod
        from . import cdp as cdp_mod

        port = int(params.get("port") or browser_mod.debug_port())
        timeout_s = float(params.get("timeout") or 300)
        poll_s = max(0.5, float(params.get("poll") or 3))
        url = str(params.get("url") or "https://i.chaoxing.com")
        min_delta = max(1, int(params.get("min_delta") or 3))
        open_browser = not bool(params.get("no_open"))

        profile = browser_mod.profile_dir()
        if not browser_mod.is_isolated(profile):
            return self._finish(
                request_id, "cookies_login", account_id, started, logger, ok=False,
                error=AdapterError(
                    code=Codes.INVALID_PARAM,
                    category=ErrorCategory.INPUT,
                    message=f"profile {profile} 被隔离守卫拒绝，不启动。",
                ),
            )

        launched = False
        browser_label = ""
        if open_browser:
            try:
                plan, process = browser_mod.launch(port=port, url=url)
                launched = process is not None
                browser_label = str(plan.browser)
            except browser_mod.IsolationError as exc:
                return self._finish(
                    request_id, "cookies_login", account_id, started, logger, ok=False,
                    error=AdapterError(
                        code=Codes.INVALID_PARAM, category=ErrorCategory.INPUT,
                        message=str(exc),
                    ),
                )
            except browser_mod.BrowserNotFound as exc:
                return self._finish(
                    request_id, "cookies_login", account_id, started, logger, ok=False,
                    error=AdapterError(
                        code=Codes.NOT_SUPPORTED, category=ErrorCategory.NOT_SUPPORTED,
                        message=str(exc),
                    ),
                )

        def _count() -> int | None:
            try:
                raw = cdp_mod.get_all_cookies(port)
            except CdpError:
                return None
            return len(CookieJar.from_cdp(raw).filter_domains())

        # 进入轮询前必须先确认 CDP 就绪。
        # 否则 baseline 拿不到，_count() 每轮都失败，结果必然是等满
        # --timeout 秒（默认 300）后报超时 —— 让使用者白等五分钟才拿到
        # 一个"超时"，而真正的原因（浏览器没起来）在第一秒就已经确定了。
        status = browser_mod.probe_cdp(port)
        if not status.alive:
            tail = browser_mod.read_log_tail()
            return self._finish(
                request_id, "cookies_login", account_id, started, logger, ok=False,
                state=TaskState.NEEDS_MANUAL_ACTION,
                error=AdapterError(
                    code=Codes.CDP_UNAVAILABLE,
                    category=ErrorCategory.TRANSIENT,
                    message=(
                        f"CDP 端点 {browser_mod.cdp_url(port)} 不可达，"
                        f"浏览器没有起来（或已退出），因此不进入登录轮询。"
                    ),
                    extra={
                        "port": port,
                        "browser": browser_label,
                        "log_tail": tail,
                    },
                ),
                next_actions=[
                    "python -m orchestrator.browser --diagnose   # 看卡在哪一环",
                    "python -m orchestrator.browser --launch --url https://i.chaoxing.com"
                    "   # 手动启动（在你的终端里跑）",
                    f"若端口被占用，换一个：--port {port + 1}",
                ],
                warnings=(
                    [f"浏览器日志末尾：{tail}"] if tail else
                    ["浏览器没有产生任何输出 —— 通常意味着它没能启动。"]
                ),
            )

        if self.echo is not None:
            self.echo(
                f"[login] 独立浏览器{'已启动' if launched else '已在运行'}（端口 {port}）。"
                f"请在那个窗口里打开 {url} 并登录学习通。"
            )

        baseline = _count()
        if baseline is None:
            baseline = -1

        if baseline >= min_delta:
            threshold = baseline
            already = True
        else:
            threshold = min_delta if baseline <= 0 else baseline + min_delta
            already = False

        deadline = time.monotonic() + timeout_s
        rounds = 0
        last_count = max(baseline, 0)
        cancelled = False

        while time.monotonic() < deadline:
            if self.control.read(request_id) is not None:
                cancelled = True
                break
            rounds += 1
            count = _count()
            if count is not None:
                last_count = count
                logger.emit(
                    Event.REQUEST_RECEIVED,
                    action="cookies_login_poll",
                    round=rounds,
                    cookies=count,
                    threshold=threshold,
                    baseline=baseline,
                )
                if self.echo is not None and rounds % 4 == 1:
                    self.echo(
                        f"[login] 第 {rounds} 次检查：学习通 cookie {count} 条"
                        f"（阈值 {threshold}）…"
                    )
                if count >= threshold:
                    break
            self.sleeper(poll_s)
        else:
            # 循环自然结束 = 超时
            return self._finish(
                request_id, "cookies_login", account_id, started, logger, ok=False,
                state=TaskState.NEEDS_MANUAL_ACTION,
                error=AdapterError(
                    code=Codes.LOGIN_TIMEOUT,
                    category=ErrorCategory.AUTH,
                    message=(
                        f"等待 {timeout_s:.0f}s 仍未见学习通 cookie 增长"
                        f"（当前 {last_count} 条，阈值 {threshold}）"
                    ),
                    extra={"baseline": baseline, "last_count": last_count,
                           "threshold": threshold, "rounds": rounds},
                ),
                next_actions=[
                    "确认你是在独立浏览器窗口里登录，而不是日常 Chrome",
                    f"若还没登录完，把 --timeout 调大重试（当前 {timeout_s:.0f}s）",
                    "若浏览器没起来，先跑 python -m orchestrator.browser --launch",
                ],
            )

        if cancelled:
            return self._finish(
                request_id, "cookies_login", account_id, started, logger, ok=False,
                state=TaskState.CANCELLED,
                error=AdapterError(
                    code=Codes.INTERNAL_ERROR, category=ErrorCategory.TRANSIENT,
                    message="收到取消信号，已停止等待登录",
                ),
            )

        raw_cookies = cdp_mod.get_all_cookies(port)
        jar = CookieJar.from_cdp(raw_cookies)
        saved = self.cookie_store.save(
            account_id, jar, only_chaoxing=not bool(params.get("keep_all"))
        )
        logger.emit(
            Event.REQUEST_RECEIVED,
            action="cookies_login_done",
            baseline=baseline,
            kept=saved["kept"],
            rounds=rounds,
        )

        warnings = [
            "「检测到 cookie 增长」只是**弱信号** —— 它说明浏览器侧有登录动作，"
            "不代表这些 cookie 在服务端还有效。请用 cookies_verify 做真实确认。"
        ]
        if already:
            warnings.append(
                f"启动时该 profile 已有 {baseline} 条学习通 cookie，可能之前就登录过。"
            )

        data = {
            **saved,
            "port": port,
            "baseline": baseline,
            "final_count": saved["kept"],
            "threshold": threshold,
            "rounds": rounds,
            "browser": browser_label,
            "detected": "already_logged_in" if already else "cookie_growth",
        }
        return self._finish(
            request_id, "cookies_login", account_id, started, logger, ok=True,
            state=TaskState.COMPLETED, data=data, warnings=warnings,
            next_actions=[
                "下一步：python -m orchestrator.cli cookies_verify  # 真实确认会话有效",
            ],
        )

    def _cookies_verify(
        self,
        request_id: str,
        params: dict[str, Any],
        account_id: str,
        started: Any,
        logger: StructuredLogger,
    ) -> Envelope:
        """用已落盘的 cookie 真实请求一次平台，验证会话。

        这是**唯一**能真正回答"这个登录态还能用吗"的手段。
        """
        from . import session_verify

        jar = self.cookie_store.load(account_id)
        if jar is None or not jar.cookies:
            return self._finish(
                request_id, "cookies_verify", account_id, started, logger, ok=False,
                error=AdapterError(
                    code=Codes.AUTH_EXPIRED, category=ErrorCategory.AUTH,
                    message="尚未落盘任何 cookie，无法验证会话",
                ),
                next_actions=[
                    "先跑 python -m orchestrator.cli cookies_login（一站式登录）",
                    "或 python -m orchestrator.cli cookies_import --header \"...\"",
                ],
            )

        url = str(params.get("url") or session_verify.DEFAULT_BASE_URL)
        timeout_s = float(params.get("timeout") or 15)

        probe = session_verify.probe_session(jar, url=url, timeout_s=timeout_s)
        logger.emit(
            Event.REQUEST_RECEIVED,
            action="session_verify",
            verdict=str(probe.verdict),
            status=probe.status,
            body_size=probe.body_size,
            elapsed_ms=probe.elapsed_ms,
            cookie_count=probe.cookie_count,
            signals=probe.signals,
        )

        if probe.verdict is session_verify.SessionVerdict.VALID:
            return self._finish(
                request_id, "cookies_verify", account_id, started, logger, ok=True,
                state=TaskState.COMPLETED, data=probe.to_dict(),
                next_actions=["可以进入 M1：python -m orchestrator.cli list_courses"],
            )

        if probe.verdict is session_verify.SessionVerdict.INVALID:
            return self._finish(
                request_id, "cookies_verify", account_id, started, logger, ok=False,
                state=TaskState.NEEDS_MANUAL_ACTION,
                error=AdapterError(
                    code=Codes.SESSION_INVALID, category=ErrorCategory.AUTH,
                    message="；".join(probe.signals) or "会话无效",
                    extra={"status": probe.status, "location": probe.location},
                ),
                data=probe.to_dict(),
                next_actions=session_verify.next_actions_for(probe),
            )

        # INCONCLUSIVE / UNREACHABLE：**不假装成功**，把证据交出去
        is_unreachable = probe.verdict is session_verify.SessionVerdict.UNREACHABLE
        return self._finish(
            request_id, "cookies_verify", account_id, started, logger, ok=False,
            state=TaskState.FAILED if is_unreachable else TaskState.COMPLETED,
            error=AdapterError(
                code=(Codes.ADAPTER_TIMEOUT if is_unreachable else Codes.SESSION_UNCONFIRMED),
                category=(
                    ErrorCategory.TRANSIENT if is_unreachable
                    else ErrorCategory.PLATFORM_CHANGED
                ),
                message="；".join(probe.signals) or probe.detail or "无法确认会话状态",
                retryable=is_unreachable,
                extra={"status": probe.status, "body_size": probe.body_size},
            ),
            data=probe.to_dict(),
            warnings=[
                "结论是「无法确认」而不是「失败」—— 请勿据此判定登录态失效。"
            ],
            next_actions=session_verify.next_actions_for(probe),
        )

    # ------------------------------------------------------------------
    # 收尾
    # ------------------------------------------------------------------
    def _finish_from_outcome(
        self,
        request_id: str,
        command: str,
        account_id: str,
        started: Any,
        logger: StructuredLogger,
        outcome: RouterOutcome,
        data: Any = None,
        warnings: list[str] | None = None,
        state: TaskState | None = None,
    ) -> Envelope:
        """由 Router 结果生成 Envelope。

        `state` 用于覆盖：`run_*` 命令可能"技术上成功但被暂停/取消"，
        此时 Router 报的是 completed，而真实业务状态是 paused/cancelled。
        若不覆盖，`pause` 就会在 Envelope 里显示成"完成"——正是"隐藏
        真实状态"的一种形式。
        """
        return self._finish(
            request_id, command, account_id, started, logger,
            ok=outcome.ok,
            state=state or outcome.state,
            data=outcome.data if data is None else data,
            warnings=(list(outcome.warnings) if warnings is None else warnings),
            error=outcome.error,
            adapter_id=outcome.adapter_id,
            adapter_version=outcome.adapter_version,
            fallback_trace=outcome.fallback_trace,
            next_actions=outcome.next_actions,
        )

    def _finish(
        self,
        request_id: str,
        command: str,
        account_id: str,
        started: Any,
        logger: StructuredLogger,
        *,
        ok: bool,
        state: TaskState = TaskState.COMPLETED,
        data: Any = None,
        warnings: list[str] | None = None,
        error: AdapterError | None = None,
        adapter_id: str | None = None,
        adapter_version: str | None = None,
        fallback_trace: list[dict[str, Any]] | None = None,
        next_actions: list[str] | None = None,
    ) -> Envelope:
        finished = now()
        duration = int((finished - started).total_seconds() * 1000)
        envelope = Envelope(
            ok=ok,
            command=command,
            request_id=request_id,
            account=account_id,
            adapter=adapter_id,
            adapter_version=adapter_version,
            state=str(state),
            started_at=to_iso(started),
            finished_at=to_iso(finished),
            duration_ms=duration,
            data=data,
            warnings=list(warnings or []),
            error=error.to_dict() if error else None,
            fallback_trace=list(fallback_trace or []),
            next_actions=list(next_actions or []),
        )
        logger.emit(
            Event.REQUEST_FINISHED,
            level="INFO" if ok else "ERROR",
            adapter=adapter_id,
            state_to=str(state),
            duration_ms=duration,
            ok=ok,
            error=envelope.error,
            warning_count=len(envelope.warnings),
            fallback_count=len(envelope.fallback_trace),
        )
        return envelope


class _CheckpointDriver:
    """把断点状态变化同时记录到控制日志。"""

    def __init__(self, checkpoint: Checkpoint, logger: StructuredLogger) -> None:
        self.checkpoint = checkpoint
        self.logger = logger
        self.state = checkpoint.state

    def to(self, target: TaskState, **fields: Any) -> TaskState:
        from .state import is_valid_transition

        if target != self.state and is_valid_transition(self.state, target):
            self.logger.emit(
                Event.STATE_TRANSITION,
                state_from=str(self.state),
                state_to=str(target),
                **fields,
            )
        self.state = target
        self.checkpoint.state = target
        return target


_ = (Course, fixtures, CAPABILITIES, now_iso)
