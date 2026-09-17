"""Mock Adapter：M0 的验证基座。

这不是"测试脚手架"，而是 **Router 与状态机可验证性的前提**。
理由（docs/04 §6）：如果 M0 直接接真实项目，那么 fallback、超时、
风控熔断、暂停断点这些边界情况**几乎不可能自然触发**，只能靠"读代码
觉得对"。有了可注入故障的 Mock，这些路径全部可以被断言。

能力：
- `responses`：指定某能力的正常返回
- `faults`：让某能力恒定失败（指定 ErrorCategory）
- `transient_failures`：前 N 次失败、之后成功（测退避重试）
- `overrides`：完全接管某能力
- `risk_output`：往 raw_output 里注入风控特征（测熔断）
- `run_script`：模拟跑课时逐个上报任务点（测暂停/断点/进度）
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import fixtures
from ..errors import (
    AdapterError,
    Codes,
    ErrorCategory,
    invalid_param,
    not_supported,
)
from ..models import (
    AdapterResult,
    ProbeResult,
    TaskContext,
    TaskPoint,
    TaskPointStatus,
    TaskType,
)
from ..registry import Manifest
from ..structured_log import Event
from .base import Adapter

Handler = Callable[[str, dict[str, Any], TaskContext], AdapterResult]


@dataclass
class RunScript:
    """跑课时的行为脚本。"""

    #: 上报进度前是否真的 sleep；M0 默认不 sleep（测试要快）
    step_delay_ms: int = 0
    #: 这些任务点上报"完成"后额外上报一次失败（用于测失败统计）
    fail_ids: list[str] = field(default_factory=list)
    #: 只处理这些类型；空表示全部
    only_types: list[str] = field(default_factory=list)


class MockAdapter(Adapter):
    def __init__(
        self,
        manifest: Manifest,
        *,
        responses: dict[str, Any] | None = None,
        faults: dict[str, AdapterError] | None = None,
        transient_failures: dict[str, int] | None = None,
        overrides: dict[str, Handler] | None = None,
        risk_output: str = "",
        raw_output: str = "",
        probe_healthy: bool = True,
        run_script: RunScript | None = None,
        capability_levels: dict[str, str] | None = None,
    ) -> None:
        super().__init__(manifest)
        self.responses: dict[str, Any] = dict(responses or {})
        self.faults: dict[str, AdapterError] = dict(faults or {})
        self.transient_failures: dict[str, int] = dict(transient_failures or {})
        self.overrides: dict[str, Handler] = dict(overrides or {})
        self.risk_output = risk_output
        self.default_raw_output = raw_output
        self.probe_healthy = probe_healthy
        self.run_script = run_script or RunScript()
        self.capability_levels = dict(capability_levels or {})

        #: 观测点：测试靠这些断言"Router 试过谁、试了几次"
        self.calls: list[dict[str, Any]] = []
        self.setup_calls: list[str] = []
        self.probe_calls = 0
        self.cancel_calls = 0

    # ------------------------------------------------------------------
    def setup(self, account: Any) -> None:
        self.setup_calls.append(account.account_id)

    def probe(self) -> ProbeResult:
        self.probe_calls += 1
        if not self.probe_healthy:
            return ProbeResult(healthy=False, detail="mock: 探活失败")
        return ProbeResult(healthy=True, latency_ms=1, detail="mock")

    def supports(self, capability_id: str) -> bool:
        override = self.capability_levels.get(capability_id)
        if override is not None:
            return override in ("full", "partial")
        return super().supports(capability_id)

    def cancel(self, ctx: TaskContext) -> None:
        self.cancel_calls += 1
        ctx.cancel_event.set()

    # ------------------------------------------------------------------
    def invoke(
        self, capability_id: str, params: dict[str, Any], ctx: TaskContext
    ) -> AdapterResult:
        self.calls.append(
            {"capability": capability_id, "params": dict(params), "dry_run": ctx.dry_run}
        )

        # 1) 完全接管
        if capability_id in self.overrides:
            return self.overrides[capability_id](capability_id, params, ctx)

        # 2) 恒定故障
        if capability_id in self.faults:
            error = self.faults[capability_id]
            return self.failure(error, raw_output=self._raw(capability_id))

        # 3) 前 N 次瞬时失败
        remaining = self.transient_failures.get(capability_id, 0)
        if remaining > 0:
            self.transient_failures[capability_id] = remaining - 1
            error = AdapterError(
                code=Codes.ADAPTER_TIMEOUT,
                category=ErrorCategory.TRANSIENT,
                message=f"mock: 第 {remaining} 次瞬时失败",
                retryable=True,
            )
            return self.failure(error, raw_output=self._raw(capability_id))

        # 4) 显式指定返回
        if capability_id in self.responses:
            return self.success(
                self.responses[capability_id], raw_output=self._raw(capability_id)
            )

        # 5) 内置默认行为
        handler = _DEFAULT_HANDLERS.get(capability_id)
        if handler is None:
            return self.failure(not_supported(self.adapter_id, capability_id))

        return handler(self, capability_id, params, ctx)

    # ------------------------------------------------------------------
    def _raw(self, capability_id: str) -> str:
        if self.risk_output:
            return self.risk_output
        if self.default_raw_output:
            return self.default_raw_output
        return f"[mock:{self.adapter_id}] {capability_id} 执行结束"

    def raw_with_risk(self, signature: str = "操作过于频繁，请稍后再试") -> "MockAdapter":
        self.risk_output = signature
        return self


# --------------------------------------------------------------------------
# 内置默认行为（对齐上游术语与数据结构）
# --------------------------------------------------------------------------


def _needs_course(params: dict[str, Any]) -> str:
    course_id = params.get("course_id")
    if not course_id:
        raise _MissingCourse()
    return str(course_id)


class _MissingCourse(Exception):
    pass


def _shape_error(capability_id: str, params: dict[str, Any]) -> AdapterResult | None:
    try:
        _needs_course(params)
    except _MissingCourse:
        return AdapterResult.failure(
            invalid_param(f"能力 {capability_id} 需要参数 course_id")
        )
    return None


def _h_courses(adapter: MockAdapter, cap: str, params: dict, ctx: TaskContext) -> AdapterResult:
    return adapter.success(
        {"courses": [c.to_dict() for c in fixtures.make_courses()]},
        raw_output=adapter._raw(cap),
    )


def _h_course_detail(adapter: MockAdapter, cap: str, params: dict, ctx: TaskContext) -> AdapterResult:
    bad = _shape_error(cap, params)
    if bad:
        return bad
    course_id = str(params["course_id"])
    course = next((c for c in fixtures.make_courses() if c.course_id == course_id), None)
    if course is None:
        return adapter.failure(
            AdapterError(
                code=Codes.COURSE_NOT_FOUND,
                category=ErrorCategory.INPUT,
                message=f"课程不存在：{course_id}",
            )
        )
    chapters = fixtures.make_chapters(course_id)
    points = fixtures.make_task_points(course_id)
    payload = course.to_dict()
    payload.update(
        {
            "chapter_count": len(chapters),
            "task_point_count": len(points),
            "done_task_point_count": sum(
                1 for p in points if p.status == TaskPointStatus.DONE
            ),
        }
    )
    return adapter.success({"course": payload}, raw_output=adapter._raw(cap))


def _h_chapters(adapter: MockAdapter, cap: str, params: dict, ctx: TaskContext) -> AdapterResult:
    bad = _shape_error(cap, params)
    if bad:
        return bad
    course_id = str(params["course_id"])
    return adapter.success(
        {"chapters": [c.to_dict() for c in fixtures.make_chapters(course_id)]},
        raw_output=adapter._raw(cap),
    )


def _h_task_points(adapter: MockAdapter, cap: str, params: dict, ctx: TaskContext) -> AdapterResult:
    bad = _shape_error(cap, params)
    if bad:
        return bad
    course_id = str(params["course_id"])
    points = fixtures.make_task_points(course_id)
    return adapter.success(
        {
            "course_id": course_id,
            "chapters": [c.to_dict() for c in fixtures.make_chapters(course_id)],
            "task_points": [p.to_dict() for p in points],
        },
        warnings=["mock: 任务点类型由章节页推断，非平台原生字段"],
        raw_output=adapter._raw(cap),
    )


def _h_progress(adapter: MockAdapter, cap: str, params: dict, ctx: TaskContext) -> AdapterResult:
    courses = fixtures.make_courses()
    if params.get("course_id"):
        courses = [c for c in courses if c.course_id == str(params["course_id"])]
    rows = []
    for course in courses:
        points = fixtures.make_task_points(course.course_id)
        total = len(points)
        done = sum(1 for p in points if p.status == TaskPointStatus.DONE)
        rows.append(
            {
                "course_id": course.course_id,
                "name": course.name,
                "done": done,
                "total": total,
                "ratio": round(done / total, 4) if total else 0.0,
            }
        )
    total_done = sum(r["done"] for r in rows)
    total_all = sum(r["total"] for r in rows)
    return adapter.success(
        {
            "overall": {
                "done": total_done,
                "total": total_all,
                "ratio": round(total_done / total_all, 4) if total_all else 0.0,
            },
            "courses": rows,
        },
        raw_output=adapter._raw(cap),
    )


def _h_homework(adapter: MockAdapter, cap: str, params: dict, ctx: TaskContext) -> AdapterResult:
    items = fixtures.make_homework(
        str(params["course_id"]) if params.get("course_id") else None
    )
    unsubmitted = [i for i in items if not i.submitted]
    unsubmitted.sort(key=lambda i: i.due_at or "9999")
    return adapter.success(
        {
            "homework": [i.to_dict() for i in items],
            "deadlines": [i.to_dict() for i in unsubmitted],
        },
        raw_output=adapter._raw(cap),
    )


def _h_notices(adapter: MockAdapter, cap: str, params: dict, ctx: TaskContext) -> AdapterResult:
    notices = fixtures.make_notices()
    keyword = str(params.get("keyword", "") or "")
    unread_only = bool(params.get("unread_only", False))
    items = []
    for notice in notices:
        if unread_only and not notice["unread"]:
            continue
        if keyword and keyword not in notice["title"] and keyword not in notice["content"]:
            continue
        items.append(notice)
    return adapter.success(
        {
            "notices": items,
            "unread_count": sum(1 for n in notices if n["unread"]),
            "note": "服务端关键词参数无效，此处为客户端过滤（对齐上游实测行为）",
        },
        raw_output=adapter._raw(cap),
    )


def _h_schedule(adapter: MockAdapter, cap: str, params: dict, ctx: TaskContext) -> AdapterResult:
    week = int(params.get("week", 3) or 3)
    return adapter.success(fixtures.make_schedule(week), raw_output=adapter._raw(cap))


def _h_download(adapter: MockAdapter, cap: str, params: dict, ctx: TaskContext) -> AdapterResult:
    return adapter.success(
        {
            "downloaded": [
                {
                    "name": str(params.get("filename", "讲义.pdf")),
                    "bytes": 2401280,
                    "path": str(params.get("save_dir", "./downloads")),
                }
            ]
        },
        raw_output=adapter._raw(cap),
    )


def _h_login(adapter: MockAdapter, cap: str, params: dict, ctx: TaskContext) -> AdapterResult:
    return adapter.success(
        {
            "logged_in": True,
            "account": ctx.account_id,
            "session": "mock-session",
        },
        raw_output=adapter._raw(cap),
    )


def _h_session(adapter: MockAdapter, cap: str, params: dict, ctx: TaskContext) -> AdapterResult:
    if cap == "C03":
        return adapter.success({"renewed": True}, raw_output=adapter._raw(cap))
    return adapter.success(
        {"persisted": True, "path": f"{ctx.account_id}/cookies.txt"},
        raw_output=adapter._raw(cap),
    )


def _h_run_course(adapter: MockAdapter, cap: str, params: dict, ctx: TaskContext) -> AdapterResult:
    """模拟"跑课"：逐个任务点上报进度。

    这是 M0 里最有价值的一段模拟，因为它让**暂停/取消/断点/进度日志**
    四条链路都能被真实触发——真实上游 A1 的行为方式也是这样：
    课程级入口，进度通过持续输出体现。
    """
    bad = _shape_error(cap, params)
    if bad:
        return bad

    course_id = str(params["course_id"])
    chapter_filter = params.get("chapter_id")
    target_types = {str(t) for t in (params.get("target_types") or [])}
    only_types = {str(t) for t in adapter.run_script.only_types}

    points: list[TaskPoint] = fixtures.make_task_points(course_id)
    if chapter_filter:
        points = [p for p in points if p.chapter_id == str(chapter_filter)]
    if target_types:
        points = [p for p in points if str(p.task_type) in target_types]
    if only_types:
        points = [p for p in points if str(p.task_type) in only_types]

    # resume 语义：断点里已完成的任务点跳过。
    # 真实上游不需要这个参数（学习通自己会跳过已完成任务点），但支持它
    # 能让 mock 诚实反映"恢复后只跑剩余部分"，从而让 V7 可断言。
    skip_ids = {str(x) for x in (params.get("skip_task_point_ids") or [])}
    skipped_done = [p for p in points if p.task_point_id in skip_ids]

    todo = [
        p
        for p in points
        if p.status == TaskPointStatus.TODO and p.task_point_id not in skip_ids
    ]
    locked = [p for p in points if p.status == TaskPointStatus.LOCKED]

    completed: list[str] = []
    failed: list[str] = []
    skipped: list[dict[str, str]] = [
        {"id": p.task_point_id, "type": str(p.task_type), "reason": "already_done"}
        for p in skipped_done
    ]
    stopped_reason = "finished"

    for point in locked:
        skipped.append(
            {"id": point.task_point_id, "type": str(point.task_type), "reason": "locked"}
        )
        ctx.report(
            Event.TASK_POINT_SKIPPED,
            task_point_id=point.task_point_id,
            task_type=str(point.task_type),
            reason="locked",
        )

    if ctx.dry_run:
        return adapter.success(
            {
                "dry_run": True,
                "would_process": [p.task_point_id for p in todo],
                "would_skip": skipped,
                "note": "dry-run：未产生任何写操作",
            },
            raw_output=adapter._raw(cap),
        )

    for point in todo:
        if ctx.cancelled:
            stopped_reason = "cancelled"
            break
        if ctx.pause_requested:
            stopped_reason = "paused"
            break

        ctx.report(
            Event.TASK_POINT_STARTED,
            task_point_id=point.task_point_id,
            task_type=str(point.task_type),
            chapter_id=point.chapter_id,
        )

        if adapter.run_script.step_delay_ms:
            time.sleep(adapter.run_script.step_delay_ms / 1000.0)

        if point.task_point_id in adapter.run_script.fail_ids:
            failed.append(point.task_point_id)
            ctx.report(
                Event.TASK_POINT_FAILED,
                task_point_id=point.task_point_id,
                task_type=str(point.task_type),
                error={"code": Codes.ADAPTER_ERROR, "category": "transient"},
            )
            continue

        completed.append(point.task_point_id)
        ctx.report(
            Event.TASK_POINT_COMPLETED,
            task_point_id=point.task_point_id,
            task_type=str(point.task_type),
            chapter_id=point.chapter_id,
        )

    locked_count = sum(1 for s in skipped if s["reason"] == "locked")
    already_count = sum(1 for s in skipped if s["reason"] == "already_done")
    warnings: list[str] = []
    if locked_count:
        warnings.append(f"mock: {locked_count} 个未开放任务点被跳过")
    if already_count:
        warnings.append(
            f"mock: {already_count} 个任务点因断点记录被跳过（对应真实平台的"
            "「已完成任务点自动跳过」行为）"
        )

    return adapter.success(
        {
            "course_id": course_id,
            "completed": completed,
            "failed": failed,
            "skipped": skipped,
            "stopped_reason": stopped_reason,
        },
        warnings=warnings,
        raw_output=adapter._raw(cap),
    )


def _h_answer(adapter: MockAdapter, cap: str, params: dict, ctx: TaskContext) -> AdapterResult:
    if cap == "C43":
        return adapter.success(
            {
                "questions": [
                    {
                        "index": 1,
                        "type": "single_choice",
                        "stem": "下列关于 TTL 与非门输入端的说法，正确的是",
                        "options": [
                            {"key": "A", "text": "悬空相当于高电平"},
                            {"key": "B", "text": "悬空相当于低电平"},
                            {"key": "C", "text": "悬空时输出不确定"},
                            {"key": "D", "text": "必须接地"},
                        ],
                        "image_ref": None,
                    }
                ],
                "raw_prompt": fixtures.make_upstream_question_prompt(),
            },
            raw_output=adapter._raw(cap),
        )
    if cap == "C44":
        return adapter.success(
            {"accepted": True, "format": "A#B#C"},
            raw_output=adapter._raw(cap),
        )
    return adapter.success(
        {"loop": "ok", "note": "mock: Agent 答题闭环"},
        raw_output=adapter._raw(cap),
    )


def _h_sign(adapter: MockAdapter, cap: str, params: dict, ctx: TaskContext) -> AdapterResult:
    activity = fixtures.make_sign_activity()
    if cap == "C48":
        return adapter.success(
            {"sign_type": str(params.get("type", "normal")), "status": "signed", "activity": activity},
            raw_output=adapter._raw(cap),
        )
    if cap == "C49":
        return adapter.success(
            {"supported_types": ["normal", "location", "qr", "gesture", "photo", "code"]},
            raw_output=adapter._raw(cap),
        )
    if cap == "C50":
        return adapter.success(
            {"watching": True, "activity": activity, "interval_s": int(params.get("interval", 30) or 30)},
            raw_output=adapter._raw(cap),
        )
    return adapter.success({"signed_out": True}, raw_output=adapter._raw(cap))


def _h_stub(adapter: MockAdapter, cap: str, params: dict, ctx: TaskContext) -> AdapterResult:
    """未细化的能力的占位实现：返回可辨识的确认结构。"""
    return adapter.success(
        {"capability": cap, "accepted": True, "note": "mock: 占位实现"},
        raw_output=adapter._raw(cap),
    )


_DEFAULT_HANDLERS: dict[str, Handler] = {
    "C01": _h_login,
    "C02": _h_session,
    "C03": _h_session,
    "C04": _h_stub,
    "C05": _h_stub,
    "C06": _h_courses,
    "C07": _h_course_detail,
    "C08": _h_chapters,
    "C09": _h_task_points,
    "C10": _h_progress,
    "C11": _h_progress,
    "C12": _h_run_course,
    "C13": _h_run_course,
    "C14": _h_run_course,
    "C15": _h_run_course,
    "C16": _h_run_course,
    "C17": _h_run_course,
    "C18": _h_run_course,
    "C19": _h_stub,
    "C20": _h_stub,
    "C21": _h_run_course,
    "C22": _h_stub,
    "C23": _h_stub,
    "C24": _h_stub,
    "C25": _h_stub,
    "C26": _h_run_course,
    "C27": _h_run_course,
    "C28": _h_stub,
    "C29": _h_homework,
    "C30": _h_stub,
    "C31": _h_notices,
    "C32": _h_download,
    "C33": _h_schedule,
    "C34": _h_stub,
    "C35": _h_stub,
    "C36": _h_stub,
    "C37": _h_stub,
    "C39": _h_stub,
    "C40": _h_stub,
    "C41": _h_stub,
    "C42": _h_stub,
    "C43": _h_answer,
    "C44": _h_answer,
    "C45": _h_answer,
    "C46": _h_answer,
    "C47": _h_answer,
    "C48": _h_sign,
    "C49": _h_sign,
    "C50": _h_sign,
    "C51": _h_sign,
}


def build_mock_adapter(manifest: Manifest, **kwargs: Any) -> MockAdapter:
    return MockAdapter(manifest, **kwargs)


_ = TaskType  # 保留引用：mock 的类型过滤与 models 的枚举定义对齐
