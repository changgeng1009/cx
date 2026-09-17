"""Agent 答题链路的端到端演示（跨进程、无需账号、无需网络）。

它验证的核心事实是：**上游 A1 零改动**就能把题目交给操控 Agent。
做法是模拟 A1 的行为（向本地 OpenAI 兼容代理发一个 chat/completions
请求），然后用 HTTP 完成"拉题 → 作答 → 回填"。

运行：
    python scripts/demo_agent_answering.py

你会看到三个进程角色：
    1. 代理进程（本脚本拉起的子进程）：承担 /v1/chat/completions
    2. 上游模拟（本脚本的主线程）：像 A1 那样阻塞等待答案
    3. 操控 Agent（本脚本的另一个线程）：轮询 /v1/pending 并作答
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PORT = 8791
API_KEY = "local-agent"
BASE = f"http://127.0.0.1:{PORT}"
HEADERS = {"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"}

#: 模拟上游 A1 的 api/answer.py 发过来的题目文本
UPSTREAM_PROMPT = """1. 下列逻辑门中，属于通用门的是（ ）
A. 与门
B. 或门
C. 与非门
D. 异或门
2. 组合逻辑电路的输出仅取决于当前输入。（ ）
A. 正确
B. 错误
3. 逻辑函数 F = A·B + A·C 可以化简为（ ）
A. A(B+C)
B. A+C
C. B+C
D. AB
"""

#: 操控 Agent 给出的答案（真实场景里由 DeepSeek Agent / 人来产生）
AGENT_ANSWERS = ["C", "A", "A"]


def wait_for_health(timeout_s: float = 15.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{BASE}/health", timeout=2) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.15)
    return False


def upstream_calls_agent(captured: dict) -> None:
    """角色 2：像 A1 一样发出请求并阻塞等待。"""
    payload = {
        "model": "agent-in-the-loop",
        "messages": [{"role": "user", "content": UPSTREAM_PROMPT}],
        "temperature": 0,
    }
    request = urllib.request.Request(
        f"{BASE}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=HEADERS,
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            captured["response"] = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        captured["error"] = repr(exc)


def agent_answers(answered: dict) -> None:
    """角色 3：操控 Agent —— 拉题、作答、回填。"""
    deadline = time.time() + 30
    ticket = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                urllib.request.Request(f"{BASE}/v1/pending", headers=HEADERS), timeout=5
            ) as response:
                data = json.loads(response.read().decode("utf-8"))
            if data.get("tickets"):
                ticket = data["tickets"][0]
                break
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.1)

    if ticket is None:
        answered["error"] = "Agent 未在超时内发现工单"
        return

    answered["ticket_id"] = ticket["ticket_id"]
    answered["questions"] = ticket.get("questions") or []

    # 这里就是"Agent 答题"的落点：真实场景换成 DeepSeek Agent 或人工复核
    body = {"ticket_id": ticket["ticket_id"], "answers": AGENT_ANSWERS}
    request = urllib.request.Request(
        f"{BASE}/v1/answer", data=json.dumps(body).encode("utf-8"), headers=HEADERS
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        answered["submit_result"] = json.loads(response.read().decode("utf-8"))


def main() -> int:
    python = sys.executable
    print("=" * 70)
    print("Agent 答题链路演示（跨进程）")
    print("=" * 70)

    server = subprocess.Popen(
        [python, "-m", "orchestrator.cli", "shim-serve", "--port", str(PORT), "--api-key", API_KEY],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        if not wait_for_health():
            print("[x] 代理未能在超时内启动")
            return 1
        print(f"[1] 代理已启动：{BASE}/v1   （模拟 A1 的 config.ini endpoint 指向这里）")

        captured: dict = {}
        answered: dict = {}

        upstream_thread = threading.Thread(
            target=upstream_calls_agent, args=(captured,), daemon=True
        )
        agent_thread = threading.Thread(
            target=agent_answers, args=(answered,), daemon=True
        )
        upstream_thread.start()
        time.sleep(0.3)
        agent_thread.start()
        upstream_thread.join(timeout=45)
        agent_thread.join(timeout=15)

        if "error" in answered:
            print(f"[x] Agent 侧失败：{answered['error']}")
            return 1

        questions = answered.get("questions") or []
        print(f"[2] 代理已把上游请求转成工单 {answered['ticket_id']}，"
              f"解析出 {len(questions)} 道题：")
        for question in questions:
            stem = question.get("stem", "")
            print(f"      {question.get('index')}. [{question.get('type')}] {stem[:34]}")

        print(f"[3] 操控 Agent 作答并回填：{AGENT_ANSWERS}")
        print(f"[4] 代理把答案包装成 OpenAI 响应返回给上游")

        if "error" in captured:
            print(f"[x] 上游侧失败：{captured['error']}")
            return 1

        response = captured["response"]
        content = response["choices"][0]["message"]["content"]
        print()
        print("-" * 70)
        print("上游收到的响应（A1 会照它原有的解析逻辑处理）：")
        print(f"  object      : {response['object']}")
        print(f"  model       : {response['model']}")
        print(f"  content     : {content!r}")
        print(f"  finish      : {response['choices'][0]['finish_reason']}")
        print("-" * 70)

        expected = "\n".join(AGENT_ANSWERS)
        if content != expected:
            print(f"[x] 内容不符，期望 {expected!r}")
            return 1

        print()
        print("演示成功：整条链路未使用任何第三方 LLM API Key，"
              "也未访问学习通平台。")
        return 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    raise SystemExit(main())
