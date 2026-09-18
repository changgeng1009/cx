"""Router 契约测试（验收项 V4 / V5 / V6）。

这是整个 M0 最重要的一组测试，因为用户原则 6 的四条要求
（自动选择 / 自动 fallback / 明确错误 / 不隐藏失败）全部落在这里。
"""

from __future__ import annotations

import unittest

from orchestrator.adapters.mock import MockAdapter
from orchestrator.errors import AdapterError, Codes, ErrorCategory
from orchestrator.models import TaskContext, TaskState
from orchestrator.registry import CapabilityRegistry, Manifest
from orchestrator.risk import RiskDetector
from orchestrator.router import TaskRouter
from orchestrator.structured_log import StructuredLogger
from orchestrator.throttle import AccountThrottle

from .helpers import mock_manifest


def _router(registry: CapabilityRegistry, logger=None) -> TaskRouter:
    """测试用 Router：不休眠、不限速，保证确定性。"""
    return TaskRouter(
        registry,
        logger=logger,
        throttle=AccountThrottle(min_interval_ms=0, backoff=(0, 0, 0), cooldown_after_block=1800),
        risk_detector=RiskDetector(),
        max_attempts=2,
        sleeper=lambda _seconds: None,
        clock=lambda: 1000.0,
    )


def _ctx(request_id: str = "req_test") -> TaskContext:
    return TaskContext(request_id=request_id, account_id="acc_01")


def _registry(*adapters) -> CapabilityRegistry:
    registry = CapabilityRegistry()
    for adapter in adapters:
        registry.register(adapter, replace=True)
    return registry


class SelectionTests(unittest.TestCase):
    def test_full_level_preferred_over_partial(self) -> None:
        partial = MockAdapter(mock_manifest("partial-adapter", priority=1))
        full = MockAdapter(mock_manifest("full-adapter", priority=99))
        registry = _registry(
            MockAdapter(
                mock_manifest("partial-adapter", priority=1, capability_levels={"C06": "partial"})
            ),
            MockAdapter(mock_manifest("full-adapter", priority=99)),
        )
        candidates = registry.candidates("C06")
        self.assertEqual(candidates[0].manifest.id, "full-adapter")
        self.assertEqual(candidates[1].manifest.id, "partial-adapter")
        _ = (partial, full)

    def test_priority_breaks_ties_within_same_level(self) -> None:
        registry = _registry(
            MockAdapter(mock_manifest("low-priority", priority=50)),
            MockAdapter(mock_manifest("high-priority", priority=5)),
        )
        self.assertEqual(
            [a.manifest.id for a in registry.candidates("C06")],
            ["high-priority", "low-priority"],
        )

    def test_no_candidate_reports_clear_error(self) -> None:
        registry = _registry(MockAdapter(mock_manifest(capability_levels={"C24": "partial"})))
        outcome = _router(registry).execute("C99", {}, _ctx())
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error.code, Codes.NO_ADAPTER_AVAILABLE)
        self.assertTrue(outcome.next_actions)


class FallbackTests(unittest.TestCase):
    def test_falls_back_when_capability_declared_unsupported(self) -> None:
        primary = MockAdapter(mock_manifest("primary", priority=1))
        primary.capability_levels["C09"] = "none"
        secondary = MockAdapter(mock_manifest("secondary", priority=2))
        outcome = _router(_registry(primary, secondary)).execute(
            "C09", {"course_id": "240100001"}, _ctx()
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.adapter_id, "secondary")
        self.assertEqual(len(outcome.fallback_trace), 0, "primary 未被选中，不该有失败轨迹")

    def test_falls_back_on_permission_error(self) -> None:
        permission = AdapterError(
            code=Codes.ADAPTER_ERROR,
            category=ErrorCategory.PERMISSION,
            message="无权限",
        )
        primary = MockAdapter(mock_manifest("primary", priority=1), faults={"C06": permission})
        secondary = MockAdapter(mock_manifest("secondary", priority=2))
        outcome = _router(_registry(primary, secondary)).execute("C06", {}, _ctx())

        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.adapter_id, "secondary")
        self.assertEqual(len(outcome.fallback_trace), 1)
        entry = outcome.fallback_trace[0]
        self.assertEqual(entry["adapter"], "primary")
        self.assertFalse(entry["ok"])
        self.assertEqual(entry["error_code"], Codes.ADAPTER_ERROR)

    def test_falls_back_on_platform_changed(self) -> None:
        changed = AdapterError(
            code=Codes.ADAPTER_ERROR,
            category=ErrorCategory.PLATFORM_CHANGED,
            message="解析失败，平台疑似改版",
        )
        primary = MockAdapter(mock_manifest("primary", priority=1), faults={"C06": changed})
        secondary = MockAdapter(mock_manifest("secondary", priority=2))
        outcome = _router(_registry(primary, secondary)).execute("C06", {}, _ctx())
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.adapter_id, "secondary")

    def test_transient_error_retries_same_adapter_first(self) -> None:
        primary = MockAdapter(
            mock_manifest("primary", priority=1), transient_failures={"C06": 1}
        )
        secondary = MockAdapter(mock_manifest("secondary", priority=2))
        outcome = _router(_registry(primary, secondary)).execute("C06", {}, _ctx())

        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.adapter_id, "primary", "瞬时失败应先在同 Adapter 重试")
        self.assertEqual(len(primary.calls), 2)
        self.assertEqual(len(secondary.calls), 0)

    def test_unhealthy_adapter_is_skipped_not_called(self) -> None:
        unhealthy = MockAdapter(mock_manifest("unhealthy", priority=1), probe_healthy=False)
        healthy = MockAdapter(mock_manifest("healthy", priority=2))
        outcome = _router(_registry(unhealthy, healthy)).execute("C06", {}, _ctx())

        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.adapter_id, "healthy")
        self.assertEqual(len(unhealthy.calls), 0, "探活失败就不该真正调用")
        self.assertTrue(outcome.fallback_trace[0]["skipped"])
        self.assertEqual(outcome.fallback_trace[0]["error_code"], Codes.ADAPTER_NOT_READY)

    def test_input_error_does_not_fallback(self) -> None:
        bad_input = AdapterError(
            code=Codes.INVALID_PARAM, category=ErrorCategory.INPUT, message="缺少 course_id"
        )
        primary = MockAdapter(mock_manifest("primary", priority=1), faults={"C09": bad_input})
        secondary = MockAdapter(mock_manifest("secondary", priority=2))
        outcome = _router(_registry(primary, secondary)).execute("C09", {}, _ctx())

        self.assertFalse(outcome.ok)
        self.assertEqual(len(secondary.calls), 0, "参数错误换 Adapter 无意义，不该 fallback")
        self.assertEqual(outcome.error.category, ErrorCategory.INPUT)


class NoHiddenFailureTests(unittest.TestCase):
    """用户原则 6 的核心：不隐藏失败。"""

    def test_all_adapters_failed_reports_every_attempt(self) -> None:
        transient = AdapterError(
            code=Codes.ADAPTER_TIMEOUT,
            category=ErrorCategory.TRANSIENT,
            message="超时",
            retryable=True,
        )
        first = MockAdapter(mock_manifest("first", priority=1), faults={"C06": transient})
        second = MockAdapter(mock_manifest("second", priority=2), faults={"C06": transient})
        outcome = _router(_registry(first, second)).execute("C06", {}, _ctx())

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error.code, Codes.ALL_ADAPTERS_FAILED)
        adapters_tried = [e["adapter"] for e in outcome.fallback_trace]
        self.assertEqual(adapters_tried, ["first", "second"])
        self.assertTrue(all(not e["ok"] for e in outcome.fallback_trace))
        # 全部失败时必须返回 None 数据，不能返回空 dict 伪装成功
        self.assertIsNone(outcome.data)
        self.assertEqual(outcome.state, TaskState.FAILED)

    def test_data_none_when_not_ok_never_fabricated(self) -> None:
        failing = AdapterError(
            code=Codes.ADAPTER_ERROR,
            category=ErrorCategory.PERMISSION,
            message="无权限",
        )
        outcome = _router(
            _registry(MockAdapter(mock_manifest("only"), faults={"C06": failing}))
        ).execute("C06", {}, _ctx())
        self.assertFalse(outcome.ok)
        self.assertIsNone(outcome.data)


class RiskControlTests(unittest.TestCase):
    """风控必须阻断，且**不得** fallback（docs/03 §2.1）。"""

    def test_risk_control_blocks_and_does_not_fallback(self) -> None:
        risky = MockAdapter(mock_manifest("risky", priority=1)).raw_with_risk()
        backup = MockAdapter(mock_manifest("backup", priority=2))
        router = _router(_registry(risky, backup))
        outcome = router.execute("C06", {}, _ctx())

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.state, TaskState.BLOCKED)
        self.assertEqual(outcome.adapter_id, "risky")
        self.assertEqual(outcome.error.category, ErrorCategory.RISK_CONTROL)
        self.assertEqual(len(backup.calls), 0, "风控下不能 fallback —— 同一账号只会加重风控")
        self.assertTrue(router.throttle.is_blocked("acc_01"))
        self.assertTrue(any("不要切换" in a for a in outcome.next_actions))

    def test_blocked_account_short_circuits_further_calls(self) -> None:
        risky = MockAdapter(mock_manifest("risky", priority=1)).raw_with_risk()
        router = _router(_registry(risky))
        router.execute("C06", {}, _ctx())
        calls_before = len(risky.calls)

        second = router.execute("C06", {}, _ctx())
        self.assertFalse(second.ok)
        self.assertEqual(second.state, TaskState.BLOCKED)
        self.assertEqual(second.error.code, Codes.ACCOUNT_COOLING)
        self.assertEqual(len(risky.calls), calls_before, "冷却期内不该再发起真实调用")

    def test_login_page_is_not_treated_as_risk(self) -> None:
        detector = RiskDetector()
        hit = detector.detect("请登录 passport2.chaoxing.com/fanyalogin")
        self.assertIsNone(hit, "会话失效属于 auth，不该误判为风控触发全局熔断")

    def test_plain_403_alone_is_not_risk(self) -> None:
        detector = RiskDetector()
        self.assertIsNone(
            detector.detect("", status_code=403),
            "单独 403 更可能是权限问题；误判会导致账号被无谓冷却",
        )

    def test_429_is_risk(self) -> None:
        self.assertIsNotNone(RiskDetector().detect("", status_code=429))


class MockMustNeverWinTests(unittest.TestCase):
    """mock 永远排在真实 Adapter 之后 —— 哪怕它"声明得更完整"。

    实测事故（2026-09-18）：C48 签到如实标 partial、mock 标 full，于是按
    "full 优先于 partial" 的排序，`sign_in` **静默走了 mock**，返回"已签到"
    而平台上什么都没发生。写操作上的假成功比报错危险得多。
    """

    def _adapter(self, mid: str, kind: str, decl: dict, priority: int):
        raw = {
            "id": mid,
            "name": mid,
            "kind": kind,
            "enabled": True,
            "priority": priority,
            "capabilities": decl,
        }
        manifest = Manifest.from_dict(raw, source_path=f"{mid}.json")
        return MockAdapter(manifest)

    def test_partial_real_beats_full_mock(self) -> None:
        real = self._adapter("real", "subprocess", {"C48": {"level": "partial"}}, 15)
        mock = self._adapter("mock", "mock", {"C48": {"level": "full"}}, 90)
        registry = _registry(mock, real)
        order = [a.manifest.id for a in registry.candidates("C48")]
        self.assertEqual(order, ["real", "mock"])

    def test_mock_still_serves_when_alone(self) -> None:
        mock = self._adapter("mock", "mock", {"C48": {"level": "full"}}, 90)
        registry = _registry(mock)
        self.assertEqual([a.manifest.id for a in registry.candidates("C48")], ["mock"])

    def test_two_real_adapters_keep_full_before_partial(self) -> None:
        """真实 Adapter 之间仍按 full > partial（这条老规则不能被破坏）。"""
        full = self._adapter("a-full", "subprocess", {"C48": {"level": "full"}}, 50)
        part = self._adapter("b-part", "subprocess", {"C48": {"level": "partial"}}, 10)
        registry = _registry(part, full)
        order = [a.manifest.id for a in registry.candidates("C48")]
        self.assertEqual(order, ["a-full", "b-part"])


if __name__ == "__main__":
    unittest.main()
