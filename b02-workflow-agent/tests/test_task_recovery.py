"""B02 执行超时、迟到结果隔离与进程崩溃恢复测试。"""

from __future__ import annotations

import time

from backend.task_orchestrator import now_iso
from tests.test_task_orchestrator import (
    OrchestratorTestCase,
    TaskOrchestrationError,
    execution_result,
    make_task,
)


class TaskTimeoutTests(OrchestratorTestCase):
    def test_timeout_exhausts_retry_budget_blocks_downstream_and_ignores_late_success(self):
        calls = []

        def slow_success(request):
            calls.append(request["attempt_id"])
            time.sleep(1.25)
            return execution_result(request)

        orchestrator, _ = self.loaded_run(
            [
                make_task("T01", timeout=1, retry_budget=1),
                make_task("T02", depends_on=("T01",)),
            ],
            executor=slow_success,
        )
        finished = orchestrator.run("TASK-RUN-TEST", max_steps=5)
        states = {task["task_id"]: task for task in finished["run"]["tasks"]}
        self.assertEqual("FAILED", states["T01"]["state"])
        self.assertEqual("BLOCKED", states["T02"]["state"])
        self.assertEqual(1, states["T01"]["retry_count"])
        self.assertEqual(2, states["T01"]["attempt_id"])
        self.assertIn("TASK_TIMEOUT", states["T01"]["error"])
        self.assertEqual([1, 2], calls)
        self.assertEqual(
            ["RETRYING", "FAILED"],
            [attempt["state"] for attempt in states["T01"]["attempts"]],
        )
        rejected = self.events_of_type("TASK-RUN-TEST", "TASK_REJECTED")
        self.assertEqual(2, len(rejected))
        for event in rejected:
            self.assertIn("TASK_TIMEOUT", event["payload"]["reason"])
            self.assertEqual("TASK_TIMEOUT", event["payload"]["findings"][0]["code"])
        self.assertTrue(orchestrator.replay("TASK-RUN-TEST")["consistent"])

        # 两个工作线程随后都会返回成功，但它们只能写入自己的隔离队列。
        time.sleep(0.5)
        latest = self.task(orchestrator, "TASK-RUN-TEST", "T01")
        self.assertEqual("FAILED", latest["state"])
        self.assertEqual(2, latest["attempt_id"])
        self.assertTrue(orchestrator.replay("TASK-RUN-TEST")["consistent"])

    def test_invalid_timeout_is_rejected_before_dispatch(self):
        orchestrator, _ = self.loaded_run([make_task("T01", timeout=0)])
        with self.assertRaises(TaskOrchestrationError):
            orchestrator.dispatch("TASK-RUN-TEST", "T01")
        task = self.task(orchestrator, "TASK-RUN-TEST", "T01")
        self.assertEqual("READY", task["state"])
        self.assertEqual(0, task["attempt_id"])


class TaskRecoveryTests(OrchestratorTestCase):
    def test_recover_running_and_reviewing_without_repeating_completed_tasks(self):
        calls = []

        def executor(request):
            calls.append((request["task_id"], request["attempt_id"]))
            return execution_result(request)

        orchestrator, _ = self.loaded_run(
            [make_task("T03"), make_task("T01", retry_budget=2), make_task("T02")],
            executor=executor,
        )
        orchestrator.dispatch("TASK-RUN-TEST", "T03")
        run = orchestrator._require_run("TASK-RUN-TEST")

        # 构造带完整事件链的崩溃现场：T01 执行中，T02 验收中，T03 已成功。
        orchestrator._transition(
            run, "T01", "RUNNING", event_type="TASK_DISPATCHED",
            attempt_id=2, retry_count=1, started_at=now_iso(),
        )
        orchestrator._emit(run, "TASK_STARTED", "T01", attempt_id=2)
        orchestrator._transition(
            run, "T02", "RUNNING", event_type="TASK_DISPATCHED",
            attempt_id=1, started_at=now_iso(),
        )
        orchestrator._emit(run, "TASK_STARTED", "T02", attempt_id=1)
        orchestrator._transition(
            run, "T02", "REVIEWING", event_type="TASK_OUTPUT_SUBMITTED",
            candidate_commit="abc1234",
        )
        self.assertTrue(orchestrator.replay("TASK-RUN-TEST")["consistent"])

        recovered = orchestrator.recover(
            "TASK-RUN-TEST", request_id="CTRL-RECOVER-1", operator="teacher"
        )
        self.assertEqual({"T01", "T02"}, set(recovered["recovered_tasks"]))
        self.assertEqual("CTRL-RECOVER-1", recovered["ack"]["request_id"])
        states = {task["task_id"]: task for task in recovered["run"]["tasks"]}
        self.assertEqual("READY", states["T01"]["state"])
        self.assertEqual("READY", states["T02"]["state"])
        self.assertEqual("SUCCEEDED", states["T03"]["state"])
        self.assertEqual(2, states["T01"]["attempt_id"])
        self.assertEqual(1, states["T01"]["retry_count"])
        self.assertIsNone(states["T02"]["candidate_commit"])
        interrupted = states["T01"]["attempts"][-1]
        self.assertEqual("INTERRUPTED", interrupted["state"])
        self.assertEqual("PROCESS_RESTART_RECOVERY", interrupted["reason"])
        recovery_events = self.events_of_type("TASK-RUN-TEST", "RUN_RECOVERED")
        self.assertEqual(1, len(recovery_events))
        self.assertEqual({"T01", "T02"}, set(recovery_events[0]["payload"]["recovered_tasks"]))
        self.assertTrue(orchestrator.replay("TASK-RUN-TEST")["consistent"])

        completed = orchestrator.run("TASK-RUN-TEST")
        self.assertEqual("SUCCEEDED", completed["run"]["status"])
        final_states = {task["task_id"]: task for task in completed["run"]["tasks"]}
        self.assertEqual(3, final_states["T01"]["attempt_id"])
        self.assertEqual(2, final_states["T02"]["attempt_id"])
        self.assertEqual(1, final_states["T01"]["retry_count"])
        self.assertEqual(1, final_states["T03"]["attempt_id"])
        self.assertEqual(1, len(final_states["T03"]["attempts"]))
        self.assertEqual(1, sum(1 for task_id, _ in calls if task_id == "T03"))
        self.assertTrue(orchestrator.replay("TASK-RUN-TEST")["consistent"])

    def test_recovery_preserves_pause_and_cancelled_run_cannot_recover(self):
        orchestrator, _ = self.loaded_run([make_task("T01")])
        orchestrator.pause("TASK-RUN-TEST")
        run = orchestrator._require_run("TASK-RUN-TEST")
        orchestrator._transition(
            run, "T01", "RUNNING", event_type="TASK_DISPATCHED",
            attempt_id=1, started_at=now_iso(),
        )
        recovered = orchestrator.recover("TASK-RUN-TEST")
        self.assertEqual("PAUSED", recovered["run"]["status"])
        self.assertEqual("READY", recovered["run"]["tasks"][0]["state"])
        with self.assertRaises(TaskOrchestrationError):
            orchestrator.step("TASK-RUN-TEST")
        orchestrator.resume("TASK-RUN-TEST")
        self.assertEqual("SUCCEEDED", orchestrator.step("TASK-RUN-TEST")["run"]["status"])

        cancelled, _ = self.loaded_run([make_task("T01")], run_id="TASK-RUN-CANCELLED")
        cancelled.cancel("TASK-RUN-CANCELLED")
        with self.assertRaises(TaskOrchestrationError) as caught:
            cancelled.recover("TASK-RUN-CANCELLED")
        self.assertEqual(409, caught.exception.status)

    def test_recovery_rejects_unfinished_dependencies_without_partial_changes(self):
        orchestrator, _ = self.loaded_run(
            [make_task("T01"), make_task("T02", depends_on=("T01",))]
        )
        self.store.update_task_state(
            "TASK-RUN-TEST", "T02", state="RUNNING", attempt_id=1, started_at=now_iso()
        )
        with self.assertRaises(TaskOrchestrationError) as caught:
            orchestrator.recover("TASK-RUN-TEST")
        self.assertEqual(409, caught.exception.status)
        self.assertIn("上游尚未全部通过", str(caught.exception))
        self.assertEqual("RUNNING", self.task(orchestrator, "TASK-RUN-TEST", "T02")["state"])
        self.assertEqual([], self.events_of_type("TASK-RUN-TEST", "RUN_RECOVERED"))
