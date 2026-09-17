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
