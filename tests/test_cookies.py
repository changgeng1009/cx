"""Cookie 规范化、格式导出与落盘（含凭据不外泄校验）。"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from orchestrator.cookies import (
    CHAOXING_DOMAIN_SUFFIXES,
    Cookie,
    CookieJar,
    CookieStore,
    domain_matches,
)

from .helpers import Sandbox

#: 假的会话 cookie，形状参考真实值但绝不是真凭据
SAMPLE = [
    {"name": "_uid", "value": "1234567", "domain": "i.chaoxing.com", "path": "/",
     "expires": -1, "httpOnly": True, "secure": True, "sameSite": "None"},
    {"name": "fid", "value": "7213", "domain": ".chaoxing.com", "path": "/",
     "expires": -1, "httpOnly": False, "secure": False, "sameSite": ""},
    {"name": "_d", "value": "1760000000", "domain": ".chaoxing.com", "path": "/",
     "expires": time.time() + 86400, "httpOnly": False, "secure": False},
    {"name": "SESSION", "value": "abcdef0123456789", "domain": "example.com", "path": "/",
     "expires": time.time() + 86400, "httpOnly": True, "secure": True},
]


class DomainMatchTests(unittest.TestCase):
    def test_matches_exact_and_subdomain(self) -> None:
        self.assertTrue(domain_matches("chaoxing.com", "chaoxing.com"))
        self.assertTrue(domain_matches("i.chaoxing.com", "chaoxing.com"))
        self.assertTrue(domain_matches(".chaoxing.com", "chaoxing.com"))

    def test_does_not_match_lookalike(self) -> None:
        self.assertFalse(domain_matches("notchaoxing.com", "chaoxing.com"))
        self.assertFalse(domain_matches("chaoxing.com.evil.test", "chaoxing.com"))

    def test_case_insensitive(self) -> None:
        self.assertTrue(domain_matches("I.ChaoXing.COM", "chaoxing.com"))


class CookieTests(unittest.TestCase):
    def test_session_and_expiry(self) -> None:
        session = Cookie(name="a", value="b", expires=-1)
        self.assertTrue(session.is_session)
        self.assertFalse(session.is_expired())

        past = Cookie(name="a", value="b", expires=time.time() - 10)
        self.assertFalse(past.is_session)
        self.assertTrue(past.is_expired())

    def test_masked_value_never_shows_full_secret(self) -> None:
        short = Cookie(name="n", value="abcd")
        self.assertNotIn("abcd", short.masked_value())

        long = Cookie(name="n", value="0123456789abcdef")
        masked = long.masked_value()
        self.assertNotIn("0123456789abcdef", masked)
        self.assertTrue(masked.startswith("0123"))

    def test_netscape_line_format(self) -> None:
        cookie = Cookie(
            name="_uid", value="1234567", domain=".chaoxing.com",
            path="/", expires=-1, secure=False, http_only=True,
        )
        line = cookie.to_netscape_line()
        parts = line.split("\t")
        self.assertEqual(len(parts), 7)
        self.assertTrue(line.startswith("#HttpOnly_.chaoxing.com"))
        self.assertEqual(parts[1], "TRUE", "域名以 . 开头应标记包含子域")
        self.assertEqual(parts[4], "0", "会话 cookie 的 expires 应为 0")

    def test_netscape_line_non_httponly_has_no_prefix(self) -> None:
        cookie = Cookie(name="a", value="b", domain="i.chaoxing.com")
        self.assertFalse(cookie.to_netscape_line().startswith("#"))

    def test_roundtrip_through_dict(self) -> None:
        cookie = Cookie.from_dict(
            {"name": "x", "value": "y", "domain": "d", "httpOnly": True, "expires": 123.0}
        )
        restored = Cookie.from_dict(cookie.to_dict())
        self.assertEqual(restored, cookie)

    def test_missing_expires_becomes_session(self) -> None:
        self.assertTrue(Cookie.from_dict({"name": "x", "value": "y"}).is_session)
        self.assertTrue(
            Cookie.from_dict({"name": "x", "value": "y", "expires": "bad"}).is_session
        )


class CookieJarTests(unittest.TestCase):
    def test_from_cdp_and_filter(self) -> None:
        jar = CookieJar.from_cdp(SAMPLE)
        self.assertEqual(len(jar), 4)
        self.assertEqual(len(jar.filter_domains()), 3, "应剔除 example.com")
        self.assertEqual(len(jar.filter_domains(["example.com"])), 1)

    def test_add_dedupes_by_domain_path_name(self) -> None:
        jar = CookieJar()
        jar.add(Cookie(name="a", value="1", domain="d", path="/"))
        jar.add(Cookie(name="a", value="2", domain="d", path="/"))
        self.assertEqual(len(jar), 1)
        self.assertEqual(jar.cookies[0].value, "2", "后者应覆盖前者")

    def test_without_expired(self) -> None:
        jar = CookieJar.from_cdp(SAMPLE)
        jar.add(Cookie(name="old", value="x", domain="d", expires=time.time() - 100))
        self.assertEqual(len(jar.without_expired()), 4)

    def test_to_header_only_emits_name_value_pairs(self) -> None:
        header = CookieJar.from_cdp(SAMPLE).to_header(CHAOXING_DOMAIN_SUFFIXES)
        self.assertIn("_uid=1234567", header)
        self.assertIn("fid=7213", header)
        self.assertNotIn("SESSION", header, "非学习通域名不该进入请求头")
        self.assertNotIn("domain", header)

    def test_json_roundtrip_preserves_values(self) -> None:
        jar = CookieJar.from_cdp(SAMPLE)
        jar.account_id = "acc_01"
        restored = CookieJar.from_json(jar.to_json())
        self.assertEqual(len(restored), len(jar))
        self.assertEqual(restored.account_id, "acc_01")
        self.assertEqual(
            {c.name for c in restored}, {c.name for c in jar}
        )

    def test_from_header_parses_loose_input(self) -> None:
        jar = CookieJar.from_header("UID=abc; _d=def;  bad ;=empty; token=xyz")
        names = [c.name for c in jar]
        self.assertIn("UID", names)
        self.assertIn("token", names)
        self.assertNotIn("bad", names)
        self.assertNotIn("", names)

    def test_netscape_roundtrip(self) -> None:
        jar = CookieJar.from_cdp(SAMPLE)
        text = jar.to_netscape()
        restored = CookieJar.from_netscape(text)
        self.assertEqual(len(restored), len(jar))
        by_name = {c.name: c for c in restored}
        self.assertTrue(by_name["_uid"].http_only, "HttpOnly 前缀应被解析回来")
        self.assertTrue(by_name["_uid"].is_session, "expires=0 应还原成会话 cookie")
        self.assertTrue(by_name["_d"].is_expired() is False)
        self.assertEqual(by_name["fid"].domain, ".chaoxing.com")

    def test_netscape_skips_pure_comments(self) -> None:
        text = "# Netscape HTTP Cookie File\n# a comment\n\ni.chaoxing.com\tFALSE\t/\tFALSE\t0\tk\tv\n"
        jar = CookieJar.from_netscape(text)
        self.assertEqual(len(jar), 1)
        self.assertEqual(jar.cookies[0].name, "k")

    def test_diagnose_reports_expiry_and_domains(self) -> None:
        jar = CookieJar.from_cdp(SAMPLE)
        jar.add(Cookie(name="old", value="x", domain="i.chaoxing.com", expires=1.0))
        report = jar.diagnose()
        self.assertEqual(report["total"], 5)
        self.assertEqual(report["chaoxing_related"], 4)
        self.assertEqual(report["expired"], 1)
        self.assertIn("old", report["expired_names"])
        self.assertIn("i.chaoxing.com", report["domains"])


class CookieStoreTests(unittest.TestCase):
    def test_save_writes_three_formats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CookieStore(Path(tmp))
            result = store.save("acc_01", CookieJar.from_cdp(SAMPLE))
            self.assertEqual(result["kept"], 3)
            self.assertEqual(result["dropped"], 1)
            for key in ("json", "netscape", "header"):
                self.assertTrue(Path(result["files"][key]).is_file(), f"缺少 {key}")

    def test_roundtrip_and_clear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CookieStore(Path(tmp))
            store.save("acc_01", CookieJar.from_cdp(SAMPLE))
            self.assertTrue(store.has("acc_01"))
            loaded = store.load("acc_01")
            self.assertIsNotNone(loaded)
            self.assertEqual(len(loaded), 3)
            self.assertEqual(loaded.account_id, "acc_01")

            removed = store.clear("acc_01")
            self.assertEqual(len(removed), 3)
            self.assertFalse(store.has("acc_01"))

    def test_meta_without_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CookieStore(Path(tmp))
            self.assertFalse(store.meta("acc_none")["present"])

    def test_keep_all_preserves_other_domains(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CookieStore(Path(tmp))
            result = store.save("acc_01", CookieJar.from_cdp(SAMPLE), only_chaoxing=False)
            self.assertEqual(result["kept"], 4)


class CookieCommandTests(unittest.TestCase):
    def test_status_before_anything_saved(self) -> None:
        with Sandbox() as sandbox:
            envelope = sandbox.run("cookies")
            self.assertTrue(envelope.ok)
            self.assertFalse(envelope.data["present"])

    def test_import_from_header_string(self) -> None:
        header = "UID=1234567; _d=1760000000; fid=7213"
        with Sandbox() as sandbox:
            envelope = sandbox.run("cookies_import", header=header, source="test")
            self.assertTrue(envelope.ok)
            self.assertEqual(envelope.data["kept"], 3)

            status = sandbox.run("cookies")
            self.assertTrue(status.data["present"])
            self.assertEqual(status.data["total"], 3)

    def test_import_without_input_is_clear_error(self) -> None:
        with Sandbox() as sandbox:
            envelope = sandbox.run("cookies_import")
            self.assertFalse(envelope.ok)
            self.assertEqual(envelope.error["category"], "input")
            self.assertTrue(any("--header" in a for a in envelope.next_actions))

    def test_header_import_stamps_default_domain(self) -> None:
        """header 串没有域名信息，补默认域名而不是丢弃。

        理由：手动粘贴是最省事、零依赖的导入方式。若因为"域名不匹配"
        把 cookie 全丢掉，这条路就废了。域名不可知时保留才是对的。
        """
        with Sandbox() as sandbox:
            envelope = sandbox.run("cookies_import", header="UID=1; _d=2")
            self.assertTrue(envelope.ok)
            self.assertEqual(envelope.data["kept"], 2)
            netscape = Path(envelope.data["files"]["netscape"]).read_text(encoding="utf-8")
            self.assertIn(".chaoxing.com", netscape)

    def test_import_domain_override(self) -> None:
        with Sandbox() as sandbox:
            envelope = sandbox.run(
                "cookies_import", header="UID=1", domain=".custom.test"
            )
            self.assertTrue(envelope.ok)
            netscape = Path(envelope.data["files"]["netscape"]).read_text(encoding="utf-8")
            self.assertIn(".custom.test", netscape)

    def test_structured_import_drops_foreign_domains(self) -> None:
        """域名已知时（CDP / Netscape / JSON）才做过滤。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.txt"
            path.write_text(
                "# Netscape HTTP Cookie File\n"
                ".chaoxing.com\tTRUE\t/\tFALSE\t0\tfid\t7213\n"
                "example.com\tFALSE\t/\tFALSE\t0\tSESSION\tabc\n",
                encoding="utf-8",
            )
            with Sandbox() as sandbox:
                envelope = sandbox.run("cookies_import", file=str(path))
                self.assertTrue(envelope.ok)
                self.assertEqual(envelope.data["kept"], 1)
                self.assertEqual(envelope.data["dropped"], 1)

    def test_keep_all_preserves_foreign_domains(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.txt"
            path.write_text(
                "# Netscape HTTP Cookie File\n"
                "example.com\tFALSE\t/\tFALSE\t0\tSESSION\tabc\n",
                encoding="utf-8",
            )
            with Sandbox() as sandbox:
                envelope = sandbox.run("cookies_import", file=str(path), keep_all=True)
                self.assertEqual(envelope.data["kept"], 1)
                self.assertEqual(envelope.data["dropped"], 0)

    def test_import_from_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.txt"
            path.write_text(
                "# Netscape HTTP Cookie File\n"
                ".chaoxing.com\tTRUE\t/\tFALSE\t0\tfid\t7213\n",
                encoding="utf-8",
            )
            with Sandbox() as sandbox:
                envelope = sandbox.run("cookies_import", file=str(path))
                self.assertTrue(envelope.ok)
                self.assertEqual(envelope.data["kept"], 1)

    def test_import_missing_file_reports_path(self) -> None:
        with Sandbox() as sandbox:
            envelope = sandbox.run("cookies_import", file="C:/nope/cookies.txt")
            self.assertFalse(envelope.ok)
            self.assertIn("文件不存在", envelope.error["message"])

    def test_clear(self) -> None:
        with Sandbox() as sandbox:
            sandbox.run("cookies_import", header="UID=1; fid=2")
            cleared = sandbox.run("cookies_clear")
            self.assertTrue(cleared.ok)
            self.assertEqual(cleared.data["count"], 3)
            self.assertFalse(sandbox.run("cookies").data["present"])

    def test_extract_without_browser_gives_actionable_error(self) -> None:
        with Sandbox() as sandbox:
            envelope = sandbox.run("cookies_extract", port=9461)
            self.assertFalse(envelope.ok)
            self.assertEqual(envelope.error["category"], "not_supported")
            joined = " ".join(envelope.next_actions)
            self.assertIn("orchestrator.browser --launch", joined)
            self.assertIn("独立窗口里登录", joined)
            self.assertIn("9461", joined)

    def test_extract_rejects_non_isolated_profile(self) -> None:
        """提取只能针对项目内独立实例，不能指向系统默认 profile。"""
        with tempfile.TemporaryDirectory() as tmp:
            fake_system = Path(tmp) / "Edge" / "User Data"
            fake_system.mkdir(parents=True)
            import unittest.mock as mock

            from orchestrator import browser as browser_mod

            with mock.patch.object(
                browser_mod, "system_profile_dirs", return_value=[fake_system]
            ), mock.patch.dict(
                "os.environ", {browser_mod.ENV_PROFILE: str(fake_system)}
            ), Sandbox() as sandbox:
                envelope = sandbox.run("cookies_extract")
                self.assertFalse(envelope.ok)
                self.assertIn("隔离守卫拒绝", envelope.error["message"])


class CookieLeakTests(unittest.TestCase):
    """cookie 字符串整串就是凭据，绝不能进日志（V9）。"""

    def test_cookie_header_never_reaches_logs(self) -> None:
        secret = "UID=SUPERSECRETVALUE; _d=ALSOSECRET"
        with Sandbox() as sandbox:
            envelope = sandbox.run("cookies_import", header=secret)
            blob = (sandbox.ctx.run_dir / f"{envelope.request_id}.jsonl").read_text(
                encoding="utf-8"
            )
            self.assertNotIn("SUPERSECRETVALUE", blob)
            self.assertNotIn("ALSOSECRET", blob)

    def test_cookie_file_values_never_echoed_in_envelope(self) -> None:
        secret = "SECRETCOOKIEVALUE123"
        with Sandbox() as sandbox:
            envelope = sandbox.run("cookies_import", header=f"UID={secret}")
            payload = json.dumps(envelope.to_dict(), ensure_ascii=False)
            self.assertNotIn(secret, payload)

    def test_status_report_masks_values(self) -> None:
        with Sandbox() as sandbox:
            sandbox.run("cookies_import", header="UID=SECRETVALUE9999")
            status = json.dumps(sandbox.run("cookies").to_dict(), ensure_ascii=False)
            self.assertNotIn("SECRETVALUE9999", status)


if __name__ == "__main__":
    unittest.main()
