"""Xuexitong-mcp worker：Adapter 与上游之间的进程边界。

协议（stdin → stdout，各一行 JSON）：
    请求: {"upstream_path": "...", "config_dir": "...", "op": "...", "args": {...}}
    成功: {"ok": true,  "data": ...}
    失败: {"ok": false, "code": "...", "message": "...", "hint": "..."}

退出码（Adapter 据此映射 ErrorCategory，**不在 Adapter 里重复判定逻辑**）：
    0 成功；3 登录态失效(auth)；2 平台侧失败(platform_changed/transient)；
    4 依赖缺失(internal，附安装提示)；1 其他内部错误。

本文件是统一层自研的"胶水"：只做参数转发与异常翻译，不复制上游的
任何业务逻辑（红线 R3）。上游代码在 `upstreams/` 下只读（红线 R2）。
"""

from __future__ import annotations

import json
import sys

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_PLATFORM = 2
EXIT_AUTH = 3
EXIT_DEPS = 4

INSTALL_HINT = (
    "本命令需要上游依赖 requests/pycryptodome。"
    "请用运行 orchestrator 的同一个解释器执行："
    "python -m pip install requests pycryptodome"
)


def _emit(payload: dict) -> None:
    # default=str：上游偶尔返回 datetime 等不可序列化对象，转字符串而不是崩
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str))
    sys.stdout.write("\n")


def _fail(code: str, message: str, exit_code: int, hint: str = "") -> int:
    _emit({"ok": False, "code": code, "message": message, "hint": hint})
    return exit_code


def resolve_course(client, course_id):
    """把统一层的 course_id 解析成上游的 course dict。

    上游 course dict 的主键是 `courseid`/`clazzid`/`cpi`。course_id 允许
    三种写法：`courseid`、`courseid:clazzid`、或课名关键词（find_course）。
    """
    if not course_id:
        return None
    courses = client.fetch_courses()
    raw = str(course_id).strip()
    if ":" in raw:
        cid, _, clazzid = raw.partition(":")
        for c in courses:
            if str(c.get("courseid")) == cid and str(c.get("clazzid")) == clazzid:
                return c
    for c in courses:
        if str(c.get("courseid")) == raw:
            return c
    found = client.find_course(raw)
    if found is not None:
        return found
    raise KeyError(f"课程不存在或不可见：{course_id!r}（共 {len(courses)} 门课）")


def main() -> int:
    try:
        request = json.loads(sys.stdin.read())
    except json.JSONDecodeError as exc:
        return _fail("BAD_REQUEST", f"worker 输入不是合法 JSON：{exc}", EXIT_INTERNAL)

    upstream_path = request.get("upstream_path") or ""
    config_dir = request.get("config_dir") or ""
    op = request.get("op") or ""
    args = request.get("args") or {}

    if not upstream_path:
        return _fail("BAD_REQUEST", "缺少 upstream_path", EXIT_INTERNAL)
    sys.path.insert(0, upstream_path)

    try:
        from xuexitong_mcp.client import ChaoxingClient
    except ImportError:
        return _fail("DEPS_MISSING", "上游依赖不可用", EXIT_DEPS, INSTALL_HINT)

    client = ChaoxingClient(config_dir=config_dir or None)

    try:
        if op == "ping":
            data = {"ok": True}
        elif op == "fetch_courses":
            data = client.fetch_courses(refresh=bool(args.get("refresh")))
        elif op == "fetch_profile":
            data = client.fetch_profile()
        elif op == "fetch_course_meta":
            course = resolve_course(client, args.get("course_id"))
            data = course
        elif op == "fetch_progress_overview":
            data = client.fetch_progress_overview()
        elif op == "fetch_chapters":
            course = resolve_course(client, args.get("course_id"))
            if course is None:
                return _fail("INVALID_PARAM", "fetch_chapters 需要 course_id", EXIT_INTERNAL)
            data = client.fetch_chapters(course, with_nodes=True)
        elif op == "fetch_homework":
            course = resolve_course(client, args.get("course_id"))
            if course is None:
                return _fail("INVALID_PARAM", "fetch_homework 需要 course_id", EXIT_INTERNAL)
            data = client.fetch_homework(course)
        elif op == "fetch_homework_detail":
            course = resolve_course(client, args.get("course_id"))
            if course is None:
                return _fail("INVALID_PARAM", "fetch_homework_detail 需要 course_id", EXIT_INTERNAL)
            data = client.fetch_homework_detail(course, index=int(args.get("index") or 1))
        elif op == "fetch_deadline_overview":
            # course 键兼容：统一层历史上有传 course / course_id 两种写法
            data = client.fetch_deadline_overview(
                course=args.get("course") or args.get("course_id")
            )
        elif op == "fetch_exams":
            data = client.fetch_exams()
        elif op == "fetch_notices":
            data = client.fetch_notices(
                limit=int(args.get("limit") or 20),
                unread_only=bool(args.get("unread_only")),
                keyword=str(args.get("keyword") or ""),
            )
        elif op == "fetch_schedule":
            data = client.fetch_schedule(week=args.get("week"))
        else:
            return _fail("UNKNOWN_OP", f"未知 op：{op}", EXIT_INTERNAL)
    except KeyError as exc:
        return _fail("COURSE_NOT_FOUND", str(exc.args[0] if exc.args else exc), EXIT_PLATFORM)
    except RuntimeError as exc:
        # 上游用 RuntimeError 表达"平台拒绝了这次请求"（如签名校验失败）
        return _fail("PLATFORM_REJECTED", str(exc), EXIT_PLATFORM)
    except Exception as exc:  # noqa: BLE001 —— worker 边界必须兜住一切
        return _fail("WORKER_ERROR", f"{type(exc).__name__}: {exc}", EXIT_INTERNAL)

    _emit({"ok": True, "data": data})
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
