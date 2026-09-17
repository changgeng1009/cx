"""浏览器隔离守卫测试（对应用户的硬约束）。

这组测试守的是一条**物理边界**：
1. 不许用日常浏览器的 profile（Chrome）；
2. 不许碰其他 Agent 占用的 Edge 窗口。

只要 `assert_isolated` 失效，某个未来的 Adapter 就可能一句
`Popen(["msedge.exe"])` 把使用者正在用的浏览器会话卷进自动化里。
所以这里必须像 V10（GPL 隔离）一样有测试兜底。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator import browser


class IsolationGuardTests(unittest.TestCase):
    def test_rejects_system_profile_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_system = Path(tmp) / "Edge" / "User Data"
            fake_system.mkdir(parents=True)
            with mock.patch.object(
                browser, "system_profile_dirs", return_value=[fake_system]
            ):
                with self.assertRaises(browser.IsolationError) as ctx:
                    browser.assert_isolated(fake_system)
                self.assertIn("默认 profile", str(ctx.exception))

    def test_rejects_path_inside_system_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_system = Path(tmp) / "Edge" / "User Data"
            (fake_system / "Profile 3").mkdir(parents=True)
            with mock.patch.object(
                browser, "system_profile_dirs", return_value=[fake_system]
            ):
                with self.assertRaises(browser.IsolationError):
                    browser.assert_isolated(fake_system / "Profile 3")

    def test_accepts_project_local_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_system = Path(tmp) / "Edge" / "User Data"
            fake_system.mkdir(parents=True)
            ours = Path(tmp) / "project" / ".browser" / "edge-profile"
            with mock.patch.object(
                browser, "system_profile_dirs", return_value=[fake_system]
            ):
                self.assertEqual(browser.assert_isolated(ours), ours.resolve())
                self.assertTrue(browser.is_isolated(ours))

    def test_rejects_filesystem_root_and_home(self) -> None:
        with self.assertRaises(browser.IsolationError):
            browser.assert_isolated(Path(Path.home().anchor))
        with self.assertRaises(browser.IsolationError):
            browser.assert_isolated(Path.home())

    def test_real_default_profile_of_this_machine_is_rejected_if_present(self) -> None:
        """本机真的装了 Chrome/Edge 的话，它们的默认 profile 必须被拒。"""
        for forbidden in browser.system_profile_dirs():
            with self.subTest(path=str(forbidden)):
                with self.assertRaises(browser.IsolationError):
                    browser.assert_isolated(forbidden)

    def test_our_own_profile_is_not_a_forbidden_path(self) -> None:
        ours = browser.profile_dir(browser.project_root())
        self.assertTrue(browser.is_isolated(ours))
        self.assertIn(".browser", str(ours))
        for forbidden in browser.system_profile_dirs():
            self.assertNotIn(forbidden.resolve(), ours.parents)


class LaunchArgsTests(unittest.TestCase):
    def test_required_isolation_flags_always_present(self) -> None:
        args = browser.launch_args(
            Path("msedge.exe"), Path("/tmp/p"), 9333, url="https://example.com"
        )
        joined = " ".join(args)
        self.assertIn("--user-data-dir=", joined, "缺少 user-data-dir 就会附着到默认实例")
        self.assertIn("--remote-debugging-port=9333", joined)
        self.assertIn("--remote-debugging-address=127.0.0.1", joined, "CDP 不应暴露到局域网")
        self.assertIn("--no-first-run", joined)
        self.assertIn("--no-default-browser-check", joined)
        self.assertEqual(args[-1], "https://example.com")

    def test_profile_path_is_passed_verbatim(self) -> None:
        profile = Path("D:/CodexWork/学习通刷课脚本/.browser/edge-profile")
        args = browser.launch_args(Path("msedge.exe"), profile, 9333)
        self.assertIn(f"--user-data-dir={profile}", args)

    def test_extra_args_appended_before_url(self) -> None:
        args = browser.launch_args(
            Path("msedge.exe"), Path("/tmp/p"), 9333, url="about:blank",
            extra=["--headless=new"],
        )
        self.assertIn("--headless=new", args)
        self.assertLess(args.index("--headless=new"), args.index("about:blank"))

    def test_default_port_is_not_the_common_9222(self) -> None:
        """9222 太常被别的工具占用；撞端口会让我们连到别人的浏览器上。"""
        self.assertEqual(browser.DEFAULT_DEBUG_PORT, 9333)
        self.assertNotEqual(browser.DEFAULT_DEBUG_PORT, 9222)


class ResolutionTests(unittest.TestCase):
    def test_default_profile_is_project_local(self) -> None:
        resolved = browser.profile_dir(browser.project_root())
        self.assertEqual(
            resolved,
            (browser.project_root() / ".browser" / "edge-profile").resolve(),
        )

    def test_env_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            custom = Path(tmp) / "custom-profile"
            with mock.patch.dict(
                "os.environ",
                {browser.ENV_PROFILE: str(custom), browser.ENV_PORT: "9401"},
            ):
                self.assertEqual(browser.profile_dir(), custom.resolve())
                self.assertEqual(browser.debug_port(), 9401)
                self.assertEqual(browser.cdp_url(), "http://127.0.0.1:9401")

    def test_bad_port_env_falls_back_to_default(self) -> None:
        with mock.patch.dict("os.environ", {browser.ENV_PORT: "not-a-number"}):
            self.assertEqual(browser.debug_port(), browser.DEFAULT_DEBUG_PORT)

    def test_binary_env_missing_file_raises(self) -> None:
        with mock.patch.dict(
            "os.environ", {browser.ENV_BINARY: "C:/definitely/not/here.exe"}
        ):
            with self.assertRaises(browser.BrowserNotFound):
                browser.find_browser()

    def test_browser_is_found_on_this_machine(self) -> None:
        try:
            found = browser.find_browser()
        except browser.BrowserNotFound as exc:
            self.skipTest(f"本机未安装 Edge：{exc}")
        self.assertTrue(found.is_file())
        self.assertIn("msedge", found.name.lower())


class CdpProbeTests(unittest.TestCase):
    def test_closed_port_reports_offline(self) -> None:
        status = browser.probe_cdp(port=9455, timeout_s=0.4)
        self.assertFalse(status.alive)
        self.assertTrue(status.detail)

    def test_status_serialises(self) -> None:
        payload = browser.probe_cdp(port=9456, timeout_s=0.4).to_dict()
        self.assertEqual(
            set(payload), {"alive", "browser", "protocol", "endpoint", "detail"}
        )


class PlanTests(unittest.TestCase):
    def test_plan_rejects_pointing_at_system_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_system = Path(tmp) / "Edge" / "User Data"
            fake_system.mkdir(parents=True)
            with mock.patch.object(
                browser, "system_profile_dirs", return_value=[fake_system]
            ), mock.patch.dict(
                "os.environ", {browser.ENV_PROFILE: str(fake_system)}
            ):
                with self.assertRaises(browser.IsolationError):
                    browser.plan()

    def test_plan_has_no_side_effects(self) -> None:
        """`plan()` 必须只读：不该创建目录、不该启动进程。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile_before = profile = root / ".browser" / "edge-profile"
            self.assertFalse(profile_before.exists())
            try:
                plan = browser.plan(root=root, port=9457)
            except browser.BrowserNotFound as exc:
                self.skipTest(str(exc))
            self.assertFalse(
                profile.exists(), "plan() 不应该创建 profile 目录"
            )
            self.assertIn("--user-data-dir", " ".join(plan.args))
            self.assertEqual(plan.port, 9457)


class PidRecordTests(unittest.TestCase):
    def test_owned_pids_reads_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = browser.pid_file(root)
            record.parent.mkdir(parents=True, exist_ok=True)
            record.write_text(json.dumps({"pid": 4321}), encoding="utf-8")
            self.assertEqual(browser.owned_pids(root), [4321])

    def test_owned_pids_empty_without_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(browser.owned_pids(Path(tmp)), [])

    def test_stop_instructions_warn_against_mass_kill(self) -> None:
        text = browser.stop_instructions(browser.project_root())
        self.assertIn("不要用", text)
        self.assertIn("msedge.exe", text)


class DiagnosisTests(unittest.TestCase):
    """诊断能力测试。

    起因：使用者报告"没拉起来"，而我最初无法回答"卡在哪一环"——
    因为启动时把浏览器的 stdout/stderr 丢进了 DEVNULL，进程秒退也不检查，
    最后还打印一句"可能仍在初始化"，把失败伪装成进行中。
    这组测试守的就是"失败必须被明确报告"。
    """

    def test_port_is_free_reports_occupied_port(self) -> None:
        import socket

        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        try:
            port = holder.getsockname()[1]
            self.assertFalse(browser.port_is_free(port))
        finally:
            holder.close()

    def test_port_is_free_true_for_unused_port(self) -> None:
        import socket

        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        self.assertTrue(browser.port_is_free(port))

    def test_read_log_tail_returns_empty_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(browser.read_log_tail(Path(tmp)), "")

    def test_read_log_tail_keeps_only_tail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".browser").mkdir(parents=True)
            lines = [f"line-{i}" for i in range(200)]
            browser.log_file(root).write_text("\n".join(lines), encoding="utf-8")
            tail = browser.read_log_tail(root, max_bytes=100)
            self.assertNotIn("line-0", tail)
            self.assertIn("line-199", tail)

    def test_process_alive_detects_current_process(self) -> None:
        import os as _os

        self.assertTrue(browser.process_alive(_os.getpid()))

    def test_process_alive_false_for_impossible_pid(self) -> None:
        # 0x7FFFFFFF 在 Windows 上不会是有效 PID
        self.assertFalse(browser.process_alive(0x7FFFFFFF))
        self.assertFalse(browser.process_alive(0))

    def test_diagnose_reports_unavailable_cdp(self) -> None:
        """CDP 离线时必须给出"未运行/失败"的结论，而不是含糊其辞。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".browser").mkdir(parents=True)
            with mock.patch.object(
                browser, "probe_cdp",
                return_value=browser.CdpStatus(alive=False, detail="test"),
            ):
                report = browser.diagnose(root)
            self.assertFalse(report["cdp_alive"])
            self.assertIn("verdict", report)
            self.assertIsInstance(report["verdict"], str)

    def test_diagnose_verdict_points_at_occupied_port(self) -> None:
        """端口被占用时，结论必须直接指向端口 —— 这是最常见的启动失败原因。"""
        import socket

        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        try:
            port = holder.getsockname()[1]
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / ".browser").mkdir(parents=True)
                with mock.patch.dict(
                    "os.environ", {browser.ENV_PORT: str(port)}
                ), mock.patch.object(
                    browser, "probe_cdp",
                    return_value=browser.CdpStatus(alive=False, detail="test"),
                ):
                    report = browser.diagnose(root)
                self.assertFalse(report["port_free"])
                self.assertIn("端口", report["verdict"])
        finally:
            holder.close()

    def test_diagnose_verdict_online_when_cdp_alive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".browser").mkdir(parents=True)
            with mock.patch.object(
                browser, "probe_cdp",
                return_value=browser.CdpStatus(alive=True, browser="Edg/test"),
            ):
                report = browser.diagnose(root)
            self.assertTrue(report["cdp_alive"])
            self.assertIn("在线", report["verdict"])

    def test_diagnose_spots_dead_process_from_pid_record(self) -> None:
        """PID 记录存在但进程已死 —— 这是"启动后立刻死了"的指纹。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".browser").mkdir(parents=True)
            browser.pid_file(root).write_text(
                json.dumps({"pid": 0x7FFFFFFF}), encoding="utf-8"
            )
            with mock.patch.object(
                browser, "probe_cdp",
                return_value=browser.CdpStatus(alive=False, detail="test"),
            ), mock.patch.object(browser, "port_is_free", return_value=True):
                # port_is_free 也必须 stub：开发者机器上真实 Edge 可能正占着
                # 9333，那样 diagnose 会先报"端口被占用"而非"进程死了"。
                report = browser.diagnose(root)
            self.assertFalse(report["pid_alive"])
            self.assertIn("立刻死", report["verdict"])


class LaunchDiagnosticsTests(unittest.TestCase):
    """启动失败必须被明确报告，且要留下浏览器自己的话。"""

    def test_launch_captures_browser_output_to_log(self) -> None:
        """stdout/stderr 必须落盘 —— 丢了它就没有诊断能力。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".browser").mkdir(parents=True)

            class FakeProcess:
                pid = 0x7FFFFFFF

                def poll(self) -> int:
                    return 1  # 秒退

            with mock.patch.object(
                browser, "find_browser", return_value=Path("/fake/msedge")
            ), mock.patch.object(
                browser, "probe_cdp",
                return_value=browser.CdpStatus(alive=False, detail="test"),
            ), mock.patch.object(
                browser.subprocess, "Popen", return_value=FakeProcess()
            ):
                plan, process = browser.launch(root, wait_ready_s=0.01)

            self.assertIsNotNone(process)
            self.assertFalse(plan.already_running)
            self.assertEqual(plan.exit_code, 1)
            self.assertTrue(browser.log_file(root).exists(), "日志文件必须被创建")

    def test_launch_records_log_path_in_pid_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".browser").mkdir(parents=True)

            class FakeProcess:
                pid = 4242

                def poll(self) -> None:
                    return None

            with mock.patch.object(
                browser, "find_browser", return_value=Path("/fake/msedge")
            ), mock.patch.object(
                browser, "probe_cdp",
                return_value=browser.CdpStatus(alive=False, detail="test"),
            ), mock.patch.object(
                browser.subprocess, "Popen", return_value=FakeProcess()
            ):
                browser.launch(root, wait_ready_s=0.01)

            record = json.loads(browser.pid_file(root).read_text(encoding="utf-8"))
            self.assertEqual(record["pid"], 4242)
            self.assertIn("log", record, "PID 记录里要带日志路径，方便照着去看")

    def test_launch_skips_when_already_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".browser").mkdir(parents=True)
            with mock.patch.object(
                browser, "find_browser", return_value=Path("/fake/msedge")
            ), mock.patch.object(
                browser, "probe_cdp",
                return_value=browser.CdpStatus(alive=True, browser="Edg/live"),
            ), mock.patch.object(browser.subprocess, "Popen") as popen:
                plan, process = browser.launch(root)
            popen.assert_not_called()
            self.assertIsNone(process)
            self.assertTrue(plan.already_running)


if __name__ == "__main__":
    unittest.main()
