"""统一命令入口（C34）。

用法示例：
    python -m orchestrator.cli list_courses
    python -m orchestrator.cli scan_tasks --course-id 240100001
    python -m orchestrator.cli run_chapter --course-id 240100001 --chapter-id ch_02 --dry-run
    python -m orchestrator.cli run_course --course-id 240100001 --confirm
    python -m orchestrator.cli status --all
    python -m orchestrator.cli adapters
    python -m orchestrator.cli shim-serve --port 8765

所有命令输出统一 Envelope（`--json` 给机器读，默认给人读）。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Sequence

from . import __version__
from .bootstrap import build
from .errors import Codes
from .models import Envelope, TaskState
from .services import COMMANDS

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_BLOCKED = 3

_READ_COMMANDS = tuple(n for n, s in COMMANDS.items() if s.kind == "read")
_WRITE_COMMANDS = tuple(n for n, s in COMMANDS.items() if s.kind == "write")
_CONTROL_COMMANDS = tuple(n for n, s in COMMANDS.items() if s.kind == "control")
_AUX_COMMANDS = tuple(n for n, s in COMMANDS.items() if s.kind == "aux")
_AGENT_COMMANDS = tuple(n for n, s in COMMANDS.items() if s.kind == "agent")
_SIGN_COMMANDS = tuple(n for n, s in COMMANDS.items() if s.kind == "sign")
_COOKIE_COMMANDS = tuple(n for n, s in COMMANDS.items() if s.kind == "cookie")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orchestrator",
        description="学习通自动化能力平台 · 统一命令层",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "命令分组：\n"
            f"  读       {', '.join(_READ_COMMANDS)}\n"
            f"  写       {', '.join(_WRITE_COMMANDS)}\n"
            f"  控制     {', '.join(_CONTROL_COMMANDS)}\n"
            f"  辅助     {', '.join(_AUX_COMMANDS)}\n"
            f"  Agent    {', '.join(_AGENT_COMMANDS)}\n"
            f"  签到     {', '.join(_SIGN_COMMANDS)}\n"
            f"  Cookie   {', '.join(_COOKIE_COMMANDS)}\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"orchestrator {__version__}")
    parser.add_argument("--account", default=None, help="账号 ID（默认取第一个已存在的）")
    parser.add_argument("--root", default=None, help="仓库根目录（默认自动推断）")
    parser.add_argument("--json", action="store_true", help="输出原始 JSON Envelope")
    parser.add_argument("--quiet", action="store_true", help="不输出人读摘要")
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="打印结构化日志到 stderr"
    )

    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help_text)

    # ---- 读 ----
    add("list_courses", "列出全部课程")
    p = add("get_course", "单门课元数据")
    p.add_argument("--course-id", required=True)
    p = add("get_progress", "学习进度")
    p.add_argument("--course-id", default=None)
    p = add("scan_tasks", "扫描任务点并按类型汇总")
    p.add_argument("--course-id", required=True)
    p.add_argument("--chapter-id", default=None)
    p.add_argument("--types", default=None, help="逗号分隔，如 video,reading")
    p = add("get_homework", "作业列表与截止看板")
    p.add_argument("--course-id", default=None)
    p = add("get_notices", "通知中心")
    p.add_argument("--keyword", default=None)
    p.add_argument("--unread-only", action="store_true")
    p = add("get_schedule", "课表")
    p.add_argument("--week", type=int, default=None)
    add("list_exams", "考试安排")
    p = add("list_materials", "章节与资料")
    p.add_argument("--course-id", required=True)
    p = add("download_material", "下载课程资料")
    p.add_argument("--course-id", required=True)
    p.add_argument("--data-id", default=None)
    p.add_argument("--save-dir", default="./downloads")

    # ---- 写 ----
    for name, help_text in (
        ("run_course", "跑完一门课"),
        ("run_video_tasks", "只跑视频任务点"),
        ("run_reading_tasks", "只跑阅读/文档任务点"),
    ):
        p = add(name, help_text)
        p.add_argument("--course-id", required=True)
        p.add_argument("--chapter-id", default=None)
        p.add_argument("--types", default=None, help="覆盖默认类型过滤")
        p.add_argument("--speed", type=float, default=None)
        p.add_argument("--dry-run", action="store_true", help="只预览不写")
        p.add_argument("--confirm", action="store_true", help="确认真实写操作")
    p = add("run_chapter", "只跑指定章节")
    p.add_argument("--course-id", required=True)
    p.add_argument("--chapter-id", required=True)
    p.add_argument("--types", default=None)
    p.add_argument("--speed", type=float, default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--confirm", action="store_true")

    # ---- 控制 ----
    p = add("status", "查询任务状态")
    p.add_argument("--request-id", default=None)
    p.add_argument("--all", action="store_true")
    for name, help_text in (
        ("pause", "在任务点边界暂停"),
        ("resume", "从断点继续"),
        ("retry", "重放未完成/失败的任务点"),
        ("stop", "终止并保留断点"),
    ):
        p = add(name, help_text)
        p.add_argument("--request-id", default=None)
        p.add_argument("--note", default=None)
        if name in ("resume", "retry"):
            # resume / retry 会真实推进平台上的任务点，所以与 run_* 一样
            # 需要显式确认，避免"绕过确认直接写"的旁路。
            p.add_argument("--confirm", action="store_true", help="确认真实写操作")

    # ---- 辅助 ----
    add("adapters", "列出 Adapter 与能力声明")
    add("probe", "探活全部 Adapter")
    add("accounts", "列出账号与会话状态")
    add("capabilities", "列出能力注册表与覆盖情况")

    # ---- Agent 答题 ----
    p = add("answer_pending", "取出待答工单")
    p.add_argument("--limit", type=int, default=10)
    p = add("answer_submit", "回填 Agent 答案")
    p.add_argument("--ticket-id", required=True)
    p.add_argument("--answers", required=True, help="换行/分号分隔，或 JSON 数组")
    p.add_argument("--answered-by", default="agent")
    add("answer_stats", "工单队列统计")
    p = add("shim_config", "输出上游需要的 config.ini 片段")
    p.add_argument("--port", type=int, default=8765)

    # ---- 签到 ----
    p = add("sign_in", "执行签到")
    p.add_argument("--course-id", default=None)
    p.add_argument("--type", default="normal")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--confirm", action="store_true")
    p = add("sign_status", "查询签到状态")
    p.add_argument("--course-id", default=None)
    p = add("sign_watch", "轮询监测新签到")
    p.add_argument("--interval", type=int, default=30)

    # ---- Cookie 管理 ----
    add("cookies", "查看已落盘的 cookie 与体检报告（值已掩码）")
    p = add("cookies_import", "从 cookie 字符串或文件导入")
    p.add_argument(
        "--header",
        default=None,
        help='DevTools 里复制的 Cookie 请求头的值，如 "UID=xxx; _d=yyy"',
    )
    p.add_argument("--file", default=None, help="从文件导入（.txt=Netscape，.json=本平台格式）")
    p.add_argument(
        "--format", default=None, choices=["header", "netscape", "json"],
        help="显式指定格式（默认按文件扩展名/内容自动判断）",
    )
    p.add_argument("--source", default=None, help="来源标注，写进 cookies.json 便于追溯")
    p.add_argument(
        "--domain", default=None,
        help="给缺域名的 cookie 补的域名（默认 .chaoxing.com；header 串没有域名信息）",
    )
    p.add_argument("--keep-all", action="store_true", help="保留非学习通域名的 cookie")
    p = add("cookies_extract", "通过 CDP 从独立浏览器实例提取 cookie")
    p.add_argument("--port", type=int, default=None, help="CDP 端口（默认 9333）")
    p.add_argument("--keep-all", action="store_true", help="保留非学习通域名的 cookie")
    add("cookies_clear", "清除已落盘的 cookie 文件")

    p = add("cookies_login", "一站式登录：启动独立浏览器 → 等你登录 → 自动提取落盘")
    p.add_argument("--port", type=int, default=None, help="CDP 端口（默认 9333）")
    p.add_argument("--url", default=None, help="登录地址（默认 https://i.chaoxing.com）")
    p.add_argument("--timeout", type=float, default=None, help="等待登录的秒数（默认 300）")
    p.add_argument("--poll", type=float, default=None, help="轮询间隔秒（默认 3）")
    p.add_argument("--min-delta", type=int, default=None, help="cookie 增长阈值（默认 3）")
    p.add_argument("--no-open", action="store_true", help="不启动浏览器（窗口已在运行）")
    p.add_argument("--keep-all", action="store_true", help="保留非学习通域名的 cookie")

    p = add("cookies_verify", "用已落盘 cookie 真实请求一次平台，验证会话有效性")
    p.add_argument("--url", default=None, help="探测地址（默认 https://i.chaoxing.com/base）")
    p.add_argument("--timeout", type=float, default=None, help="请求超时秒（默认 15）")

    # ---- 代理服务（独立进程，不是 Orchestrator 命令） ----
    p = add("shim-serve", "启动本地 OpenAI 兼容代理（阻塞）")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--api-key", default="local-agent")

    return parser


def _params_from_args(args: argparse.Namespace) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for key, value in vars(args).items():
        if key in ("command", "account", "root", "json", "quiet", "verbose", "confirm"):
            continue
        if value is None:
            continue
        if key == "types" and isinstance(value, str):
            params["types"] = [t.strip() for t in value.split(",") if t.strip()]
            continue
        params[key.replace("-", "_")] = value
    return params


def _exit_code(envelope: Envelope) -> int:
    if envelope.ok:
        return EXIT_OK
    if envelope.state == str(TaskState.BLOCKED):
        return EXIT_BLOCKED
    error = envelope.error or {}
    if error.get("code") in (Codes.INVALID_PARAM,):
        return EXIT_USAGE
    return EXIT_FAILED


def _render(envelope: Envelope) -> str:
    lines: list[str] = []
    status = "OK  " if envelope.ok else "FAIL"
    lines.append(
        f"[{status}] {envelope.command}  state={envelope.state}  "
        f"{envelope.duration_ms}ms  adapter={envelope.adapter or '-'}"
    )
    if envelope.adapter_version:
        lines.append(f"        adapter_version = {envelope.adapter_version}")

    data = envelope.data
    if isinstance(data, dict):
        lines.extend(_render_data(envelope.command, data))

    for warning in envelope.warnings:
        lines.append(f"  ! {warning}")

    if envelope.error:
        lines.append(
            f"  x [{envelope.error.get('category')}] {envelope.error.get('message')}"
        )
    if envelope.fallback_trace:
        lines.append("  fallback_trace:")
        for entry in envelope.fallback_trace:
            mark = "ok" if entry.get("ok") else "fail"
            detail = entry.get("error_code") or entry.get("reason") or ""
            lines.append(
                f"    - {entry.get('adapter')} [{mark}] {detail}"
                + ("  (skipped)" if entry.get("skipped") else "")
            )
    for action in envelope.next_actions:
        lines.append(f"  -> {action}")
    return "\n".join(lines)


def _render_data(command: str, data: dict[str, Any]) -> list[str]:
    out: list[str] = []

    if command == "list_courses" and data.get("courses") is not None:
        for course in data["courses"]:
            out.append(
                f"  - {course.get('course_id')}  {course.get('name')}  "
                f"教师={course.get('teacher')}  clazz={course.get('clazz_id')}"
            )
        return out

    if command == "scan_tasks":
        summary = data.get("summary") or {}
        out.append(
            f"  任务点 {summary.get('total')} 个  "
            f"按类型={summary.get('by_type')}  按状态={summary.get('by_status')}"
        )
        for point in data.get("task_points") or []:
            flag = " [含图]" if point.get("needs_image") else ""
            out.append(
                f"  - {point.get('task_point_id')}  {point.get('type'):<10} "
                f"{point.get('status'):<8} {point.get('chapter_name')} / "
                f"{point.get('title')}{flag}"
            )
        return out

    if command == "adapters":
        for row in data.get("adapters") or []:
            state = "已注册" if row.get("registered") else "已声明未接入"
            out.append(
                f"  - {row.get('adapter'):<20} {row.get('kind'):<11} "
                f"{row.get('license'):<20} 能力 {row.get('supported_count'):>2} 项  {state}"
            )
        return out

    if command == "probe":
        for row in data.get("adapters") or []:
            mark = "healthy" if row.get("healthy") else "unhealthy"
            out.append(
                f"  - {row.get('adapter'):<20} {mark:<10} {row.get('detail')}"
            )
        return out

    if command == "capabilities":
        coverage = data.get("coverage") or {}
        out.append(
            f"  能力总数 {coverage.get('total_capabilities')}  "
            f"已覆盖 {coverage.get('covered')}  缺口 {coverage.get('uncovered')}"
        )
        if coverage.get("gap_ids"):
            out.append(f"  缺口能力：{', '.join(coverage['gap_ids'])}")
        return out

    if command == "status":
        checkpoints = data.get("checkpoints")
        if checkpoints is not None:
            if not checkpoints:
                out.append("  （无断点记录）")
            for row in checkpoints:
                out.append(
                    f"  - {row.get('request_id')}  state={row.get('state'):<20} "
                    f"course={row.get('course_id')} "
                    f"完成={len(row.get('completed_task_points') or [])} "
                    f"失败={len(row.get('failed_task_points') or [])} "
                    f"跳过={len(row.get('skipped_task_points') or [])}  "
                    f"updated={row.get('updated_at')}"
                )
            out.append(f"  共 {data.get('count')} 条断点")
        else:
            checkpoint = data.get("checkpoint")
            if checkpoint:
                out.append(
                    f"  request={checkpoint.get('request_id')} state={checkpoint.get('state')} "
                    f"course={checkpoint.get('course_id')} "
                    f"完成={len(checkpoint.get('completed_task_points') or [])} "
                    f"失败={len(checkpoint.get('failed_task_points') or [])} "
                    f"跳过={len(checkpoint.get('skipped_task_points') or [])}"
                )
            else:
                out.append("  （无断点记录）")
        for signal in data.get("control") or []:
            out.append(f"  控制信号：{signal}")
        return out

    if command == "accounts":
        for row in data.get("accounts") or []:
            ready = "就绪" if row.get("ready") else "未登录"
            out.append(
                f"  - {row.get('account_id'):<12} {row.get('phone_masked') or '(无)':<16} "
                f"{ready}"
            )
        return out

    if command == "get_progress":
        overall = data.get("overall") or {}
        out.append(
            f"  总进度 {overall.get('done')}/{overall.get('total')} "
            f"({(overall.get('ratio') or 0) * 100:.1f}%)"
        )
        for row in data.get("courses") or []:
            out.append(
                f"  - {row.get('name')}: {row.get('done')}/{row.get('total')}"
            )
        return out

    if command == "get_homework":
        for row in data.get("deadlines") or []:
            out.append(
                f"  - [{row.get('course_id')}#{row.get('index')}] {row.get('title')} "
                f"截止 {row.get('due_at')}"
            )
        if not data.get("deadlines"):
            out.append("  （无未交作业）")
        return out

    if command == "get_notices":
        for row in data.get("notices") or []:
            flag = " [未读]" if row.get("unread") else ""
            out.append(f"  - {row.get('time') or '时间未知'}  {row.get('title')}{flag}")
            content = str(row.get("content") or "").replace("\r", "")
            if content:
                first = content.strip().splitlines()[0] if content.strip() else ""
                out.append(f"      {first[:80]}")
        if not data.get("notices"):
            out.append("  （没有通知）")
        return out

    if command == "get_schedule":
        fw = data.get("first_week_date")
        if fw:
            out.append(f"  首周日期：{fw}")
        for les in data.get("lessons") or []:
            out.append(
                f"  - 周{les.get('weekday')}  {les.get('name')}  "
                f"{les.get('section')}  {les.get('location')}  {les.get('teacher')}"
            )
        if not data.get("lessons"):
            out.append("  （本周无排课记录）")
        return out

    if command in ("list_materials", "get_course"):
        if data.get("progress"):
            out.append(
                f"  章节任务点进度：{data['progress'].get('done')}/"
                f"{data['progress'].get('total')}"
            )
        for ch in data.get("chapters") or []:
            status = ch.get("status_text") or ""
            mark = f"  [{status}]" if status else ""
            indent = "    " if str(ch.get("parent_id") or "") else "  "
            out.append(f"{indent}- {ch.get('index')}  {ch.get('name')}{mark}")
        if not data.get("chapters"):
            out.append("  （该课程没有返回章节，可能未开放章节功能）")
        return out

    if command == "answer_pending":
        for ticket in data.get("tickets") or []:
            out.append(
                f"  - {ticket.get('ticket_id')}  题数={len(ticket.get('questions') or [])}  "
                f"{ticket.get('created_at')}"
            )
        if not data.get("tickets"):
            out.append("  （当前没有待答工单）")
        return out

    if command == "shim_config":
        out.append(data.get("config_ini_snippet", ""))
        return out

    if command == "cookies":
        if not data.get("present"):
            out.append("  （尚未落盘任何 cookie）")
            out.append("  下一步：启动独立浏览器 → 登录学习通 → cookies_extract")
            return out
        out.append(
            f"  共 {data.get('total')} 条｜学习通相关 {data.get('chaoxing_related')} 条"
            f"｜会话型 {data.get('session_cookies')}｜已过期 {data.get('expired')}"
        )
        out.append(f"  来源：{data.get('source')}  采集于 {data.get('captured_at')}")
        for domain, count in (data.get("domains") or {}).items():
            out.append(f"  - {domain:<32} {count} 条")
        if data.get("expired_names"):
            out.append(f"  已过期：{', '.join(data['expired_names'])}")
        out.append(f"  json : {data.get('json_path')}")
        out.append(f"  txt  : {data.get('netscape_path')}")
        out.append(f"  hdr  : {data.get('header_path')}")
        return out

    if command in ("cookies_import", "cookies_extract"):
        diagnose = data.get("diagnose") or {}
        out.append(
            f"  已落盘 {data.get('kept')} 条（丢弃非学习通域名 {data.get('dropped')} 条）"
        )
        if diagnose:
            out.append(
                f"  会话型 {diagnose.get('session_cookies')}｜已过期 {diagnose.get('expired')}"
                f"｜涉及域名 {len(diagnose.get('domains') or {})} 个"
            )
        for label, path in (data.get("files") or {}).items():
            out.append(f"  {label:<9}: {path}")
        return out

    if command == "cookies_login":
        out.append(
            f"  检测方式：{data.get('detected')}｜cookie {data.get('baseline')} → "
            f"{data.get('final_count')} 条（阈值 {data.get('threshold')}，"
            f"轮询 {data.get('rounds')} 次）"
        )
        if data.get("browser"):
            out.append(f"  浏览器：{data.get('browser')}")
        out.append(f"  CDP 端口：{data.get('port')}")
        for label, path in (data.get("files") or {}).items():
            out.append(f"  {label:<9}: {path}")
        return out

    if command == "cookies_verify":
        out.append(
            f"  判定：{data.get('verdict')}｜HTTP {data.get('status')}"
            f"｜响应 {data.get('body_size')}B｜耗时 {data.get('elapsed_ms')}ms"
            f"｜携带 cookie {data.get('cookie_count')} 条"
        )
        for signal in data.get("signals") or []:
            out.append(f"  · {signal}")
        if data.get("location"):
            out.append(f"  重定向到：{data.get('location')}")
        return out

    if command == "cookies_clear":
        out.append(f"  已删除 {data.get('count')} 个文件")
        for path in data.get("removed") or []:
            out.append(f"  - {path}")
        return out

    out.append("  " + json.dumps(data, ensure_ascii=False))
    return out


def _no_command_guide() -> str:
    """无参数时的引导。

    **为什么必须存在**：这个入口会被双击（尤其 `cx.cmd`）。双击时没有
    参数，argparse 只会打印一句
    `error: the following arguments are required: COMMAND`
    然后窗口一闪就关 —— 使用者看到的是"什么都没发生"，而这正是
    "拉不起来浏览器"报告的真实来源：
    双击 `cx.cmd` 本来就不会启动浏览器，它只是个命令入口。

    所以这里给一份能直接照抄的引导，并把"该双击哪个文件"讲清楚。
    中文由 Python 输出（Windows 上走 WriteConsoleW），不受控制台代码页影响。
    """
    columns = [
        ("cookies_login", "启动独立浏览器并等待你登录，然后自动保存 cookie"),
        ("cookies_verify", "真实请求一次平台，验证会话是否有效"),
        ("cookies", "查看已落盘的 cookie（掩码输出）"),
        ("adapters", "查看适配器、许可证与接入状态"),
        ("list_courses", "课程列表（真实数据来自 xuexitong-mcp 适配器）"),
        ("--help", "查看全部命令"),
    ]
    width = max(len(name) for name, _ in columns)
    body = [f"    {name:<{width}}  {text}" for name, text in columns]
    lines = [
        "",
        "=" * 70,
        "  学习通自动化能力平台",
        "=" * 70,
        "",
        "  这是一个【命令行工具】，必须带上命令名才能工作。",
        "",
        "  >>> 想启动浏览器去登录 —— 不要双击本文件，请双击：",
        "          启动独立浏览器.cmd",
        "",
        "  在终端里这样用（把 <命令> 换成下表任意一条）：",
        "      python -m orchestrator.cli <命令>",
        "",
        *body,
        "",
        "  完整上手说明见 docs/08-使用手册.md",
        "",
        "=" * 70,
        "",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(argv) if argv is not None else sys.argv[1:]

    # 无参数 = 多半是双击进来的。给引导，而不是让 argparse 吐一句
    # 英文 usage 然后窗口闪退。
    if not raw:
        print(_no_command_guide())
        return EXIT_OK

    parser = build_parser()
    args = parser.parse_args(raw)

    echo = None
    if args.verbose and not args.quiet:
        def echo(line: str) -> None:  # noqa: E731
            print(line, file=sys.stderr, flush=True)

    ctx = build(root=args.root, echo=echo)

    if args.command == "shim-serve":
        from .openai_shim import serve

        def shim_echo(line: str) -> None:
            print(line, file=sys.stderr, flush=True)

        print(
            f"本地 OpenAI 兼容代理已启动：http://{args.host}:{args.port}/v1\n"
            f"把上游 config.ini 的 endpoint 指到这里即可（key={args.api_key}）",
            file=sys.stderr,
            flush=True,
        )
        serve(ctx.broker, host=args.host, port=args.port, api_key=args.api_key, echo=shim_echo)
        return EXIT_OK

    if args.command not in COMMANDS:
        print(f"未知命令：{args.command}", file=sys.stderr)
        return EXIT_USAGE

    ctx.orchestrator.confirmed = bool(getattr(args, "confirm", False))
    envelope = ctx.orchestrator.run(
        args.command,
        _params_from_args(args),
        account_id=args.account,
    )

    if args.json:
        print(json.dumps(envelope.to_dict(), ensure_ascii=False, indent=2))
    elif not args.quiet:
        print(_render(envelope))

    return _exit_code(envelope)


if __name__ == "__main__":
    raise SystemExit(main())
