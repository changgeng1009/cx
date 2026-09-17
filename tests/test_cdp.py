"""CDP 客户端与 WebSocket 实现测试。

`cdp.py` 里的 WebSocket 是手写的（stdlib 没有 WS 客户端），所以必须用
一个**真实的** WS 服务端来验证帧编解码，而不是只做逻辑断言。

这里搭一个 mock CDP：HTTP 提供 `/json/list`，另起一个裸 socket 做
WebSocket 端点。这样 `get_all_cookies()` 能在没有浏览器的前提下完整跑通。
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import socket
import struct
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from orchestrator import cdp
from orchestrator.cdp import (
    CdpClient,
    CdpError,
    MiniWebSocket,
    WebSocketError,
    get_all_cookies,
    pick_page_target,
)

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# --------------------------------------------------------------------------
# mock 服务端
# --------------------------------------------------------------------------


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise ConnectionError("closed")
        data += chunk
    return data


def _read_client_frame(sock: socket.socket) -> tuple[int, bytes] | None:
    """读一帧客户端消息（客户端帧必须带掩码）。"""
    try:
        first, second = _recv_exact(sock, 2)
    except ConnectionError:
        return None
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        (length,) = struct.unpack("!H", _recv_exact(sock, 2))
    elif length == 127:
        (length,) = struct.unpack("!Q", _recv_exact(sock, 8))
    mask = _recv_exact(sock, 4) if masked else b"\x00\x00\x00\x00"
    payload = _recv_exact(sock, length) if length else b""
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload


def _send_server_frame(sock: socket.socket, text: str) -> None:
    """服务端帧不加掩码。"""
    payload = text.encode("utf-8")
    header = bytearray([0x80 | 0x1])
    length = len(payload)
    if length < 126:
        header.append(length)
    elif length < 65536:
        header.append(126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(127)
        header.extend(struct.pack("!Q", length))
    sock.sendall(bytes(header) + payload)


class MockCdpServer:
    """一个够用的假 CDP 服务端。"""

    def __init__(
        self,
        cookies: list[dict[str, Any]] | None = None,
        *,
        send_event_first: bool = False,
        reject_handshake: bool = False,
        page_url: str = "https://i.chaoxing.com/",
        extra_targets: list[dict[str, Any]] | None = None,
        on_method: Callable[[str, dict[str, Any]], dict[str, Any] | None] | None = None,
    ) -> None:
        self.cookies = cookies if cookies is not None else []
        self.send_event_first = send_event_first
        self.reject_handshake = reject_handshake
        self.page_url = page_url
        self.extra_targets = extra_targets or []
        self.on_method = on_method
        self.received: list[dict[str, Any]] = []

        self._ws_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._ws_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._ws_sock.bind(("127.0.0.1", 0))
        self._ws_sock.listen(4)
        self.ws_port = self._ws_sock.getsockname()[1]

        self._http = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self.http_port = self._http.server_address[1]

        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()

    # ------------------------------------------------------------------
    def _targets(self) -> list[dict[str, Any]]:
        targets = [
            {
                "id": "devtools-1",
                "type": "page",
                "url": "devtools://devtools/bundled/inspector.html",
                "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.ws_port}/devtools/page/devtools-1",
            },
            {
                "id": "page-1",
                "type": "page",
                "url": self.page_url,
                "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.ws_port}/devtools/page/page-1",
            },
            {
                "id": "worker-1",
                "type": "service_worker",
                "url": "https://i.chaoxing.com/sw.js",
                "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.ws_port}/devtools/page/worker-1",
            },
        ]
        targets.extend(self.extra_targets)
        return targets

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # 静音
                pass

            def do_GET(self) -> None:  # noqa: N802
                if self.path.startswith("/json/list"):
                    body = json.dumps(server._targets()).encode("utf-8")
                elif self.path.startswith("/json/version"):
                    body = json.dumps(
                        {"Browser": "MockEdge/1.0", "Protocol-Version": "1.3"}
                    ).encode("utf-8")
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler

    # ------------------------------------------------------------------
    def _serve_ws(self, conn: socket.socket) -> None:
        try:
            header = b""
            while b"\r\n\r\n" not in header:
                byte = conn.recv(1)
                if not byte:
                    return
                header += byte
            text = header.decode("latin-1", errors="replace")

            if self.reject_handshake:
                conn.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                return

            match = re.search(r"Sec-WebSocket-Key:\s*(\S+)", text, re.I)
            if not match:
                return
            accept = base64.b64encode(
                hashlib.sha1((match.group(1) + WS_GUID).encode()).digest()
            ).decode()
            conn.sendall(
                (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                ).encode()
            )

            while not self._stop.is_set():
                frame = _read_client_frame(conn)
                if frame is None:
                    break
                opcode, payload = frame
                if opcode == 0x8:
                    break
                if opcode != 0x1:
                    continue
                message = json.loads(payload.decode("utf-8"))
                self.received.append(message)

                if self.send_event_first:
                    _send_server_frame(
                        conn,
                        json.dumps({"method": "Network.requestWillBeSent", "params": {}}),
                    )

                response = self._respond(message)
                if response is not None:
                    _send_server_frame(conn, json.dumps(response))
        except (ConnectionError, OSError, ValueError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _respond(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = str(message.get("method", ""))
        if self.on_method is not None:
            custom = self.on_method(method, message)
            if custom is not None:
                return {"id": message.get("id"), **custom}
        if method == "Network.getAllCookies":
            return {"id": message.get("id"), "result": {"cookies": self.cookies}}
        return {"id": message.get("id"), "result": {}}

    # ------------------------------------------------------------------
    def start(self) -> "MockCdpServer":
        acceptor = threading.Thread(target=self._accept_loop, daemon=True)
        acceptor.start()
        self._threads.append(acceptor)
        http_thread = threading.Thread(target=self._http.serve_forever, daemon=True)
        http_thread.start()
        self._threads.append(http_thread)
        time.sleep(0.05)
        return self

    def _accept_loop(self) -> None:
        self._ws_sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self._ws_sock.accept()
            except (socket.timeout, OSError):
                continue
            thread = threading.Thread(
                target=self._serve_ws, args=(conn,), daemon=True
            )
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        try:
            self._http.shutdown()
            self._http.server_close()
        except OSError:
            pass
        try:
            self._ws_sock.close()
        except OSError:
            pass

    def __enter__(self) -> "MockCdpServer":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


def sample_cookies(count: int = 3, big: bool = False) -> list[dict[str, Any]]:
    base = [
        {"name": "_uid", "value": "1234567", "domain": "i.chaoxing.com", "path": "/",
         "expires": -1, "httpOnly": True, "secure": True, "sameSite": "None"},
        {"name": "fid", "value": "7213", "domain": ".chaoxing.com", "path": "/",
         "expires": -1, "httpOnly": False, "secure": False, "sameSite": ""},
        {"name": "_d", "value": "1760000000", "domain": ".chaoxing.com", "path": "/",
         "expires": time.time() + 86400, "httpOnly": False, "secure": False},
    ]
    if big:
        base.append(
            {"name": "big", "value": "x" * 70000, "domain": ".chaoxing.com", "path": "/"}
        )
    return base[:count] if not big else base


# --------------------------------------------------------------------------
# WebSocket 帧编解码
# --------------------------------------------------------------------------


class WebSocketFramingTests(unittest.TestCase):
    def test_handshake_and_text_roundtrip(self) -> None:
        """客户端发文本 → 服务端收到（掩码正确）；服务端回文本 → 客户端收到。"""
        received: list[str] = []

        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.bind(("127.0.0.1", 0))
        server_sock.listen(1)
        port = server_sock.getsockname()[1]

        def server() -> None:
            conn, _ = server_sock.accept()
            header = b""
            while b"\r\n\r\n" not in header:
                header += conn.recv(1)
            key = re.search(r"Sec-WebSocket-Key:\s*(\S+)", header.decode("latin-1")).group(1)
            accept = base64.b64encode(
                hashlib.sha1((key + WS_GUID).encode()).digest()
            ).decode()
            conn.sendall(
                (
                    "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                    f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n"
                ).encode()
            )
            frame = _read_client_frame(conn)
            assert frame is not None
            opcode, payload = frame
            self.assertEqual(opcode, 0x1)
            received.append(payload.decode("utf-8"))
            _send_server_frame(conn, "pong-from-server")
            conn.close()
            server_sock.close()

        thread = threading.Thread(target=server, daemon=True)
        thread.start()

        with MiniWebSocket("127.0.0.1", port, "/x", timeout_s=5) as ws:
            ws.send_text("hello-cdp")
            reply = ws.recv_message()

        thread.join(timeout=5)
        self.assertEqual(received, ["hello-cdp"])
        self.assertEqual(reply, "pong-from-server")

    def test_rejects_non_101_handshake(self) -> None:
        with MockCdpServer(reject_handshake=True) as server:
            with self.assertRaises(WebSocketError) as ctx:
                MiniWebSocket("127.0.0.1", server.ws_port, "/x", timeout_s=3).connect()
            self.assertIn("握手失败", str(ctx.exception))

    def test_bad_accept_header_is_rejected(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]

        def server() -> None:
            conn, _ = sock.accept()
            header = b""
            while b"\r\n\r\n" not in header:
                header += conn.recv(1)
            conn.sendall(
                b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                b"Connection: Upgrade\r\nSec-WebSocket-Accept: wrong-value\r\n\r\n"
            )
            conn.close()
            sock.close()

        thread = threading.Thread(target=server, daemon=True)
        thread.start()
        with self.assertRaises(WebSocketError) as ctx:
            MiniWebSocket("127.0.0.1", port, "/x", timeout_s=3).connect()
        thread.join(timeout=5)
        self.assertIn("Sec-WebSocket-Accept", str(ctx.exception))


# --------------------------------------------------------------------------
# CDP 会话
# --------------------------------------------------------------------------


class CdpClientTests(unittest.TestCase):
    def test_ws_url_parsing(self) -> None:
        client = CdpClient("ws://127.0.0.1:9333/devtools/page/abc")
        self.assertEqual(client.host_port_path, ("127.0.0.1", 9333, "/devtools/page/abc"))

    def test_non_ws_url_rejected(self) -> None:
        with self.assertRaises(CdpError):
            CdpClient("https://example.com").host_port_path

    def test_call_skips_event_messages(self) -> None:
        """CDP 会先推事件再回响应，必须按 id 匹配，不能拿到第一条就当结果。"""
        with MockCdpServer(sample_cookies(), send_event_first=True) as server:
            target = pick_page_target(server.http_port)
            with CdpClient(str(target["webSocketDebuggerUrl"]), timeout_s=5) as client:
                result = client.call("Network.getAllCookies")
            self.assertEqual(len(result["cookies"]), 3)

    def test_error_response_raises_cdp_error(self) -> None:
        def on_method(method: str, message: dict[str, Any]) -> dict[str, Any] | None:
            if method == "Network.getAllCookies":
                return {"error": {"code": -32000, "message": "not allowed"}}
            return None

        with MockCdpServer(on_method=on_method) as server:
            target = pick_page_target(server.http_port)
            with CdpClient(str(target["webSocketDebuggerUrl"]), timeout_s=5) as client:
                with self.assertRaises(CdpError) as ctx:
                    client.call("Network.getAllCookies")
            self.assertIn("not allowed", str(ctx.exception))


class TargetSelectionTests(unittest.TestCase):
    def test_prefers_real_page_over_devtools(self) -> None:
        with MockCdpServer() as server:
            target = pick_page_target(server.http_port)
            self.assertEqual(target["id"], "page-1")

    def test_falls_back_when_only_devtools_page_exists(self) -> None:
        with MockCdpServer(
            extra_targets=[
                {
                    "id": "only-devtools",
                    "type": "page",
                    "url": "devtools://devtools/bundled/x.html",
                    "webSocketDebuggerUrl": "ws://127.0.0.1:9/devtools/page/only-devtools",
                }
            ]
        ) as server:
            targets = cdp.list_targets(server.http_port)
            self.assertTrue(any(t["id"] == "only-devtools" for t in targets))

    def test_unreachable_port_raises_clear_error(self) -> None:
        with self.assertRaises(CdpError) as ctx:
            cdp.list_targets(9462, timeout_s=0.4)
        self.assertIn("无法访问", str(ctx.exception))


class GetAllCookiesTests(unittest.TestCase):
    def test_full_roundtrip_returns_plaintext_cookies(self) -> None:
        with MockCdpServer(sample_cookies()) as server:
            cookies = get_all_cookies(server.http_port, timeout_s=5)
            self.assertEqual(len(cookies), 3)
            by_name = {c["name"]: c for c in cookies}
            self.assertEqual(by_name["_uid"]["value"], "1234567")
            self.assertTrue(by_name["_uid"]["httpOnly"])

    def test_large_payload_uses_extended_length_header(self) -> None:
        """70000 字符的 cookie 会走 64 位长度分支，必须能正确解析。"""
        with MockCdpServer(sample_cookies(big=True)) as server:
            cookies = get_all_cookies(server.http_port, timeout_s=10)
            big = next(c for c in cookies if c["name"] == "big")
            self.assertEqual(len(big["value"]), 70000)

    def test_non_list_result_rejected(self) -> None:
        def on_method(method: str, message: dict[str, Any]) -> dict[str, Any] | None:
            if method == "Network.getAllCookies":
                return {"result": {"cookies": "not-a-list"}}
            return None

        with MockCdpServer(on_method=on_method) as server:
            with self.assertRaises(CdpError):
                get_all_cookies(server.http_port, timeout_s=5)


if __name__ == "__main__":
    unittest.main()
