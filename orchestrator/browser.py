"""浏览器隔离约定（C36 / C38 的基础设施）。

## 为什么需要这个模块

约束来自使用者（2026-09-17）：
1. 不许碰日常浏览器（Chrome）；
2. 不许碰**其他 Agent 正在占用的 Edge 窗口**（Claude 正在用它做 BOSS 直聘
   自动化调试）。

这两条约束如果只写在文档里，迟早会被某个 Adapter 作者（包括未来的我）
用一句 `subprocess.Popen(["msedge.exe", url])` 破坏掉。所以这里把约束
变成代码：**启动浏览器必须经过本模块，而本模块在结构上拒绝系统默认
profile**。

## 三个关键技术事实

1. **Chromium 的 profile 单例锁是按 `user-data-dir` 的。** 不同的
   user-data-dir 会得到完全独立的进程与窗口，互不影响。所以
   `--user-data-dir` 不是可选优化，而是隔离的**唯一手段**。

2. **不带 `--user-data-dir` 启动，一定会附着到已有的默认 profile 实例上**，
   表现为"只多开了一个标签页"。那正是使用者要避免的情形。

3. **Edge / Chrome 136 起对默认 profile 禁用 `--remote-debugging-port`。**
   也就是说，想让自动化拿得到 CDP，反而**必须**用独立 profile。
   隔离要求和自动化需求在这里刚好一致。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

#: 默认 CDP 端口。刻意避开 9222（业界最常见的默认值，容易被别的工具占用）
DEFAULT_DEBUG_PORT = 9333

ENV_PROFILE = "ORCH_EDGE_PROFILE"
ENV_PORT = "ORCH_EDGE_DEBUG_PORT"
ENV_BINARY = "ORCH_EDGE_BINARY"

#: 项目内的 profile 目录（相对仓库根）。放在项目内是为了让"用的是哪个
#: profile"一眼可见 —— 隔离性如果不可见，就等于不存在。
PROFILE_RELPATH = Path(".browser") / "edge-profile"

#: 启动后写入的 PID 文件（同样在项目内）
PID_FILENAME = "browser.pid"

#: 浏览器自身的 stdout/stderr 落盘位置。
#: 启动失败时（profile 损坏、被占用、参数被拒），唯一能说明原因的就是
#: 浏览器自己打印的那行字 —— 所以绝不能丢进 DEVNULL。
LOG_FILENAME = "browser.log"


class IsolationError(RuntimeError):
    """profile 路径指向了不该碰的地方。"""


class BrowserNotFound(RuntimeError):
    """找不到浏览器可执行文件。"""


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------
# 禁区判定
# --------------------------------------------------------------------------


def system_profile_dirs() -> list[Path]:
    """本机 Edge / Chrome 的默认 user-data-dir —— 这些是**禁区**。

    只列真实存在的目录：不存在的不算禁区，否则在没装 Chrome 的机器上
    会误报。
    """
    home = Path.home()
    local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    candidates = [
        # Windows
        local / "Microsoft" / "Edge" / "User Data",
        local / "Google" / "Chrome" / "User Data",
        local / "Chromium" / "User Data",
        # macOS
        home / "Library" / "Application Support" / "Microsoft Edge",
        home / "Library" / "Application Support" / "Google" / "Chrome",
        # Linux
        home / ".config" / "microsoft-edge",
        home / ".config" / "google-chrome",
        home / ".config" / "chromium",
    ]
    return [c for c in candidates if c.exists()]


def assert_isolated(profile: Path) -> Path:
    """确认 profile 路径是隔离的，否则抛 `IsolationError`。

    这是本模块存在的**核心理由**：让"误用日常浏览器 profile"这件事
    在代码层面做不到，而不是靠自觉。
    """
    resolved = Path(profile).expanduser().resolve()

    for forbidden in system_profile_dirs():
        forbidden_resolved = forbidden.resolve()
        if resolved == forbidden_resolved:
            raise IsolationError(
                f"拒绝启动：{resolved} 是本机浏览器的默认 profile。\n"
                f"用它启动会把你**日常浏览器的会话暴露给自动化**，并可能"
                f"干扰正在使用该 profile 的其他程序。请换一个独立目录。"
            )
        if forbidden_resolved in resolved.parents:
            raise IsolationError(
                f"拒绝启动：{resolved} 位于默认 profile 目录 "
                f"{forbidden_resolved} 之内。请换一个独立目录。"
            )

    if resolved == Path(resolved.anchor):
        raise IsolationError(f"拒绝启动：{resolved} 是文件系统根目录。")

    if resolved == Path.home().resolve():
        raise IsolationError(f"拒绝启动：{resolved} 是用户主目录。")

    if not resolved.name:
        raise IsolationError(f"拒绝启动：{resolved} 不是有效目录。")

    return resolved


def is_isolated(profile: Path) -> bool:
    try:
        assert_isolated(profile)
        return True
    except IsolationError:
        return False


# --------------------------------------------------------------------------
# 路径与端口解析
# --------------------------------------------------------------------------


def profile_dir(root: Path | None = None) -> Path:
    """解析 profile 目录，优先级：环境变量 > 项目内默认。"""
    override = os.environ.get(ENV_PROFILE)
    if override:
        return Path(override).expanduser().resolve()
    base = root or project_root()
    return (base / PROFILE_RELPATH).resolve()


def debug_port() -> int:
    raw = os.environ.get(ENV_PORT)
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return DEFAULT_DEBUG_PORT


def pid_file(root: Path | None = None) -> Path:
    base = root or project_root()
    return base / ".browser" / PID_FILENAME


def log_file(root: Path | None = None) -> Path:
    base = root or project_root()
    return base / ".browser" / LOG_FILENAME


def read_log_tail(root: Path | None = None, max_bytes: int = 2048) -> str:
    """读取浏览器日志尾部，用于把失败原因回显给使用者。

    只取尾部：Chromium 在 Windows 上会输出大量无害的 GPU/组件警告，
    真正的原因通常在最后几行。
    """
    path = log_file(root)
    if not path.is_file():
        return ""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            raw = handle.read()
    except OSError:
        return ""
    text = raw.decode("utf-8", errors="replace")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines[-8:])


# --------------------------------------------------------------------------
# 可执行文件定位
# --------------------------------------------------------------------------


def edge_candidates() -> list[Path]:
    home = Path.home()
    local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    pf = Path(os.environ.get("ProgramFiles") or "C:/Program Files")
    pf86 = Path(os.environ.get("ProgramFiles(x86)") or "C:/Program Files (x86)")
    return [
        pf / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        pf86 / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        local / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        # 非 Windows 的兜底，方便在别的机器上跑测试
        Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        Path("/usr/bin/microsoft-edge"),
    ]


def find_browser() -> Path:
    override = os.environ.get(ENV_BINARY)
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_file():
            return candidate
        raise BrowserNotFound(f"{ENV_BINARY} 指向的文件不存在：{candidate}")

    for candidate in edge_candidates():
        if candidate.is_file():
            return candidate
    raise BrowserNotFound(
        "找不到 Microsoft Edge。可用环境变量 "
        f"{ENV_BINARY} 显式指定可执行文件路径。"
    )


# --------------------------------------------------------------------------
# 启动与探测
# --------------------------------------------------------------------------


def launch_args(
    browser: Path,
    profile: Path,
    port: int,
    url: str | None = None,
    extra: Sequence[str] = (),
) -> list[str]:
    """构造启动参数。

    `--user-data-dir` 与 `--remote-debugging-address=127.0.0.1` 都必须存在：
    前者是隔离手段，后者保证 CDP 只监听本机（不暴露到局域网）。
    """
    args = [
        str(browser),
        f"--user-data-dir={profile}",
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        # 避免首次运行向导/默认浏览器检查干扰自动化
        "--no-first-run",
        "--no-default-browser-check",
    ]
    args.extend(extra)
    if url:
        args.append(url)
    return args


def cdp_url(port: int | None = None) -> str:
    return f"http://127.0.0.1:{port if port is not None else debug_port()}"


@dataclass
class CdpStatus:
    alive: bool
    browser: str = ""
    protocol: str = ""
    web_socket: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "alive": self.alive,
            "browser": self.browser,
            "protocol": self.protocol,
            "endpoint": self.web_socket,
            "detail": self.detail,
        }


def probe_cdp(port: int | None = None, timeout_s: float = 2.0) -> CdpStatus:
    """探测 CDP 端点是否活着。只读，不启动任何东西。"""
    target_port = port if port is not None else debug_port()
    url = f"http://127.0.0.1:{target_port}/json/version"
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return CdpStatus(
            alive=True,
            browser=str(payload.get("Browser", "")),
            protocol=str(payload.get("Protocol-Version", "")),
            web_socket=str(payload.get("webSocketDebuggerUrl", "")),
            detail=f"{cdp_url(target_port)}/json/version",
        )
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError) as exc:
        return CdpStatus(alive=False, detail=f"{type(exc).__name__}: {exc}")


@dataclass
class LaunchPlan:
    """启动前的完整决议，便于先"看"再"做"。"""

    browser: Path
    profile: Path
    port: int
    args: list[str]
    already_running: bool = False
    cdp: CdpStatus | None = None
    #: 启动器进程的退出码。**注意**：Edge 在 Windows 上有"启动器进程"
    #: 模型 —— 拉起了真正的浏览器主进程后，启动器自身就会退出。所以
    #: 退出码非 None **不必然**代表失败，判据始终是 CDP 是否就绪。
    exit_code: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "browser": str(self.browser),
            "profile": str(self.profile),
            "port": self.port,
            "cdp_url": cdp_url(self.port),
            "already_running": self.already_running,
            "cdp": self.cdp.to_dict() if self.cdp else None,
            "args": self.args,
            "exit_code": self.exit_code,
        }


def plan(
    root: Path | None = None,
    port: int | None = None,
    url: str | None = None,
) -> LaunchPlan:
    """计算启动计划并做全部安全检查。**不产生任何副作用。**"""
    browser = find_browser()
    profile = assert_isolated(profile_dir(root))
    target_port = port if port is not None else debug_port()
    status = probe_cdp(target_port)
    return LaunchPlan(
        browser=browser,
        profile=profile,
        port=target_port,
        args=launch_args(browser, profile, target_port, url),
        already_running=status.alive,
        cdp=status,
    )


def launch(
    root: Path | None = None,
    port: int | None = None,
    url: str | None = None,
    wait_ready_s: float = 20.0,
) -> tuple[LaunchPlan, subprocess.Popen[Any] | None]:
    """按计划启动独立 Edge 实例。

    若该 profile 对应的实例已经在跑，**不重复启动**（Chromium 也只会把
    已有窗口拉到前台），直接复用。

    两个刻意的选择：

    1. **stdout/stderr 落盘，不丢 DEVNULL。** 启动失败时浏览器会打印
       原因（profile 损坏、参数被拒、目录无权限），把它丢掉等于放弃
       诊断能力 —— 使用者只会看到"没反应"。
    2. **记录启动器退出码，但不拿它当失败判据。** Edge on Windows 有
       启动器进程模型，拉起主进程后启动器自身即退出。唯一的成功判据是
       CDP 端点是否就绪。
    """
    launch_plan = plan(root, port, url)
    launch_plan.profile.mkdir(parents=True, exist_ok=True)

    if launch_plan.already_running:
        return launch_plan, None

    log_path = log_file(root)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # 每次启动覆盖，避免上一次的残留把本次的诊断信息淹掉
    handle = open(log_path, "wb")
    try:
        process = subprocess.Popen(
            launch_plan.args,
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError as exc:
        handle.close()
        raise BrowserNotFound(f"无法启动 {launch_plan.browser}：{exc}") from exc
    finally:
        # 子进程持有自己的句柄副本，父进程可以立刻关闭
        if not handle.closed:
            handle.close()

    record = pid_file(root)
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(
        json.dumps(
            {
                "pid": process.pid,
                "profile": str(launch_plan.profile),
                "port": launch_plan.port,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "log": str(log_path),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    deadline = time.monotonic() + wait_ready_s
    while time.monotonic() < deadline:
        status = probe_cdp(launch_plan.port, timeout_s=1.0)
        if status.alive:
            launch_plan.cdp = status
            launch_plan.already_running = True
            break
        time.sleep(0.3)

    launch_plan.exit_code = process.poll()
    return launch_plan, process


# --------------------------------------------------------------------------
# 收尾
# --------------------------------------------------------------------------


def owned_pids(root: Path | None = None) -> list[int]:
    """列出属于本项目的浏览器进程 PID。

    判定依据是命令行里含本项目唯一的 profile 路径。**只有 PID 文件里记录的
    那个进程**会被视为自己的 —— 不做全盘扫描，避免误伤其他 Agent 的浏览器。
    """
    record = pid_file(root)
    if not record.is_file():
        return []
    try:
        payload = json.loads(record.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    pid = payload.get("pid")
    if not isinstance(pid, int):
        return []
    return [pid]


def process_alive(pid: int) -> bool | None:
    """检查 PID 是否仍在运行。

    返回 `None` 表示当前平台无法判定（不做猜测）。

    为什么不用 `os.kill(pid, 0)`：在 Windows 上 `os.kill` 对任意非零信号
    都会走 `TerminateProcess` —— 用它做"探活"会**直接杀掉那个进程**。
    这里改用 `OpenProcess` + `GetExitCodeProcess`，只读且安全。
    """
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return None

    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def port_is_free(port: int) -> bool:
    """端口能否被绑定 —— 只读探测，不做连接。"""
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def diagnose(root: Path | None = None) -> dict[str, Any]:
    """全面体检，**不产生任何副作用**（不启动、不写文件）。

    存在意义：使用者报告"没拉起来"时，最需要回答的是"卡在哪一环"。
    把这些环节一次列全，比来回猜快得多。
    """
    profile = profile_dir(root)
    port = debug_port()
    record = pid_file(root)

    report: dict[str, Any] = {}
    report["profile"] = str(profile)
    report["profile_isolated"] = is_isolated(profile)
    report["profile_exists"] = profile.exists()
    report["profile_writable"] = os.access(profile, os.W_OK) if profile.exists() else os.access(
        profile.parent, os.W_OK
    )

    try:
        report["browser"] = str(find_browser())
        report["browser_found"] = True
    except BrowserNotFound as exc:
        report["browser"] = ""
        report["browser_found"] = False
        report["browser_error"] = str(exc)

    report["port"] = port
    report["port_free"] = port_is_free(port)

    status = probe_cdp(port)
    report["cdp_alive"] = status.alive
    report["cdp_detail"] = status.detail
    report["cdp_browser"] = status.browser

    recorded: dict[str, Any] = {}
    if record.is_file():
        try:
            recorded = json.loads(record.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            recorded = {}
    report["pid_record"] = recorded
    pid = recorded.get("pid")
    report["pid_alive"] = process_alive(pid) if isinstance(pid, int) else None

    report["log_path"] = str(log_file(root))
    report["log_tail"] = read_log_tail(root)

    # 结论：把"卡在哪一环"直接讲出来，而不是丢一堆字段让人自己拼
    if report["cdp_alive"]:
        verdict = "在线：独立实例已在运行，可直接执行 cookies_extract / cookies_login --no-open"
    elif not report.get("browser_found"):
        verdict = "失败：找不到 Edge 可执行文件（可用 ORCH_EDGE_BINARY 指定）"
    elif not report["profile_isolated"]:
        verdict = "失败：profile 路径被隔离守卫拒绝"
    elif not report["port_free"]:
        verdict = f"失败：端口 {port} 已被占用（换 --port 9334 或用 ORCH_EDGE_DEBUG_PORT）"
    elif report["pid_alive"] is False and recorded:
        verdict = (
            "失败：上次启动的进程已退出，且 CDP 未就绪 —— 浏览器启动后立刻死了。"
            "看 log_tail 里的原因。"
        )
    else:
        verdict = "未运行：浏览器当前没起来，可执行 --launch"
    report["verdict"] = verdict

    return report


def stop_instructions(root: Path | None = None) -> str:
    record = pid_file(root)
    profile = profile_dir(root)
    return (
        f"停止方式（任选其一）：\n"
        f"  1. 直接关掉那个 Edge 窗口（最省事，推荐）\n"
        f"  2. 按 PID 结束：PID 记录在 {record}\n"
        f"  3. 结束前请确认进程命令行里含：{profile}\n"
        f"⚠️ 不要用「结束所有 msedge.exe」这类命令 —— 会杀掉其他 Agent "
        f"正在使用的 Edge 窗口。"
    )


# --------------------------------------------------------------------------
# 命令行入口
# --------------------------------------------------------------------------


def _show(root: Path | None = None) -> int:
    profile = profile_dir(root)
    print(f"profile 目录   : {profile}")
    print(f"是否为禁区路径 : {'是（拒绝使用）' if not is_isolated(profile) else '否（隔离）'}")
    print(f"调试端口       : {debug_port()}")
    print(f"CDP 端点       : {cdp_url()}")
    try:
        browser = find_browser()
        print(f"浏览器         : {browser}")
    except BrowserNotFound as exc:
        print(f"浏览器         : 未找到 —— {exc}")

    print()
    print("本机默认 profile（禁区）：")
    dirs = system_profile_dirs()
    if not dirs:
        print("  （未检测到已安装的 Chrome / Edge）")
    for directory in dirs:
        print(f"  - {directory}")

    status = probe_cdp()
    print()
    print(f"CDP 状态       : {'在线' if status.alive else '离线'}")
    print(f"  详情         : {status.detail}")
    if status.alive:
        print(f"  Browser      : {status.browser}")
    return 0


def _launch(root: Path | None, url: str | None, port: int | None) -> int:
    try:
        launch_plan, process = launch(root, port, url)
    except IsolationError as exc:
        print(f"[拒绝] {exc}", file=sys.stderr)
        return 2
    except BrowserNotFound as exc:
        print(f"[失败] {exc}", file=sys.stderr)
        return 3

    print("=" * 68)
    print("独立 Edge 实例")
    print("=" * 68)
    print(f"profile  : {launch_plan.profile}")
    print(f"端口     : {launch_plan.port}")
    print(f"CDP      : {cdp_url(launch_plan.port)}")
    print(f"启动方式 : --user-data-dir 独立目录（与任何系统 profile 隔离）")
    print()
    if launch_plan.already_running and process is None:
        print("该 profile 的实例已在运行，未重复启动。")
    elif launch_plan.cdp and launch_plan.cdp.alive:
        print(f"启动成功：{launch_plan.cdp.browser}")
    else:
        # 不要把失败说成"可能仍在初始化" —— 那会让人一直等下去。
        print("[失败] 已发起启动，但 CDP 端点始终未就绪。")
        print(f"       启动器退出码：{launch_plan.exit_code}")
        print(f"       端口 {launch_plan.port} 没有监听，浏览器很可能已经退出。")
        tail = read_log_tail(root)
        print()
        if tail:
            print("浏览器自身的输出（末尾几行，完整内容见日志）：")
            for line in tail.splitlines():
                print(f"  | {line}")
        else:
            print("浏览器没有输出任何内容（连错误都没有）。")
        print()
        print("下一步：")
        print("  1. python -m orchestrator.browser --diagnose     # 看卡在哪一环")
        print(f"  2. 删掉 profile 重建：{launch_plan.profile}")
        print("  3. 换端口重试：--port 9334")
        return 4
    print()
    print(stop_instructions(root))
    return 0


def _diagnose(root: Path | None) -> int:
    report = diagnose(root)
    print("=" * 68)
    print("浏览器环境体检")
    print("=" * 68)
    print(f"结论       : {report['verdict']}")
    print()
    print(f"浏览器     : {report.get('browser') or '（未找到）'}")
    if not report.get("browser_found"):
        print(f"             {report.get('browser_error', '')}")
    print(f"profile    : {report['profile']}")
    print(f"  已存在   : {report['profile_exists']}")
    print(f"  隔离检查 : {'通过' if report['profile_isolated'] else '★ 被拒绝'}")
    print(f"  可写     : {report['profile_writable']}")
    print(f"端口       : {report['port']}（{'空闲' if report['port_free'] else '★ 已被占用'}）")
    print(f"CDP        : {'在线' if report['cdp_alive'] else '离线'}")
    print(f"  详情     : {report['cdp_detail']}")
    if report["cdp_alive"]:
        print(f"  Browser  : {report['cdp_browser']}")

    record = report.get("pid_record") or {}
    print()
    print("上次启动记录：")
    if record:
        print(f"  PID      : {record.get('pid')}（存活={report['pid_alive']}）")
        print(f"  started  : {record.get('started_at')}")
    else:
        print("  （无记录，说明从未启动过，或记录已被清理）")

    print()
    print(f"浏览器日志 : {report['log_path']}")
    tail = report.get("log_tail") or ""
    if tail:
        for line in tail.splitlines():
            print(f"  | {line}")
    else:
        print("  （空 —— 浏览器没有产生任何输出）")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="orchestrator.browser",
        description="独立浏览器实例管理（与日常浏览器、其他 Agent 的浏览器隔离）",
    )
    parser.add_argument("--root", default=None, help="仓库根目录")
    parser.add_argument("--port", type=int, default=None, help="CDP 端口覆盖")
    parser.add_argument("--url", default=None, help="启动后打开的地址")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--show", action="store_true", help="显示配置与禁区（默认）")
    group.add_argument("--launch", action="store_true", help="启动独立实例")
    group.add_argument("--status", action="store_true", help="探测 CDP 是否在线")
    group.add_argument("--diagnose", action="store_true", help="全面体检（只读）")
    group.add_argument("--stop", action="store_true", help="显示如何停止（不执行）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出（用于 --diagnose / --status）")
    args = parser.parse_args(list(argv) if argv is not None else None)

    root = Path(args.root).resolve() if args.root else None

    if args.status:
        status = probe_cdp(args.port)
        print(json.dumps(status.to_dict(), ensure_ascii=False, indent=2))
        return 0 if status.alive else 1

    if args.diagnose:
        if args.json:
            print(json.dumps(diagnose(root), ensure_ascii=False, indent=2))
            return 0
        return _diagnose(root)

    if args.stop:
        print(stop_instructions(root))
        return 0

    if args.launch:
        return _launch(root, args.url, args.port)

    return _show(root)


if __name__ == "__main__":
    raise SystemExit(main())
