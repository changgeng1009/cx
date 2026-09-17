"""日志完整性与脱敏（验收项 V8 / V9）。

V9 是硬性安全项：日志是最容易被忽略的凭据泄漏渠道。一次 verbose 排障
就可能把手机号和密码写进 runs/*.jsonl，而那个文件通常会被贴进 issue。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from orchestrator.redact import redact
from orchestrator.structured_log import Event, StructuredLogger

from .helpers import Sandbox

COURSE_1 = "240100001"

#: 日志必须携带的维度（用户明确要求的字段）
REQUIRED_LOG_FIELDS = {
    "ts",
    "level",
    "event",
    "request_id",
    "account",
    "adapter",
    "course",
    "task_type",
    "task_point_id",
    "started_at",
    "finished_at",
    "ok",
}


class RedactTests(unittest.TestCase):
    def test_masks_phone_number(self) -> None:
        self.assertEqual(redact("手机号 13812345678 已登录"), "手机号 138****5678 已登录")

    def test_masks_key_value_password(self) -> None:
        result = redact("password=MyS3cret! uname=13812345678")
        self.assertNotIn("MyS3cret!", result)
        self.assertIn("***", result)

    def test_masks_json_style_password(self) -> None:
        result = redact('{"phone": "13812345678", "password": "hunter2"}')
        self.assertNotIn("hunter2", result)
        self.assertNotIn("13812345678", result)

    def test_masks_sensitive_dict_keys(self) -> None:
        result = redact({"user": "wei", "password": "p@ss", "cookie": "SESSION=abc"})
        self.assertEqual(result["user"], "wei")
        self.assertEqual(result["password"], "***")
        self.assertEqual(result["cookie"], "***")

    def test_is_idempotent(self) -> None:
        once = redact("password=abc123")
        twice = redact(once)
        self.assertEqual(once, twice)

    def test_leaves_normal_text_alone(self) -> None:
        text = "正在学习: 第三章 组合逻辑电路"
        self.assertEqual(redact(text), text)


class StructuredLogTests(unittest.TestCase):
    def test_task_point_events_carry_required_dimensions(self) -> None:
        with Sandbox() as sandbox:
            envelope = sandbox.run("run_course", confirmed=True, course_id=COURSE_1)
            records = sandbox.log_records(envelope.request_id)
            self.assertTrue(records)

            completed = [r for r in records if r["event"] == Event.TASK_POINT_COMPLETED]
            self.assertTrue(completed, "缺少任务点完成事件")
            for record in completed:
                missing = REQUIRED_LOG_FIELDS - set(record)
                self.assertEqual(missing, set(), f"日志缺少字段：{missing}")
                self.assertEqual(record["adapter"], "mock")
                self.assertEqual(record["course"]["id"], COURSE_1)
                self.assertTrue(record["task_type"])
                self.assertTrue(record["task_point_id"])
                self.assertTrue(record["started_at"])
                self.assertTrue(record["finished_at"])
                self.assertTrue(record["ok"])

    def test_failed_task_point_logs_error(self) -> None:
        from orchestrator.adapters.mock import MockAdapter, RunScript

        adapter = MockAdapter(
            __import__("tests.helpers", fromlist=["mock_manifest"]).mock_manifest("mock"),
            run_script=RunScript(fail_ids=["tp_002"]),
        )
        with Sandbox([adapter]) as sandbox:
            envelope = sandbox.run("run_course", confirmed=True, course_id=COURSE_1)
            records = sandbox.log_records(envelope.request_id)
            failed = [r for r in records if r["event"] == Event.TASK_POINT_FAILED]
            self.assertTrue(failed)
            self.assertFalse(failed[0]["ok"])
            self.assertIsNotNone(failed[0]["error"])

    def test_fallback_is_recorded_in_log(self) -> None:
        from orchestrator.errors import AdapterError, Codes, ErrorCategory
        from orchestrator.adapters.mock import MockAdapter

        from .helpers import mock_manifest

        broken = MockAdapter(
            mock_manifest("broken", priority=1),
            faults={
                "C06": AdapterError(
                    code=Codes.ADAPTER_ERROR,
                    category=ErrorCategory.PERMISSION,
                    message="无权限",
                )
            },
        )
        healthy = MockAdapter(mock_manifest("healthy", priority=2))
        with Sandbox([broken, healthy]) as sandbox:
            envelope = sandbox.run("list_courses")
            self.assertTrue(envelope.ok)
            self.assertEqual(envelope.adapter, "healthy")
            self.assertEqual(len(envelope.fallback_trace), 1)

    def test_raw_log_preserves_upstream_output(self) -> None:
        with Sandbox() as sandbox:
            envelope = sandbox.run("list_courses")
            raw = sandbox.raw_log(envelope.request_id)
            self.assertIn("mock", raw)

    def test_human_echo_is_separate_from_jsonl(self) -> None:
        lines: list[str] = []
        with Sandbox() as sandbox:
            logger = StructuredLogger(
                sandbox.ctx.run_dir, "req_echo_test", echo=lines.append
            )
            logger.emit(Event.TASK_POINT_COMPLETED, adapter="mock", task_point_id="tp_1")
            logger.close()
            self.assertEqual(len(lines), 1)
            self.assertIn("task_point.completed", lines[0])
            records = sandbox.log_records("req_echo_test")
            self.assertEqual(records[0]["task_point_id"], "tp_1")


class CredentialLeakTests(unittest.TestCase):
    """V9：凭据绝不能进日志、进 raw log、或进 Envelope。"""

    def test_password_never_reaches_logs_or_envelope(self) -> None:
        secret = "Sup3rSecret!Forbidden"
        with Sandbox() as sandbox:
            sandbox.ctx.accounts.write_credentials("acc_01", "13812345678", secret)
            envelope = sandbox.run(
                "run_course",
                confirmed=True,
                course_id=COURSE_1,
                password=secret,
                phone="13812345678",
            )

            jsonl = sandbox.ctx.run_dir / f"{envelope.request_id}.jsonl"
            self.assertTrue(jsonl.is_file())
            blob = jsonl.read_text(encoding="utf-8")
            self.assertNotIn(secret, blob, "密码出现在审计日志里")
            self.assertNotIn("13812345678", blob, "完整手机号出现在审计日志里")

            raw = sandbox.raw_log(envelope.request_id)
            self.assertNotIn(secret, raw)

            envelope_blob = json.dumps(envelope.to_dict(), ensure_ascii=False)
            self.assertNotIn(secret, envelope_blob)
            self.assertNotIn("13812345678", envelope_blob)

    def test_redaction_applies_to_injected_upstream_output(self) -> None:
        """上游把凭据打进 stdout 时，raw log 层必须兜住。"""
        from orchestrator.adapters.mock import MockAdapter

        from .helpers import mock_manifest

        adapter = MockAdapter(
            mock_manifest("noisy"),
            raw_output="login ok password=泄露了 token=abc123 phone=13812345678",
        )
        with Sandbox([adapter]) as sandbox:
            envelope = sandbox.run(
                "get_notices", keyword="正文", unread_only=False
            )
            raw = sandbox.raw_log(envelope.request_id)
            self.assertNotIn("泄露了", raw)
            self.assertNotIn("abc123", raw)
            self.assertNotIn("13812345678", raw)

    def test_account_context_repr_hides_credentials(self) -> None:
        with Sandbox() as sandbox:
            sandbox.ctx.accounts.write_credentials("acc_01", "13812345678", "p@ssw0rd!")
            context = sandbox.ctx.accounts.context("acc_01")
            self.assertNotIn("p@ssw0rd!", repr(context))
            self.assertIn("138****5678", repr(context))

    def test_accounts_dir_is_gitignored(self) -> None:
        root = Path(__file__).resolve().parent.parent
        gitignore = (root / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("accounts/", gitignore)
        self.assertIn("upstreams/", gitignore)
        self.assertIn("runs/", gitignore)


if __name__ == "__main__":
    unittest.main()
