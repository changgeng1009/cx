"""结构化日志（JSONL）+ 原始日志层。

三层设计（docs/03 §6.2）：
1. 审计层 `runs/{request_id}.jsonl` —— 机器读，字段固定，长期保留
2. 人读层 stdout —— 只打关键事件
3. 原始层 `runs/{request_id}.raw.log` —— 第三方项目的原始输出原样保留

第 3 层是排障的关键：当 Adapter 的解析器看不懂上游输出时，唯一能定位
原因的就是原始输出。而它同时是最容易泄漏凭据的地方——所以原始层
**写入前必须过脱敏**（V9 验收项）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable, TextIO

from .models import now_iso
from .redact import redact


class Event:
    """事件名常量。固定集合便于统计与告警。"""

    REQUEST_RECEIVED = "request.received"
    REQUEST_FINISHED = "request.finished"
    ADAPTER_SELECTED = "adapter.selected"
    ADAPTER_FALLBACK = "adapter.fallback"
    ADAPTER_PROBE = "adapter.probe"
    ADAPTER_RETRY = "adapter.retry"
    STATE_TRANSITION = "state.transition"
    COURSE_STARTED = "course.started"
    COURSE_FINISHED = "course.finished"
    CHAPTER_STARTED = "chapter.started"
    CHAPTER_FINISHED = "chapter.finished"
    TASK_POINT_STARTED = "task_point.started"
    TASK_POINT_COMPLETED = "task_point.completed"
    TASK_POINT_SKIPPED = "task_point.skipped"
    TASK_POINT_FAILED = "task_point.failed"
    RISK_CONTROL_DETECTED = "risk.control_detected"
    THROTTLE_WAIT = "throttle.wait"
    PAUSE_REQUESTED = "pause.requested"
    PAUSE_EFFECTIVE = "pause.effective"
    RESUME_EFFECTIVE = "resume.effective"
    MANUAL_ACTION_REQUIRED = "manual_action.required"
    TICKET_CREATED = "answer.ticket_created"
    TICKET_ANSWERED = "answer.ticket_answered"
    TICKET_TIMEOUT = "answer.ticket_timeout"
    SIGN_DETECTED = "sign.detected"
    SIGN_COMPLETED = "sign.completed"


class StructuredLogger:
    def __init__(
        self,
        run_dir: str | Path,
        request_id: str,
        echo: Callable[[str], None] | None = None,
        clock: Callable[[], str] = now_iso,
        write_jsonl: bool = True,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.request_id = request_id
        self._clock = clock
        self._echo = echo
        self._write_jsonl = write_jsonl
        self.records: list[dict[str, Any]] = []
        self._raw_chunks: list[str] = []
        self._bound: dict[str, Any] = {"request_id": request_id}
        self._jsonl_handle: TextIO | None = None
        self._raw_handle: TextIO | None = None
        if self._write_jsonl:
            self.run_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    @property
    def jsonl_path(self) -> Path:
        return self.run_dir / f"{self.request_id}.jsonl"

    @property
    def raw_path(self) -> Path:
        return self.run_dir / f"{self.request_id}.raw.log"

    def _jsonl(self) -> TextIO:
        if self._jsonl_handle is None:
            self._jsonl_handle = self.jsonl_path.open("a", encoding="utf-8")
        return self._jsonl_handle

    def _raw(self) -> TextIO:
        if self._raw_handle is None:
            self._raw_handle = self.raw_path.open("a", encoding="utf-8")
        return self._raw_handle

    # ------------------------------------------------------------------
    def bind(self, **fields: Any) -> "StructuredLogger":
        """绑定随请求稳定不变的字段（account / adapter …）。"""
        self._bound.update(fields)
        return self

    def emit(self, event: str, level: str = "INFO", **fields: Any) -> dict[str, Any]:
        record: dict[str, Any] = {"ts": self._clock(), "level": level, "event": event}
        record.update(self._bound)
        record.update(fields)
        record = redact(record)
        self.records.append(record)
        if self._write_jsonl:
            self._jsonl().write(json.dumps(record, ensure_ascii=False) + "\n")
            self._jsonl().flush()
        if self._echo is not None and level in ("INFO", "WARN", "ERROR"):
            self._echo(self._humanize(record))
        return record

    def raw(self, adapter_id: str, text: str) -> None:
        """记录第三方项目的原始输出（脱敏后落盘）。"""
        if not text:
            return
        safe = redact(text)
        self._raw_chunks.append(safe)
        if self._write_jsonl:
            self._raw().write(f"----- {adapter_id} @ {self._clock()} -----\n")
            self._raw().write(safe)
            if not safe.endswith("\n"):
                self._raw().write("\n")
            self._raw().flush()

    @property
    def raw_text(self) -> str:
        return "\n".join(self._raw_chunks)

    # ------------------------------------------------------------------
    def _humanize(self, record: dict[str, Any]) -> str:
        event = record.get("event", "")
        bits = [str(record.get("ts", ""))[11:19], f"{record.get('level','INFO'):<5}", event]
        for key in ("adapter", "account", "chapter", "task_type", "task_point_id"):
            if record.get(key):
                bits.append(str(record[key]))
        if record.get("message"):
            bits.append(str(record["message"]))
        if record.get("error"):
            err = record["error"]
            if isinstance(err, dict):
                bits.append(f"error={err.get('code')}")
        if record.get("reason"):
            bits.append(f"({record['reason']})")
        return "  ".join(bits)

    def close(self) -> None:
        for handle in (self._jsonl_handle, self._raw_handle):
            if handle is not None:
                handle.close()
        self._jsonl_handle = None
        self._raw_handle = None

    def __enter__(self) -> "StructuredLogger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def make_stdout_echo(stream: TextIO | None = None) -> Callable[[str], None]:
    target = stream or sys.stdout

    def _echo(line: str) -> None:
        print(line, file=target, flush=True)

    return _echo
