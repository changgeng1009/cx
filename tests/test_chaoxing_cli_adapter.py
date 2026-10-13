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

    if op == "sign_scan":
        acts = [{"course_id": "1", "course_name": "测试课", "activity_id": "a1",
                 "title": "第3周签到", "status": "pending", "open_at": "2026-09-18 10:00",
                 "deadline": "", "attend_num": 5, "user_status": 1,
                 "sign_type": "unknown", "sign_type_hint": "0", "raw": {"id": 1}}]
        print(json.dumps({"ok": True, "data": {"activities": acts,
                                               "scanned_courses": 2,
                                               "only_running": True,
                                               "errors": []}}, ensure_ascii=False))
        sys.exit(0)

    if op == "sign_execute":
        print(json.dumps({"ok": True, "data": {
            "activity_id": req["args"].get("activity_id"),
            "course_id": req["args"].get("course_id"),
            "course_name": "测试课",
            "sign_type": req["args"].get("sign_type"),
            "activity": None,
            "presign_ok": True,
            "obj_id": req["args"].get("obj_id"),
            "lat": req["args"].get("lat"),
            "lon": req["args"].get("lon"),
            "response": os.environ.get("FAKE_SIGN_TEXT", "签到成功"),
        }}, ensure_ascii=False))
        sys.exit(0)

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


class SignCapabilityTests(unittest.TestCase):
    """M6 签到：能力路由、参数透传、返回文本归一。"""

    def setUp(self) -> None:
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.worker = self.base / "fake_worker.py"
        self.worker.write_text(FAKE_WORKER, encoding="utf-8")
        accounts = self.base / "accounts" / "acc_01"
        accounts.mkdir(parents=True)
        (accounts / "cookies.json").write_text(
            json.dumps({"cookies": [{"name": "UID", "value": "v", "domain": ".chaoxing.com"}]}),
            encoding="utf-8",
        )
        self.adapter = ChaoxingCliAdapter(
            _manifest(),
            worker_path=self.worker,
            accounts_dir=self.base / "accounts",
            min_interval_ms=0,
        )
        self.ctx = TaskContext(request_id="t", account_id="acc_01")

    def _capture(self, capability: str, params: dict) -> dict:
        cap_file = self.base / "cap.json"
        os.environ["FAKE_CAPTURE"] = str(cap_file)
        try:
            result = self.adapter.invoke(capability, params, self.ctx)
            self.assertTrue(result.ok, result.error)
        finally:
            os.environ.pop("FAKE_CAPTURE", None)
        return json.loads(cap_file.read_text(encoding="utf-8"))

    def test_c50_routes_to_sign_scan(self) -> None:
        req = self._capture("C50", {"course_id": "1"})
        self.assertEqual(req["op"], "sign_scan")
        self.assertTrue(req["args"]["only_running"])

    def test_c50_all_flag_turns_off_filter(self) -> None:
        req = self._capture("C50", {"all": True})
        self.assertFalse(req["args"]["only_running"])

    def test_c50_activities_are_projected(self) -> None:
        result = self.adapter.invoke("C50", {}, self.ctx)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["count"], 1)
        self.assertEqual(result.data["activities"][0]["activity_id"], "a1")

    def test_c48_passes_sign_params(self) -> None:
        req = self._capture(
            "C48",
            {
                "course_id": "1",
                "activity_id": "a1",
                "type": "gesture",
                "obj_id": "1234",
                "lat": 22.1,
                "lon": 113.2,
            },
        )
        self.assertEqual(req["op"], "sign_execute")
        self.assertEqual(req["args"]["activity_id"], "a1")
        self.assertEqual(req["args"]["sign_type"], "gesture")
        self.assertEqual(req["args"]["obj_id"], "1234")
        self.assertEqual(req["args"]["lat"], 22.1)

    def test_sign_result_success(self) -> None:
        os.environ["FAKE_SIGN_TEXT"] = "签到成功"
        try:
            result = self.adapter.invoke(
                "C48", {"course_id": "1", "activity_id": "a1"}, self.ctx
            )
        finally:
            os.environ.pop("FAKE_SIGN_TEXT", None)
        self.assertEqual(result.data["status"], "signed")
        self.assertEqual(result.data["outcome"], "success")

    def test_sign_result_duplicate(self) -> None:
        os.environ["FAKE_SIGN_TEXT"] = "您已签到"
        try:
            result = self.adapter.invoke(
                "C48", {"course_id": "1", "activity_id": "a1"}, self.ctx
            )
        finally:
            os.environ.pop("FAKE_SIGN_TEXT", None)
        self.assertEqual(result.data["outcome"], "duplicate")

    def test_sign_result_unknown_text_stays_unknown(self) -> None:
        """平台文案没见过时必须给 unknown，而不是假装成功。"""
        os.environ["FAKE_SIGN_TEXT"] = "some unexpected body"
        try:
            result = self.adapter.invoke(
                "C48", {"course_id": "1", "activity_id": "a1"}, self.ctx
            )
        finally:
            os.environ.pop("FAKE_SIGN_TEXT", None)
        self.assertEqual(result.data["outcome"], "unknown")
        self.assertEqual(result.data["status"], "unknown")

    def test_run_passes_pace_params(self) -> None:
        req = self._capture(
            "C18", {"course_id": "1", "pace_min": 7, "pace_max": 11}
        )
        self.assertEqual(float(req["args"]["pace_min"]), 7.0)
        self.assertEqual(float(req["args"]["pace_max"]), 11.0)

    def test_run_omits_pace_when_absent(self) -> None:
        """不给就用 worker 的默认值（20~45s），不能塞 None 进去。"""
        req = self._capture("C18", {"course_id": "1"})
        self.assertNotIn("pace_min", req["args"])
        self.assertNotIn("pace_max", req["args"])

    def test_c51_is_not_claimed(self) -> None:
        """签退未实现：不能装作支持（如实返回 unsupported）。"""
        result = self.adapter.invoke("C51", {}, self.ctx)
        self.assertFalse(result.ok)


class WorkerGuardTests(unittest.TestCase):
    """worker 的两道环境防护 —— 都是"跑批静默卡死"事故的产物。"""

    def test_import_guard_makes_import_fail_fast(self) -> None:
        """被屏蔽的模块必须**立刻**抛 ImportError（ddddocr 会卡在 import numpy）。"""
        import time

        from orchestrator.adapters.cxcli_worker import install_import_guards

        install_import_guards(("cx_guard_probe_mod",))
        start = time.monotonic()
        with self.assertRaises(ImportError):
            import cx_guard_probe_mod  # noqa: F401
        self.assertLess(time.monotonic() - start, 1.0)

    def test_import_guard_is_idempotent_and_additive(self) -> None:
        import sys

        from orchestrator.adapters.cxcli_worker import _ImportBlocker, install_import_guards

        install_import_guards(("cx_guard_a",))
        install_import_guards(("cx_guard_a", "cx_guard_b"))
        names = {n for f in sys.meta_path if isinstance(f, _ImportBlocker) for n in f._names}
        self.assertIn("cx_guard_a", names)
        self.assertIn("cx_guard_b", names)
        # 同名不重复装：blocker 数量不应随调用次数增长
        count_before = sum(1 for f in sys.meta_path if isinstance(f, _ImportBlocker))
        install_import_guards(("cx_guard_a",))
        self.assertEqual(sum(1 for f in sys.meta_path if isinstance(f, _ImportBlocker)), count_before)

    def test_network_guard_sets_default_timeout(self) -> None:
        from orchestrator.adapters.cxcli_worker import install_network_guards

        install_network_guards(42.0)
        import requests

        self.assertGreater(
            getattr(requests.sessions.Session.request, "_cx_timeout_default", 0), 0
        )

    def test_network_guard_does_not_override_explicit_timeout(self) -> None:
        """已显式传 timeout 的调用不能被改写。"""
        from unittest import mock

        import requests

        from orchestrator.adapters.cxcli_worker import install_network_guards

        install_network_guards(30.0)
        session = requests.Session()
        with mock.patch.object(
            requests.sessions.Session, "send", autospec=True
        ) as send:
            send.return_value = mock.Mock(status_code=200, headers={}, text="")
            try:
                session.get("http://example.invalid", timeout=7)
            except Exception:  # noqa: BLE001 —— 只关心传给底层请求的参数
                pass
            sent = send.call_args
            self.assertIsNotNone(sent)
            # timeout 是 send(**kwargs) 的参数，不在 PreparedRequest 上
            self.assertEqual(sent.kwargs.get("timeout"), 7)


class EventForwardingTests(unittest.TestCase):
    """worker 事件的转发映射 —— 曾经 pace_wait / job_recheck 被静默丢弃。"""

    class _Ctx:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict]] = []

        def report(self, event: str, **fields) -> None:
            self.events.append((event, fields))

    def _forward(self, payload: dict) -> list[tuple[str, dict]]:
        ctx = self._Ctx()
        ChaoxingCliAdapter._forward_event(payload, ctx)
        return ctx.events

    def test_pace_wait_maps_to_throttle_wait(self) -> None:
        """限速必须可观测 —— 否则无法判断防护到底有没有生效。"""
        events = self._forward({"event": "pace_wait", "seconds": 12.5})
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "throttle.wait")
        self.assertEqual(events[0][1]["seconds"], 12.5)
        self.assertEqual(events[0][1]["source"], "worker_pace")

    def test_job_recheck_counts_as_completed_with_marker(self) -> None:
        events = self._forward(
            {"event": "job_recheck", "task_point_id": "w1", "type": "quiz", "note": "n"}
        )
        self.assertEqual(events[0][0], "task_point.completed")
        self.assertTrue(events[0][1]["recheck"])
        self.assertEqual(events[0][1]["task_point_id"], "w1")

    def test_job_done_success_is_completed(self) -> None:
        events = self._forward(
            {"event": "job_done", "task_point_id": "v1", "type": "video", "result": "SUCCESS"}
        )
        self.assertEqual(events[0][0], "task_point.completed")
        self.assertNotIn("error", events[0][1])

    def test_job_done_failure_is_failed(self) -> None:
        events = self._forward(
            {"event": "job_done", "task_point_id": "v1", "type": "video", "result": "ERROR"}
        )
        self.assertEqual(events[0][0], "task_point.failed")


class PaceTests(unittest.TestCase):
    """秒完成任务点的限速 —— 防平台判「操作异常」（实测 9010 验证码 → 403 熔断）。"""

    def setUp(self) -> None:
        from orchestrator.adapters import cxcli_worker

        self.w = cxcli_worker

    def test_fast_job_waits_within_range(self) -> None:
        import time as _time
        from unittest import mock

        with mock.patch.object(self.w, "_emit"), mock.patch.object(self.w.time, "sleep") as sl:
            wait = self.w.pace_fast_job(_time.monotonic(), 5.0, 10.0)
        self.assertGreaterEqual(wait, 5.0)
        self.assertLessEqual(wait, 10.0)
        sl.assert_called_once()

    def test_slow_job_does_not_wait(self) -> None:
        """真播放（耗时数分钟）不需要额外间隔 —— 它本身已足够慢。"""
        import time as _time

        wait = self.w.pace_fast_job(_time.monotonic() - 100, 5.0, 10.0)
        self.assertEqual(wait, 0.0)

    def test_zero_disables_pacing(self) -> None:
        import time as _time

        self.assertEqual(self.w.pace_fast_job(_time.monotonic(), 0.0, 10.0), 0.0)

    def test_hi_not_below_lo(self) -> None:
        """传反了也要能用（取 max），不能睡负数。"""
        import time as _time
        from unittest import mock

        with mock.patch.object(self.w, "_emit"), mock.patch.object(self.w.time, "sleep"):
            wait = self.w.pace_fast_job(_time.monotonic(), 8.0, 3.0)
        self.assertGreaterEqual(wait, 8.0)

    def test_default_constants(self) -> None:
        self.assertGreater(self.w.FAST_JOB_SECONDS, 0)


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
