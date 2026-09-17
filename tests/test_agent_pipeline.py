"""Agent 答题链路（C43–C47）与 MCP 出口（C36）。

这组测试之所以能在 M0 完整跑通，是因为整条答题链路都是**本地组件**：
文件队列 + 本地 HTTP 代理，不碰网络、不碰账号。这也是 v2 需求变更
（"AI 答题由操控 Agent 来答题"）带来的意外收益——它把一个依赖外部
API Key 的功能，变成了完全离线可验收的功能。
"""

from __future__ import annotations

import io
import json
import threading
import time
import unittest
import urllib.error
import urllib.request

from orchestrator.answer_broker import AnswerBroker, extract_questions, parse_answers
from orchestrator.fixtures import make_upstream_question_prompt
from orchestrator.models import TicketState
from orchestrator.openai_shim import config_snippet, extract_prompt, start_in_thread
from orchestrator.throttle import FakeClock

from .helpers import Sandbox


class QuestionExtractionTests(unittest.TestCase):
    def test_parses_numbered_questions_with_options(self) -> None:
        questions = extract_questions(make_upstream_question_prompt())
        self.assertEqual(len(questions), 3)
        self.assertEqual(questions[0].index, 1)
        self.assertIn("TTL", questions[0].stem)
        self.assertEqual(len(questions[0].options), 4)
        self.assertEqual(questions[0].options[0]["key"], "A")

    def test_detects_true_false_type(self) -> None:
        questions = extract_questions(make_upstream_question_prompt())
        self.assertEqual(questions[1].question_type, "true_false")

    def test_unparseable_prompt_returns_empty_but_keeps_raw(self) -> None:
        """解析失败是可接受的：原文仍会交给 Agent。"""
        raw = "这是一段没有题号的自由文本，无法结构化"
        self.assertEqual(extract_questions(raw), [])

    def test_extract_prompt_handles_string_and_multimodal(self) -> None:
        plain = extract_prompt({"messages": [{"role": "user", "content": "题目A"}]})
        self.assertEqual(plain, "题目A")

        multimodal = extract_prompt(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "题目B"},
                            {"type": "text", "text": "题目C"},
                        ],
                    }
                ]
            }
        )
        self.assertEqual(multimodal, "题目B\n题目C")

    def test_extract_prompt_supports_legacy_prompt_field(self) -> None:
        self.assertEqual(extract_prompt({"prompt": "旧格式题目"}), "旧格式题目")


class ParseAnswersTests(unittest.TestCase):
    def test_newline_separated(self) -> None:
        self.assertEqual(parse_answers("A\nB\nC"), ["A", "B", "C"])

    def test_semicolon_separated(self) -> None:
        self.assertEqual(parse_answers("A;B;C"), ["A", "B", "C"])

    def test_json_array(self) -> None:
        self.assertEqual(parse_answers('["A", "B#C"]'), ["A", "B#C"])

    def test_single_answer(self) -> None:
        self.assertEqual(parse_answers("正确"), ["正确"])

    def test_empty(self) -> None:
        self.assertEqual(parse_answers("   "), [])


class AnswerBrokerTests(unittest.TestCase):
    def test_ticket_lifecycle(self) -> None:
        with Sandbox() as sandbox:
            broker: AnswerBroker = sandbox.ctx.broker
            ticket = broker.create_ticket(
                make_upstream_question_prompt(),
                request_id="req_1",
                course_id="240100001",
            )
            self.assertEqual(ticket.state, TicketState.PENDING)
            self.assertEqual(len(ticket.questions), 3)
            self.assertEqual(len(broker.pending()), 1)

            answered = broker.submit(ticket.ticket_id, ["A", "正确", "A"])
            self.assertEqual(answered.state, TicketState.ANSWERED)
            self.assertEqual(answered.answers, ["A", "正确", "A"])
            self.assertEqual(answered.answered_by, "agent")
            self.assertEqual(broker.pending(), [])

    def test_wait_returns_immediately_when_answered(self) -> None:
        with Sandbox() as sandbox:
            broker = sandbox.ctx.broker
            ticket = broker.create_ticket("1. 题目\nA. 甲\nB. 乙")
            broker.submit(ticket.ticket_id, ["A"])
            result = broker.wait(ticket.ticket_id, timeout_s=5)
            self.assertEqual(result.state, TicketState.ANSWERED)

    def test_wait_times_out_without_agent(self) -> None:
        """上游是阻塞的同步进程，绝不能让代理无限等待。"""
        clock = FakeClock()
        build_dir = None
        with Sandbox() as sandbox:
            build_dir = sandbox.ctx.run_dir / "broker_timeout"
            broker = AnswerBroker(
                build_dir, timeout_s=3.0, clock=clock.now, sleeper=clock.sleep,
                poll_interval_s=0.5,
            )
            ticket = broker.create_ticket("1. 题目\nA. 甲")
            result = broker.wait(ticket.ticket_id)
            self.assertEqual(result.state, TicketState.TIMEOUT)
            self.assertIn("超时", result.note)

    def test_ticket_survives_simulated_restart(self) -> None:
        """工单落盘：新进程仍能读到未答工单并接受迟到回填（C47 验收）。"""
        with Sandbox() as sandbox:
            broker = sandbox.ctx.broker
            ticket = broker.create_ticket("1. 题目\nA. 甲\nB. 乙")
            # 模拟重启：用同一目录新建 broker 实例
            restarted = AnswerBroker(broker.root, timeout_s=5)
            pending = restarted.pending()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0].ticket_id, ticket.ticket_id)
            restarted.submit(ticket.ticket_id, ["A"])
            self.assertEqual(restarted.get(ticket.ticket_id).state, TicketState.ANSWERED)

    def test_to_openai_response_shape(self) -> None:
        with Sandbox() as sandbox:
            broker = sandbox.ctx.broker
            ticket = broker.create_ticket("1. 题目\nA. 甲")
            broker.submit(ticket.ticket_id, ["A", "B"])
            response = broker.to_openai_response(broker.get(ticket.ticket_id))
            self.assertEqual(response["object"], "chat.completion")
            self.assertEqual(
                response["choices"][0]["message"]["content"], "A\nB"
            )
            self.assertIn("usage", response)

    def test_timeout_response_is_degraded_not_crash(self) -> None:
        with Sandbox() as sandbox:
            broker = sandbox.ctx.broker
            ticket = broker.create_ticket("1. 题目\nA. 甲")
            ticket.state = TicketState.TIMEOUT
            response = broker.to_openai_response(ticket)
            content = response["choices"][0]["message"]["content"]
            self.assertIn("无法作答", content)


class AnswerCommandTests(unittest.TestCase):
    def test_pending_and_submit_commands(self) -> None:
        with Sandbox() as sandbox:
            ticket = sandbox.ctx.broker.create_ticket(make_upstream_question_prompt())

            pending = sandbox.run("answer_pending")
            self.assertTrue(pending.ok)
            self.assertEqual(pending.data["count"], 1)
            self.assertIn("answer_submit", pending.data["how_to_answer"])

            submitted = sandbox.run(
                "answer_submit", ticket_id=ticket.ticket_id, answers="A\n正确\nA"
            )
            self.assertTrue(submitted.ok)
            self.assertEqual(submitted.data["ticket"]["answers"], ["A", "正确", "A"])

            stats = sandbox.run("answer_stats")
            self.assertEqual(stats.data["answered"], 1)
            self.assertEqual(stats.data["pending"], 0)

    def test_submit_unknown_ticket_reports_clear_error(self) -> None:
        with Sandbox() as sandbox:
            envelope = sandbox.run(
                "answer_submit", ticket_id="tk_nope", answers="A"
            )
            self.assertFalse(envelope.ok)
            self.assertEqual(envelope.error["category"], "input")

    def test_shim_config_points_upstream_to_localhost(self) -> None:
        with Sandbox() as sandbox:
            envelope = sandbox.run("shim_config", port=8765)
            self.assertTrue(envelope.ok)
            snippet = envelope.data["config_ini_snippet"]
            self.assertIn("provider = AI", snippet)
            self.assertIn("127.0.0.1:8765", snippet)
            self.assertIn("submit = false", snippet)

    def test_config_snippet_has_no_external_key(self) -> None:
        """这条路线不需要任何第三方 LLM API Key —— 这正是它的价值。"""
        snippet = config_snippet()
        self.assertIn("key = local-agent", snippet)
        self.assertNotIn("sk-", snippet)


class ShimEndToEndTests(unittest.TestCase):
    """A1 视角的完整往返：POST 题目 → 工单 → Agent 作答 → 拿到 OpenAI 响应。"""

    def test_upstream_request_is_answered_by_agent_over_http(self) -> None:
        with Sandbox() as sandbox:
            sandbox.ctx.broker.timeout_s = 15
            server, thread, port = start_in_thread(
                sandbox.ctx.broker, port=0, api_key="local-agent"
            )
            try:
                captured: dict[str, object] = {}

                def upstream_call() -> None:
                    payload = {
                        "model": "agent-in-the-loop",
                        "messages": [
                            {"role": "user", "content": make_upstream_question_prompt()}
                        ],
                    }
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{port}/v1/chat/completions",
                        data=json.dumps(payload).encode("utf-8"),
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": "Bearer local-agent",
                        },
                    )
                    with urllib.request.urlopen(request, timeout=30) as response:
                        captured["body"] = json.loads(response.read().decode("utf-8"))

                caller = threading.Thread(target=upstream_call, daemon=True)
                caller.start()

                # 模拟操控 Agent：发现工单 → 作答
                ticket = None
                for _ in range(400):
                    pending = sandbox.ctx.broker.pending()
                    if pending:
                        ticket = pending[0]
                        break
                    time.sleep(0.02)
                self.assertIsNotNone(ticket, "代理没有把上游请求转成工单")
                sandbox.ctx.broker.submit(ticket.ticket_id, ["A", "A", "A"])

                caller.join(timeout=20)
                self.assertIn("body", captured)
                body = captured["body"]
                self.assertEqual(
                    body["choices"][0]["message"]["content"], "A\nA\nA"
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_wrong_api_key_rejected(self) -> None:
        with Sandbox() as sandbox:
            server, thread, port = start_in_thread(
                sandbox.ctx.broker, port=0, api_key="right-key"
            )
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    data=json.dumps({"messages": [{"content": "x"}]}).encode(),
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": "Bearer wrong",
                    },
                )
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(request, timeout=5)
                self.assertEqual(ctx.exception.code, 401)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_health_endpoint_reports_queue(self) -> None:
        with Sandbox() as sandbox:
            server, thread, port = start_in_thread(sandbox.ctx.broker, port=0)
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health", timeout=5
                ) as response:
                    body = json.loads(response.read().decode())
                self.assertEqual(body["status"], "ok")
                self.assertIn("pending", body)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


class McpServerTests(unittest.TestCase):
    def test_initialize_and_tools_list(self) -> None:
        from orchestrator.mcp_server import TOOLS, handle_request, tool_names

        with Sandbox() as sandbox:
            init = handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, sandbox.ctx)
            self.assertEqual(init["result"]["serverInfo"]["name"], "chaoxing-orchestrator")
            self.assertIn("tools", init["result"]["capabilities"])

            listing = handle_request(
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, sandbox.ctx
            )
            names = [tool["name"] for tool in listing["result"]["tools"]]
            self.assertEqual(names, tool_names())
            self.assertEqual(len(TOOLS), len(names))
            for required in (
                "list_courses",
                "scan_tasks",
                "run_course",
                "pause",
                "resume",
                "answer_pending",
                "answer_submit",
                "sign_in",
            ):
                self.assertIn(required, names)

    def test_tools_call_returns_envelope_text(self) -> None:
        from orchestrator.mcp_server import handle_request

        with Sandbox() as sandbox:
            response = handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "list_courses", "arguments": {}},
                },
                sandbox.ctx,
            )
            self.assertFalse(response["result"]["isError"])
            payload = json.loads(response["result"]["content"][0]["text"])
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["command"], "list_courses")

    def test_error_result_sets_is_error(self) -> None:
        from orchestrator.mcp_server import handle_request

        with Sandbox() as sandbox:
            response = handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {"name": "scan_tasks", "arguments": {}},
                },
                sandbox.ctx,
            )
            self.assertTrue(response["result"]["isError"])
            payload = json.loads(response["result"]["content"][0]["text"])
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error"]["category"], "input")

    def test_unknown_tool_and_method(self) -> None:
        from orchestrator.mcp_server import handle_request

        with Sandbox() as sandbox:
            bad_tool = handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {"name": "nope", "arguments": {}},
                },
                sandbox.ctx,
            )
            self.assertIn("error", bad_tool)
            bad_method = handle_request(
                {"jsonrpc": "2.0", "id": 6, "method": "no/such"}, sandbox.ctx
            )
            self.assertEqual(bad_method["error"]["code"], -32601)

    def test_initialized_notification_has_no_response(self) -> None:
        from orchestrator.mcp_server import handle_request

        with Sandbox() as sandbox:
            self.assertIsNone(handle_request({"method": "notifications/initialized"}, sandbox.ctx))

    def test_stdio_loop(self) -> None:
        from orchestrator.mcp_server import serve

        with Sandbox() as sandbox:
            stdin = io.StringIO(
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n"
                + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n"
            )
            stdout = io.StringIO()
            serve(sandbox.ctx, stdin=stdin, stdout=stdout)
            lines = [line for line in stdout.getvalue().splitlines() if line.strip()]
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0])["result"]["serverInfo"]["name"], "chaoxing-orchestrator")


if __name__ == "__main__":
    unittest.main()
