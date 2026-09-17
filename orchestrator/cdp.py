"""最小 CDP（Chrome DevTools Protocol）客户端 —— 零第三方依赖。

## 为什么需要它

要拿到 cookie，有两条路：

1. **进程外直读 profile 的 SQLite 数据库**，自己解密。
2. **通过 CDP 问浏览器要**。

路 1 在新版浏览器上已经走不通。实测（2026-09-17，本机）：
Chrome 152.0.7977.83 与 Edge 153.0.4234.32 的 `Local State` 里都存在
`app_bound_encrypted_key`，即启用了 **App-Bound Encryption（ABE）**。
ABE 把密钥封给了浏览器自身的服务，**任何外部进程都拿不到解密能力**——
这是 2024 年起 Chrome/Edge 的既定行为，不是配置问题。

路 2 完全绕开加密：**让浏览器自己解密**，我们只做它的调试客户端。
CDP 返回的 cookie 就是明文。这也顺带说明为什么必须用独立 profile——
136+ 起默认 profile 根本不允许开远程调试（见 `browser.py`）。

## 为什么手写 WebSocket

CDP 的命令通道是 WebSocket，而 stdlib 没有 WS 客户端。引入第三方库会破坏
本项目的零依赖约束（见 `docs/03` §10）。所以这里实现一个**只够用**的
RFC 6455 客户端：仅 `ws://`、仅文本帧、不做压缩协商、不做 TLS。

服务端→客户端：不掩码；客户端→服务端：必须掩码。这两点都按规范实现，
因为 CDP 服务端会校验掩码位。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterable

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BIN = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

MAX_MESSAGE_BYTES = 8 * 1024 * 1024


class WebSocketError(RuntimeError):
    pass


class CdpError(RuntimeError):
    """CDP 返回了 error 字段，或端点不可用。"""


# --------------------------------------------------------------------------
# 低层：最小 WebSocket 客户端
# --------------------------------------------------------------------------


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        chunk = sock.recv(min(remaining, 65536))
        if not chunk:
            raise WebSocketError("连接在读取帧时被关闭")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


@dataclass
class MiniWebSocket:
    """只够 CDP 用的 WebSocket 客户端。"""

    host: str
    port: int
    path: str = "/"
    timeout_s: float = 10.0
    _sock: socket.socket | None = field(default=None, repr=False)
    _buffer: list[bytes] = field(default_factory=list, repr=False)

    # ------------------------------------------------------------------
    def connect(self) -> "MiniWebSocket":
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
        # 握手期间任何失败都必须关掉 socket：连到一个"接受 TCP 但不是
        # WebSocket"的端口是很常见的情形（端口被别的服务占着），
        # 漏掉这里的关闭会让每次失败都泄漏一个句柄。
        try:
            sock.settimeout(self.timeout_s)
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            handshake = (
                f"GET {self.path} HTTP/1.1\r\n"
                f"Host: {self.host}:{self.port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                "\r\n"
            )
            sock.sendall(handshake.encode("ascii"))

            # 逐字节读头部，避免把紧随其后的帧数据一起读进缓冲区
            header = bytearray()
            while b"\r\n\r\n" not in header:
                byte = sock.recv(1)
                if not byte:
                    raise WebSocketError("握手过程中连接被关闭")
                header.extend(byte)
                if len(header) > 16384:
                    raise WebSocketError("握手响应头过长")

            text = header.decode("latin-1")
            status_line = text.split("\r\n", 1)[0]
            if "101" not in status_line:
                raise WebSocketError(f"WebSocket 握手失败：{status_line}")

            expected = base64.b64encode(
                hashlib.sha1((key + WS_GUID).encode("ascii")).digest()
            ).decode("ascii")
            if f"Sec-WebSocket-Accept: {expected}".lower() not in text.lower():
                raise WebSocketError("WebSocket 握手响应缺少正确的 Sec-WebSocket-Accept")
        except BaseException:
            try:
                sock.close()
            except OSError:
                pass
            raise

        self._sock = sock
        return self

    # ------------------------------------------------------------------
    def send_text(self, payload: str) -> None:
        self._send_frame(OP_TEXT, payload.encode("utf-8"))

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self._sock is None:
            raise WebSocketError("未连接")
        header = bytearray()
        header.append(0x80 | opcode)  # FIN + opcode

        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))

        # 客户端→服务端必须掩码
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        self._sock.sendall(bytes(header) + masked)

    def recv_message(self) -> str:
        """读一条完整消息（自动拼接 continuation 帧、回应 ping）。"""
        data = bytearray()
        opcode: int | None = None
        total = 0

        while True:
            fin, frame_opcode, payload = self._read_frame()
            total += len(payload)
            if total > MAX_MESSAGE_BYTES:
                raise WebSocketError("单条消息超过上限")

            if frame_opcode == OP_PING:
                self._send_frame(OP_PONG, payload)
                continue
            if frame_opcode == OP_PONG:
                continue
            if frame_opcode == OP_CLOSE:
                raise WebSocketError("服务端关闭了 WebSocket")
            if frame_opcode in (OP_TEXT, OP_BIN):
                opcode = frame_opcode
                data.extend(payload)
            elif frame_opcode == OP_CONT:
                data.extend(payload)
            else:
                raise WebSocketError(f"未知 opcode：{frame_opcode}")

            if fin:
                break

        if opcode == OP_BIN:
            raise WebSocketError("CDP 不应返回二进制帧")
        return data.decode("utf-8")

    def _read_frame(self) -> tuple[bool, int, bytes]:
        if self._sock is None:
            raise WebSocketError("未连接")
        first, second = _recv_exact(self._sock, 2)
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F

        if length == 126:
            (length,) = struct.unpack("!H", _recv_exact(self._sock, 2))
        elif length == 127:
            (length,) = struct.unpack("!Q", _recv_exact(self._sock, 8))

        if masked:
            mask = _recv_exact(self._sock, 4)
        else:
            mask = b""

        payload = _recv_exact(self._sock, length) if length else b""
        if mask:
            payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))

        return fin, opcode, payload

    def close(self) -> None:
        if self._sock is None:
            return
        try:
            self._send_frame(OP_CLOSE, b"")
        except (OSError, WebSocketError):
            pass
        try:
            self._sock.close()
        finally:
            self._sock = None

    def __enter__(self) -> "MiniWebSocket":
        return self.connect()

    def __exit__(self, *exc: object) -> None:
        self.close()


# --------------------------------------------------------------------------
# 中层：CDP 会话
# --------------------------------------------------------------------------


@dataclass
class CdpClient:
    """一次 CDP 会话。命令通道 = 一条 WebSocket。"""

    ws_url: str
    timeout_s: float = 10.0
    _ws: MiniWebSocket | None = field(default=None, repr=False)
    _next_id: int = field(default=0, repr=False)

    @property
    def host_port_path(self) -> tuple[str, int, str]:
        """把 `ws://host:port/path` 拆开。只支持明文 ws://（本机调试足够）。"""
        if not self.ws_url.startswith("ws://"):
            raise CdpError(f"只支持 ws:// 端点，收到：{self.ws_url}")
        rest = self.ws_url[len("ws://") :]
        host_port, _, path = rest.partition("/")
        host, _, port_text = host_port.partition(":")
        try:
            port = int(port_text)
        except ValueError as exc:
            raise CdpError(f"无法解析端点端口：{self.ws_url}") from exc
        return host, port, "/" + path

    def connect(self) -> "CdpClient":
        host, port, path = self.host_port_path
        self._ws = MiniWebSocket(host, port, path, self.timeout_s).connect()
        return self

    def call(
        self, method: str, params: dict[str, Any] | None = None, session_id: str | None = None
    ) -> dict[str, Any]:
        if self._ws is None:
            raise CdpError("未连接")
        self._next_id += 1
        message: dict[str, Any] = {"id": self._next_id, "method": method}
        if params:
            message["params"] = params
        if session_id:
            message["sessionId"] = session_id
        self._ws.send_text(json.dumps(message))

        while True:
            raw = self._ws.recv_message()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise CdpError(f"CDP 返回了非 JSON 消息：{raw[:200]}") from exc

            if payload.get("id") != self._next_id:
                continue  # 事件通知，跳过
            if "error" in payload:
                error = payload["error"]
                raise CdpError(
                    f"{method} 调用失败：{error.get('message', error)}"
                )
            result = payload.get("result")
            return result if isinstance(result, dict) else {}

    def close(self) -> None:
        if self._ws is not None:
            self._ws.close()
            self._ws = None

    def __enter__(self) -> "CdpClient":
        return self.connect()

    def __exit__(self, *exc: object) -> None:
        self.close()


# --------------------------------------------------------------------------
# 高层：端点发现与便捷调用
# --------------------------------------------------------------------------


def http_json(port: int, path: str, timeout_s: float = 5.0) -> Any:
    url = f"http://127.0.0.1:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise CdpError(f"无法访问 {url}：{type(exc).__name__}: {exc}") from exc


def list_targets(port: int, timeout_s: float = 5.0) -> list[dict[str, Any]]:
    payload = http_json(port, "/json/list", timeout_s)
    if not isinstance(payload, list):
        raise CdpError("/json/list 返回的不是数组")
    return [item for item in payload if isinstance(item, dict)]


def pick_page_target(port: int, timeout_s: float = 5.0) -> dict[str, Any]:
    """挑一个普通页面标签作为会话入口。

    过滤掉 devtools 自身与扩展页：它们的 `webSocketDebuggerUrl` 不能用于
    读 cookie，而且扩展页在 `Network` 域上权限受限。
    """
    targets = list_targets(port, timeout_s)
    pages = [t for t in targets if t.get("type") == "page"]
    if not pages:
        raise CdpError(
            "没有可用的页面标签。请确保浏览器窗口已打开（哪怕只是一个空白标签页）。"
        )

    def score(target: dict[str, Any]) -> int:
        url = str(target.get("url", ""))
        if url.startswith("devtools://"):
            return 3
        if url.startswith("chrome-extension://") or url.startswith("extension://"):
            return 2
        return 0

    pages.sort(key=lambda t: (score(t), str(t.get("id", ""))))
    chosen = pages[0]
    if not chosen.get("webSocketDebuggerUrl"):
        raise CdpError("该页面标签没有 webSocketDebuggerUrl")
    return chosen


def get_all_cookies(port: int, timeout_s: float = 15.0) -> list[dict[str, Any]]:
    """通过 CDP 取全部 cookie（明文）。

    用 `Network.getAllCookies` 而不是 `Network.getCookies`：后者默认只返回
    当前 URL 的 cookie，而 `getAllCookies` 返回整个 cookie 存储——正是我们要的。
    """
    target = pick_page_target(port, timeout_s)
    with CdpClient(str(target["webSocketDebuggerUrl"]), timeout_s) as client:
        result = client.call("Network.getAllCookies")
    cookies = result.get("cookies")
    if not isinstance(cookies, list):
        raise CdpError("Network.getAllCookies 未返回 cookies 数组")
    return [c for c in cookies if isinstance(c, dict)]


def get_cookies_for(port: int, urls: Iterable[str], timeout_s: float = 15.0) -> list[dict[str, Any]]:
    """按 URL 过滤取 cookie（`Network.getCookies`）。"""
    url_list = list(urls)
    if not url_list:
        return []
    target = pick_page_target(port, timeout_s)
    with CdpClient(str(target["webSocketDebuggerUrl"]), timeout_s) as client:
        result = client.call("Network.getCookies", {"urls": url_list})
    cookies = result.get("cookies")
    return [c for c in cookies if isinstance(c, dict)] if isinstance(cookies, list) else []
