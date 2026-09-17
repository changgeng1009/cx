"""状态机 / 断点 / 控制通道 / 限流（验收项 V3 / V6 / V7）。"""

from __future__ import annotations

import unittest

from orchestrator.control import ControlChannel
from orchestrator.models import TaskState, TaskType
from orchestrator.state import (
    ALLOWED_TRANSITIONS,
    Checkpoint,
    InvalidTransition,
    StateStore,
    TaskStateMachine,
    filter_by_types,
    is_terminal,
    is_valid_transition,
)
from orchestrator.throttle import AccountThrottle, FakeClock

from .helpers import Sandbox, mock_manifest

COURSE_1 = "240100001"


class StateMachineTests(unittest.TestCase):
    def test_every_declared_transition_is_accepted(self) -> None:
        for source, targets in ALLOWED_TRANSITIONS.items():
            for target in targets:
                self.assertTrue(
                    is_valid_transition(source, target),
                    f"声明的流转被拒绝：{source} -> {target}",
                )

    def test_undeclared_transitions_are_rejected(self) -> None:
        for source, targets in ALLOWED_TRANSITIONS.items():
            for target in TaskState:
                if target in targets:
                    continue
                machine = TaskStateMachine(initial=source)
                with self.assertRaises(InvalidTransition):
                    machine.transition(target)
                self.assertFalse(is_valid_transition(source, target))

    def test_terminal_states_have_no_outgoing(self) -> None:
        self.assertEqual(ALLOWED_TRANSITIONS[TaskState.COMPLETED], frozenset())
        self.assertEqual(ALLOWED_TRANSITIONS[TaskState.CANCELLED], frozenset())
        self.assertTrue(is_terminal(TaskState.COMPLETED))
        self.assertFalse(is_terminal(TaskState.PAUSED))

    def test_retry_path_is_failed_to_pending_not_to_running(self) -> None:
        """failed 必须先回 pending 再 running。

        直接 failed -> running 会绕过"重新入队"这一步，导致重试没有
        独立的状态记录，失败次数也就统计不出来。
        """
        self.assertTrue(is_valid_transition(TaskState.FAILED, TaskState.PENDING))
        self.assertFalse(is_valid_transition(TaskState.FAILED, TaskState.RUNNING))

    def test_blocked_cannot_jump_to_completed(self) -> None:
        """风控不能直接算完成——否则风控会被悄悄吞掉。"""
        self.assertFalse(is_valid_transition(TaskState.BLOCKED, TaskState.COMPLETED))

    def test_history_records_each_transition(self) -> None:
        machine = TaskStateMachine()
        machine.transition(TaskState.RUNNING)
        machine.transition(TaskState.PAUSED)
        machine.transition(TaskState.RUNNING)
        self.assertEqual(len(machine.history), 3)
        self.assertEqual(machine.history[0]["state_from"], "pending")
        self.assertEqual(machine.history[-1]["state_to"], "running")


class CheckpointTests(unittest.TestCase):
    def test_remaining_excludes_completed_and_skipped(self) -> None:
        checkpoint = Checkpoint("req_1", "acc_01")
        checkpoint.mark_completed("tp_001")
        checkpoint.mark_skipped("tp_002", "quiz", "locked")
        remaining = checkpoint.remaining_from(["tp_001", "tp_002", "tp_003"])
        self.assertEqual(remaining, ["tp_003"])

    def test_mark_failed_increments_attempt(self) -> None:
        checkpoint = Checkpoint("req_1", "acc_01")
        checkpoint.mark_failed("tp_005", "video", "ADAPTER_TIMEOUT", "transient")
        checkpoint.mark_failed("tp_005", "video", "ADAPTER_TIMEOUT", "transient")
        self.assertEqual(len(checkpoint.failed_task_points), 1)
        self.assertEqual(checkpoint.failed_task_points[0].attempt, 2)

    def test_mark_completed_is_idempotent(self) -> None:
        checkpoint = Checkpoint("req_1", "acc_01")
        checkpoint.mark_completed("tp_001")
        checkpoint.mark_completed("tp_001")
        self.assertEqual(checkpoint.completed_task_points, ["tp_001"])

    def test_roundtrip_through_dict(self) -> None:
        checkpoint = Checkpoint("req_1", "acc_01", command="run_course", course_id=COURSE_1)
        checkpoint.mark_completed("tp_001")
        checkpoint.mark_failed("tp_005", "video", "X", "transient")
        restored = Checkpoint.from_dict(checkpoint.to_dict())
        self.assertEqual(restored.completed_task_points, ["tp_001"])
        self.assertEqual(restored.failed_task_points[0].task_point_id, "tp_005")
        self.assertEqual(restored.state, TaskState.PENDING)


class StateStoreTests(unittest.TestCase):
    def test_save_load_and_list(self) -> None:
        with Sandbox() as sandbox:
            store: StateStore = sandbox.ctx.state_store
            store.save(Checkpoint("req_a", "acc_01", course_id=COURSE_1))
            store.save(Checkpoint("req_b", "acc_01", course_id="240100002"))
            loaded = store.load("acc_01", "req_a")
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.course_id, COURSE_1)
            self.assertEqual(len(store.list_for_account("acc_01")), 2)
            self.assertIsNone(store.load("acc_01", "req_missing"))

    def test_type_filter_is_what_makes_type_scoped_commands_possible(self) -> None:
        index = {
            "tp_1": str(TaskType.VIDEO),
            "tp_2": str(TaskType.READING),
            "tp_3": str(TaskType.PPT),
            "tp_4": str(TaskType.QUIZ),
        }
        self.assertEqual(filter_by_types(index, [TaskType.VIDEO]), ["tp_1"])
        reading = filter_by_types(index, [TaskType.READING, TaskType.PPT])
        self.assertCountEqual(reading, ["tp_2", "tp_3"])
        self.assertEqual(len(filter_by_types(index, [])), 4)


class ControlChannelTests(unittest.TestCase):
    def test_pause_cancel_clear(self) -> None:
        with Sandbox() as sandbox:
            channel: ControlChannel = sandbox.ctx.control
            channel.request_pause("req_x", note="测试暂停")
            signal = channel.read("req_x")
            self.assertIsNotNone(signal)
            self.assertTrue(signal.pause)
            self.assertFalse(signal.cancel)

            channel.request_cancel("req_x")
            signal = channel.read("req_x")
            self.assertTrue(signal.pause and signal.cancel)

            channel.clear("req_x")
            self.assertIsNone(channel.read("req_x"))


class PauseResumeTests(unittest.TestCase):
    """协作式暂停 → 断点 → 恢复只跑剩余（V7）。"""

    def test_pause_at_task_point_boundary_then_resume_skips_done(self) -> None:
        from orchestrator.adapters.mock import MockAdapter
        from orchestrator.models import AdapterResult

        adapter = MockAdapter(mock_manifest("mock"))

        with Sandbox([adapter]) as sandbox:
            def pausing_run(cap, params, ctx):
                completed: list[str] = []
                for task_point_id in ("tp_002", "tp_003", "tp_004"):
                    if ctx.cancelled:
                        return AdapterResult.success(
                            {"completed": completed, "stopped_reason": "cancelled"}
                        )
                    if ctx.pause_requested:
                        return AdapterResult.success(
                            {"completed": completed, "stopped_reason": "paused"}
                        )
                    ctx.report(
                        "task_point.started",
                        task_point_id=task_point_id,
                        task_type="video",
                    )
                    ctx.report(
                        "task_point.completed",
                        task_point_id=task_point_id,
                        task_type="video",
                    )
                    completed.append(task_point_id)
                return AdapterResult.success(
                    {"completed": completed, "stopped_reason": "finished"}
                )

            adapter.overrides["C18"] = pausing_run

            # 事前放下暂停标志：执行方在第一个任务点边界就会发现它
            sandbox.ctx.control.request_pause("req_pause")
            sandbox.ctx.orchestrator.confirmed = True
            envelope = sandbox.ctx.orchestrator.run(
                "run_course",
                {"course_id": COURSE_1},
                request_id="req_pause",
                account_id="acc_01",
            )

            self.assertTrue(envelope.ok)
            self.assertEqual(envelope.state, str(TaskState.PAUSED))
            checkpoint = sandbox.ctx.state_store.load("acc_01", "req_pause")
            self.assertIsNotNone(checkpoint)
            self.assertEqual(checkpoint.state, TaskState.PAUSED)
            self.assertEqual(len(checkpoint.completed_task_points), 1)

            # 恢复：已完成的任务点必须被跳过
            adapter.overrides.pop("C18")
            resumed = sandbox.ctx.orchestrator.run(
                "resume",
                {"request_id": "req_pause"},
                account_id="acc_01",
            )
            self.assertTrue(resumed.ok)
            inner = (resumed.data or {}).get("result") or {}
            self.assertNotIn("tp_002", inner.get("completed") or [])
            skipped_ids = {s["id"] for s in (inner.get("skipped") or [])}
            self.assertIn("tp_002", skipped_ids)
            self.assertIsNone(sandbox.ctx.control.read("req_pause"), "恢复后应清掉控制信号")


class ThrottleTests(unittest.TestCase):
    def test_min_interval_enforced_and_measured(self) -> None:
        clock = FakeClock()
        throttle = AccountThrottle(min_interval_ms=1000, clock=clock.now, sleeper=clock.sleep)
        self.assertTrue(throttle.acquire("acc_01").allowed)
        throttle.release("acc_01")

        decision = throttle.acquire("acc_01")
        self.assertTrue(decision.allowed)
        self.assertEqual(clock.sleeps, [1.0], "不足间隔时应等待补齐")
        throttle.release("acc_01")

    def test_block_triggers_cooldown(self) -> None:
        clock = FakeClock()
        throttle = AccountThrottle(
            min_interval_ms=0, cooldown_after_block=60, clock=clock.now, sleeper=clock.sleep
        )
        throttle.trigger_block("acc_01", reason="操作过于频繁")
        self.assertTrue(throttle.is_blocked("acc_01"))
        self.assertEqual(throttle.cooldown_remaining("acc_01"), 60)

        decision = throttle.check("acc_01")
        self.assertFalse(decision.allowed)
        self.assertIn("冷却", decision.reason)

        clock.advance(61)
        self.assertFalse(throttle.is_blocked("acc_01"))
        self.assertTrue(throttle.check("acc_01").allowed)

    def test_backoff_sequence_is_capped(self) -> None:
        throttle = AccountThrottle(min_interval_ms=0, backoff=(5, 15, 45))
        self.assertEqual(throttle.backoff_for("acc_01", 1), 5)
        self.assertEqual(throttle.backoff_for("acc_01", 2), 15)
        self.assertEqual(throttle.backoff_for("acc_01", 3), 45)
        self.assertEqual(throttle.backoff_for("acc_01", 9), 45, "超出序列应封顶")


if __name__ == "__main__":
    unittest.main()
