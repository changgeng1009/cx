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

from orchestrator.answer_broker import (
    AnswerBroker,
    extract_questions,
    normalize_answers,
    parse_answers,
)
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
            # M5 安全网：字母答案落库前已转成选项原文（A1 靠文本匹配定位选项）
            self.assertEqual(
                answered.answers, ["悬空相当于高电平", "正确", "A(B+C)"]
            )
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
        """响应必须是 A1 能解析的 JSON（按 A1 api/answer.py 的解析路径验证）。"""
        with Sandbox() as sandbox:
            broker = sandbox.ctx.broker
            ticket = broker.create_ticket("1. 题目\nA. 甲\nB. 乙")
            broker.submit(ticket.ticket_id, ["A"])
            response = broker.to_openai_response(broker.get(ticket.ticket_id))
            self.assertEqual(response["object"], "chat.completion")
            content = response["choices"][0]["message"]["content"]
            # 复刻 A1 的解析：json.loads(去 md 包裹) → ["Answer"] → "\n".join
            parsed = json.loads(content)
            self.assertEqual(parsed, {"Answer": ["甲"]})
            self.assertEqual("\n".join(parsed["Answer"]), "甲")
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
            # 字母已按选项原文规范化（见 normalize_answers）
            self.assertEqual(
                submitted.data["ticket"]["answers"],
                ["悬空相当于高电平", "正确", "A(B+C)"],
            )

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
                # 按 A1 的解析路径校验（json → Answer → 换行拼接）
                parsed = json.loads(body["choices"][0]["message"]["content"])
                self.assertEqual(
                    parsed,
                    {"Answer": ["悬空相当于高电平", "正确", "A(B+C)"]},
                )
                self.assertEqual(
                    "\n".join(parsed["Answer"]), "悬空相当于高电平\n正确\nA(B+C)"
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


class StaleTicketTests(unittest.TestCase):
    """历史残留工单必须可清理，否则会污染 answer_pending（实测踩过）。"""

    def test_expire_stale_marks_and_hides(self) -> None:
        with Sandbox() as sandbox:
            broker = sandbox.ctx.broker
            stale = broker.create_ticket("1. 旧题\nA. 甲")
            fresh = broker.create_ticket("1. 新题\nA. 乙")
            # 把旧工单的创建时间推早 1 小时（远超其默认 120s 等待上限）
            from datetime import datetime, timedelta

            ticket = broker.get(stale.ticket_id)
            ticket.created_at = (datetime.now().astimezone() - timedelta(hours=1)).isoformat(
                timespec="milliseconds"
            )
            broker._pending_path(stale.ticket_id).write_text(
                json.dumps(ticket.to_dict(), ensure_ascii=False), encoding="utf-8"
            )

            expired = broker.expire_stale()
            self.assertEqual(expired, [stale.ticket_id])

            remaining = [t.ticket_id for t in broker.pending()]
            self.assertIn(fresh.ticket_id, remaining)
            self.assertNotIn(stale.ticket_id, remaining)

            stats = broker.stats()
            self.assertEqual(stats["pending"], 1)
            self.assertEqual(stats["timeout"], 1)

    def test_answered_ticket_never_expired(self) -> None:
        with Sandbox() as sandbox:
            broker = sandbox.ctx.broker
            ticket = broker.create_ticket("1. 题\nA. 甲")
            broker.submit(ticket.ticket_id, ["甲"])
            self.assertEqual(broker.expire_stale(reference_ts=time.time() + 3600), [])

    def test_expire_within_limit_keeps_pending(self) -> None:
        with Sandbox() as sandbox:
            broker = sandbox.ctx.broker
            ticket = broker.create_ticket("1. 题\nA. 甲")
            self.assertEqual(broker.expire_stale(), [])
            self.assertIn(ticket.ticket_id, [t.ticket_id for t in broker.pending()])


class A1ProviderFormatTests(unittest.TestCase):
    """M5：对齐 A1 `AI` provider 的真实发题/收答格式（实测校准）。"""

    #: A1 的 user 消息原文形态（选项字母已被上游剥掉）
    A1_PROMPT = (
        "本题为单选题，你只能选择一个选项，请根据题目和选项回答问题，以json格式输出正确的选项内容，"
        "示例回答：{\"Answer\": [\"答案\"]}。除此之外不要输出任何多余的内容，也不要使用MD语法。"
        "\n题目：建筑火灾烟气的主要危害是什么？"
        "\n选项：造成人员窒息死亡"
        "\n影响安全疏散"
        "\n使消防员难以接近火源"
    )

    def test_parses_a1_prompt_into_structured_question(self) -> None:
        questions = extract_questions(self.A1_PROMPT)
        self.assertEqual(len(questions), 1)
        q = questions[0]
        self.assertIn("建筑火灾烟气", q.stem)
        self.assertEqual([o["key"] for o in q.options], ["A", "B", "C"])
        self.assertEqual(q.options[0]["text"], "造成人员窒息死亡")

    def test_letter_answer_is_converted_to_option_text(self) -> None:
        """字母答案必须转成选项原文——A1 用文本子序列匹配选项。"""
        with Sandbox() as sandbox:
            broker = sandbox.ctx.broker
            ticket = broker.create_ticket(self.A1_PROMPT)
            self.assertEqual(len(ticket.questions), 1)
            broker.submit(ticket.ticket_id, ["B"])
            answered = broker.get(ticket.ticket_id)
            self.assertEqual(answered.answers, ["影响安全疏散"])

    def test_multi_letter_answer_becomes_newline_joined_text(self) -> None:
        prompt = (
            "本题为多选题，你必须选择两个或以上选项。\n"
            "题目：属于防烟设施的有？\n"
            "选项：加压送风机\n机械加压送风管道\n排烟风机\n送风口"
        )
        with Sandbox() as sandbox:
            broker = sandbox.ctx.broker
            ticket = broker.create_ticket(prompt)
            broker.submit(ticket.ticket_id, ["AB"])
            answered = broker.get(ticket.ticket_id)
            self.assertEqual(answered.answers, ["加压送风机\n机械加压送风管道"])

    def test_judgement_and_text_answers_untouched(self) -> None:
        prompt = "本题为判断题。\n题目：防烟楼梯间应设置防烟设施。\n选项：正确\n错误"
        with Sandbox() as sandbox:
            broker = sandbox.ctx.broker
            t1 = broker.create_ticket(prompt)
            broker.submit(t1.ticket_id, ["正确"])
            self.assertEqual(broker.get(t1.ticket_id).answers, ["正确"])
        # 长文本答案不会被字母规则误伤
        with Sandbox() as sandbox:
            broker = sandbox.ctx.broker
            t2 = broker.create_ticket(self.A1_PROMPT)
            broker.submit(t2.ticket_id, ["影响安全疏散"])
            self.assertEqual(broker.get(t2.ticket_id).answers, ["影响安全疏散"])

    def test_full_loop_matches_a1_option_lookup(self) -> None:
        """端到端：Agent 答字母 → shim 回 JSON → 模拟 A1 的选项定位必须命中。"""
        # 真实 A1 的 options 自带字母前缀（o[:1] 取到的才是选项字母）
        option_texts = [
            "A. 造成人员窒息死亡",
            "B. 影响安全疏散",
            "C. 使消防员难以接近火源",
        ]

        def is_subsequence(needle: str, haystack: str) -> bool:
            it = iter(haystack)
            return all(ch in it for ch in needle)

        with Sandbox() as sandbox:
            broker = sandbox.ctx.broker
            ticket = broker.create_ticket(self.A1_PROMPT)
            broker.submit(ticket.ticket_id, ["B"])
            response = broker.to_openai_response(broker.get(ticket.ticket_id))
            content = response["choices"][0]["message"]["content"]
            a1_answer = "\n".join(json.loads(content)["Answer"])

            matched = next(
                (o[:1] for o in option_texts if is_subsequence(a1_answer, o)), None
            )
            self.assertEqual(matched, "B")


if __name__ == "__main__":
    unittest.main()
