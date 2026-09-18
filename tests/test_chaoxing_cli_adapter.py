"""ChaoxingCliAdapter 单元测试（假 worker，不触网、不跑真实任务）。"""

from __future__ import annotations

import json
import os
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path

from orchestrator.adapters.chaoxing_cli import ChaoxingCliAdapter
from orchestrator.errors import ErrorCategory
from orchestrator.models import TaskContext
from orchestrator.registry import Manifest, load_manifests
from orchestrator.structured_log import Event

ROOT = Path(__file__).resolve().parents[1]


def _manifest() -> Manifest:
    for m in load_manifests():
        if m.id == "chaoxing-cli":
            return m
    raise AssertionError("chaoxing_cli.json manifest 缺失")


#: 测试专用 worker：按 op / FAKE_MODE 返回固定事件流与 summary
FAKE_WORKER = textwrap.dedent(
    """
    import json, sys, os
    req = json.loads(sys.stdin.readline())
    cap = os.environ.get("FAKE_CAPTURE")
    if cap:
        open(cap, "w", encoding="utf-8").write(json.dumps(req, ensure_ascii=False))
    op = req["op"]
    mode = os.environ.get("FAKE_MODE", "ok")

    if op == "ping":
        print(json.dumps({"ok": True, "data": {}}))
        sys.exit(0)

    if mode == "auth":
        print(json.dumps({"ok": False, "code": "SESSION_INVALID",
                          "message": "cookie 登录失败"}))
        sys.exit(3)

    if mode == "notfound":
        print(json.dumps({"ok": False, "code": "COURSE_NOT_FOUND",
                          "message": "课程不存在"}))
        sys.exit(1)

    # ---- run：事件流 ----
    import threading
    got_cancel = {"v": False}

    def _listen() -> None:
        for line in sys.stdin:
            if line.strip() == "cancel":
                got_cancel["v"] = True
                return

    if op == "run":
        threading.Thread(target=_listen, daemon=True).start()
        print(json.dumps({"event": "job_start", "task_point_id": "j1", "type": "video"}), flush=True)
        print(json.dumps({"event": "job_done", "task_point_id": "j1",
                          "type": "video", "result": "SUCCESS"}), flush=True)
        print(json.dumps({"event": "job_skipped", "task_point_id": "j2",
                          "type": "quiz", "reason": "quiz_disabled"}), flush=True)
        import time
        time.sleep(0.6)  # 给控制行留到达时间（仅测试 worker 需要）

    summary = {"completed": ["j1"], "failed": [], "skipped": [
        {"id": "j2", "type": "quiz", "reason": "quiz_disabled"}],
        "stopped_reason": "finished", "dry_run": False}
    if mode == "risk":
        summary["risk_control"] = True
        summary["failed"] = [{"id": "j3", "type": "video", "result": "FORBIDDEN"}]
    if mode == "cancel":
        summary["stopped_reason"] = "cancelled"
    summary["got_cancel"] = got_cancel["v"]
    print(json.dumps({"ok": True, "data": summary}))
    sys.exit(0)
    """
)


class QuizRecheckTests(unittest.TestCase):
    """M5 假失败兜底：quiz 报错后复扫平台，任务点消失即视为已完成。"""

    class _Client:
        def __init__(self, jobs=None, boom=False):
            self._jobs = jobs or []
            self._boom = boom

        def get_job_list(self, course, point):
            if self._boom:
                raise RuntimeError("网络抖动")
            return self._jobs, {}

    def setUp(self) -> None:
        from orchestrator.adapters.cxcli_worker import job_still_pending

        self.fn = job_still_pending
        self.course = {"courseId": "1"}
        self.point = {"id": "ch_1"}

    def test_job_absent_means_finished(self) -> None:
        client = self._Client(jobs=[{"jobid": "other"}])
        self.assertFalse(self.fn(client, self.course, self.point, "work-abc"))

    def test_job_present_means_still_pending(self) -> None:
        client = self._Client(jobs=[{"jobid": "work-abc"}])
        self.assertTrue(self.fn(client, self.course, self.point, "work-abc"))

    def test_query_failure_is_conservative(self) -> None:
        client = self._Client(boom=True)
        self.assertTrue(self.fn(client, self.course, self.point, "work-abc"))


class ChaoxingCliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.worker = self.base / "fake_worker.py"
        self.worker.write_text(FAKE_WORKER, encoding="utf-8")
        self.accounts = self.base / "accounts" / "acc_01"
        self.accounts.mkdir(parents=True)
        (self.accounts / "cookies.json").write_text(
            json.dumps({"cookies": [
                {"name": "UID", "value": "v1", "domain": ".chaoxing.com"},
                {"name": "_uid", "value": "v2", "domain": ""},
            ]}),
            encoding="utf-8",
        )
        self.adapter = ChaoxingCliAdapter(
            _manifest(),
            worker_path=self.worker,
            accounts_dir=self.base / "accounts",
            min_interval_ms=0,
        )
        self.ctx = TaskContext(request_id="t", account_id="acc_01")

    # ------------------------------------------------------------------
    def test_probe_healthy(self) -> None:
        self.assertTrue(self.adapter.probe().healthy)

    def test_bridge_writes_header_format(self) -> None:
        target = self.adapter.ensure_bridge("acc_01")
        self.assertEqual(target.name, "cookies.txt")
        text = target.read_text(encoding="utf-8")
        # A1 的格式是 k=v;k=v（分号分隔 header），不是 Netscape
        self.assertEqual(text, "UID=v1;_uid=v2")

    def test_scan_normalizes_summary(self) -> None:
        # 假 worker 没实现 scan_tasks…… 走 run 分支也返回 data；
        # 这里用 run 数据喂 normalize 等价路径，直接测 _normalize_scan。
        data = self.adapter._normalize_scan({
            "task_points": [
                {"type": "video", "status": "todo"},
                {"type": "video", "status": "todo"},
                {"type": "document", "status": "locked"},
            ]
        })
        self.assertEqual(data["summary"]["total"], 3)
        self.assertEqual(data["summary"]["by_type"], {"video": 2, "document": 1})
        self.assertEqual(data["summary"]["by_status"], {"todo": 2, "locked": 1})

    def test_run_reports_task_point_events(self) -> None:
        seen: list[tuple[str, dict]] = []
        self.ctx.progress_sink = lambda event, fields: seen.append((event, fields))
        result = self.adapter.invoke("C18", {"course_id": "123"}, self.ctx)
        self.assertTrue(result.ok, result.error)
        kinds = [e for e, _ in seen]
        self.assertIn(Event.TASK_POINT_STARTED, kinds)
        self.assertIn(Event.TASK_POINT_COMPLETED, kinds)
        self.assertIn(Event.TASK_POINT_SKIPPED, kinds)
        # quiz 跳过原因必须透传
        skipped = [f for e, f in seen if e == Event.TASK_POINT_SKIPPED]
        self.assertEqual(skipped[0]["reason"], "quiz_disabled")

    def test_run_returns_mock_compatible_contract(self) -> None:
        result = self.adapter.invoke("C12", {"course_id": "123", "speed": 1.5}, self.ctx)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["completed"], ["j1"])
        self.assertEqual(result.data["stopped_reason"], "finished")

    def test_risk_control_does_not_succeed(self) -> None:
        os.environ["FAKE_MODE"] = "risk"
        try:
            result = self.adapter.invoke("C18", {"course_id": "123"}, self.ctx)
        finally:
            os.environ.pop("FAKE_MODE", None)
        self.assertFalse(result.ok)
        self.assertEqual(result.error.category, ErrorCategory.RISK_CONTROL)

    def test_cancel_reaches_worker_stdin(self) -> None:
        """ctx 取消 → Adapter 必须向 worker stdin 写 cancel（任务点边界生效）。"""
        self.ctx.cancel_event.set()
        os.environ["FAKE_MODE"] = "cancel"
        try:
            result = self.adapter.invoke("C18", {"course_id": "123"}, self.ctx)
        finally:
            os.environ.pop("FAKE_MODE", None)
        self.assertTrue(result.ok)
        self.assertTrue(result.data.get("got_cancel"))
        self.assertEqual(result.data["stopped_reason"], "cancelled")

    def test_exit_3_maps_to_auth(self) -> None:
        os.environ["FAKE_MODE"] = "auth"
        try:
            result = self.adapter.invoke("C09", {"course_id": "123"}, self.ctx)
        finally:
            os.environ.pop("FAKE_MODE", None)
        self.assertFalse(result.ok)
        self.assertEqual(result.error.category, ErrorCategory.AUTH)

    def test_course_not_found_maps_to_input(self) -> None:
        os.environ["FAKE_MODE"] = "notfound"
        try:
            result = self.adapter.invoke("C18", {"course_id": "999"}, self.ctx)
        finally:
            os.environ.pop("FAKE_MODE", None)
        self.assertFalse(result.ok)
        self.assertEqual(result.error.category, ErrorCategory.INPUT)

    def test_unsupported_capability(self) -> None:
        self.assertFalse(self.adapter.invoke("C06", {}, self.ctx).ok)

    # ------------------------------------------------------------------
    def _capture_invoke(self, params: dict) -> dict:
        """跑一次 invoke，返回假 worker 实际收到的请求体。"""
        cap = self.base / "captured.json"
        os.environ["FAKE_CAPTURE"] = str(cap)
        try:
            result = self.adapter.invoke("C18", {"course_id": "123", **params}, self.ctx)
            self.assertTrue(result.ok, result.error)
        finally:
            os.environ.pop("FAKE_CAPTURE", None)
        return json.loads(cap.read_text(encoding="utf-8"))

    def test_allow_work_enables_tiku_and_shim(self) -> None:
        """M5 接线：--allow-work 必须同时打开 tiku 并给出 shim 地址。"""
        captured = self._capture_invoke({"allow_work": True})
        args = captured["args"]
        self.assertTrue(args["allow_work"])
        self.assertTrue(args["tiku_enabled"])
        self.assertEqual(args["shim_endpoint"], "http://127.0.0.1:8765/v1")
        # 安全默认：不交卷
        self.assertEqual(args["tiku_submit"], "false")

    def test_allow_work_off_by_default(self) -> None:
        args = self._capture_invoke({})["args"]
        self.assertFalse(args["allow_work"])
        self.assertFalse(args["tiku_enabled"])

    def test_submit_answers_flag_propagates(self) -> None:
        args = self._capture_invoke({"allow_work": True, "submit_answers": True})["args"]
        self.assertEqual(args["tiku_submit"], "true")

    def test_shim_endpoint_override(self) -> None:
        args = self._capture_invoke(
            {"allow_work": True, "shim_endpoint": "http://127.0.0.1:9999/v1"}
        )["args"]
        self.assertEqual(args["shim_endpoint"], "http://127.0.0.1:9999/v1")

    # ------------------------------------------------------------------
    def test_build_tiku_against_real_upstream(self) -> None:
        """用真实上游构造 AI provider —— 证明 M5 接线在上游侧成立。

        需要 .venv（含 openai 等依赖）与 upstreams/chaoxing；缺一则跳过。
        """
        from orchestrator.adapters.cxcli_worker import build_tiku

        upstream = ROOT / "upstreams" / "chaoxing"
        if not (upstream / "api" / "answer.py").is_file():
            self.skipTest("上游未 clone")
        try:
            tiku = build_tiku(str(upstream), "http://127.0.0.1:8765/v1", submit=False)
        except ImportError as exc:
            self.skipTest(f"上游依赖缺失：{exc}")

        from api.answer import AI

        self.assertIsInstance(tiku, AI)
        self.assertEqual(tiku.endpoint, "http://127.0.0.1:8765/v1")
        self.assertEqual(tiku.model, "agent-in-the-loop")
        self.assertFalse(tiku.SUBMIT)

    def test_build_tiku_submit_true(self) -> None:
        from orchestrator.adapters.cxcli_worker import build_tiku

        upstream = ROOT / "upstreams" / "chaoxing"
        if not (upstream / "api" / "answer.py").is_file():
            self.skipTest("上游未 clone")
        try:
            tiku = build_tiku(str(upstream), "http://127.0.0.1:8765/v1", submit=True)
        except ImportError as exc:
            self.skipTest(f"上游依赖缺失：{exc}")
        self.assertTrue(tiku.SUBMIT)

    def test_upstream_env_fixes_font_table_path(self) -> None:
        """字体反爬映射表必须可加载（相对 CWD 加载会失败 → 题目变乱码）。"""
        from orchestrator.adapters.cxcli_worker import prepare_upstream_env

        upstream = ROOT / "upstreams" / "chaoxing"
        table = upstream / "resource" / "font_map_table.json"
        if not table.is_file():
            self.skipTest("上游资源未 clone")
        prepare_upstream_env(str(upstream))
        from api.cxsecret_font import fonthash_dao, resource_path

        resolved = Path(resource_path("resource/font_map_table.json"))
        self.assertTrue(resolved.is_file(), f"字体表路径未指向上游：{resolved}")
        self.assertEqual(resolved.resolve(), table.resolve())
        # 单例已加载出真实映射（不是空表兜底）
        self.assertGreater(len(fonthash_dao.char_map), 1000)

    def test_build_tiku_bypasses_local_proxy(self) -> None:
        """本地 shim 必须绕过系统代理（httpx 默认 trust_env，会 502）。"""
        from orchestrator.adapters.cxcli_worker import build_tiku

        upstream = ROOT / "upstreams" / "chaoxing"
        if not (upstream / "api" / "answer.py").is_file():
            self.skipTest("上游未 clone")
        saved = {k: os.environ.get(k) for k in ("NO_PROXY", "no_proxy")}
        os.environ.pop("NO_PROXY", None)
        os.environ.pop("no_proxy", None)
        try:
            try:
                build_tiku(str(upstream), "http://127.0.0.1:8765/v1", submit=False)
            except ImportError as exc:
                self.skipTest(f"上游依赖缺失：{exc}")
            self.assertIn("127.0.0.1", os.environ.get("NO_PROXY", ""))
            self.assertIn("localhost", os.environ.get("no_proxy", ""))
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_manifest_gpl_pinned_and_enabled(self) -> None:
        m = _manifest()
        self.assertTrue(m.enabled)
        self.assertEqual(m.upstream.get("license"), "GPL-3.0")
        self.assertEqual(m.upstream.get("isolation"), "process")
        self.assertTrue(m.upstream.get("pinned_commit"))
        # C13（音频）显式不声明：上游在视频通道里自动 fallback Audio
        self.assertNotIn("C13", m.capabilities)


if __name__ == "__main__":
    unittest.main()
