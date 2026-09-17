"""统一错误模型。

设计原则（docs/03 §2.1）：
- Adapter **只负责**把原生异常映射成 `ErrorCategory`；
- **是否 fallback 由 Router 决定**，Adapter 不得自行决定。

这条分工是"多项目故障回退"（用户原则 6）能成立的前提：Adapter 的作者
不需要知道系统里还有哪些其他 Adapter。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ErrorCategory(StrEnum):
    TRANSIENT = "transient"
    AUTH = "auth"
    PERMISSION = "permission"
    NOT_SUPPORTED = "not_supported"
    PLATFORM_CHANGED = "platform_changed"
    RISK_CONTROL = "risk_control"
    INPUT = "input"
    INTERNAL = "internal"


#: 允许在同 Adapter 上退避重试的类别
RETRYABLE_CATEGORIES: frozenset[ErrorCategory] = frozenset({ErrorCategory.TRANSIENT})

#: 触发全局熔断、且**禁止 fallback** 的类别。
#: 风控是针对账号的，换 Adapter 用的是同一个账号，继续调用只会加深风控。
FATAL_BLOCK_CATEGORIES: frozenset[ErrorCategory] = frozenset(
    {ErrorCategory.RISK_CONTROL}
)

#: 换 Adapter 也无意义的类别——直接失败返回，不做 fallback
NO_FALLBACK_CATEGORIES: frozenset[ErrorCategory] = frozenset(
    {ErrorCategory.INPUT, ErrorCategory.INTERNAL, ErrorCategory.RISK_CONTROL}
)

#: 需要人工介入的类别
MANUAL_CATEGORIES: frozenset[ErrorCategory] = frozenset({ErrorCategory.AUTH})


# 机器可读错误码
class Codes:
    OK = "OK"
    NOT_SUPPORTED = "CAPABILITY_NOT_SUPPORTED"
    ADAPTER_TIMEOUT = "ADAPTER_TIMEOUT"
    ADAPTER_ERROR = "ADAPTER_ERROR"
    ADAPTER_NOT_READY = "ADAPTER_NOT_READY"
    ADAPTER_CRASHED = "ADAPTER_CRASHED"
    NO_ADAPTER_AVAILABLE = "NO_ADAPTER_AVAILABLE"
    ALL_ADAPTERS_FAILED = "ALL_ADAPTERS_FAILED"
    RISK_CONTROL_SUSPECTED = "RISK_CONTROL_SUSPECTED"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    INVALID_PARAM = "INVALID_PARAM"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
    ACCOUNT_COOLING = "ACCOUNT_COOLING"
    COURSE_NOT_FOUND = "COURSE_NOT_FOUND"
    TICKET_NOT_FOUND = "TICKET_NOT_FOUND"
    ANSWER_TIMEOUT = "ANSWER_TIMEOUT"
    # 登录 / 会话
    LOGIN_TIMEOUT = "LOGIN_TIMEOUT"
    SESSION_INVALID = "SESSION_INVALID"
    SESSION_UNCONFIRMED = "SESSION_UNCONFIRMED"
    # 浏览器侧：CDP 不可达（浏览器没起来/已退出/端口不对）。
    # 刻意与 LOGIN_TIMEOUT 分开：前者是"环境没就绪"，后者是"环境就绪但
    # 操作没完成"。混用会让排障时无法一眼看出该去启动浏览器还是该去登录。
    CDP_UNAVAILABLE = "CDP_UNAVAILABLE"


@dataclass
class AdapterError(Exception):
    """Adapter 层抛出的统一错误。"""

    code: str
    category: ErrorCategory
    message: str
    retryable: bool = False
    adapter_raw: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.message)
        if self.category in RETRYABLE_CATEGORIES and not self.retryable:
            self.retryable = True

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "category": str(self.category),
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.adapter_raw:
            payload["adapter_raw"] = self.adapter_raw
        if self.extra:
            payload["extra"] = self.extra
        return payload


def not_supported(adapter_id: str, capability_id: str) -> AdapterError:
    return AdapterError(
        code=Codes.NOT_SUPPORTED,
        category=ErrorCategory.NOT_SUPPORTED,
        message=f"Adapter {adapter_id} 不支持能力 {capability_id}",
    )


def timeout(adapter_id: str, seconds: float, raw: str = "") -> AdapterError:
    return AdapterError(
        code=Codes.ADAPTER_TIMEOUT,
        category=ErrorCategory.TRANSIENT,
        message=f"Adapter {adapter_id} 在 {seconds}s 内未返回",
        retryable=True,
        adapter_raw=raw,
    )


def risk_control(detail: str, raw: str = "") -> AdapterError:
    return AdapterError(
        code=Codes.RISK_CONTROL_SUSPECTED,
        category=ErrorCategory.RISK_CONTROL,
        message=f"检测到风控特征：{detail}",
        retryable=False,
        adapter_raw=raw,
        extra={"signature": detail},
    )


def auth_expired(detail: str = "会话失效") -> AdapterError:
    return AdapterError(
        code=Codes.AUTH_EXPIRED,
        category=ErrorCategory.AUTH,
        message=detail,
    )


def invalid_param(detail: str) -> AdapterError:
    return AdapterError(
        code=Codes.INVALID_PARAM,
        category=ErrorCategory.INPUT,
        message=detail,
    )


def internal(detail: str) -> AdapterError:
    return AdapterError(
        code=Codes.INTERNAL_ERROR,
        category=ErrorCategory.INTERNAL,
        message=detail,
    )


def not_implemented(detail: str) -> AdapterError:
    return AdapterError(
        code=Codes.NOT_IMPLEMENTED,
        category=ErrorCategory.NOT_SUPPORTED,
        message=detail,
    )
