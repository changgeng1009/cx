"""XuexitongMcpAdapter 单元测试（假 worker，不触网）。

worker 用注入的 python_exe 执行一个测试专用脚本，按 op 返回固定 JSON，
覆盖：退出码→ErrorCategory 映射、规范化、cookie 桥接、路由顺序。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from orchestrator.adapters.mock import MockAdapter
from orchestrator.adapters.xuexitong_mcp import XuexitongMcpAdapter
from orchestrator.errors import ErrorCategory
from orchestrator.models import TaskContext
from orchestrator.registry import Manifest, load_manifests

ROOT = Path(__file__).resolve().parents[1]


def _manifest() -> Manifest:
    for m in load_manifests():
        if m.id == "xuexitong-mcp":
            return m
    raise AssertionError("xuexitong_mcp.json manifest 缺失")


#: 测试专用 worker：按请求里的 op 返回预设结果
FAKE_WORKER = textwrap.dedent(
    """
    import json, sys, os
    req = json.loads(sys.stdin.read())
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
                          "message": "会话无效"}))
        sys.exit(3)
    if mode == "platform":
        print(json.dumps({"ok": False, "code": "PLATFORM_REJECTED",
                          "message": "签名校验失败"}))
        sys.exit(2)
    if mode == "deps":
        print(json.dumps({"ok": False, "code": "DEPS_MISSING",
                          "message": "上游依赖不可用",
                          "hint": "python -m pip install requests pycryptodome"}))
        sys.exit(4)
    if mode == "garbage":
        sys.stdout.write("not json at all")
        sys.exit(0)
    canned = {
        "fetch_courses": [{"courseid": "123", "clazzid": "456",
                           "cpi": "1", "name": "测试课", "teacher": "张三"}],
        "fetch_profile": {"name": "李四"},
        "fetch_progress_overview": [
            {"course": "A", "done": 3, "total": 5, "error": None},
            {"course": "B", "done": None, "total": None, "error": "无进度数据"},
        ],
    }
    print(json.dumps({"ok": True, "data": canned.get(op, {})}))
    sys.exit(0)
    """
)


class AdapterTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.worker = self.base / "fake_worker.py"
        self.worker.write_text(FAKE_WORKER, encoding="utf-8")
        self.accounts = self.base / "accounts" / "acc_01"
        self.accounts.mkdir(parents=True)
        (self.accounts / "cookies.json").write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": "UID", "value": "v1", "domain": ".chaoxing.com"},
                        {"name": "_uid", "value": "v2", "domain": ""},
                    ]
                }
            ),
            encoding="utf-8",
        )
        self.adapter = XuexitongMcpAdapter(
            _manifest(),
            worker_path=self.worker,
            accounts_dir=self.base / "accounts",
            min_interval_ms=0,
        )
        self.ctx = TaskContext(request_id="t", account_id="acc_01")

    # ------------------------------------------------------------------
    def test_probe_healthy(self) -> None:
        probe = self.adapter.probe()
        self.assertTrue(probe.healthy, probe.detail)

    def test_c06_normalizes_course_keys(self) -> None:
        result = self.adapter.invoke("C06", {}, self.ctx)
        self.assertTrue(result.ok, result.error)
        course = result.data["courses"][0]
        self.assertEqual(course["course_id"], "123")
        self.assertEqual(course["clazz_id"], "456")
        self.assertEqual(course["teacher"], "张三")

    def test_c11_overall_sums(self) -> None:
        result = self.adapter.invoke("C11", {}, self.ctx)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["overall"]["done"], 3)
        self.assertEqual(result.data["overall"]["total"], 5)

    def test_exit_3_maps_to_auth(self) -> None:
        os.environ["FAKE_MODE"] = "auth"
        try:
            result = self.adapter.invoke("C06", {}, self.ctx)
        finally:
            os.environ.pop("FAKE_MODE", None)
        self.assertFalse(result.ok)
        self.assertEqual(result.error.category, ErrorCategory.AUTH)

    def test_exit_2_maps_to_platform_changed(self) -> None:
        os.environ["FAKE_MODE"] = "platform"
        try:
            result = self.adapter.invoke("C06", {}, self.ctx)
        finally:
            os.environ.pop("FAKE_MODE", None)
        self.assertFalse(result.ok)
        self.assertEqual(result.error.category, ErrorCategory.PLATFORM_CHANGED)

    def test_exit_4_maps_to_internal_with_hint(self) -> None:
        os.environ["FAKE_MODE"] = "deps"
        try:
            result = self.adapter.invoke("C06", {}, self.ctx)
        finally:
            os.environ.pop("FAKE_MODE", None)
        self.assertFalse(result.ok)
        self.assertEqual(result.error.category, ErrorCategory.INTERNAL)
        self.assertIn("pip install", result.error.message)

    def test_garbage_stdout_maps_to_internal(self) -> None:
        os.environ["FAKE_MODE"] = "garbage"
        try:
            result = self.adapter.invoke("C06", {}, self.ctx)
        finally:
            os.environ.pop("FAKE_MODE", None)
        self.assertFalse(result.ok)
        self.assertEqual(result.error.category, ErrorCategory.INTERNAL)

    def test_unsupported_capability(self) -> None:
        result = self.adapter.invoke("C18", {}, self.ctx)
        self.assertFalse(result.ok)

    # ------------------------------------------------------------------
    def test_bridge_writes_upstream_format(self) -> None:
        target = self.adapter.ensure_bridge("acc_01")
        self.assertTrue(target.is_file())
        flat = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(flat, {"UID": "v1", "_uid": "v2"})

    def test_bridge_skips_rewrite_when_fresh(self) -> None:
        first = self.adapter.ensure_bridge("acc_01")
        old = json.loads(first.read_text(encoding="utf-8"))
        # 源文件没变 → 第二次直接返回，内容不重写
        time.sleep(0.01)
        second = self.adapter.ensure_bridge("acc_01")
        self.assertEqual(first, second)
        self.assertEqual(json.loads(second.read_text(encoding="utf-8")), old)

    def test_bridge_raises_without_cookies(self) -> None:
        (self.accounts / "cookies.json").unlink()
        with self.assertRaises(FileNotFoundError):
            self.adapter.ensure_bridge("acc_01")

    # ------------------------------------------------------------------
    def test_manifest_enabled_and_pinned(self) -> None:
        m = _manifest()
        self.assertTrue(m.enabled)
        self.assertTrue(m.upstream.get("pinned_commit"))
        self.assertEqual(m.upstream.get("license"), "MIT")

    def test_router_prefers_real_adapter_over_mock(self) -> None:
        from orchestrator.bootstrap import build

        ctx = build(adapter_classes={"__test__": None} if False else None,
                    root=self.base)
        # build() 会在 self.base 下重建 registry；只需要 candidates 顺序
        candidates = ctx.registry.candidates("C06")
        ids = [a.manifest.id for a in candidates]
        self.assertIn("xuexitong-mcp", ids)
        self.assertIn("mock", ids)
        self.assertEqual(ids[0], "xuexitong-mcp", ids)


class C29RoutingTests(AdapterTestCase):
    """C29 语义分叉：带 course_id → 该课作业；不带 → 全部课程截止总览。"""

    def _capture(self, params: dict) -> dict:
        cap = self.base / "cap.json"
        os.environ["FAKE_CAPTURE"] = str(cap)
        try:
            result = self.adapter.invoke("C29", params, self.ctx)
            self.assertTrue(result.ok, result.error)
        finally:
            os.environ.pop("FAKE_CAPTURE", None)
        return json.loads(cap.read_text(encoding="utf-8"))

    def test_with_course_id_routes_to_per_course_homework(self) -> None:
        req = self._capture({"course_id": "267147955"})
        self.assertEqual(req["op"], "fetch_homework")
        self.assertEqual(req["args"]["course_id"], "267147955")

    def test_without_course_id_falls_back_to_overview(self) -> None:
        req = self._capture({})
        self.assertEqual(req["op"], "fetch_deadline_overview")


if __name__ == "__main__":
    unittest.main()
