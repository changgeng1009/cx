"""Task Router：选路、fallback、限流、熔断。

这是统一层唯一"有智慧"的地方，也是用户原则 6 的落点：
> 同一种能力存在多个项目时：自动选择最合适的实现；主实现失败后自动
> fallback；返回明确错误原因；不隐藏失败。

三条不可动摇的规则：
1. **只有 Router 决定 fallback**，Adapter 只负责报告错误类别。
2. **风控不 fallback**。换 Adapter 仍是同一个账号，继续调用只会加重风控。
3. **失败必须显式**。`fallback_trace` 记录试过谁、为什么不行；
   全部失败时返回 `ok=false`，绝不返回空数据伪装成功。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import errors as errs
from .errors import (
    FATAL_BLOCK_CATEGORIES,
    NO_FALLBACK_CATEGORIES,
    RETRYABLE_CATEGORIES,
    AdapterError,
    Codes,
    ErrorCategory,
)
from .models import AdapterResult, ProbeResult, TaskContext, TaskState
from .registry import CapabilityRegistry, Manifest
from .risk import RiskDetector
from .structured_log import Event, StructuredLogger
from .throttle import AccountThrottle


@dataclass
class RouterOutcome:
    ok: bool
    state: TaskState
    data: Any = None
    adapter_id: str | None = None
    adapter_version: str | None = None
    warnings: list[str] = field(default_factory=list)
    error: AdapterError | None = None
    fallback_trace: list[dict[str, Any]] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)

    def to_error_dict(self) -> dict[str, Any] | None:
        return self.error.to_dict() if self.error else None


@dataclass
class _Health:
    healthy: bool
    detail: str
    checked_at: float


DEFAULT_MAX_ATTEMPTS = 2
DEFAULT_PROBE_TTL_S = 300.0


class TaskRouter:
    def __init__(
        self,
        registry: CapabilityRegistry,
        logger: StructuredLogger | None = None,
        throttle: AccountThrottle | None = None,
        risk_detector: RiskDetector | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        probe_ttl_s: float = DEFAULT_PROBE_TTL_S,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.registry = registry
        self.logger = logger
        self.throttle = throttle or AccountThrottle()
        self.risk = risk_detector or RiskDetector()
        self.max_attempts = max(1, max_attempts)
        self.probe_ttl_s = probe_ttl_s
        self._clock = clock
        self._sleep = sleeper
        self._health: dict[str, _Health] = {}
        #: 记录当前请求最后一次失败，用于最后汇总。必须是实例属性——
        #: 类属性会在并发请求间互相污染，导致错误归因错乱。
        self._last_error: AdapterError | None = None

    # ------------------------------------------------------------------
    # 探活
    # ------------------------------------------------------------------
    def health(self, adapter: Any, force: bool = False) -> _Health:
        adapter_id = adapter.manifest.id
        cached = self._health.get(adapter_id)
        if (
            cached is not None
            and not force
            and (self._clock() - cached.checked_at) < self.probe_ttl_s
        ):
            return cached
        try:
            result: ProbeResult = adapter.probe()
            health = _Health(result.healthy, result.detail, self._clock())
        except Exception as exc:  # 探活本身失败不应拖垮请求
            health = _Health(False, f"probe 抛异常：{exc!r}", self._clock())
        self._health[adapter_id] = health
        return health

    def probe_all(self, include_disabled: bool = True) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for adapter in self.registry.all():
            manifest: Manifest = adapter.manifest
            if not manifest.enabled and not include_disabled:
                continue
            if not manifest.enabled:
                rows.append(
                    {
                        "adapter": manifest.id,
                        "name": manifest.name,
                        "kind": manifest.kind,
                        "license": manifest.license,
                        "enabled": False,
                        "healthy": False,
                        "detail": "manifest.enabled = false（尚未接入）",
                    }
                )
                continue
            health = self.health(adapter, force=True)
            rows.append(
                {
                    "adapter": manifest.id,
                    "name": manifest.name,
                    "kind": manifest.kind,
                    "license": manifest.license,
                    "enabled": True,
                    "healthy": health.healthy,
                    "detail": health.detail,
                }
            )
        return rows

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def execute(
        self,
        capability_id: str,
        params: dict[str, Any],
        ctx: TaskContext,
        command: str = "",
        account_id: str | None = None,
    ) -> RouterOutcome:
        account = account_id or ctx.account_id
        candidates = self.registry.candidates(capability_id)

        if not candidates:
            outcome = RouterOutcome(
                ok=False,
                state=TaskState.FAILED,
                error=AdapterError(
                    code=Codes.NO_ADAPTER_AVAILABLE,
                    category=ErrorCategory.NOT_SUPPORTED,
                    message=f"没有 Adapter 能提供能力 {capability_id}",
                ),
                next_actions=[
                    f"检查 manifests/ 下是否有 Adapter 声明了 {capability_id}",
                    "运行 `orchestrator adapters` 查看已注册能力",
                ],
            )
            self._log(
                Event.ADAPTER_FALLBACK,
                level="ERROR",
                capability=capability_id,
                reason="no_candidate",
                error=outcome.to_error_dict(),
            )
            return outcome

        trace: list[dict[str, Any]] = []
        last_error: AdapterError | None = None

        for index, adapter in enumerate(candidates):
            manifest: Manifest = adapter.manifest

            health = self.health(adapter)
            if not health.healthy:
                trace.append(
                    self._trace_entry(
                        manifest, capability_id, ok=False,
                        error_code=Codes.ADAPTER_NOT_READY,
                        reason=health.detail or "探活失败",
                        skipped=True,
                    )
                )
                self._log(
                    Event.ADAPTER_FALLBACK,
                    level="WARN",
                    capability=capability_id,
                    adapter=manifest.id,
                    reason="probe_unhealthy",
                    detail=health.detail,
                )
                continue

            # 限流间隔是上游项目的属性（不同项目风控敏感度不同），
            # 而限流状态是账号级的。所以每次换 Adapter 都要对齐一次间隔值：
            # 否则从"1.5s 间隔的项目"切到"3s 间隔的项目"时，新间隔不会生效。
            self.throttle.min_interval_ms = manifest.min_interval_ms

            decision = self.throttle.check(account)
            if not decision.allowed and self.throttle.is_blocked(account):
                remaining = self.throttle.cooldown_remaining(account)
                trace.append(
                    self._trace_entry(
                        manifest, capability_id, ok=False,
                        error_code=Codes.ACCOUNT_COOLING,
                        reason=decision.reason,
                        skipped=True,
                    )
                )
                return RouterOutcome(
                    ok=False,
                    state=TaskState.BLOCKED,
                    error=AdapterError(
                        code=Codes.ACCOUNT_COOLING,
                        category=ErrorCategory.RISK_CONTROL,
                        message=f"账号 {account} 处于风控冷却期，剩余 {remaining}s",
                    ),
                    fallback_trace=trace,
                    next_actions=[
                        "等待冷却结束（不要在此期间重试，会加重风控）",
                        "冷却后运行 `orchestrator resume` 继续",
                    ],
                )
            if not decision.allowed:
                self.throttle.acquire(account, block=True)

            try:
                outcome = self._attempt_chain(
                    adapter, capability_id, params, ctx, account, command, trace
                )
            finally:
                self.throttle.release(account)

            if outcome is not None:
                # outcome.ok 为 True 表示成功；为 False 表示"必须立即终止"
                if outcome.ok:
                    outcome.fallback_trace = trace
                else:
                    outcome.fallback_trace = trace
                return outcome

            # 走到这里说明该 Adapter 已用尽重试仍失败，记录后换下一个
            last_error = self._last_error
            trace.append(
                self._trace_entry(
                    manifest, capability_id, ok=False,
                    error_code=last_error.code if last_error else Codes.ADAPTER_ERROR,
                    reason=last_error.message if last_error else "未知失败",
                )
            )
            if index + 1 < len(candidates):
                self._log(
                    Event.ADAPTER_FALLBACK,
                    level="WARN",
                    capability=capability_id,
                    adapter=manifest.id,
                    next_adapter=candidates[index + 1].manifest.id,
                    reason=last_error.message if last_error else "unknown",
                )

        final_error = last_error or AdapterError(
            code=Codes.ALL_ADAPTERS_FAILED,
            category=ErrorCategory.INTERNAL,
            message="所有 Adapter 均失败",
        )
        final_error = AdapterError(
            code=Codes.ALL_ADAPTERS_FAILED,
            category=final_error.category,
            message=f"全部 {len(candidates)} 个 Adapter 处理 {capability_id} 失败：{final_error.message}",
            retryable=final_error.retryable,
            adapter_raw=final_error.adapter_raw,
        )
        state = (
            TaskState.NEEDS_MANUAL_ACTION
            if final_error.category in errs.MANUAL_CATEGORIES
            else TaskState.FAILED
        )
        return RouterOutcome(
            ok=False,
            state=state,
            error=final_error,
            fallback_trace=trace,
            next_actions=_suggest(final_error),
        )

    # ------------------------------------------------------------------
    def _attempt_chain(
        self,
        adapter: Any,
        capability_id: str,
        params: dict[str, Any],
        ctx: TaskContext,
        account: str,
        command: str,
        trace: list[dict[str, Any]],
    ) -> RouterOutcome | None:
        """在单个 Adapter 上尝试（含退避重试）。

        返回：
        - `RouterOutcome(ok=True)` 成功
        - `RouterOutcome(ok=False)` 必须立即终止（风控 / 参数错 / 内部错）
        - `None` 表示该 Adapter 不行，可以换下一个
        """
        manifest: Manifest = adapter.manifest
        # 绑定到日志上下文：这样该 Adapter 产生的一切事件（含它上报的
        # 任务点进度）都自动带上 adapter 字段，不需要每个上报点自己传。
        # 用户要求"记录使用了哪个 Adapter"，靠的就是这一步。
        if self.logger is not None:
            self.logger.bind(adapter=manifest.id)
        self._log(
            Event.ADAPTER_SELECTED,
            capability=capability_id,
            adapter=manifest.id,
            attempt=1,
        )

        for attempt in range(1, self.max_attempts + 1):
            if ctx.cancelled:
                return RouterOutcome(
                    ok=False,
                    state=TaskState.CANCELLED,
                    adapter_id=manifest.id,
                    fallback_trace=list(trace),
                    error=AdapterError(
                        code=Codes.INTERNAL_ERROR,
                        category=ErrorCategory.TRANSIENT,
                        message="任务已取消",
                    ),
                )
            try:
                result: AdapterResult = adapter.invoke(capability_id, params, ctx)
            except Exception as exc:  # Adapter 崩了不该拖垮整个请求
                result = AdapterResult.failure(
                    AdapterError(
                        code=Codes.ADAPTER_CRASHED,
                        category=ErrorCategory.INTERNAL,
                        message=f"Adapter {manifest.id} 抛出未捕获异常：{exc!r}",
                    ),
                    raw_output=repr(exc),
                )

            if result.raw_output:
                self._log_raw(manifest.id, result.raw_output)

            # 风控优先判定：即使 Adapter 自认为成功，输出里有风控特征也要拦
            hit = self.risk.detect(result.raw_output)
            if hit is None and not result.ok and result.error is not None:
                if result.error.category == ErrorCategory.RISK_CONTROL:
                    hit = self.risk.detect(result.error.message) or None

            if hit is not None:
                remaining = self.throttle.trigger_block(account, reason=hit.signature)
                self._log(
                    Event.RISK_CONTROL_DETECTED,
                    level="ERROR",
                    capability=capability_id,
                    adapter=manifest.id,
                    signature=hit.signature,
                    source=hit.source,
                    excerpt=hit.excerpt,
                )
                error = errs.risk_control(hit.signature, raw=result.raw_output[:500])
                return RouterOutcome(
                    ok=False,
                    state=TaskState.BLOCKED,
                    adapter_id=manifest.id,
                    adapter_version=self._version(manifest),
                    error=error,
                    fallback_trace=list(trace),
                    next_actions=[
                        f"账号 {account} 已熔断 {remaining}s，期间不要重试",
                        "不要切换到其他 Adapter 重试（同一账号，会加重风控）",
                        "冷却结束后用 `orchestrator resume` 继续",
                    ],
                )

            if result.ok:
                self.throttle.note_success(account)
                return RouterOutcome(
                    ok=True,
                    state=TaskState.COMPLETED,
                    data=result.data,
                    adapter_id=manifest.id,
                    adapter_version=self._version(manifest),
                    warnings=list(result.warnings),
                    fallback_trace=list(trace),
                )

            error = result.error or AdapterError(
                code=Codes.ADAPTER_ERROR,
                category=ErrorCategory.INTERNAL,
                message="Adapter 返回失败但未提供 error",
            )
            self._last_error = error
            self.risk_hits_note(adapter, capability_id, error)

            # 参数错误 / 内部错误：换 Adapter 无意义
            if error.category in NO_FALLBACK_CATEGORIES:
                return RouterOutcome(
                    ok=False,
                    state=TaskState.FAILED,
                    adapter_id=manifest.id,
                    adapter_version=self._version(manifest),
                    error=error,
                    fallback_trace=list(trace),
                    next_actions=_suggest(error),
                )

            if error.category in RETRYABLE_CATEGORIES and attempt < self.max_attempts:
                self.throttle.note_failure(account)
                delay = self.throttle.backoff_for(account, attempt)
                self._log(
                    Event.ADAPTER_RETRY,
                    level="WARN",
                    capability=capability_id,
                    adapter=manifest.id,
                    attempt=attempt,
                    delay_s=delay,
                    error=error.to_dict(),
                )
                if delay:
                    self._sleep(delay)
                continue

            # 其他类别（permission / not_supported / platform_changed / transient 用尽）
            return None

        return None

    # ------------------------------------------------------------------
    def risk_hits_note(
        self, adapter: Any, capability_id: str, error: AdapterError
    ) -> None:
        if error.category == ErrorCategory.PLATFORM_CHANGED:
            self._log(
                Event.ADAPTER_FALLBACK,
                level="WARN",
                capability=capability_id,
                adapter=adapter.manifest.id,
                reason="platform_changed —— 该 Adapter 可能已失效，考虑单独替换",
                error=error.to_dict(),
            )

    # ------------------------------------------------------------------
    @staticmethod
    def _version(manifest: Manifest) -> str:
        return manifest.pinned_commit or f"{manifest.id}@{manifest.kind}"

    @staticmethod
    def _trace_entry(
        manifest: Manifest,
        capability_id: str,
        ok: bool,
        error_code: str = "",
        reason: str = "",
        skipped: bool = False,
        elapsed_ms: int = 0,
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "adapter": manifest.id,
            "capability": capability_id,
            "ok": ok,
        }
        if error_code:
            entry["error_code"] = error_code
        if reason:
            entry["reason"] = reason
        if skipped:
            entry["skipped"] = True
        if elapsed_ms:
            entry["elapsed_ms"] = elapsed_ms
        return entry

    def _log(self, event: str, level: str = "INFO", **fields: Any) -> None:
        if self.logger is not None:
            self.logger.emit(event, level=level, **fields)

    def _log_raw(self, adapter_id: str, text: str) -> None:
        if self.logger is not None:
            self.logger.raw(adapter_id, text)


def _suggest(error: AdapterError) -> list[str]:
    """把错误类别翻译成给用户/Agent 的可执行建议。"""
    if error.category == ErrorCategory.AUTH:
        return [
            "运行 `orchestrator accounts` 检查会话状态",
            "凭据失效时重新登录后再试",
        ]
    if error.category == ErrorCategory.PERMISSION:
        return [
            "确认该课程已加入你的学习通账号",
            "确认 course_id 与 clazz_id 匹配（用 `list_courses` 复核）",
        ]
    if error.category == ErrorCategory.NOT_SUPPORTED:
        return ["运行 `orchestrator adapters` 查看哪些 Adapter 支持该能力"]
    if error.category == ErrorCategory.PLATFORM_CHANGED:
        return [
            "平台可能已改版，对应 Adapter 需要更新（这正是 Adapter 隔离的价值：只换它一个）",
            "运行 `orchestrator probe` 确认受影响范围",
        ]
    if error.category == ErrorCategory.INPUT:
        return ["检查命令参数"]
    return []
