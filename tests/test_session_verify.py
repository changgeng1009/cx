"""会话有效性探测（三态判定）。

关键是**不假装确定**：拿不准必须报 inconclusive，而不是报 valid。
一个假的"登录成功"会让后续所有排障建立在错误前提上。

mock 服务端是本地的，所以这组测试不需要真的访问 chaoxing ——
重定向到 `passport2.chaoxing.com` 的情形用 302 + Location 模拟，
而我们的实现刻意**不跟随重定向**，因此也不会产生外网请求。
"""

from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from orchestrator import session_verify
from orchestrator.cookies import Cookie, CookieJar
from orchestrator.session_verify import SessionVerdict

from .helpers import Sandbox


def make_jar(count: int = 3) -> CookieJar:
    jar = CookieJar(source="test")
    for index in range(count):
        jar.add(
            Cookie(
                name=f"k{index}", value=f"v{index}",
                domain=".chaoxing.com", path="/",
            )
        )
    return jar


class MockPageServer:
    """按路径返回不同响应，用来驱动三种判定结果。"""

    def __init__(self, routes: dict[str, tuple[int, dict[str, str], str]]) -> None:
        self.routes = routes
        self.requests: list[dict[str, Any]] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802
                server.requests.append(
                    {"path": self.path, "cookie": self.headers.get("Cookie", "")}
                )
                status, headers, body = server.routes.get(
                    self.path, (404, {}, "not found")
                )
                raw = body.encode("utf-8")
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self._http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._http.server_address[1]
        self._thread = threading.Thread(target=self._http.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/base"

    def __enter__(self) -> "MockPageServer":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._http.shutdown()
        self._http.server_close()


class ClassifyTests(unittest.TestCase):
    def test_login_redirect_is_invalid(self) -> None:
        verdict, signals = session_verify.classify(
            302, "https://passport2.chaoxing.com/login?xxx=1", ""
        )
        self.assertIs(verdict, SessionVerdict.INVALID)
        self.assertTrue(any("登录流程" in s for s in signals))

    def test_401_and_403_are_invalid(self) -> None:
        for status in (401, 403):
            with self.subTest(status=status):
                verdict, _ = session_verify.classify(status, "", "")
                self.assertIs(verdict, SessionVerdict.INVALID)

    def test_logged_in_marker_is_valid(self) -> None:
        body = '<div id="siteName" dataurl="kb.chaoxing.com"></div>'
        verdict, signals = session_verify.classify(200, "", body)
        self.assertIs(verdict, SessionVerdict.VALID)
        self.assertTrue(any("标记" in s for s in signals))

    def test_200_without_marker_is_inconclusive_not_valid(self) -> None:
        """最重要的断言：识别不出来时**不能**当成功。"""
        verdict, signals = session_verify.classify(200, "", "<html>改版了</html>")
        self.assertIs(verdict, SessionVerdict.INCONCLUSIVE)
        self.assertTrue(any("不做假设" in s for s in signals))

    def test_other_redirect_is_inconclusive(self) -> None:
        verdict, _ = session_verify.classify(301, "https://i.chaoxing.com/x", "")
        self.assertIs(verdict, SessionVerdict.INCONCLUSIVE)

    def test_none_status_is_unreachable(self) -> None:
        verdict, _ = session_verify.classify(None, "", "")
        self.assertIs(verdict, SessionVerdict.UNREACHABLE)

    def test_strip_query_removes_token(self) -> None:
        stripped = session_verify.strip_query(
            "https://passport2.chaoxing.com/login?token=SECRET123&x=1"
        )
        self.assertNotIn("SECRET123", stripped)
        self.assertEqual(stripped, "https://passport2.chaoxing.com/login")


class HeaderTests(unittest.TestCase):
    def test_headers_include_cookie_and_user_agent(self) -> None:
        headers = session_verify.build_headers(make_jar(2))
        self.assertIn("Cookie", headers)
        self.assertIn("k0=v0", headers["Cookie"])
        self.assertIn("Chrome", headers["User-Agent"])

    def test_no_cookie_header_when_empty(self) -> None:
        headers = session_verify.build_headers(CookieJar())
        self.assertNotIn("Cookie", headers)


class ProbeSessionTests(unittest.TestCase):
    def test_empty_jar_short_circuits_without_request(self) -> None:
        probe = session_verify.probe_session(CookieJar(), url="http://127.0.0.1:9/base")
        self.assertIs(probe.verdict, SessionVerdict.INVALID)
        self.assertEqual(probe.status, None)

    def test_valid_page(self) -> None:
        with MockPageServer(
            {"/base": (200, {"Content-Type": "text/html"}, "<div dataurl='x'></div>")}
        ) as server:
            probe = session_verify.probe_session(make_jar(), url=server.url, timeout_s=5)
            self.assertIs(probe.verdict, SessionVerdict.VALID)
            self.assertEqual(probe.status, 200)
            self.assertTrue(probe.body_size > 0)
            self.assertEqual(probe.cookie_count, 3)
            # cookie 真的被带上了
            self.assertIn("k0=v0", server.requests[0]["cookie"])

    def test_redirect_to_login_is_invalid_and_not_followed(self) -> None:
        with MockPageServer(
            {
                "/base": (
                    302,
                    {"Location": "https://passport2.chaoxing.com/login?token=LEAKME"},
                    "",
                )
            }
        ) as server:
            probe = session_verify.probe_session(make_jar(), url=server.url, timeout_s=5)
            self.assertIs(probe.verdict, SessionVerdict.INVALID)
            self.assertEqual(probe.status, 302)
            self.assertNotIn("LEAKME", probe.location, "重定向 URL 里的 token 不该外传")
            self.assertEqual(len(server.requests), 1, "不该跟随重定向再发一次请求")

    def test_unreachable_port(self) -> None:
        probe = session_verify.probe_session(
            make_jar(), url="http://127.0.0.1:9463/base", timeout_s=0.5
        )
        self.assertIs(probe.verdict, SessionVerdict.UNREACHABLE)
        self.assertTrue(probe.detail)

    def test_inconclusive_page(self) -> None:
        with MockPageServer(
            {"/base": (200, {"Content-Type": "text/html"}, "<html>nothing useful</html>")}
        ) as server:
            probe = session_verify.probe_session(make_jar(), url=server.url, timeout_s=5)
            self.assertIs(probe.verdict, SessionVerdict.INCONCLUSIVE)

    def test_probe_serialises(self) -> None:
        payload = session_verify.probe_session(
            make_jar(), url="http://127.0.0.1:9464/base", timeout_s=0.5
        ).to_dict()
        self.assertEqual(
            set(payload),
            {"verdict", "status", "final_url", "location", "body_size",
             "signals", "detail", "elapsed_ms", "cookie_count"},
        )


class NextActionTests(unittest.TestCase):
    def test_every_verdict_has_guidance_except_valid(self) -> None:
        self.assertEqual(
            session_verify.next_actions_for(
                session_verify.SessionProbe(SessionVerdict.VALID)
            ),
            [],
        )
        for verdict in (
            SessionVerdict.INVALID,
            SessionVerdict.INCONCLUSIVE,
            SessionVerdict.UNREACHABLE,
        ):
            with self.subTest(verdict=verdict):
                actions = session_verify.next_actions_for(
                    session_verify.SessionProbe(verdict)
                )
                self.assertTrue(actions)

    def test_inconclusive_guidance_explicitly_says_not_failure(self) -> None:
        actions = " ".join(
            session_verify.next_actions_for(
                session_verify.SessionProbe(SessionVerdict.INCONCLUSIVE)
            )
        )
        self.assertIn("不代表登录失败", actions)


class CookieLoginCommandTests(unittest.TestCase):
    def test_isolation_guard_blocks_login_flow(self) -> None:
        import tempfile
        import unittest.mock as mock
        from pathlib import Path as _Path

        from orchestrator import browser as browser_mod

        with tempfile.TemporaryDirectory() as tmp:
            fake_system = _Path(tmp) / "Edge" / "User Data"
            fake_system.mkdir(parents=True)
            with mock.patch.object(
                browser_mod, "system_profile_dirs", return_value=[fake_system]
            ), mock.patch.dict(
                "os.environ", {browser_mod.ENV_PROFILE: str(fake_system)}
            ), Sandbox() as sandbox:
                envelope = sandbox.run("cookies_login", no_open=True, timeout=0.1)
                self.assertFalse(envelope.ok)
                self.assertIn("隔离守卫拒绝", envelope.error["message"])

    def test_cdp_unreachable_fails_fast_instead_of_waiting(self) -> None:
        """CDP 不可达必须**立刻**失败，而不是等满 --timeout。

        这是本轮修掉的一个真实缺陷：原来即便浏览器压根没起来，命令也会
        傻等 300 秒（每次轮询都拿不到 cookie），最后报"登录超时"——
        而真实原因在第一秒就已经确定了。白等五分钟换一个误导性的错误，
        比直接说"浏览器没起来"糟糕得多。
        """
        from unittest import mock

        from orchestrator import browser as browser_mod

        with Sandbox() as sandbox:
            slept: list[float] = []
            sandbox.ctx.orchestrator.sleeper = slept.append
            with mock.patch.object(
                browser_mod, "probe_cdp",
                return_value=browser_mod.CdpStatus(alive=False, detail="test: 离线"),
            ):
                envelope = sandbox.run(
                    "cookies_login", no_open=True, timeout=300, poll=1
                )

            self.assertFalse(envelope.ok)
            self.assertEqual(envelope.error["code"], "CDP_UNAVAILABLE")
            self.assertEqual(envelope.error["category"], "transient")
            # 状态是 needs_manual_action：需要人去做一件事（把浏览器起起来）
            self.assertEqual(envelope.state, "needs_manual_action")
            # 关键断言：一次都没睡 —— 没有进入轮询
            self.assertEqual(slept, [], "CDP 不可达时不应进入轮询")

    def test_cdp_alive_but_no_login_still_times_out(self) -> None:
        """CDP 在线但迟迟不出现 cookie 增长 —— 这才是真正的"登录超时"。

        与上一条分开测：两种失败原因不同、错误码不同、给使用者的
        下一步也不同（去启动浏览器 vs 去登录）。
        """
        from unittest import mock

        from orchestrator import browser as browser_mod
        from orchestrator import cdp as cdp_mod

        with Sandbox() as sandbox:
            sandbox.ctx.orchestrator.sleeper = lambda _seconds: None
            # get_all_cookies 也必须 stub：开发者机器上真实 Edge 可能正
            # 占着 9333 且带有效 cookie，轮询会真的读到 cookie 增长。
            with mock.patch.object(
                browser_mod, "probe_cdp",
                return_value=browser_mod.CdpStatus(alive=True, browser="Edg/test"),
            ), mock.patch.object(
                cdp_mod, "get_all_cookies", return_value=[]
            ):
                envelope = sandbox.run(
                    "cookies_login", no_open=True, timeout=0.2, poll=0.5, min_delta=3
                )

            self.assertFalse(envelope.ok)
            self.assertEqual(envelope.state, "needs_manual_action")
            self.assertEqual(envelope.error["code"], "LOGIN_TIMEOUT")
            self.assertEqual(envelope.error["category"], "auth")
            joined = " ".join(envelope.next_actions)
            self.assertIn("独立浏览器窗口", joined)


class CookieVerifyCommandTests(unittest.TestCase):
    def test_without_cookies_reports_auth_error(self) -> None:
        with Sandbox() as sandbox:
            envelope = sandbox.run("cookies_verify")
            self.assertFalse(envelope.ok)
            self.assertEqual(envelope.error["category"], "auth")
            self.assertTrue(any("cookies_login" in a for a in envelope.next_actions))

    def test_valid_session(self) -> None:
        with MockPageServer(
            {"/base": (200, {"Content-Type": "text/html"}, "<div dataurl='x'></div>")}
        ) as server:
            with Sandbox() as sandbox:
                sandbox.run("cookies_import", header="k0=v0; k1=v1")
                envelope = sandbox.run("cookies_verify", url=server.url, timeout=5)
                self.assertTrue(envelope.ok)
                self.assertEqual(envelope.data["verdict"], "valid")
                self.assertIn("list_courses", " ".join(envelope.next_actions))

    def test_invalid_session_points_to_relogin(self) -> None:
        with MockPageServer(
            {"/base": (302, {"Location": "https://passport2.chaoxing.com/fanyalogin"}, "")}
        ) as server:
            with Sandbox() as sandbox:
                sandbox.run("cookies_import", header="k0=v0")
                envelope = sandbox.run("cookies_verify", url=server.url, timeout=5)
                self.assertFalse(envelope.ok)
                self.assertEqual(envelope.state, "needs_manual_action")
                self.assertEqual(envelope.error["code"], "SESSION_INVALID")
                joined = " ".join(envelope.next_actions)
                self.assertIn("cookies_login", joined)
                self.assertIn("不会碰你的日常浏览器", joined)

    def test_inconclusive_is_not_reported_as_failure_of_login(self) -> None:
        with MockPageServer(
            {"/base": (200, {"Content-Type": "text/html"}, "<html>changed</html>")}
        ) as server:
            with Sandbox() as sandbox:
                sandbox.run("cookies_import", header="k0=v0")
                envelope = sandbox.run("cookies_verify", url=server.url, timeout=5)
                self.assertFalse(envelope.ok)
                self.assertEqual(envelope.error["code"], "SESSION_UNCONFIRMED")
                self.assertEqual(envelope.error["category"], "platform_changed")
                self.assertTrue(any("不是「失败」" in w for w in envelope.warnings))

    def test_cookie_values_never_appear_in_verify_output(self) -> None:
        secret = "TOPSECRETCOOKIEVALUE"
        with MockPageServer(
            {"/base": (200, {"Content-Type": "text/html"}, "<div dataurl='x'></div>")}
        ) as server:
            with Sandbox() as sandbox:
                sandbox.run("cookies_import", header=f"k0={secret}")
                envelope = sandbox.run("cookies_verify", url=server.url, timeout=5)
                blob = json.dumps(envelope.to_dict(), ensure_ascii=False)
                self.assertNotIn(secret, blob)
                logs = (sandbox.ctx.run_dir / f"{envelope.request_id}.jsonl").read_text(
                    encoding="utf-8"
                )
                self.assertNotIn(secret, logs)


if __name__ == "__main__":
    unittest.main()
