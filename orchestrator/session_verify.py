"""会话有效性探测。

## 为什么需要它

"提取到 cookie" 和 "cookie 能用" 是两件不同的事。前者是本地文件操作，
后者必须真的调一次平台接口才知道。

`cookies` 命令的体检只能回答"文件里有没有东西"，回答不了"这个登录态
还有效吗"。所以这里做一次**真实请求**来验证。

## 设计原则：只下能下的结论

Cookie 是平台内部实现，哪些字段代表登录态**没有人有权威文档**。
所以判定结果是三态，而不是布尔：

| verdict | 含义 |
|---|---|
| `valid` | 拿到了登录后的页面，命中可识别的登录态标记 |
| `invalid` | 被重定向到登录页 —— 明确的"没登录/已失效" |
| `inconclusive` | 请求成功但识别不出标记。**不猜**，把证据交给人看 |
| `unreachable` | 网络不通 / 超时 / DNS 失败 |

宁可报 `inconclusive` 也不报假的 `valid`：一个假的"登录成功"会让后续
所有排障都建立在错误前提上。

## 两个刻意的实现选择

1. **不跟随重定向**。跳转到 `passport2.chaoxing.com` 本身就是"未登录"的
   最强信号；跟过去只会白白多拉一个登录页。用 `NoRedirect` 拦下即可。
2. **不记录响应体**。页面里可能有姓名、学号、课程信息。只记大小与命中的
   标记，正文一律不落盘。
"""

from __future__ import annotations

import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .cookies import CookieJar

#: 登录后可用页面的入口。来源：上游 `Xuexitong-mcp` 的实测协议备忘
#: （登录后 `GET https://i.chaoxing.com/base`，左侧菜单的 dataurl 即各功能入口）
DEFAULT_BASE_URL = "https://i.chaoxing.com/base"

#: 出现这些 host 说明被踢回登录流程
LOGIN_HOST_MARKERS: tuple[str, ...] = (
    "passport2.chaoxing.com",
    "passport.chaoxing.com",
    "/fanyalogin",
)

#: 登录后页面里可能出现的内容标记。
#: 保守起见只用结构性标记（属性名、菜单容器名），不用人名/课名这类
#: 易变且涉及隐私的文本。
LOGGED_IN_MARKERS: tuple[str, ...] = (
    "dataurl",
    "mooc2-ans",
    "personal-info",
    "mycourse",
    "账号：",
)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)


class SessionVerdict(StrEnum):
    VALID = "valid"
    INVALID = "invalid"
    INCONCLUSIVE = "inconclusive"
    UNREACHABLE = "unreachable"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """拦下重定向，把 3xx 当普通响应交回调用方判断。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


@dataclass
class SessionProbe:
    verdict: SessionVerdict
    status: int | None = None
    final_url: str = ""
    location: str = ""
    body_size: int = 0
    signals: list[str] = field(default_factory=list)
    detail: str = ""
    elapsed_ms: int = 0
    cookie_count: int = 0

    @property
    def ok(self) -> bool:
        return self.verdict is SessionVerdict.VALID

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": str(self.verdict),
            "status": self.status,
            "final_url": self.final_url,
            "location": self.location,
            "body_size": self.body_size,
            "signals": list(self.signals),
            "detail": self.detail,
            "elapsed_ms": self.elapsed_ms,
            "cookie_count": self.cookie_count,
        }


def strip_query(url: str) -> str:
    """去掉查询串再展示 —— 重定向 URL 里可能带 token。"""
    if not url:
        return ""
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def build_headers(jar: CookieJar, user_agent: str = DEFAULT_USER_AGENT) -> dict[str, str]:
    header = jar.to_header()
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if header:
        headers["Cookie"] = header
    return headers


def classify(
    status: int | None, location: str, body: str
) -> tuple[SessionVerdict, list[str]]:
    """把一次响应归类。三态判定，宁可 inconclusive 也不猜。"""
    signals: list[str] = []

    if status is None:
        return SessionVerdict.UNREACHABLE, ["请求未能完成"]

    lowered_location = (location or "").lower()
    if any(marker in lowered_location for marker in LOGIN_HOST_MARKERS):
        signals.append(f"被重定向到登录流程：{strip_query(location)}")
        return SessionVerdict.INVALID, signals

    if status in (401, 403):
        signals.append(f"HTTP {status} —— 会话被拒绝")
        return SessionVerdict.INVALID, signals

    if 300 <= status < 400:
        signals.append(f"HTTP {status} 重定向到 {strip_query(location) or '(未知)'}")
        return SessionVerdict.INCONCLUSIVE, signals

    if status != 200:
        signals.append(f"HTTP {status}")
        return SessionVerdict.INCONCLUSIVE, signals

    for marker in LOGGED_IN_MARKERS:
        if marker in body:
            signals.append(f"命中登录态标记 {marker!r}")
            return SessionVerdict.VALID, signals

    signals.append("HTTP 200，但未命中任何已知登录态标记（不做假设）")
    return SessionVerdict.INCONCLUSIVE, signals


def probe_session(
    jar: CookieJar,
    url: str = DEFAULT_BASE_URL,
    timeout_s: float = 15.0,
    user_agent: str = DEFAULT_USER_AGENT,
) -> SessionProbe:
    """用给定 cookie 请求一次平台页面，判断登录态。

    **只发一次 GET，不跟随重定向，不提交任何表单。**
    """
    if not jar.cookies:
        return SessionProbe(
            verdict=SessionVerdict.INVALID,
            signals=["cookie 为空，不可能有有效登录态"],
            detail="请先 cookies_extract 或 cookies_import",
        )

    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, headers=build_headers(jar, user_agent), method="GET")
    started = time.monotonic()

    status: int | None = None
    location = ""
    body = ""
    detail = ""

    try:
        with opener.open(request, timeout=timeout_s) as response:
            status = response.status
            location = response.headers.get("Location", "") or ""
            raw = response.read(65536)
            body = raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        location = (exc.headers.get("Location", "") if exc.headers else "") or ""
        try:
            body = exc.read(65536).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 读不到正文不影响判定
            body = ""
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        detail = f"{type(exc).__name__}: {exc}"

    elapsed_ms = int((time.monotonic() - started) * 1000)
    verdict, signals = classify(status, location, body)

    # 命中标记的位置不作为证据展示，只报大小；正文绝不外传
    return SessionProbe(
        verdict=verdict,
        status=status,
        final_url=strip_query(url),
        location=strip_query(location),
        body_size=len(body),
        signals=signals,
        detail=detail,
        elapsed_ms=elapsed_ms,
        cookie_count=len(jar),
    )


def next_actions_for(probe: SessionProbe) -> list[str]:
    if probe.verdict is SessionVerdict.VALID:
        return []
    if probe.verdict is SessionVerdict.INVALID:
        return [
            "登录态已失效或尚未登录。用一站式登录："
            "`python -m orchestrator.cli cookies_login`",
            "它会启动**项目内的独立浏览器**（不会碰你的日常浏览器）并等你登录，"
            "登录完成后自动提取落盘",
            "登录后重新执行 `python -m orchestrator.cli cookies_verify` 确认",
        ]
    if probe.verdict is SessionVerdict.UNREACHABLE:
        return [
            "确认本机网络能访问 chaoxing.com",
            "若使用代理，检查平台是否需要配置 http_proxy",
            "临时换个网络或用手机热点试一次，以区分是本机网络还是平台侧问题",
        ]
    return [
        "请求成功但识别不出登录态标记 —— 平台标记库可能需要更新"
        "（这是预期内的：平台会改版）",
        "把上面的 status / body_size / signals 反馈给我，我据此补充标记",
        "注意：这不代表登录失败，只是**无法自动确认**。"
        "可以用 cookies_import 手动贴一次 cookie 后直接试 list_courses 验证",
    ]
