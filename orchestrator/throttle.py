"""账号级限流。

为什么放在统一层而不是 Adapter 里：限流的主体是**账号**，不是项目。
同一个账号无论用 A1 还是 A2 调用，都共享同一份请求预算。如果每个
Adapter 自己限流，换一次 Adapter 就等于重置了一次预算——风控照样会触发。

时间与休眠都可注入，因此限流逻辑可以被确定性地测试（V6 验收项）。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

#: 默认指数退避序列（秒）
DEFAULT_BACKOFF: tuple[int, ...] = (5, 15, 45, 120)

#: 命中风控后的冷却时长（秒）
DEFAULT_COOLDOWN_S = 1800


@dataclass
class ThrottleDecision:
    allowed: bool
    wait_ms: int = 0
    reason: str = ""

    def to_dict(self) -> dict[str, object]:
        return {"allowed": self.allowed, "wait_ms": self.wait_ms, "reason": self.reason}


@dataclass
class _AccountState:
    last_call_at: float | None = None
    blocked_until: float | None = None
    block_reason: str = ""
    consecutive_failures: int = 0
    calls: int = 0
    waits: int = 0


class AccountThrottle:
    def __init__(
        self,
        min_interval_ms: int = 1500,
        max_concurrency: int = 1,
        backoff: tuple[int, ...] = DEFAULT_BACKOFF,
        cooldown_after_block: int = DEFAULT_COOLDOWN_S,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.min_interval_ms = max(0, min_interval_ms)
        self.max_concurrency = max(1, max_concurrency)
        self.backoff = backoff
        self.cooldown_after_block = max(0, cooldown_after_block)
        self._clock = clock
        self._sleep = sleeper
        self._states: dict[str, _AccountState] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._semaphores: dict[str, threading.BoundedSemaphore] = {}
        self._guard = threading.Lock()

    # ------------------------------------------------------------------
    def _state(self, account_id: str) -> _AccountState:
        with self._guard:
            if account_id not in self._states:
                self._states[account_id] = _AccountState()
            return self._states[account_id]

    def _lock(self, account_id: str) -> threading.Lock:
        with self._guard:
            if account_id not in self._locks:
                self._locks[account_id] = threading.Lock()
            return self._locks[account_id]

    def _semaphore(self, account_id: str) -> threading.BoundedSemaphore:
        with self._guard:
            if account_id not in self._semaphores:
                self._semaphores[account_id] = threading.BoundedSemaphore(
                    self.max_concurrency
                )
            return self._semaphores[account_id]

    # ------------------------------------------------------------------
    def cooldown_remaining(self, account_id: str) -> int:
        state = self._state(account_id)
        if state.blocked_until is None:
            return 0
        remaining = state.blocked_until - self._clock()
        return max(0, int(round(remaining)))

    def is_blocked(self, account_id: str) -> bool:
        return self.cooldown_remaining(account_id) > 0

    def check(self, account_id: str) -> ThrottleDecision:
        """只判断不等待，供 Router 在选路前快速筛查。"""
        remaining = self.cooldown_remaining(account_id)
        if remaining > 0:
            return ThrottleDecision(
                allowed=False,
                wait_ms=remaining * 1000,
                reason=f"账号冷却中，剩余 {remaining}s",
            )
        state = self._state(account_id)
        if state.last_call_at is not None and self.min_interval_ms:
            elapsed_ms = (self._clock() - state.last_call_at) * 1000
            wait = self.min_interval_ms - elapsed_ms
            if wait > 0:
                return ThrottleDecision(
                    allowed=False,
                    wait_ms=int(wait),
                    reason=f"距上次调用不足 {self.min_interval_ms}ms",
                )
        return ThrottleDecision(allowed=True)

    def acquire(self, account_id: str, block: bool = True) -> ThrottleDecision:
        """占用一次调用配额。

        `block=False` 时不休眠，直接把需要等待的时长返回给调用方——
        这在"宁可失败也不要卡住"的场景下有用（例如签到窗口即将关闭）。
        """
        with self._lock(account_id):
            decision = self.check(account_id)
            if not decision.allowed and decision.wait_ms > 0 and block:
                if self.cooldown_remaining(account_id) <= 0:
                    self._sleep(decision.wait_ms / 1000.0)
                    self._state(account_id).waits += 1
                    decision = self.check(account_id)
        if decision.allowed:
            state = self._state(account_id)
            state.last_call_at = self._clock()
            state.calls += 1
            self._semaphore(account_id).acquire()
        return decision

    def release(self, account_id: str) -> None:
        try:
            self._semaphore(account_id).release()
        except (KeyError, ValueError):
            pass

    # ------------------------------------------------------------------
    def backoff_for(self, account_id: str, attempt: int) -> int:
        """第 attempt 次失败后应等待的秒数（attempt 从 1 开始）。"""
        if not self.backoff:
            return 0
        index = min(max(attempt, 1) - 1, len(self.backoff) - 1)
        return self.backoff[index]

    def note_success(self, account_id: str) -> None:
        self._state(account_id).consecutive_failures = 0

    def note_failure(self, account_id: str) -> int:
        state = self._state(account_id)
        state.consecutive_failures += 1
        return state.consecutive_failures

    # ------------------------------------------------------------------
    def trigger_block(self, account_id: str, reason: str = "risk_control") -> int:
        state = self._state(account_id)
        state.blocked_until = self._clock() + self.cooldown_after_block
        state.block_reason = reason
        return self.cooldown_remaining(account_id)

    def clear_block(self, account_id: str) -> None:
        state = self._state(account_id)
        state.blocked_until = None
        state.block_reason = ""

    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, dict[str, object]]:
        result: dict[str, dict[str, object]] = {}
        for account_id, state in self._states.items():
            result[account_id] = {
                "calls": state.calls,
                "waits": state.waits,
                "consecutive_failures": state.consecutive_failures,
                "blocked": self.is_blocked(account_id),
                "cooldown_remaining_s": self.cooldown_remaining(account_id),
                "block_reason": state.block_reason,
            }
        return result


@dataclass
class FakeClock:
    """测试用时钟：时间只在 `advance()` 时前进。"""

    value: float = 1000.0
    sleeps: list[float] = field(default_factory=list)

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds

    def advance(self, seconds: float) -> None:
        self.value += seconds
