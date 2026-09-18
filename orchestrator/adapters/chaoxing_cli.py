"""Samueli924/chaoxing Adapter（写侧主力，GPL-3.0，**仅 subprocess**）。

隔离方式（R1）：统一层通过 `cxcli_worker.py` 子进程使用上游，GPL 代码
不进统一层进程。cookie 走文件边界：统一层 cookies.json → 上游的
`cookies.txt`（header 格式 `k=v;k=v`，上游从 CWD 相对路径读取，所以
worker 的 cwd 固定在账号工作区的 `upstream/chaoxing/`）。

职责边界：`invoke` 只把 worker 退出码/结果映射成 ErrorCategory；fallback
由 Router 决定。**risk_control（FORBIDDEN）不 fallback、不重试** —— 风控下
继续操作会加重风控。

安全门禁：quiz（章节检测）任务点默认跳过（`allow_work=false`），自动答题
在 M5 接入 Agent 答题链路后才启用。
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from ..errors import AdapterError, Codes, ErrorCategory
from ..models import AdapterResult, ProbeResult, TaskContext
from ..registry import Manifest
from ..structured_log import Event
from .base import Adapter
from .cxcli_worker import EXIT_AUTH, EXIT_DEPS, EXIT_INTERNAL, EXIT_OK

#: manifest 里没有列、但参数上属于该命令的能力暂不支持
_INSTALL_HINT = (
    "python -m pip install requests pyaes beautifulsoup4 lxml loguru tqdm "
    "openai fonttools tenacity chardet httpx ddddocr"
)


class ChaoxingCliAdapter(Adapter):
    def __init__(
        self,
        manifest: Manifest,
        worker_path: str | Path | None = None,
        python_exe: str | None = None,
        upstream_path: str | Path | None = None,
        accounts_dir: str | Path | None = None,
        min_interval_ms: int = 0,
    ) -> None:
        super().__init__(manifest)
        root = Path(__file__).resolve().parents[2]
        self.root = root
        self.accounts_dir = Path(accounts_dir or (root / "accounts"))
        self.worker_path = Path(worker_path or (root / "orchestrator" / "adapters" / "cxcli_worker.py"))
        self.upstream_path = Path(upstream_path or (root / "upstreams" / "chaoxing"))
        self.python_exe = Path(python_exe or self._default_python(root))
        self.min_interval_s = max(min_interval_ms, 0) / 1000.0
        self._last_invoke_at = 0.0

    @staticmethod
    def _default_python(root: Path) -> str:
        """与 XuexitongMcpAdapter 相同的解析顺序。"""
        import os
        import sys

        env = os.environ.get("CXCLI_PYTHON")
        if env:
            return env
        venv_python = root / ".venv" / (
            "Scripts/python.exe" if os.name == "nt" else "bin/python"
        )
        if venv_python.is_file():
            return str(venv_python)
        return sys.executable

    # ------------------------------------------------------------------
    def config_dir(self, account_id: str) -> Path:
        return self.accounts_dir / account_id / "upstream" / "chaoxing"

    def ensure_bridge(self, account_id: str) -> Path:
        """cookies.json → 上游 cookies.txt（header 格式，幂等，mtime 判断）。"""
        src = self.accounts_dir / account_id / "cookies.json"
        if not src.is_file():
            raise FileNotFoundError(
                f"账号 {account_id} 还没有 cookie：{src} 不存在。"
                "请先 cookies_login / cookies_import。"
            )
        target = self.config_dir(account_id) / "cookies.txt"
        if target.is_file() and target.stat().st_mtime >= src.stat().st_mtime:
            return target

        raw = json.loads(src.read_text(encoding="utf-8"))
        items = raw.get("cookies") if isinstance(raw, dict) else raw
        pairs: dict[str, str] = {}
        for item in items or []:
            name = str(item.get("name") or "")
            value = str(item.get("value") or "")
            if name:
                pairs[name] = value
        if not pairs:
            raise FileNotFoundError(f"{src} 里没有可用的 cookie")

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            ";".join(f"{k}={v}" for k, v in pairs.items()), encoding="utf-8"
        )
        return target

    def setup(self, account) -> None:
        self.ensure_bridge(account.account_id)

    # ------------------------------------------------------------------
    def _base_payload(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        return {
            "upstream_path": str(self.upstream_path),
            "op": op,
            "args": args,
        }

    # ------------------------------------------------------------------
    def _call_short(
        self, op: str, args: dict[str, Any], account_id: str, timeout_s: float,
        needs_bridge: bool = True,
    ) -> tuple[int, dict[str, Any]]:
        """短任务（ping / list_points / scan_tasks）。ping 不需要 cookie，
        但 cwd（config_dir）必须存在 —— worker 的 CWD 就是上游读 cookies.txt 的位置。"""
        if needs_bridge:
            self.ensure_bridge(account_id)
        else:
            self.config_dir(account_id).mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(  # noqa: S603
            [str(self.python_exe), str(self.worker_path)],
            input=json.dumps(self._base_payload(op, args), ensure_ascii=False) + "\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(self.config_dir(account_id)),
            timeout=timeout_s,
        )
        stdout = (proc.stdout or "").strip()
        try:
            parsed = json.loads(stdout.splitlines()[-1]) if stdout else {}
        except (json.JSONDecodeError, IndexError):
            parsed = {}
        return proc.returncode, parsed

    def _call_stream(
        self, op: str, args: dict[str, Any], ctx: TaskContext, account_id: str
    ) -> tuple[int, dict[str, Any], list[str]]:
        """长任务（run）：流式读事件 → ctx.report；取消/暂停在任务点边界生效。"""
        self.ensure_bridge(account_id)
        # 上游的 loguru/tqdm/验证码提示全走 stderr——必须落盘，
        # 否则任务卡住时没有任何诊断依据（实测教训：视频上报卡死半小时无迹可寻）
        err_log = self.root / "runs" / f"{ctx.request_id}.worker.err.log"
        err_log.parent.mkdir(parents=True, exist_ok=True)
        err_fh = open(err_log, "w", encoding="utf-8", errors="replace")
        # 上游用 loguru 往 stderr 打中文：必须显式指定 UTF-8，
        # 否则 Windows 控制台代码页（GBK）会让日志变成乱码（实测）
        child_env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        proc = subprocess.Popen(  # noqa: S603
            [str(self.python_exe), str(self.worker_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=err_fh,
            text=True,
            encoding="utf-8",
            cwd=str(self.config_dir(account_id)),
            env=child_env,
        )
        err_fh.close()  # Popen 持有句柄，父进程侧可关
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(json.dumps(self._base_payload(op, args), ensure_ascii=False) + "\n")
        proc.stdin.flush()

        signalled = False
        events: list[str] = []
        final: dict[str, Any] = {}

        def _signal(word: str) -> None:
            nonlocal signalled
            if not signalled:
                signalled = True
                try:
                    proc.stdin.write(word + "\n")
                    proc.stdin.flush()
                except OSError:
                    pass

        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                events.append(line)
                continue
            if "event" in payload:
                events.append(line)
                self._forward_event(payload, ctx)
                # 取消/暂停请求在下一个任务点边界生效（协作式）
                if ctx.cancelled:
                    _signal("cancel")
                elif ctx.pause_requested:
                    _signal("pause")
            elif "ok" in payload:
                final = payload
        proc.wait()
        try:
            proc.stdin.close()
            proc.stdout.close()
        except OSError:
            pass
        return proc.returncode, final, events

    @staticmethod
    def _forward_event(payload: dict[str, Any], ctx: TaskContext) -> None:
        event = payload["event"]
        if event == "job_start":
            ctx.report(
                Event.TASK_POINT_STARTED,
                task_point_id=str(payload.get("task_point_id") or ""),
                task_type=str(payload.get("type") or ""),
            )
        elif event == "job_done":
            failed = payload.get("result") != "SUCCESS"
            ctx.report(
                Event.TASK_POINT_FAILED if failed else Event.TASK_POINT_COMPLETED,
                task_point_id=str(payload.get("task_point_id") or ""),
                task_type=str(payload.get("type") or ""),
                **({"error": {"code": payload.get("result")}}
                   if failed else {}),
            )
        elif event in ("job_skipped", "chapter_skipped"):
            ctx.report(
                Event.TASK_POINT_SKIPPED,
                task_point_id=str(payload.get("task_point_id") or payload.get("chapter_id") or ""),
                task_type=str(payload.get("type") or ""),
                reason=str(payload.get("reason") or ""),
            )

    # ------------------------------------------------------------------
    @staticmethod
    def _error_from_exit(code: int, parsed: dict[str, Any], op: str) -> AdapterError:
        message = str(parsed.get("message") or f"worker 退出码 {code}（op={op}）")
        hint = str(parsed.get("hint") or "")
        detail = message + (f"（{hint}）" if hint else "")
        if code == EXIT_AUTH:
            return AdapterError(
                code=Codes.SESSION_INVALID,
                category=ErrorCategory.AUTH,
                message=detail,
            )
        if code == EXIT_DEPS:
            return AdapterError(
                code=Codes.INTERNAL_ERROR,
                category=ErrorCategory.INTERNAL,
                message=detail,
            )
        if parsed.get("code") == "COURSE_NOT_FOUND":
            return AdapterError(
                code=Codes.COURSE_NOT_FOUND,
                category=ErrorCategory.INPUT,
                message=detail,
            )
        return AdapterError(
            code=Codes.INTERNAL_ERROR,
            category=ErrorCategory.INTERNAL,
            message=detail,
        )

    # ------------------------------------------------------------------
    def invoke(
        self, capability_id: str, params: dict[str, Any], ctx: TaskContext
    ) -> AdapterResult:
        op_map = {
            "C09": "scan_tasks",
            "C12": "run",
            "C14": "run",
            "C18": "run",
            # M6 签到：C50 发现、C48 执行（C49 类型覆盖是声明，无独立 op）
            "C50": "sign_scan",
            "C48": "sign_execute",
        }
        op = op_map.get(capability_id)
        if op is None:
            return self.unsupported(capability_id)

        args: dict[str, Any] = {
            "course_id": str(params.get("course_id") or ""),
            "speed": float(params.get("speed") or 1.0),
            "skip_task_point_ids": params.get("skip_task_point_ids") or [],
            "target_types": params.get("target_types") or [],
            "dry_run": bool(params.get("dry_run")),
        }
        if op == "run":
            args["chapter_id"] = str(params.get("chapter_id") or "")
            # quiz（章节检测）默认跳过；显式开启时才注入答题链路。
            # 开启 allow_work 必须同时注入 tiku，否则上游 study_work 无题可答。
            args["allow_work"] = bool(params.get("allow_work"))
            args["tiku_enabled"] = bool(params.get("allow_work"))
            args["shim_endpoint"] = str(
                params.get("shim_endpoint") or "http://127.0.0.1:8765/v1"
            )
            # 默认不交卷（安全默认）：答完只在平台侧保存，由使用者决定是否正式提交
            args["tiku_submit"] = "true" if params.get("submit_answers") else "false"

        if op == "sign_scan":
            # CLI 给的是 `--all`（要看已结束的）；能力声明里是 only_running
            only_running = params.get("only_running")
            if only_running is None:
                only_running = not bool(params.get("all"))
            args["only_running"] = bool(only_running)

        if op == "sign_execute":
            args["activity_id"] = str(params.get("activity_id") or "")
            # 参数名兼容：CLI/服务层历史上用 `type`，能力声明里写的是 sign_type
            args["sign_type"] = str(
                params.get("sign_type") or params.get("type") or "normal"
            )
            args["obj_id"] = str(params.get("obj_id") or "aaa")
            args["lat"] = params.get("lat")
            args["lon"] = params.get("lon")

        self._throttle()

        if op in ("scan_tasks", "sign_scan", "sign_execute"):
            try:
                code, parsed = self._call_short(op, args, ctx.account_id, 180.0)
            except subprocess.TimeoutExpired:
                return self._timeout_result(op, 180.0)
            except FileNotFoundError as exc:
                return self._worker_missing_result(exc)
            if code != EXIT_OK:
                return self.failure(self._error_from_exit(code, parsed, op))
            data = parsed.get("data") or {}
            if op == "scan_tasks":
                return self.success(self._normalize_scan(data))
            if op == "sign_scan":
                return self.success(self._normalize_sign_scan(data))
            return self.success(self._normalize_sign_result(data))

        # ---- run：长任务流式 ----
        try:
            code, final, _events = self._call_stream(op, args, ctx, ctx.account_id)
        except FileNotFoundError as exc:
            return self._worker_missing_result(exc)
        if code != EXIT_OK or not final.get("ok"):
            if final:
                return self.failure(self._error_from_exit(code, final, op))
            return self.failure(
                AdapterError(
                    code=Codes.ADAPTER_CRASHED,
                    category=ErrorCategory.INTERNAL,
                    message=f"worker 异常退出（退出码 {code}，op={op}）",
                )
            )

        data = final.get("data") or {}
        if data.get("risk_control"):
            return self.failure(
                AdapterError(
                    code=Codes.RISK_CONTROL_SUSPECTED,
                    category=ErrorCategory.RISK_CONTROL,
                    message=(
                        "上游返回 FORBIDDEN（403），疑似触发风控。已停止执行；"
                        "同账号不重试、不 fallback。"
                    ),
                    extra={"completed": data.get("completed"),
                           "skipped": data.get("skipped")},
                )
            )
        return self.success(data)

    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_scan(data: dict[str, Any]) -> dict[str, Any]:
        points = data.get("task_points") or []
        by_type: dict[str, int] = {}
        by_status: dict[str, int] = {}
        for row in points:
            by_type[row["type"]] = by_type.get(row["type"], 0) + 1
            by_status[row["status"]] = by_status.get(row["status"], 0) + 1
        return {
            "task_points": points,
            "summary": {
                "total": len(points),
                "by_type": by_type,
                "by_status": by_status,
            },
        }

    @staticmethod
    def _normalize_sign_scan(data: dict[str, Any]) -> dict[str, Any]:
        """签到活动发现（C50）。

        保留 `raw`（平台原始活动 dict）：签到子类型（普通/手势/位置/二维码/拍照）
        在平台侧没有稳定字段，实测前不臆断，把原始数据一并交给使用者与编排层。
        """
        activities = list(data.get("activities") or [])
        return {
            "activities": activities,
            "count": len(activities),
            "scanned_courses": data.get("scanned_courses"),
            "only_running": data.get("only_running"),
            "errors": data.get("errors") or [],
        }

    @staticmethod
    def _normalize_sign_result(data: dict[str, Any]) -> dict[str, Any]:
        """签到执行结果（C48）。

        契约对齐 mock 的 C48（`{"sign_type", "status", "activity"}`）。
        平台 `stuSignajax` 只回一段文本，这里只做**关键词归一**，不做业务判断；
        判定不了就如实给 unknown，原文一律保留在 `response`。

        实测到的原文（2026-09-18）：
        - "您已签到过了"          → duplicate（已签过，重复签到）
        - "签到失败，请重新扫描"    → failed（该活动需要扫码/手势等其它方式）
        尚未观察到首次成功时的文案（现有靶子一个已签过、一个需扫码），
        故 "签到成功" → success 这条是**按文案惯例**写的，未实测。
        """
        text = str(data.get("response") or "")
        if "成功" in text:
            outcome, status = "success", "signed"
        elif "已签到" in text or "已经签到" in text:
            outcome, status = "duplicate", "signed"
        elif any(word in text for word in ("失败", "错误", "无效", "过期", "未开始", "不能")):
            outcome, status = "failed", "failed"
        else:
            outcome, status = "unknown", "unknown"
        return {**data, "outcome": outcome, "status": status}

    def _timeout_result(self, op: str, timeout_s: float) -> AdapterResult:
        return self.failure(
            AdapterError(
                code=Codes.ADAPTER_TIMEOUT,
                category=ErrorCategory.TRANSIENT,
                message=f"上游调用超时（>{timeout_s:.0f}s，op={op}）",
            )
        )

    def _worker_missing_result(self, exc: FileNotFoundError) -> AdapterResult:
        return self.failure(
            AdapterError(
                code=Codes.INTERNAL_ERROR,
                category=ErrorCategory.INTERNAL,
                message=f"worker 或解释器不可用：{exc}（{_INSTALL_HINT}）",
            )
        )

    def _throttle(self) -> None:
        if self.min_interval_s <= 0:
            return
        wait = self._last_invoke_at + self.min_interval_s - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_invoke_at = time.monotonic()

    # ------------------------------------------------------------------
    def probe(self) -> ProbeResult:
        if not self.manifest.enabled:
            return ProbeResult(healthy=False, detail="manifest.enabled = false")
        try:
            code, parsed = self._call_short(
                "ping", {}, "__probe__", 20.0, needs_bridge=False
            )
        except Exception as exc:  # noqa: BLE001
            return ProbeResult(healthy=False, detail=f"worker 不可用：{exc}")
        if code == EXIT_OK and parsed.get("ok"):
            return ProbeResult(healthy=True, detail="worker 就绪（upstream=chaoxing）")
        return ProbeResult(
            healthy=False,
            detail=str(parsed.get("message") or f"退出码 {code}"),
        )
