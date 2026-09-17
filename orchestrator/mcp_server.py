"""MCP 服务端（C36）—— 把统一命令暴露给 MCP Client / DeepSeek Agent。

实现说明（重要，不要当成完整 MCP 实现）：
这是 **M0 的最小实现**，用 stdlib 手写 JSON-RPC over stdio，不依赖官方
`mcp` SDK。好处是 M0 保持零第三方依赖；代价是只实现了 `initialize` /
`tools/list` / `tools/call` / `ping` 四个方法。

> ⚠️ 待办（M4）：改用官方 `mcp` SDK，并与真实 MCP Client（ZCode /
> Claude Desktop / Cursor）联调。**当前实现尚未经过真实客户端验证**，
> 不要把它当作已验收的能力。

stdio 传输按 MCP 约定：一行一个 JSON-RPC 消息。
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable, Iterable, TextIO

from . import __version__
from .bootstrap import Context, build

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "chaoxing-orchestrator"

_STR = {"type": "string"}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}
_STR_ARRAY = {"type": "array", "items": {"type": "string"}}


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": True,
    }


_COURSE = {"course_id": {**_STR, "description": "课程 ID（用 list_courses 获取）"}}
_TYPES = {
    "types": {
        **_STR_ARRAY,
        "description": "任务类型过滤：video/audio/document/ppt/reading/live/discussion/quiz",
    }
}


TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "list_courses",
        "description": "列出当前学习通账号下的全部课程，返回 course_id / 课名 / 教师 / clazz_id。任何涉及具体课程的操作都应先调用它确认真实 ID。",
        "inputSchema": _schema({}),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "get_course",
        "description": "获取单门课程的元数据（章节数、任务点总数、已完成数）。",
        "inputSchema": _schema(_COURSE, ["course_id"]),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "get_progress",
        "description": "查询学习进度。不传 course_id 时返回全部课程的总体进度与逐课进度。",
        "inputSchema": _schema({"course_id": _STR}),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "scan_tasks",
        "description": "扫描课程的全部任务点，返回每个任务点的类型（视频/音频/文档/PPT/阅读/直播/讨论/测验）、状态（已完成/待完成/未开放）与所属章节，并给出按类型/状态的汇总。这是判断'还差什么没做'的权威入口。",
        "inputSchema": _schema(
            {**_COURSE, "chapter_id": _STR, **_TYPES}, ["course_id"]
        ),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "get_homework",
        "description": "查询作业列表与未交作业的截止看板（按截止时间排序）。不传 course_id 时覆盖全部课程。",
        "inputSchema": _schema({"course_id": _STR}),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "get_notices",
        "description": "查询通知中心。注意：学习通服务端的关键词参数无效，此处为客户端过滤，检索范围仅最近约 100 条。",
        "inputSchema": _schema({"keyword": _STR, "unread_only": _BOOL}),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "get_schedule",
        "description": "查询课表。传 week 查看某一周（含真实日期），不传则看全学期汇总。",
        "inputSchema": _schema({"week": _INT}),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "run_course",
        "description": "【写操作】自动完成一门课的未完成任务点（视频/音频/文档/PPT 等）。默认不会真正执行，必须先加 dry_run=true 预览，再用 confirm=true 执行。quiz 类任务点若需作答，会通过 Agent 答题链路向你发起工单。",
        "inputSchema": _schema(
            {
                **_COURSE,
                "dry_run": {**_BOOL, "description": "true 只预览不产生任何写操作"},
                "confirm": {**_BOOL, "description": "确认真实写操作"},
                **_TYPES,
            },
            ["course_id"],
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
    {
        "name": "run_chapter",
        "description": "【写操作】只跑指定章节。同样需要先 dry_run 预览再 confirm。",
        "inputSchema": _schema(
            {**_COURSE, "chapter_id": _STR, "dry_run": _BOOL, "confirm": _BOOL},
            ["course_id", "chapter_id"],
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
    {
        "name": "run_video_tasks",
        "description": "【写操作】只跑视频类任务点（会过滤掉其他类型）。",
        "inputSchema": _schema(
            {**_COURSE, "chapter_id": _STR, "dry_run": _BOOL, "confirm": _BOOL},
            ["course_id"],
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
    {
        "name": "run_reading_tasks",
        "description": "【写操作】只跑阅读/文档/PPT 类任务点。",
        "inputSchema": _schema(
            {**_COURSE, "chapter_id": _STR, "dry_run": _BOOL, "confirm": _BOOL},
            ["course_id"],
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
    {
        "name": "status",
        "description": "查询任务状态与断点。传 all=true 列出该账号的全部历史断点。",
        "inputSchema": _schema({"request_id": _STR, "all": _BOOL}),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "pause",
        "description": "请求在下一个任务点边界优雅暂停（协作式）。暂停后已完成任务点会被记录为断点。",
        "inputSchema": _schema({"request_id": _STR, "note": _STR}),
        "annotations": {"readOnlyHint": False},
    },
    {
        "name": "resume",
        "description": "从断点继续执行。已完成的任务点会被跳过，因此只会跑剩余部分。",
        "inputSchema": _schema({"request_id": _STR}),
        "annotations": {"readOnlyHint": False},
    },
    {
        "name": "retry",
        "description": "重放未完成与失败的任务点（不是重启整个课程）。",
        "inputSchema": _schema({"request_id": _STR}),
        "annotations": {"readOnlyHint": False},
    },
    {
        "name": "stop",
        "description": "终止执行并保留断点（与 pause 的区别是：不计划恢复）。",
        "inputSchema": _schema({"request_id": _STR, "note": _STR}),
        "annotations": {"readOnlyHint": False},
    },
    {
        "name": "adapters",
        "description": "列出所有 Adapter：已注册的与已声明未接入的，含许可证、接入方式、支持的能力数量。用于判断某个能力由谁提供。",
        "inputSchema": _schema({}),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "probe",
        "description": "对全部 Adapter 做轻量探活，返回可用性矩阵。",
        "inputSchema": _schema({}),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "answer_pending",
        "description": "取出待答题目工单。当刷课过程遇到需要作答的测验时，题目会进入队列；你应取出工单、作答、再用 answer_submit 回填。",
        "inputSchema": _schema({"limit": _INT}),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "answer_submit",
        "description": "回填你对工单的答案。answers 可以是换行分隔的字符串（一题一行）、分号分隔，或 JSON 数组。多选题用 # 分隔选项（如 A#C）。",
        "inputSchema": _schema(
            {
                "ticket_id": {**_STR, "description": "工单 ID，来自 answer_pending"},
                "answers": {**_STR, "description": "答案：换行/分号分隔，或 JSON 数组"},
                "answered_by": _STR,
            },
            ["ticket_id", "answers"],
        ),
        "annotations": {"readOnlyHint": False},
    },
    {
        "name": "answer_stats",
        "description": "答题工单队列统计（待答/已答/总数）。",
        "inputSchema": _schema({}),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "sign_in",
        "description": "【写操作】执行签到。签到窗口通常只有几分钟，错过不可补。",
        "inputSchema": _schema(
            {
                "course_id": _STR,
                "type": {
                    **_STR,
                    "description": "签到类型：normal/location/qr/gesture/photo/code",
                },
                "dry_run": _BOOL,
                "confirm": _BOOL,
            }
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
    {
        "name": "sign_status",
        "description": "查询签到状态。",
        "inputSchema": _schema({"course_id": _STR}),
        "annotations": {"readOnlyHint": True},
    },
)


def tool_names() -> list[str]:
    return [tool["name"] for tool in TOOLS]


def _ok(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle_request(message: dict[str, Any], ctx: Context) -> dict[str, Any] | None:
    """处理一条 JSON-RPC 消息。返回 None 表示这是通知，不需响应。"""
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        return _ok(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
                "instructions": (
                    "学习通自动化能力平台。所有命令返回统一 Envelope，"
                    "含 ok / state / adapter / error / fallback_trace 字段。"
                    "写操作请先用 dry_run 预览。"
                ),
            },
        )

    if method in ("notifications/initialized", "initialized"):
        return None

    if method == "ping":
        return _ok(request_id, {})

    if method == "tools/list":
        return _ok(request_id, {"tools": list(TOOLS)})

    if method == "tools/call":
        name = str(params.get("name", ""))
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _error(request_id, -32602, "arguments 必须是对象")
        if name not in tool_names():
            return _error(request_id, -32602, f"未知工具：{name}")

        confirmed = bool(arguments.pop("confirm", False))
        ctx.orchestrator.confirmed = confirmed
        envelope = ctx.orchestrator.run(name, dict(arguments))
        payload = envelope.to_dict()
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        return _ok(
            request_id,
            {
                "content": [{"type": "text", "text": text}],
                "isError": not envelope.ok,
            },
        )

    return _error(request_id, -32601, f"未实现的方法：{method}")


def serve(
    ctx: Context,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> None:
    """stdio 循环：一行一条 JSON-RPC 消息。"""
    source = stdin or sys.stdin
    sink = stdout or sys.stdout
    for line in source:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            sink.write(
                json.dumps(_error(None, -32700, "JSON 解析失败"), ensure_ascii=False) + "\n"
            )
            sink.flush()
            continue
        response = handle_request(message, ctx)
        if response is None:
            continue
        sink.write(json.dumps(response, ensure_ascii=False) + "\n")
        sink.flush()


def main(argv: Iterable[str] | None = None) -> int:
    _ = argv
    ctx = build()
    serve(ctx)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
