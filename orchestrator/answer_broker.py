"""待答工单队列（C43 / C44 / C45 / C47）—— Agent 答题链路的核心。

设计要点（docs/03 §11）：

1. **跨进程、可重启**。代理跑在刷课进程里，Agent 通过 CLI/MCP 在**另一个
   进程**里提交答案。所以信令走**文件**而不是内存队列：submit 写答案文件，
   代理轮询该文件。这样不需要 socket、不需要共享内存、重启也不丢工单。

2. **`raw_prompt` 必须原样保留**。上游 A1 拼装的 prompt 格式会变，
   结构化解析（`questions`）只是尽力而为。解析失败时 Agent 仍拿到原文，
   自己也能读懂 —— 这比"解析失败就报错"健壮得多。

3. **硬超时**。上游是阻塞的同步 Python；如果 Agent 一直不响应而这里
   无限等待，整门课就停摆了。所以超时后立即返回降级答复。
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .models import TicketState, now_iso

DEFAULT_TIMEOUT_S = 120.0

QUESTION_RE = re.compile(r"^\s*(\d{1,3})\s*[.、)]\s*(.+?)\s*$")
OPTION_RE = re.compile(r"^\s*([A-Ha-h])\s*[.、)]\s*(.+?)\s*$")
#: A1 `AI` provider 的发文格式（M5 实测）：选项字母已被上游剥掉
A1_STEM_RE = re.compile(r"^\s*题目[：:]\s*(.+?)\s*$")
A1_OPTIONS_RE = re.compile(r"^\s*选项[：:]\s*(.*)$")
#: 纯字母答案（A / AC / A、C…），需要转成选项原文
LETTER_ANSWER_RE = re.compile(r"^\s*([A-Ha-h])(?:[\s,，、]?([A-Ha-h]))*\s*$")

#: 把字母答案拆成字母列表
LETTER_SPLIT_RE = re.compile(r"[A-Ha-h]")
TRUE_FALSE = {"正确", "错误", "对", "错", "√", "×", "是", "否", "T", "F"}


@dataclass
class Question:
    index: int
    stem: str
    question_type: str = "unknown"
    options: list[dict[str, str]] = field(default_factory=list)
    image_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "type": self.question_type,
            "stem": self.stem,
            "options": list(self.options),
            "image_ref": self.image_ref,
        }


@dataclass
class Ticket:
    ticket_id: str
    raw_prompt: str
    created_at: str = field(default_factory=now_iso)
    state: TicketState = TicketState.PENDING
    request_id: str = ""
    course_id: str = ""
    chapter_id: str = ""
    questions: list[Question] = field(default_factory=list)
    answers: list[str] | None = None
    answered_at: str | None = None
    answered_by: str | None = None
    timeout_s: float = DEFAULT_TIMEOUT_S
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "created_at": self.created_at,
            "state": str(self.state),
            "request_id": self.request_id,
            "course_id": self.course_id,
            "chapter_id": self.chapter_id,
            "raw_prompt": self.raw_prompt,
            "questions": [q.to_dict() for q in self.questions],
            "answers": self.answers,
            "answered_at": self.answered_at,
            "answered_by": self.answered_by,
            "timeout_s": self.timeout_s,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Ticket":
        return cls(
            ticket_id=str(raw.get("ticket_id", "")),
            raw_prompt=str(raw.get("raw_prompt", "")),
            created_at=str(raw.get("created_at", now_iso())),
            state=TicketState(str(raw.get("state", "pending"))),
            request_id=str(raw.get("request_id", "")),
            course_id=str(raw.get("course_id", "")),
            chapter_id=str(raw.get("chapter_id", "")),
            questions=[
                Question(
                    index=int(q.get("index", 0)),
                    stem=str(q.get("stem", "")),
                    question_type=str(q.get("type", "unknown")),
                    options=list(q.get("options") or []),
                    image_ref=q.get("image_ref"),
                )
                for q in raw.get("questions") or []
            ],
            answers=list(raw["answers"]) if raw.get("answers") is not None else None,
            answered_at=raw.get("answered_at"),
            answered_by=raw.get("answered_by"),
            timeout_s=float(raw.get("timeout_s", DEFAULT_TIMEOUT_S)),
            note=str(raw.get("note", "")),
        )


def _parse_a1_provider_prompt(raw_prompt: str) -> list[Question]:
    """解析 A1 `AI` provider 的真实发题格式（M5 实测）。

    A1 每次只问一道题，user 消息形如：
        题目：<题干>
        选项：<选项1>
        <选项2>
        ...
    其中**选项字母已被 A1 剥掉**（`re.sub(r"^[A-Z]\\s*", "", option)`），
    所以这里按顺序补 A/B/C… 便于操控 Agent 用字母指认；回填时会再转成
    选项原文（A1 靠文本子序列匹配选项，答字母会导致随机作答）。
    """
    stem = ""
    raw_options: list[str] = []
    in_options = False
    for line in (raw_prompt or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        m_stem = A1_STEM_RE.match(stripped)
        if m_stem:
            stem = m_stem.group(1).strip()
            in_options = False
            continue
        m_opt = A1_OPTIONS_RE.match(stripped)
        if m_opt:
            in_options = True
            first = m_opt.group(1).strip()
            if first:
                raw_options.append(first)
            continue
        if in_options:
            raw_options.append(stripped)

    if not stem:
        return []
    options = [
        {"key": chr(ord("A") + i), "text": text}
        for i, text in enumerate(raw_options)
        if text
    ]
    question = Question(index=1, stem=stem, options=options)
    texts = {o["text"] for o in options}
    if texts and texts <= TRUE_FALSE:
        question.question_type = "true_false"
    elif options:
        question.question_type = "choice"
    else:
        question.question_type = "essay"
    return [question]


def extract_questions(raw_prompt: str) -> list[Question]:
    """尽力把上游 prompt 解析成结构化题目。

    解析失败（返回空列表）是**可接受的**：`raw_prompt` 仍然完整交给 Agent。
    两种格式都认：编号列表（题库类 provider）与 A1 的「题目/选项」格式。
    """
    a1_questions = _parse_a1_provider_prompt(raw_prompt)
    if a1_questions:
        return a1_questions

    questions: list[Question] = []
    current: Question | None = None

    for line in (raw_prompt or "").splitlines():
        if not line.strip():
            continue
        question_match = QUESTION_RE.match(line)
        option_match = OPTION_RE.match(line)

        # 先判选项，否则 "A. 选项" 会被选项规则误吞成新题
        if option_match and current is not None:
            key = option_match.group(1).upper()
            text = option_match.group(2).strip()
            if text:
                current.options.append({"key": key, "text": text})
                continue

        if question_match:
            if current is not None:
                questions.append(current)
            current = Question(index=int(question_match.group(1)), stem=question_match.group(2))
            continue

        if current is not None and not current.options:
            current.stem = f"{current.stem} {line.strip()}".strip()
        elif current is not None:
            current.options.append({"key": "", "text": line.strip()})

    if current is not None:
        questions.append(current)

    for question in questions:
        texts = {o["text"] for o in question.options}
        if texts and texts <= TRUE_FALSE:
            question.question_type = "true_false"
        elif question.options:
            question.question_type = "choice"
        else:
            question.question_type = "essay"
    return questions


def normalize_answers(ticket: "Ticket", answers: list[str]) -> list[str]:
    """把字母答案（`A`、`AC`）转成选项原文（M5 安全网）。

    为什么必须转：A1 收到我们的答案后，是用**文本子序列匹配**去定位选项的
    （`api/base.py` study_work：`is_subsequence(t_res[0], o)` → `o[:1]`）。
    若我们回的是字母 `B`，匹配不上就会退化成 `random_answer()` 随机作答 ——
    那比不答更糟。所以这里按选项原文回填，匹配不上时保持原样（交给上游判定）。
    """
    if not ticket.questions:
        return [str(a) for a in answers]

    normalized: list[str] = []
    for index, answer in enumerate(answers):
        text = str(answer).strip()
        if not LETTER_ANSWER_RE.match(text):
            normalized.append(text)
            continue
        letters = [char.upper() for char in LETTER_SPLIT_RE.findall(text)]
        question = (
            ticket.questions[index]
            if index < len(ticket.questions)
            else ticket.questions[0]
        )
        picked = [o["text"] for o in question.options if o.get("key") in letters]
        normalized.append("\n".join(picked) if picked else text)
    return normalized


class AnswerBroker:
    """基于文件系统的工单队列。"""

    def __init__(
        self,
        root: str | Path,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        poll_interval_s: float = 0.05,
    ) -> None:
        self.root = Path(root)
        self.audit_path = self.root / "tickets.jsonl"
        self.pending_dir = self.root / "pending"
        self.answers_dir = self.root / "answers"
        self.timeout_s = timeout_s
        self._clock = clock
        self._sleep = sleeper
        self.poll_interval_s = poll_interval_s

    def ensure_dirs(self) -> None:
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self.answers_dir.mkdir(parents=True, exist_ok=True)
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    def _audit(self, ticket: Ticket) -> None:
        self.ensure_dirs()
        with self.audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(ticket.to_dict(), ensure_ascii=False) + "\n")

    def _pending_path(self, ticket_id: str) -> Path:
        return self.pending_dir / f"{ticket_id}.json"

    def _answer_path(self, ticket_id: str) -> Path:
        return self.answers_dir / f"{ticket_id}.json"

    # ------------------------------------------------------------------
    def create_ticket(
        self,
        raw_prompt: str,
        request_id: str = "",
        course_id: str = "",
        chapter_id: str = "",
        timeout_s: float | None = None,
        questions: list[Question] | None = None,
    ) -> Ticket:
        self.ensure_dirs()
        ticket = Ticket(
            ticket_id=f"tk_{uuid.uuid4().hex[:12]}",
            raw_prompt=raw_prompt,
            request_id=request_id,
            course_id=course_id,
            chapter_id=chapter_id,
            timeout_s=float(timeout_s if timeout_s is not None else self.timeout_s),
            questions=questions if questions is not None else extract_questions(raw_prompt),
        )
        self._pending_path(ticket.ticket_id).write_text(
            json.dumps(ticket.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self._audit(ticket)
        return ticket

    def get(self, ticket_id: str) -> Ticket | None:
        pending = self._pending_path(ticket_id)
        answer = self._answer_path(ticket_id)
        if not pending.is_file() and not answer.is_file():
            return None
        base: dict[str, Any] = {}
        if pending.is_file():
            base = json.loads(pending.read_text(encoding="utf-8"))
        if answer.is_file():
            base.update(json.loads(answer.read_text(encoding="utf-8")))
        return Ticket.from_dict(base)

    def is_answered(self, ticket_id: str) -> bool:
        return self._answer_path(ticket_id).is_file()

    def pending(self, limit: int = 20) -> list[Ticket]:
        self.ensure_dirs()
        items: list[Ticket] = []
        for path in sorted(self.pending_dir.glob("tk_*.json")):
            ticket_id = path.stem
            if self.is_answered(ticket_id):
                continue
            ticket = self.get(ticket_id)
            # 已过期/已丢弃的工单不再算"待答"：否则历史残留会一直出现在
            # answer_pending 里，把操控 Agent 引到上一轮的旧题上（实测踩过）
            if ticket is None or ticket.state in (TicketState.TIMEOUT, TicketState.DROPPED):
                continue
            items.append(ticket)
        items.sort(key=lambda t: t.created_at)
        return items[:limit]

    def expire_stale(
        self, reference_ts: float | None = None, margin_s: float = 0.0
    ) -> list[str]:
        """把超过自身等待上限仍未作答的工单标记为过期，返回被标记的工单号。

        `wait()` 超时只写审计日志，工单文件里仍是 pending —— 历史残留于是
        一直出现在 `answer_pending` 中（实测：实跑时把上一轮的残留工单误判成
        本轮题目）。这里按 `created_at + timeout_s` 判定并落盘状态。
        """
        self.ensure_dirs()
        now_ts = reference_ts if reference_ts is not None else time.time()
        expired: list[str] = []
        for path in sorted(self.pending_dir.glob("tk_*.json")):
            ticket = self.get(path.stem)
            if ticket is None or self.is_answered(ticket.ticket_id):
                continue
            if ticket.state in (TicketState.TIMEOUT, TicketState.DROPPED):
                continue
            try:
                created_ts = datetime.fromisoformat(ticket.created_at).timestamp()
            except ValueError:
                continue
            if now_ts - created_ts <= float(ticket.timeout_s) + margin_s:
                continue
            expired.append(ticket.ticket_id)

        if not expired:
            return []
        # 批量落盘：把 pending 文件里的状态改写为 timeout（用 ticket.to_dict 保持形状一致）
        for ticket_id in expired:
            ticket = self.get(ticket_id)
            if ticket is None:
                continue
            ticket.state = TicketState.TIMEOUT
            ticket.note = (
                f"工单过期：创建后 {ticket.timeout_s:.0f}s 内无人应答"
                f"（可用 `cx answer_clean` 批量清理）"
            )
            self._pending_path(ticket_id).write_text(
                json.dumps(ticket.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self._audit(ticket)
        return expired

    def submit(
        self, ticket_id: str, answers: list[str], answered_by: str = "agent"
    ) -> Ticket:
        self.ensure_dirs()
        ticket = self.get(ticket_id)
        if ticket is None:
            raise KeyError(f"工单不存在：{ticket_id}")
        # 安全网：字母答案 → 选项原文（见 normalize_answers 的说明）
        answers = normalize_answers(ticket, answers)
        payload = {
            "ticket_id": ticket_id,
            "state": str(TicketState.ANSWERED),
            "answers": [str(a) for a in answers],
            "answered_at": now_iso(),
            "answered_by": answered_by,
        }
        self._answer_path(ticket_id).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        merged = self.get(ticket_id)
        assert merged is not None
        self._audit(merged)
        return merged

    # ------------------------------------------------------------------
    def wait(self, ticket_id: str, timeout_s: float | None = None) -> Ticket:
        """阻塞等待答案，超时返回 `timeout` 状态的工单。

        **绝不允许无限等待**：上游是阻塞的同步进程，卡住会让整门课停摆。
        """
        limit = float(timeout_s if timeout_s is not None else self.timeout_s)
        deadline = self._clock() + limit
        while self._clock() < deadline:
            if self.is_answered(ticket_id):
                ticket = self.get(ticket_id)
                if ticket is not None:
                    return ticket
            self._sleep(self.poll_interval_s)

        ticket = self.get(ticket_id) or Ticket(ticket_id=ticket_id, raw_prompt="")
        if not self.is_answered(ticket_id):
            ticket.state = TicketState.TIMEOUT
            ticket.note = f"等待操控 Agent 应答超时（{limit:.0f}s）"
            self._audit(ticket)
        return ticket

    def stats(self) -> dict[str, Any]:
        """队列计数：待答 / 已答 / 过期，三者互斥且合计等于总数。"""
        self.ensure_dirs()
        total = 0
        answered = 0
        pending = 0
        timeout = 0
        dropped = 0
        for path in self.pending_dir.glob("tk_*.json"):
            total += 1
            if self.is_answered(path.stem):
                answered += 1
                continue
            ticket = self.get(path.stem)
            state = ticket.state if ticket is not None else TicketState.PENDING
            if state == TicketState.TIMEOUT:
                timeout += 1
            elif state == TicketState.DROPPED:
                dropped += 1
            else:
                pending += 1
        return {
            "total": total,
            "answered": answered,
            "pending": pending,
            "timeout": timeout,
            "dropped": dropped,
            "root": str(self.root),
        }

    # ------------------------------------------------------------------
    def to_openai_response(self, ticket: Ticket, model: str = "agent-in-the-loop") -> dict[str, Any]:
        """把工单答案包装成 OpenAI Chat Completions 响应。

        **格式已实测校准（M5，依据 A1 `api/answer.py:1313`）**：
            response = json.loads(remove_md_json_wrapper(content))
            return "\n".join(response["Answer"]).strip()

        即上游要的是**JSON 字符串** `{"Answer": ["答案文本", ...]}`，不是裸文本。
        注意A1 会先剥掉选项字母前缀（`re.sub(r"^[A-Z]\\s*", "", option)`）再发给
        模型，所以答案应是**选项内容文本**，不是 A/B/C 字母。

        未作答/超时时故意返回无法解析的中文提示：A1 的 except 分支会返回
        None（按"没搜到"处理，跳过该题）——这比返回空答案安全得多。
        """
        if ticket.state == TicketState.ANSWERED and ticket.answers:
            content = json.dumps(
                {"Answer": [str(a) for a in ticket.answers]}, ensure_ascii=False
            )
        else:
            content = "（平台无法作答：等待操控 Agent 应答超时或未配置 Agent）"

        return {
            "id": f"chatcmpl-{ticket.ticket_id}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": len(ticket.raw_prompt) // 2,
                "completion_tokens": len(content) // 2,
                "total_tokens": (len(ticket.raw_prompt) + len(content)) // 2,
            },
        }


def parse_answers(raw: str) -> list[str]:
    """把 Agent 提交的答案文本切成列表。

    支持三种输入：JSON 数组、换行分隔、分号分隔。
    宽松解析是有意的 —— Agent 产出的格式不可控。
    """
    text = (raw or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(item) for item in parsed]
        except json.JSONDecodeError:
            pass
    if "\n" in text:
        return [line.strip() for line in text.splitlines() if line.strip()]
    if ";" in text:
        return [part.strip() for part in text.split(";") if part.strip()]
    return [text]
