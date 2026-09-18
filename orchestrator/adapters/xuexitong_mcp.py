"""Xuexitong-mcp Adapter（读侧主力，MIT 许可）。

接入方式是 **subprocess 文件边界**，不是 import：
- 统一层保持零第三方依赖；上游需要 requests/pycryptodome，只在
  worker 子进程里被使用。
- 上游 `ChaoxingClient(config_dir=...)` 从 `{config_dir}/session_cookies.json`
  读取会话（格式 `{name: value}`）——所以 cookie 传递完全走文件：
  `setup`/`invoke` 把账号工作区 `cookies.json` 桥接成上游格式，**零改上游**。

职责边界（docs/03 §5.2）：`invoke` 只把 worker 退出码映射成 ErrorCategory，
fallback 由 Router 决定；本 Adapter 不自行决定任何重试/降级。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from ..errors import AdapterError, Codes, ErrorCategory
from ..models import AccountContext, AdapterResult, ProbeResult, TaskContext
from ..registry import Manifest
from .base import Adapter
from .xtmcp_worker import (
    EXIT_AUTH,
    EXIT_DEPS,
    EXIT_INTERNAL,
    EXIT_OK,
    EXIT_PLATFORM,
)

#: 能力 → (worker op, 参数构造函数)
_OP_MAP: dict[str, str] = {
    "C06": "fetch_courses",
    "C07": "fetch_profile",       # get_course：见 _args_for 的特殊处理
    "C08": "fetch_chapters",
    "C10": "fetch_progress_overview",
    "C11": "fetch_progress_overview",
    "C29": "fetch_deadline_overview",
    "C30": "fetch_exams",
    "C31": "fetch_notices",
    "C33": "fetch_schedule",
}

#: 不同能力的合理超时（秒）。schedule_full/逐课作业要拉多页，给长一些。
_OP_TIMEOUT_S: dict[str, float] = {
    "C33": 150.0,
    "C29": 120.0,
    "C08": 90.0,
}


class XuexitongMcpAdapter(Adapter):
    def __init__(
        self,
        manifest: Manifest,
        worker_path: str | Path | None = None,
        python_exe: str | None = None,
        upstream_path: str | Path | None = None,
        min_interval_ms: int = 0,
        accounts_dir: str | Path | None = None,
    ) -> None:
        super().__init__(manifest)
        root = Path(__file__).resolve().parents[2]
        self.root = root
        self.accounts_dir = Path(accounts_dir or (root / "accounts"))
        self.worker_path = Path(worker_path or (root / "orchestrator" / "adapters" / "xtmcp_worker.py"))
        self.upstream_path = Path(upstream_path or (root / "upstreams" / "xuexitong-mcp"))
        self.python_exe = python_exe or sys.executable
        self.min_interval_s = max(min_interval_ms, 0) / 1000.0
        self._last_invoke_at = 0.0
        self._throttle_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 目录约定
    # ------------------------------------------------------------------
    def upstream_config_dir(self, account_id: str) -> Path:
        """上游 config_dir：账号工作区内的独立子目录（gitignored）。"""
        return self.accounts_dir / account_id / "upstream" / "xtmcp"

    # ------------------------------------------------------------------
    # cookie 桥接（文件边界；只读 cookies.json，不写它）
    # ------------------------------------------------------------------
    def ensure_bridge(self, account_id: str) -> Path:
        """把统一层 cookies.json 转成上游 session_cookies.json（幂等）。

        Router 不会调用 `setup`，所以这里必须在每次 `invoke` 前执行；
        以源文件 mtime 判断是否需要重写。上游格式是扁平的 `{name: value}`，
        同名 cookie 后写覆盖先写（与统一层"手动粘贴不按域过滤"的取舍一致）。
        """
        src = self.accounts_dir / account_id / "cookies.json"
        if not src.is_file():
            raise FileNotFoundError(
                f"账号 {account_id} 还没有 cookie：{src} 不存在。"
                "请先 cookies_login / cookies_import。"
            )
        target = self.upstream_config_dir(account_id) / "session_cookies.json"
        if target.is_file() and target.stat().st_mtime >= src.stat().st_mtime:
            return target

        raw = json.loads(src.read_text(encoding="utf-8"))
        items = raw.get("cookies") if isinstance(raw, dict) else raw
        flat: dict[str, str] = {}
        for item in items or []:
            name = str(item.get("name") or "")
            value = str(item.get("value") or "")
            if not name:
                continue
            flat[name] = value
        if not flat:
            raise FileNotFoundError(f"{src} 里没有可用的 cookie")

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(flat, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        return target

    def setup(self, account: AccountContext) -> None:
        """幂等准备。失败抛 FileNotFoundError，由 Router 归类处理。"""
        self.ensure_bridge(account.account_id)

    # ------------------------------------------------------------------
    # 节流：尊重 manifest 的 min_interval_ms，避免触发平台风控
    # ------------------------------------------------------------------
    def _throttle(self) -> None:
        if self.min_interval_s <= 0:
            return
        with self._throttle_lock:
            wait = self._last_invoke_at + self.min_interval_s - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_invoke_at = time.monotonic()

    # ------------------------------------------------------------------
    # worker 调用
    # ------------------------------------------------------------------
    def _call_worker(
        self, op: str, args: dict[str, Any], account_id: str, timeout_s: float
    ) -> tuple[int, dict[str, Any]]:
        config_dir = self.upstream_config_dir(account_id)
        payload = {
            "upstream_path": str(self.upstream_path),
            "config_dir": str(config_dir),
            "op": op,
            "args": args,
        }
        proc = subprocess.run(  # noqa: S603 —— 参数全部内置，无用户可注入 shell
            [self.python_exe, str(self.worker_path)],
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout_s,
        )
        stdout = (proc.stdout or "").strip()
        try:
            parsed = json.loads(stdout.splitlines()[-1]) if stdout else {}
        except (json.JSONDecodeError, IndexError):
            parsed = {}
        return proc.returncode, parsed

    @staticmethod
    def _error_from_exit(
        code: int, parsed: dict[str, Any], op: str
    ) -> AdapterError:
        message = str(parsed.get("message") or f"worker 退出码 {code}（op={op}）")
        hint = str(parsed.get("hint") or "")
        detail = f"{message}" + (f"（{hint}）" if hint else "")
        if code == EXIT_AUTH:
            return AdapterError(
                code=Codes.SESSION_INVALID,
                category=ErrorCategory.AUTH,
                message=detail,
            )
        if code == EXIT_PLATFORM:
            return AdapterError(
                code=Codes.ADAPTER_ERROR,
                category=ErrorCategory.PLATFORM_CHANGED,
                message=detail,
            )
        if code == EXIT_DEPS:
            return AdapterError(
                code=Codes.INTERNAL_ERROR,
                category=ErrorCategory.INTERNAL,
                message=detail,
            )
        return AdapterError(
            code=Codes.INTERNAL_ERROR,
            category=ErrorCategory.INTERNAL,
            message=detail,
        )

    @staticmethod
    def _args_for(op: str, params: dict[str, Any]) -> dict[str, Any]:
        if op == "fetch_schedule":
            return {"week": params.get("week")}
        if op == "fetch_notices":
            return {
                "keyword": params.get("keyword"),
                "unread_only": bool(params.get("unread_only")),
                "limit": params.get("limit") or 20,
            }
        if op in ("fetch_courses", "fetch_progress_overview", "fetch_exams"):
            return {}
        # C07 get_course：带 course_id 时解析课程元数据，不带则退化为 profile
        if op == "fetch_profile":
            return {"course_id": params.get("course_id")}
        return {"course_id": params.get("course_id")}

    # ------------------------------------------------------------------
    # 规范化：上游原始形状 → 统一层数据契约（与 mock/fixtures 对齐）
    # 只做字段映射与解析上游"已公开"的输出，不复制上游业务逻辑（R3）。
    # ------------------------------------------------------------------
    @staticmethod
    def _norm_course(raw: dict[str, Any]) -> dict[str, Any]:
        return {
            "course_id": str(raw.get("courseid") or ""),
            "clazz_id": str(raw.get("clazzid") or ""),
            "cpi": str(raw.get("cpi") or ""),
            "name": str(raw.get("name") or "?"),
            "teacher": str(raw.get("teacher") or ""),
            "fid": str(raw.get("fid") or ""),
        }

    @staticmethod
    def _norm_chapters(data: dict[str, Any]) -> dict[str, Any]:
        import re as _re

        progress = data.get("progress")
        rows: list[dict[str, Any]] = []
        parent = ""
        for level, text in data.get("nodes") or []:
            stripped = str(text).strip()
            if level == 0:
                num, _, title = stripped.partition(". ")
                rows.append({
                    "chapter_id": f"ch_{num}", "name": title or stripped,
                    "index": num, "parent_id": "", "status_text": "",
                })
                parent = f"ch_{num}"
            else:
                head, _, status = stripped.partition("|")
                m = _re.match(r"^([\d.]+)\s+(.*)$", head.strip())
                num, title = (m.group(1), m.group(2)) if m else ("", head.strip())
                rows.append({
                    "chapter_id": f"ch_{num}" if num else head.strip(),
                    "name": title, "index": num, "parent_id": parent,
                    "status_text": status.strip(),
                })
        return {
            "progress": (
                {"done": progress[0], "total": progress[1]} if progress else None
            ),
            "chapters": rows,
        }

    @staticmethod
    def _norm_progress(rows: list[dict[str, Any]]) -> dict[str, Any]:
        done = sum(int(r["done"]) for r in rows if r.get("done") is not None)
        total = sum(int(r["total"]) for r in rows if r.get("total") is not None)
        return {
            "overall": {
                "done": done,
                "total": total,
                "ratio": (done / total) if total else 0.0,
            },
            "courses": [
                {
                    "name": r.get("course"),
                    "done": r.get("done"),
                    "total": r.get("total"),
                    "error": r.get("error"),
                }
                for r in rows
            ],
        }

    _PENDING_MARKS = ("未", "待")

    @classmethod
    def _norm_homework(cls, data: dict[str, Any], course_label: str) -> dict[str, Any]:
        items = []
        for it in data.get("items") or []:
            status = str(it.get("status") or "")
            submitted = not any(mark in status for mark in cls._PENDING_MARKS)
            items.append({
                "course_id": course_label,
                "index": it.get("index"),
                "title": it.get("title"),
                "submitted": submitted,
                "progress": status,
                "due_at": None,
                "score": None,
            })
        deadlines = sorted(
            (i for i in items if not i["submitted"]),
            key=lambda i: str(i["index"]),
        )
        return {"homework": items, "deadlines": deadlines}

    @staticmethod
    def _norm_deadlines(rows: list[dict[str, Any]]) -> dict[str, Any]:
        deadlines = [
            {
                "course_id": r.get("course"),
                "index": "",
                "title": r.get("title"),
                "submitted": False,
                "progress": r.get("status"),
                "due_at": r.get("deadline"),
                "score": None,
            }
            for r in rows
        ]
        return {"homework": deadlines, "deadlines": deadlines}

    @staticmethod
    def _norm_notices(items: list[dict[str, Any]]) -> dict[str, Any]:
        rows = []
        for it in items:
            rows.append({
                "title": it.get("title"),
                "content": it.get("content"),
                "sender": it.get("senderUserName") or it.get("sender") or "",
                "time": it.get("createDate") or it.get("time") or "",
                "unread": not bool(it.get("isread")),
            })
        return {"notices": rows}

    @staticmethod
    def _norm_schedule(data: dict[str, Any], week: Any) -> dict[str, Any]:
        curriculum = data.get("curriculum") or {}
        # firstWeekDate / firstWeekDateReal 都可能是毫秒时间戳
        from datetime import datetime, timezone, timedelta

        def _ms_to_date(value: Any) -> str:
            try:
                if value and int(value) > 10**11:  # 毫秒级时间戳才转
                    dt = datetime.fromtimestamp(
                        int(value) / 1000, tz=timezone(timedelta(hours=8))
                    )
                    return dt.strftime("%Y-%m-%d")
            except (TypeError, ValueError, OSError):
                pass
            return str(value or "")

        first_week = _ms_to_date(curriculum.get("firstWeekDate")) or _ms_to_date(
            curriculum.get("firstWeekDateReal")
        )
        lessons = []
        for les in data.get("lessons") or []:
            lessons.append({
                "name": les.get("courseName") or les.get("name") or "",
                "weekday": les.get("dayOfWeek") or les.get("weekday"),
                "date": les.get("day") or les.get("date") or "",
                "section": les.get("sections") or les.get("section") or "",
                "location": les.get("classroom") or les.get("location") or "",
                "teacher": les.get("teacherName") or les.get("teacher") or "",
            })
        return {
            "week": week,
            "first_week_date": first_week,
            "lessons": lessons,
        }

    def _normalize(self, capability_id: str, op: str, data: Any,
                   params: dict[str, Any]) -> Any:
        if capability_id == "C06":
            return {"courses": [self._norm_course(c) for c in data or []]}
        if capability_id == "C07":
            if op == "fetch_course_meta":
                return {"course": self._norm_course(data or {})}
            return {"profile": data}
        if capability_id == "C08":
            return self._norm_chapters(data or {})
        if capability_id in ("C10", "C11"):
            return self._norm_progress(data or [])
        if capability_id == "C29":
            if op == "fetch_homework":
                return self._norm_homework(
                    data or {}, str(params.get("course_id") or "")
                )
            return self._norm_deadlines(data or [])
        if capability_id == "C30":
            return {"exams": data or []}
        if capability_id == "C31":
            return self._norm_notices(data or [])
        if capability_id == "C33":
            return self._norm_schedule(data or {}, params.get("week"))
        return data

    # ------------------------------------------------------------------
    # Adapter 契约
    # ------------------------------------------------------------------
    def supports(self, capability_id: str) -> bool:
        if not self.manifest.enabled:
            return False
        if capability_id in self.manifest.capabilities:
            return True
        # C07 在 manifest 里对应 fetch_profile；course_id 分支同能力承载
        return False

    def invoke(
        self, capability_id: str, params: dict[str, Any], ctx: TaskContext
    ) -> AdapterResult:
        if capability_id not in _OP_MAP and capability_id != "C07":
            return self.unsupported(capability_id)

        # C07 语义分叉：带 course_id → 课程元数据；不带 → 个人资料。
        if capability_id == "C07":
            op = "fetch_course_meta" if params.get("course_id") else "fetch_profile"
            args = {"course_id": params.get("course_id")}
        # C29 语义分叉：带 course_id → 该课作业列表；不带 → 全部课程截止总览。
        # （此前恒走 overview，于是 `get_homework --course-id X` 会返回所有课程，
        #   course_id 形同虚设 —— 实跑时踩到，读者会以为过滤生效了）
        elif capability_id == "C29" and params.get("course_id"):
            op = "fetch_homework"
            args = {"course_id": params.get("course_id")}
        else:
            op = _OP_MAP[capability_id]
            args = self._args_for(op, params)

        timeout_s = _OP_TIMEOUT_S.get(capability_id, 60.0)
        try:
            self.ensure_bridge(ctx.account_id)
        except FileNotFoundError as exc:
            return self.failure(
                AdapterError(
                    code=Codes.SESSION_INVALID,
                    category=ErrorCategory.AUTH,
                    message=str(exc),
                )
            )
        self._throttle()
        try:
            code, parsed = self._call_worker(op, args, ctx.account_id, timeout_s)
        except subprocess.TimeoutExpired:
            return self.failure(
                AdapterError(
                    code=Codes.ADAPTER_TIMEOUT,
                    category=ErrorCategory.TRANSIENT,
                    message=f"上游调用超时（>{timeout_s:.0f}s，op={op}）",
                )
            )
        except FileNotFoundError as exc:
            return self.failure(
                AdapterError(
                    code=Codes.INTERNAL_ERROR,
                    category=ErrorCategory.INTERNAL,
                    message=f"worker 或解释器不可用：{exc}",
                )
            )

        if code != EXIT_OK:
            return self.failure(self._error_from_exit(code, parsed, op))
        if not parsed.get("ok"):
            return self.failure(
                AdapterError(
                    code=Codes.INTERNAL_ERROR,
                    category=ErrorCategory.INTERNAL,
                    message=f"worker 返回异常：{parsed}",
                )
            )
        return self.success(self._normalize(capability_id, op, parsed.get("data"), params))

    # ------------------------------------------------------------------
    def probe(self) -> ProbeResult:
        """轻量探活：worker ping（无网络副作用）。真实连通性由 cookies_verify 承担。"""
        if not self.manifest.enabled:
            return ProbeResult(healthy=False, detail="manifest.enabled = false")
        try:
            code, parsed = self._call_worker("ping", {}, "__probe__", 15.0)
        except Exception as exc:  # noqa: BLE001
            return ProbeResult(healthy=False, detail=f"worker 不可用：{exc}")
        if code == EXIT_OK and parsed.get("ok"):
            return ProbeResult(
                healthy=True, detail=f"worker 就绪（upstream={self.upstream_path.name}）"
            )
        return ProbeResult(
            healthy=False,
            detail=str(parsed.get("message") or f"退出码 {code}（{EXIT_DEPS}=缺依赖）"),
        )
