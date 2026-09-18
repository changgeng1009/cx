"""Samueli924/chaoxing worker：写侧 Adapter 与上游（GPL-3.0）之间的进程边界。

协议（stdin 一行请求 JSON → stdout 多行事件 JSON + 最后一行 summary JSON）：
    请求: {"upstream_path": "...", "config_dir": "...", "op": "...", "args": {...}}
    事件: {"event": "chapter_start"|"job_done"|"skipped", ...}
    结果: {"ok": true, "data": {...summary...}} 或 {"ok": false, "code": "...", ...}

控制：父进程向 stdin 写一行 "cancel" 或 "pause"，worker 在**下一个任务点
边界**优雅停止（协作式，不打断正在播放的视频）。

退出码：0 成功；3 登录失败(auth)；4 依赖缺失；1 内部错误。

隔离说明（R1/R3）：本文件运行在**子进程**里，通过运行时注入的路径加载
GPL 上游——这是进程边界方案本身。上游的答题/刷课业务逻辑全部留在上游
函数（`process_job` 等）中执行，本文件只做**编排**：循环、过滤、事件上报。
"""

from __future__ import annotations

import json
import os
import sys
import threading

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_AUTH = 3
EXIT_DEPS = 4

INSTALL_HINT = (
    "本命令需要写侧上游依赖。请用运行 orchestrator 的同一个解释器执行："
    "python -m pip install requests pyaes beautifulsoup4 lxml loguru tqdm "
    "openai fonttools tenacity chardet httpx ddddocr"
)

#: 上游 job["type"] → 统一层 TaskType
_JOB_TYPE_MAP = {
    "video": "video",
    "document": "document",
    "read": "reading",
    "workid": "quiz",
    "live": "live",
}


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _fail(code: str, message: str, exit_code: int, hint: str = "") -> int:
    _emit({"ok": False, "code": code, "message": message, "hint": hint})
    return exit_code


class ControlFlag:
    """父进程控制信号。监听线程把 stdin 的行变成标志位。"""

    def __init__(self) -> None:
        self.cancelled = threading.Event()
        self.paused = threading.Event()

    def listen_forever(self) -> None:
        for raw in sys.stdin:
            line = raw.strip().lower()
            if line == "cancel":
                self.cancelled.set()
                return
            if line == "pause":
                self.paused.set()
                return


def prepare_upstream_env(upstream_path: str) -> None:
    """把上游的资源基准指到上游目录（字体反爬映射表必需）。

    上游 `api/cxsecret_font.resource_path()` 的基准是 `sys._MEIPASS`（PyInstaller
    兼容钩子），未打包时退化为 `os.path.abspath(".")` 即**当前工作目录**。
    我们的 worker 为了 cookie 桥接把 CWD 固定在账号工作区，因此上游会找不到
    `resource/font_map_table.json`（1.5MB 字体哈希表）→ 字体反爬解不开 →
    **题目正文变成乱码字**（实测：整道题不可读，M5 直接失效）。

    这里复用上游自带的 `_MEIPASS` 钩子：不 chdir（cookie 相对路径不受影响）、
    不改上游一行代码，只是把基准指回上游目录。
    """
    upstream = os.path.abspath(upstream_path)
    if upstream not in sys.path:
        sys.path.insert(0, upstream)
    sys._MEIPASS = upstream  # type: ignore[attr-defined]


def build_tiku(upstream_path: str, shim_endpoint: str, submit: bool = False) -> Any:
    """构造 A1 的 AI 题库 provider，把题目请求引到本地 shim。

    M5 的关键接线：A1 以为自己在问大模型，实际 endpoint 指向统一层的
    `openai_shim`（127.0.0.1:8765/v1），题目被转成待答工单交给操控 Agent。

    配置键按 A1 `AI._init_tiku` 的读取项给全，缺键会 KeyError。
    submit=False（默认）：答完**不自动交卷**，由使用者显式开启 ——
    章节检测提交不可逆，默认值必须偏保守。
    """
    prepare_upstream_env(upstream_path)
    # 本地 shim 绝不能被系统代理劫持：A1 用 openai SDK（httpx）且默认
    # trust_env=True，会把发往 127.0.0.1 的请求也交给 HTTP_PROXY
    # （实测表现：HTTP 502 Bad Gateway，整条答题链路静默失效）。
    os.environ["NO_PROXY"] = "127.0.0.1,localhost,::1"
    os.environ["no_proxy"] = os.environ["NO_PROXY"]

    from api.answer import Tiku  # noqa: PLC0415 —— 子进程内动态加载上游

    conf = {
        "provider": "AI",
        "endpoint": shim_endpoint,
        "key": "local-agent",
        "model": "agent-in-the-loop",
        "http_proxy": "",
        "min_interval_seconds": "0",
        "check_llm_connection": "false",
        "submit": "true" if submit else "false",
        "cover_rate": "0.9",
        "true_list": "正确,对,√,是",
        "false_list": "错误,错,×,否,不对,不正确",
    }
    tiku = Tiku.get_tiku_from_config(config=conf)
    tiku.init_tiku()
    return tiku


def build_client(upstream_path: str, tiku: Any = None):
    """绕开上游 main.init_chaoxing（带交互/题库），直接构造客户端。

    tiku=None：不启用答题（M2 默认，quiz 任务点被编排层跳过）；
    M5 起由 build_tiku() 注入 AI provider，把题目转给本地 shim。
    """
    prepare_upstream_env(upstream_path)
    try:
        from api.base import Account, Chaoxing  # noqa: PLC0415 —— 子进程内动态加载
    except ImportError:
        return None
    return Chaoxing(Account("", ""), tiku=tiku)


def job_type_of(job: dict) -> str:
    return _JOB_TYPE_MAP.get(str(job.get("type") or ""), "unknown")


def do_login(client, use_cookies: bool = True) -> tuple[bool, str]:
    try:
        state = client.login(login_with_cookies=use_cookies)
    except Exception as exc:  # noqa: BLE001 —— 上游异常类型不稳定，边界兜住
        return False, f"{type(exc).__name__}: {exc}"
    if not state.get("status"):
        return False, str(state.get("msg") or "登录失败")
    return True, "登录成功"


def scan_course_points(client, course: dict) -> list[dict]:
    """章节列表（**上游原始 point dict**，get_job_list 依赖其中的 id 等字段）。"""
    points = client.get_course_point(course["courseId"], course["clazzId"], course["cpi"])
    return list(points.get("points") or [])


def point_row(p: dict) -> dict:
    """上游 point → 对外投影。"""
    return {
        "chapter_id": str(p.get("id") or ""),
        "title": p.get("title"),
        "has_finished": bool(p.get("has_finished")),
    }


def find_course(client, course_id: str) -> dict | None:
    """统一层 course_id → 上游 course dict（courseId/clazzId/cpi/title）。"""
    all_course = client.get_course_list()
    raw = str(course_id).strip()
    for c in all_course:
        if str(c.get("courseId")) == raw:
            return c
    return None


def job_still_pending(client, course: dict, raw_point: dict, job_id: str) -> bool:
    """复扫该章节任务点，判断目标任务点是否仍待完成（**以平台为准**）。

    用途（M5 实测）：章节检测交卷成功后，若未满分，上游会"带反馈重做"，
    而重做取题页解析不出来（上游限制）→ 最终返回 StudyResult.ERROR。此时
    worker 汇总会写成 failed，但**平台侧该任务点其实已完成**。这里复扫一次
    消除这个假失败。

    查询本身失败时保守返回 True（宁可报失败，也不虚报成功）。
    """
    try:
        jobs, _ = client.get_job_list(course, raw_point)
    except Exception:  # noqa: BLE001 —— 复扫失败不该影响主流程
        return True
    for job in jobs or []:
        if str(job.get("jobid") or "") == job_id:
            return True
    return False


def run_course(client, course: dict, args: dict, flag: ControlFlag) -> dict:
    """逐章节编排。每个任务点的**处理**都走上游 process_job，本函数只做：
    循环、类型过滤、跳过决策、事件上报、取消检查（编排，不是业务逻辑）。"""
    import main as cxmain  # noqa: PLC0415 —— 子进程内动态加载上游

    speed = float(args.get("speed") or 1.0)
    skip_ids = {str(x) for x in (args.get("skip_task_point_ids") or [])}
    target_types = {str(x) for x in (args.get("target_types") or [])}
    allow_work = bool(args.get("allow_work"))
    chapter_filter = str(args.get("chapter_id") or "")
    dry_run = bool(args.get("dry_run"))

    raw_points = scan_course_points(client, course)
    points = [point_row(p) for p in raw_points]
    if chapter_filter:
        raw_points = [p for p in raw_points if str(p.get("id")) == chapter_filter]
        points = [p for p in points if p["chapter_id"] == chapter_filter]
    total = len(points)

    completed: list[str] = []
    failed: list[dict] = []
    skipped: list[dict] = []
    stopped_reason = "finished"
    risk_control_hit = False

    _emit({"event": "course_start", "course": course.get("title"),
           "total_chapters": total, "dry_run": dry_run})

    for index, point in enumerate(points):
        raw_point = raw_points[index]
        if flag.cancelled.is_set():
            stopped_reason = "cancelled"
            break
        if flag.paused.is_set():
            stopped_reason = "paused"
            break

        chapter_id = point["chapter_id"]
        if point["has_finished"]:
            skipped.append({"id": chapter_id, "type": "chapter", "reason": "already_done"})
            _emit({"event": "chapter_skipped", "chapter_id": chapter_id,
                   "title": point["title"], "reason": "already_done"})
            continue

        _emit({"event": "chapter_start", "chapter_id": chapter_id,
               "title": point["title"], "index": index + 1, "total": total})

        jobs, job_info = client.get_job_list(course, raw_point)
        if job_info.get("notOpen", False):
            skipped.append({"id": chapter_id, "type": "chapter", "reason": "locked"})
            _emit({"event": "chapter_skipped", "chapter_id": chapter_id,
                   "title": point["title"], "reason": "locked"})
            continue

        chapter_failed = False
        for job in jobs or []:
            if flag.cancelled.is_set():
                stopped_reason = "cancelled"
                break
            if flag.paused.is_set():
                stopped_reason = "paused"
                break

            job_id = str(job.get("jobid") or "")
            jtype = job_type_of(job)
            if job_id in skip_ids:
                skipped.append({"id": job_id, "type": jtype, "reason": "already_done"})
                continue
            if target_types and jtype not in target_types:
                skipped.append({"id": job_id, "type": jtype, "reason": "type_filtered"})
                continue
            if jtype == "quiz" and not allow_work:
                skipped.append({"id": job_id, "type": jtype, "reason": "quiz_disabled"})
                _emit({"event": "job_skipped", "task_point_id": job_id, "type": jtype,
                       "reason": "quiz_disabled",
                       "note": "M2 不自动答题；M5 接入 Agent 答题后启用"})
                continue

            if dry_run:
                _emit({"event": "would_process", "task_point_id": job_id, "type": jtype})
                completed.append(job_id)
                continue

            _emit({"event": "job_start", "task_point_id": job_id, "type": jtype})
            try:
                result = cxmain.process_job(client, course, job, job_info, speed)
                ok = (result == cxmain.StudyResult.SUCCESS)
            except Exception as exc:  # noqa: BLE001 —— 单任务点失败不拖垮整课
                if jtype == "quiz" and not job_still_pending(
                    client, course, raw_point, job_id
                ):
                    _emit({"event": "job_recheck", "task_point_id": job_id, "type": jtype,
                           "note": "上游抛异常，复扫平台确认已完成"})
                    completed.append(job_id)
                    continue
                _emit({"event": "job_done", "task_point_id": job_id, "type": jtype,
                       "result": "ERROR", "error": f"{type(exc).__name__}: {exc}"})
                failed.append({"id": job_id, "type": jtype, "result": "ERROR"})
                chapter_failed = True
                continue

            if result == cxmain.StudyResult.FORBIDDEN:
                risk_control_hit = True
                _emit({"event": "job_done", "task_point_id": job_id, "type": jtype,
                       "result": "FORBIDDEN"})
                failed.append({"id": job_id, "type": jtype, "result": "FORBIDDEN"})
                chapter_failed = True
                continue

            _emit({"event": "job_done", "task_point_id": job_id, "type": jtype,
                   "result": "SUCCESS" if ok else str(result)})
            if ok:
                completed.append(job_id)
            elif jtype == "quiz" and not job_still_pending(
                client, course, raw_point, job_id
            ):
                # 假失败消除：上游报错（典型是交卷成功后"重做取题失败"），
                # 但平台侧任务点已从待完成列表消失 —— 按已完成计
                _emit({"event": "job_recheck", "task_point_id": job_id, "type": jtype,
                       "note": "上游报失败，复扫平台确认已完成（M5 交卷成功后重做取题失败的已知情况）"})
                completed.append(job_id)
            else:
                failed.append({"id": job_id, "type": jtype, "result": str(result)})
                chapter_failed = True

        if stopped_reason != "finished":
            break

    return {
        "course": course.get("title"),
        "course_id": str(course.get("courseId") or ""),
        "completed": completed,
        "failed": failed,
        "skipped": skipped,
        "stopped_reason": stopped_reason,
        "risk_control": risk_control_hit,
        "dry_run": dry_run,
    }


def scan_tasks(client, course: dict) -> dict:
    """C09 数据源：逐章节列出任务点与类型（纯 GET，不触发处理）。"""
    rows = []
    for raw in scan_course_points(client, course):
        point = point_row(raw)
        if point["has_finished"]:
            continue
        jobs, job_info = client.get_job_list(course, raw)
        if job_info.get("notOpen", False):
            rows.append({"task_point_id": point["chapter_id"], "type": "chapter",
                         "title": point["title"], "chapter_id": point["chapter_id"],
                         "status": "locked"})
            continue
        for job in jobs or []:
            rows.append({
                "task_point_id": str(job.get("jobid") or ""),
                "type": job_type_of(job),
                "title": job.get("name") or job.get("title") or point["title"],
                "chapter_id": point["chapter_id"],
                "chapter_name": point["title"],
                "status": "todo",
            })
    return {"task_points": rows, "course": course.get("title")}


def main() -> int:
    try:
        request = json.loads(sys.stdin.readline())
    except json.JSONDecodeError as exc:
        return _fail("BAD_REQUEST", f"worker 输入不是合法 JSON：{exc}", EXIT_INTERNAL)

    upstream_path = request.get("upstream_path") or ""
    op = request.get("op") or ""
    args = request.get("args") or {}

    if not upstream_path:
        return _fail("BAD_REQUEST", "缺少 upstream_path", EXIT_INTERNAL)

    client = None
    if args.get("tiku_enabled"):
        # 答题链路：题目经本地 shim → 工单 → 操控 Agent
        try:
            tiku = build_tiku(
                upstream_path,
                str(args.get("shim_endpoint") or ""),
                submit=str(args.get("tiku_submit") or "false").lower() == "true",
            )
        except Exception as exc:  # noqa: BLE001 —— tiku 构造失败不该中断刷课
            _emit({"event": "tiku_failed", "error": f"{type(exc).__name__}: {exc}"})
            tiku = None
        client = build_client(upstream_path, tiku=tiku)
    else:
        client = build_client(upstream_path)
    if client is None:
        return _fail("DEPS_MISSING", "写侧上游依赖不可用", EXIT_DEPS, INSTALL_HINT)

    flag = ControlFlag()
    if op == "run":
        threading.Thread(target=flag.listen_forever, daemon=True).start()

    if op == "ping":
        _emit({"ok": True, "data": {"upstream": "chaoxing"}})
        return EXIT_OK

    logged_in, msg = do_login(client, use_cookies=True)
    if not logged_in:
        return _fail(
            "SESSION_INVALID",
            f"cookie 登录失败：{msg}",
            EXIT_AUTH,
            "请重新登录：cx cookies_login（或双击 启动独立浏览器.cmd）",
        )

    try:
        if op == "list_points":
            course = find_course(client, args.get("course_id"))
            if course is None:
                return _fail("COURSE_NOT_FOUND", f"课程不存在：{args.get('course_id')}", EXIT_INTERNAL)
            _emit({"ok": True, "data": {"course": course.get("title"),
                                        "points": scan_course_points(client, course)}})
            return EXIT_OK
        if op == "scan_tasks":
            course = find_course(client, args.get("course_id"))
            if course is None:
                return _fail("COURSE_NOT_FOUND", f"课程不存在：{args.get('course_id')}", EXIT_INTERNAL)
            _emit({"ok": True, "data": scan_tasks(client, course)})
            return EXIT_OK
        if op == "run":
            course = find_course(client, args.get("course_id"))
            if course is None:
                return _fail("COURSE_NOT_FOUND", f"课程不存在：{args.get('course_id')}", EXIT_INTERNAL)
            summary = run_course(client, course, args, flag)
            _emit({"ok": True, "data": summary})
            return EXIT_OK
        return _fail("UNKNOWN_OP", f"未知 op：{op}", EXIT_INTERNAL)
    except Exception as exc:  # noqa: BLE001 —— worker 边界必须兜住一切
        return _fail("WORKER_ERROR", f"{type(exc).__name__}: {exc}", EXIT_INTERNAL)


if __name__ == "__main__":
    sys.exit(main())
