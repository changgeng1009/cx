"""本地 OpenAI 兼容代理（C46）—— "让操控 Agent 答题"的落地装置。

原理（docs/03 §11.1）：上游 A1 的 AI 答题走的是 **OpenAI 兼容协议**
（`config.ini` 里的 `endpoint` / `key` / `model`）。所以统一层只要在本地
实现 `POST /v1/chat/completions`，把 A1 的 endpoint 指向 `127.0.0.1`，
**上游零改动**就会把题目抛过来，再由本代理转交操控 Agent。

这条路线的好处：
- 不需要任何第三方 LLM API Key
- 题目经过平台 → 可审计、可人工复核、可缓存
- 与学习通平台完全解耦，纯本地协议适配，不含任何上游代码（满足红线 R3）

上游 `config.ini` 应配置为：
    [tiku]
    provider = AI
    endpoint = http://127.0.0.1:8765/v1
    key      = local-agent
    model    = agent-in-the-loop
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .answer_broker import AnswerBroker, parse_answers

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_API_KEY = "local-agent"
DEFAULT_MODEL = "agent-in-the-loop"


def extract_prompt(payload: dict[str, Any]) -> str:
    """从 OpenAI 请求体里抽出题目文本。

    刻意宽松：`messages` 可能是字符串 content，也可能是多模态 parts 数组；
    有些客户端还会用 legacy 的 `prompt` 字段。全部兼容，因为**这个函数
    决定了题目能不能到达 Agent**，漏一种格式就等于漏一种上游。
    """
    parts: list[str] = []
    for message in payload.get("messages") or []:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text" and item.get("text"):
                        parts.append(str(item["text"]))
                    elif item.get("text"):
                        parts.append(str(item["text"]))
    if not parts and payload.get("prompt"):
        parts.append(str(payload["prompt"]))
    return "\n".join(part for part in parts if part)


class _Handler(BaseHTTPRequestHandler):
    server_version = "OrchestratorAnswerShim/0.1"

    # ------------------------------------------------------------------
    @property
    def broker(self) -> AnswerBroker:
        return self.server.broker  # type: ignore[attr-defined]

    @property
    def api_key(self) -> str:
        return self.server.api_key  # type: ignore[attr-defined]

    @property
    def echo(self) -> Callable[[str], None] | None:
        return self.server.echo  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:  # 静音默认 stderr 日志
        if self.echo is not None:
            self.echo(f"[shim] {fmt % args}")

    # ------------------------------------------------------------------
    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _authorized(self) -> bool:
        if not self.api_key:
            return True
        header = self.headers.get("Authorization") or ""
        if header.startswith("Bearer "):
            return header[len("Bearer ") :].strip() == self.api_key
        # 有些客户端把 key 放在 body 里，由调用方自行传入；这里只校验 header
        return False

    # ------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path in ("/health", "/healthz"):
            stats = self.broker.stats()
            self._send_json(200, {"status": "ok", "model": DEFAULT_MODEL, **stats})
            return
        if path == "/v1/models":
            if not self._authorized():
                self._send_json(401, {"error": {"message": "invalid api key"}})
                return
            self._send_json(
                200,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": DEFAULT_MODEL,
                            "object": "model",
                            "created": 0,
                            "owned_by": "orchestrator",
                        }
                    ],
                },
            )
            return
        if path == "/v1/pending":
            if not self._authorized():
                self._send_json(401, {"error": {"message": "invalid api key"}})
                return
            self._send_json(
                200, {"tickets": [t.to_dict() for t in self.broker.pending()]}
            )
            return
        self._send_json(404, {"error": {"message": f"not found: {path}"}})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]

        if path == "/v1/answer":
            payload = self._read_json()
            ticket_id = str(payload.get("ticket_id", ""))
            answers = payload.get("answers")
            if isinstance(answers, str):
                answers = parse_answers(answers)
            if not ticket_id or not isinstance(answers, list):
                self._send_json(
                    400, {"error": {"message": "需要 ticket_id 与 answers"}}
                )
                return
            try:
                ticket = self.broker.submit(
                    ticket_id, [str(a) for a in answers], answered_by="agent"
                )
            except KeyError:
                self._send_json(
                    404, {"error": {"message": f"工单不存在：{ticket_id}"}}
                )
                return
            self._send_json(200, {"ok": True, "ticket": ticket.to_dict()})
            return

        if path not in ("/v1/chat/completions", "/chat/completions"):
            self._send_json(404, {"error": {"message": f"not found: {path}"}})
            return

        if not self._authorized():
            self._send_json(401, {"error": {"message": "invalid api key"}})
            return

        payload = self._read_json()
        prompt = extract_prompt(payload)
        if not prompt.strip():
            self._send_json(
                400,
                {
                    "error": {
                        "message": "请求体里没有可识别的题目文本（messages/prompt 均为空）"
                    }
                },
            )
            return

        model = str(payload.get("model") or DEFAULT_MODEL)
        ticket = self.broker.create_ticket(raw_prompt=prompt, timeout_s=self.broker.timeout_s)
        if self.echo is not None:
            self.echo(
                f"[shim] 新工单 {ticket.ticket_id}："
                f"{len(ticket.questions)} 题已解析，等待操控 Agent 应答"
            )

        answered = self.broker.wait(ticket.ticket_id)
        self._send_json(200, self.broker.to_openai_response(answered, model=model))


def build_server(
    broker: AnswerBroker,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    api_key: str = DEFAULT_API_KEY,
    echo: Callable[[str], None] | None = None,
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), _Handler)
    server.broker = broker  # type: ignore[attr-defined]
    server.api_key = api_key  # type: ignore[attr-defined]
    server.echo = echo  # type: ignore[attr-defined]
    return server


def serve(
    broker: AnswerBroker,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    api_key: str = DEFAULT_API_KEY,
    echo: Callable[[str], None] | None = None,
) -> None:
    server = build_server(broker, host, port, api_key, echo)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def start_in_thread(
    broker: AnswerBroker,
    host: str = DEFAULT_HOST,
    port: int = 0,
    api_key: str = DEFAULT_API_KEY,
    echo: Callable[[str], None] | None = None,
) -> tuple[ThreadingHTTPServer, threading.Thread, int]:
    """在后台线程启动代理。`port=0` 时由系统分配，返回实际端口。

    测试与 CLI 都用它 —— 因为代理与刷课必须能同进程协作（代理在子线程里
    承接上游请求，主线程继续调度）。
    """
    server = build_server(broker, host, port, api_key, echo)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    actual_port = server.server_address[1]
    return server, thread, actual_port


def config_snippet(port: int = DEFAULT_PORT, api_key: str = DEFAULT_API_KEY) -> str:
    """生成给上游 A1 用的 `[tiku]` 配置片段。"""
    return (
        "[tiku]\n"
        "provider = AI\n"
        f"endpoint = http://{DEFAULT_HOST}:{port}/v1\n"
        f"key = {api_key}\n"
        f"model = {DEFAULT_MODEL}\n"
        "submit = false\n"
        "# submit=false：Agent 缺席时只保存不提交，不会阻塞课程\n"
    )
