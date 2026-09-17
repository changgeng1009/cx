"""命令层与 Envelope 契约（验收项 V1 / V2 / V5 / V8）。

V1 的核心断言是：**每一条命令都要返回结构完整的 Envelope**。这条看起来
很形式化，但它是"不隐藏失败"的载体——只要有一条命令偷偷返回裸数据或
省略 `fallback_trace`，调用方（Agent）就无法区分"真的没数据"和"失败了"。
"""

from __future__ import annotations

import unittest

from orchestrator.models import Envelope
from orchestrator.services import COMMANDS

from .helpers import Sandbox

REQUIRED_KEYS = {
    "ok",
    "command",
    "request_id",
    "account",
    "adapter",
    "adapter_version",
    "state",
    "started_at",
    "finished_at",
    "duration_ms",
    "data",
    "warnings",
    "error",
    "fallback_trace",
    "next_actions",
}

COURSE_1 = "240100001"

#: 每条命令的最小合法参数，用于 V1 全量遍历
MINIMAL_PARAMS: dict[str, dict[str, object]] = {
    "get_course": {"course_id": COURSE_1},
    "scan_tasks": {"course_id": COURSE_1},
    "list_materials": {"course_id": COURSE_1},
    "download_material": {"course_id": COURSE_1},
    "run_course": {"course_id": COURSE_1, "dry_run": True},
    "run_chapter": {"course_id": COURSE_1, "chapter_id": "ch_02", "dry_run": True},
    "run_video_tasks": {"course_id": COURSE_1, "dry_run": True},
    "run_reading_tasks": {"course_id": COURSE_1, "dry_run": True},
    "answer_submit": {"ticket_id": "tk_missing", "answers": "A"},
    "sign_in": {"dry_run": True},
    "cookies_login": {"no_open": True, "timeout": 0.1, "poll": 0.5},
}


class EnvelopeContractTests(unittest.TestCase):
    def test_every_command_returns_full_envelope(self) -> None:
        with Sandbox() as sandbox:
            for command in sorted(COMMANDS):
                with self.subTest(command=command):
                    envelope = sandbox.run(command, **MINIMAL_PARAMS.get(command, {}))
                    self.assertIsInstance(envelope, Envelope)
                    payload = envelope.to_dict()
                    self.assertEqual(
                        set(payload) - REQUIRED_KEYS,
                        set(),
                        f"{command} 返回了未约定的字段",
                    )
                    self.assertEqual(
                        REQUIRED_KEYS - set(payload),
                        set(),
                        f"{command} 缺少必需字段",
                    )
                    self.assertEqual(payload["command"], command)
                    self.assertIsInstance(payload["fallback_trace"], list)
                    self.assertIsInstance(payload["warnings"], list)
                    self.assertIsInstance(payload["next_actions"], list)
                    # ok 与 error 必须一致：失败必须带 error
                    if not payload["ok"]:
                        self.assertIsNotNone(payload["error"], f"{command} 失败但无 error")

    def test_missing_required_param_is_input_error_not_crash(self) -> None:
        with Sandbox() as sandbox:
            envelope = sandbox.run("scan_tasks")
            self.assertFalse(envelope.ok)
            self.assertEqual(envelope.error["category"], "input")

    def test_unknown_command_is_reported(self) -> None:
        with Sandbox() as sandbox:
            envelope = sandbox.run("does_not_exist")
            self.assertFalse(envelope.ok)
            self.assertEqual(envelope.error["category"], "input")


class ScanTasksTests(unittest.TestCase):
    def test_returns_task_points_with_type_and_status(self) -> None:
        with Sandbox() as sandbox:
            envelope = sandbox.run("scan_tasks", course_id=COURSE_1)
            self.assertTrue(envelope.ok)
            summary = envelope.data["summary"]
            self.assertEqual(summary["total"], 13)
            self.assertIn("video", summary["by_type"])
            self.assertIn("locked", summary["by_status"])
            self.assertTrue(summary["needs_image"], "含图任务点必须被标出")

    def test_type_filter_applies_before_summary(self) -> None:
        with Sandbox() as sandbox:
            all_points = sandbox.run("scan_tasks", course_id=COURSE_1).data
            only_video = sandbox.run(
                "scan_tasks", course_id=COURSE_1, types=["video"]
            ).data
            self.assertEqual(only_video["summary"]["total"], 4)
            self.assertEqual(set(only_video["summary"]["by_type"]), {"video"})
            self.assertLess(
                only_video["summary"]["total"], all_points["summary"]["total"]
            )

    def test_chapter_filter(self) -> None:
        with Sandbox() as sandbox:
            data = sandbox.run(
                "scan_tasks", course_id=COURSE_1, chapter_id="ch_02"
            ).data
            self.assertEqual(data["summary"]["total"], 3)
            self.assertTrue(
                all(p["chapter_id"] == "ch_02" for p in data["task_points"])
            )

    def test_warns_about_image_questions_and_capability_gap(self) -> None:
        with Sandbox() as sandbox:
            envelope = sandbox.run("scan_tasks", course_id=COURSE_1)
            joined = " ".join(envelope.warnings)
            self.assertIn("图片题", joined)
            self.assertIn("C24", joined)

    def test_capability_hint_reports_provider(self) -> None:
        with Sandbox() as sandbox:
            data = sandbox.run("scan_tasks", course_id=COURSE_1).data
            hint = data["capability_hint"]["task_point_detail"]
            self.assertEqual(hint["capability"], "C09")
            self.assertIn("mock", hint["providers"])


class WriteCommandGateTests(unittest.TestCase):
    def test_write_requires_confirmation(self) -> None:
        with Sandbox() as sandbox:
            envelope = sandbox.run("run_course", course_id=COURSE_1)
            self.assertFalse(envelope.ok)
            self.assertEqual(envelope.error["category"], "input")
            self.assertTrue(any("--confirm" in a for a in envelope.next_actions))

    def test_dry_run_does_not_need_confirmation_and_writes_nothing(self) -> None:
        with Sandbox() as sandbox:
            envelope = sandbox.run("run_course", course_id=COURSE_1, dry_run=True)
            self.assertTrue(envelope.ok)
            self.assertTrue(envelope.data["dry_run"])
            self.assertIn("dry-run", " ".join(envelope.warnings))
            self.assertEqual(
                sandbox.ctx.state_store.list_for_account("acc_01"),
                [],
                "dry-run 不该落断点",
            )

    def test_confirmed_run_persists_checkpoint(self) -> None:
        with Sandbox() as sandbox:
            envelope = sandbox.run("run_course", confirmed=True, course_id=COURSE_1)
            self.assertTrue(envelope.ok)
            checkpoint = sandbox.ctx.state_store.load(
                "acc_01", envelope.request_id
            )
            self.assertIsNotNone(checkpoint)
            self.assertTrue(checkpoint.completed_task_points)
            self.assertEqual(envelope.data["checkpoint"]["completed"], len(
                checkpoint.completed_task_points
            ))

    def test_locked_task_points_are_skipped_with_reason(self) -> None:
        with Sandbox() as sandbox:
            envelope = sandbox.run("run_course", confirmed=True, course_id=COURSE_1)
            skipped = envelope.data["skipped"]
            self.assertTrue(
                any(s["reason"] == "locked" for s in skipped),
                "未开放任务点必须显式记为跳过，而不是静默忽略",
            )


class TypeScopedCommandTests(unittest.TestCase):
    """run_video_tasks / run_reading_tasks 的类型过滤只能由统一层做（docs/02 §4）。"""

    def test_video_only_command_filters_types(self) -> None:
        with Sandbox() as sandbox:
            envelope = sandbox.run(
                "run_video_tasks", confirmed=True, course_id=COURSE_1
            )
            self.assertTrue(envelope.ok)
            points = sandbox.run("scan_tasks", course_id=COURSE_1).data["task_points"]
            video_ids = {
                p["task_point_id"] for p in points if p["type"] == "video"
            }
            self.assertTrue(set(envelope.data["completed"]).issubset(video_ids))
            self.assertTrue(envelope.data["completed"])

    def test_reading_command_covers_document_ppt_reading(self) -> None:
        with Sandbox() as sandbox:
            envelope = sandbox.run(
                "run_reading_tasks", confirmed=True, course_id=COURSE_1
            )
            points = {
                p["task_point_id"]: p["type"]
                for p in sandbox.run("scan_tasks", course_id=COURSE_1).data["task_points"]
            }
            for task_point_id in envelope.data["completed"]:
                self.assertIn(points[task_point_id], {"document", "ppt", "reading"})

    def test_run_chapter_scopes_to_chapter(self) -> None:
        with Sandbox() as sandbox:
            envelope = sandbox.run(
                "run_chapter", confirmed=True, course_id=COURSE_1, chapter_id="ch_02"
            )
            points = {
                p["task_point_id"]: p["chapter_id"]
                for p in sandbox.run("scan_tasks", course_id=COURSE_1).data["task_points"]
            }
            for task_point_id in envelope.data["completed"]:
                self.assertEqual(points[task_point_id], "ch_02")


class AuxiliaryCommandTests(unittest.TestCase):
    def test_adapters_lists_registered_and_declared(self) -> None:
        with Sandbox() as sandbox:
            data = sandbox.run("adapters").data
            by_id = {row["adapter"]: row for row in data["adapters"]}
            self.assertTrue(by_id["mock"]["registered"])
            # 真实上游此时只声明未接入 —— 这条断言防止"假装已接入"
            self.assertFalse(by_id["chaoxing-cli"]["registered"])
            self.assertEqual(by_id["chaoxing-cli"]["license"], "GPL-3.0")

    def test_capabilities_coverage_math(self) -> None:
        with Sandbox() as sandbox:
            coverage = sandbox.run("capabilities").data["coverage"]
            self.assertEqual(coverage["total_capabilities"], 51)
            self.assertEqual(
                coverage["covered"] + coverage["uncovered"],
                coverage["total_capabilities"],
            )
            self.assertIn("C38", coverage["gap_ids"], "mock 不提供浏览器自动化，应计为缺口")

    def test_probe_reports_unregistered_adapters_as_disabled(self) -> None:
        with Sandbox() as sandbox:
            rows = sandbox.run("probe").data["adapters"]
            disabled = [r for r in rows if not r["enabled"]]
            self.assertTrue(disabled)
            self.assertTrue(all(not r["healthy"] for r in disabled))

    def test_accounts_reports_not_ready_before_login(self) -> None:
        with Sandbox() as sandbox:
            rows = sandbox.run("accounts").data["accounts"]
            self.assertTrue(rows)
            self.assertFalse(rows[0]["ready"])
            self.assertFalse(rows[0]["has_credentials"])

    def test_status_empty_state_is_explicit(self) -> None:
        with Sandbox() as sandbox:
            envelope = sandbox.run("status")
            self.assertTrue(envelope.ok)
            self.assertIsNone(envelope.data["checkpoint"])

    def test_get_homework_deadlines_sorted(self) -> None:
        with Sandbox() as sandbox:
            data = sandbox.run("get_homework").data
            dues = [row["due_at"] for row in data["deadlines"]]
            self.assertEqual(dues, sorted(dues))


class NoArgumentGuideTests(unittest.TestCase):
    """无参数时必须给出引导，而不是让 argparse 吐一句英文 usage。

    起因是一次真实的误用：使用者**双击** `cx.cmd`（它只是个命令行入口），
    无参数时 argparse 报 `the following arguments are required: COMMAND`，
    窗口一闪就关，于是报告"浏览器没拉起来"。

    双击本来就不会启动浏览器 —— 但工具至少该把这件事讲清楚，
    并且告诉使用者"该双击哪个文件"。这条测试守的就是这个。
    """

    def _run_main(self, argv: list[str]) -> tuple[int, str]:
        import io
        from contextlib import redirect_stdout, redirect_stderr

        from orchestrator import cli

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue() + err.getvalue()

    def test_no_args_prints_guide_and_exits_ok(self) -> None:
        code, output = self._run_main([])
        self.assertEqual(code, 0, "无参数不该是错误退出 —— 那是双击的正常路径")
        self.assertIn("命令行工具", output)
        # 必须把"该双击哪个文件"说清楚，否则使用者还是不知道怎么办
        self.assertIn("启动独立浏览器.cmd", output)
        # 必须给几条能直接照抄的命令
        self.assertIn("cookies_login", output)
        self.assertIn("docs/08-使用手册.md", output)

    def test_no_args_does_not_leak_argparse_usage_error(self) -> None:
        _, output = self._run_main([])
        self.assertNotIn("required: COMMAND", output)

    def test_help_still_works(self) -> None:
        """引导不能把 --help 抢掉。"""
        import io
        from contextlib import redirect_stdout

        from orchestrator import cli

        out = io.StringIO()
        # argparse 的 --help 打印后会用 SystemExit(0) 结束进程 —— 这是
        # 它的标准行为，不是缺陷，所以这里显式接住。
        with redirect_stdout(out), self.assertRaises(SystemExit) as caught:
            cli.main(["--help"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("命令分组", out.getvalue())


if __name__ == "__main__":
    unittest.main()
