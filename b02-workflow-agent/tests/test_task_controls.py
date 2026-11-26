"""课堂作业三：运行级执行控制、审计事件与人工重试。"""

from __future__ import annotations

from tests.test_task_orchestrator import (
    OrchestratorTestCase,
    TaskOrchestrationError,
    execution_result,
    make_plan,
    make_task,
)


class TaskExecutionControlTests(OrchestratorTestCase):
    def test_pause_blocks_dispatch_and_resume_restores_scheduling(self):
        orchestrator, _ = self.loaded_run([make_task("T01")])

        paused = orchestrator.pause(
            "TASK-RUN-TEST", request_id="CTRL-PAUSE-1", operator="teacher", reason="展示暂停"
        )
        self.assertEqual("PAUSED", paused["run"]["status"])
        self.assertEqual("CTRL-PAUSE-1", paused["ack"]["request_id"])
        self.assertTrue(paused["ack"]["accepted"])
        with self.assertRaises(TaskOrchestrationError) as caught:
            orchestrator.step("TASK-RUN-TEST")
        self.assertEqual(409, caught.exception.status)
        with self.assertRaises(TaskOrchestrationError):
            orchestrator.run("TASK-RUN-TEST")

        resumed = orchestrator.resume("TASK-RUN-TEST", operator="teacher")
        self.assertEqual("RUNNING", resumed["run"]["status"])
        stepped = orchestrator.step("TASK-RUN-TEST")
        self.assertEqual(["T01"], stepped["dispatched"])
        self.assertEqual("SUCCEEDED", stepped["run"]["status"])
        types = [event["type"] for event in self.all_events("TASK-RUN-TEST")]
        self.assertIn("RUN_PAUSED", types)
        self.assertIn("RUN_RESUMED", types)
        self.assertTrue(orchestrator.replay("TASK-RUN-TEST")["consistent"])

    def test_cancel_preserves_task_snapshot_and_rejects_further_dispatch(self):
        orchestrator, before = self.loaded_run([make_task("T01"), make_task("T02", depends_on=("T01",))])
        cancelled = orchestrator.cancel(
            "TASK-RUN-TEST", request_id="CTRL-CANCEL-1", reason="课堂取消演示"
        )
        self.assertEqual("CANCELLED", cancelled["run"]["status"])
        self.assertEqual(
            {task["task_id"]: task["state"] for task in before["tasks"]},
            {task["task_id"]: task["state"] for task in cancelled["run"]["tasks"]},
        )
        with self.assertRaises(TaskOrchestrationError):
            orchestrator.step("TASK-RUN-TEST")
        with self.assertRaises(TaskOrchestrationError):
            orchestrator.resume("TASK-RUN-TEST")
        last = self.all_events("TASK-RUN-TEST")[-1]
        self.assertEqual("RUN_CANCELLED", last["type"])
        self.assertEqual("课堂取消演示", last["payload"]["reason"])

    def test_manual_retry_recovers_downstream_blocked_tasks(self):
        calls = {"count": 0}

        def fail_once(request):
            calls["count"] += 1
            if calls["count"] == 1:
                return execution_result(request, status="FAILED", error="injected_failure")
            return execution_result(request)

        orchestrator, _ = self.loaded_run(
            [make_task("T01"), make_task("T02", depends_on=("T01",))],
            executor=fail_once,
        )
        failed = orchestrator.step("TASK-RUN-TEST")["run"]
        self.assertEqual("FAILED", failed["tasks"][0]["state"])
        self.assertEqual("BLOCKED", failed["tasks"][1]["state"])

        retried = orchestrator.retry_task(
            "TASK-RUN-TEST", "T01", request_id="CTRL-RETRY-1", operator="teacher"
        )
        states = {task["task_id"]: task["state"] for task in retried["run"]["tasks"]}
        self.assertEqual("SUCCEEDED", states["T01"])
        self.assertEqual("READY", states["T02"])
        self.assertEqual(2, calls["count"])
        self.assertEqual("T01 已重派，本次结果：SUCCEEDED。", retried["ack"]["message"])

        completed = orchestrator.run("TASK-RUN-TEST")
        self.assertEqual("SUCCEEDED", completed["run"]["status"])
        types = [event["type"] for event in self.all_events("TASK-RUN-TEST")]
        self.assertIn("TASK_RETRY_REQUESTED", types)
        self.assertTrue(orchestrator.replay("TASK-RUN-TEST")["consistent"])

    def test_retry_cannot_bypass_unfinished_dependencies(self):
        orchestrator, _ = self.loaded_run([make_task("T01"), make_task("T02", depends_on=("T01",))])
        self.store.update_task_state("TASK-RUN-TEST", "T02", state="BLOCKED", blocked_reason="manual")
        with self.assertRaises(TaskOrchestrationError) as caught:
            orchestrator.retry_task("TASK-RUN-TEST", "T02")
        self.assertEqual(409, caught.exception.status)
        self.assertIn("上游尚未全部通过", str(caught.exception))
        self.assertEqual([], self.events_of_type("TASK-RUN-TEST", "TASK_RETRY_REQUESTED"))

    def test_pause_and_cancel_are_rejected_after_success(self):
        orchestrator, _ = self.loaded_run([make_task("T01")])
        orchestrator.run("TASK-RUN-TEST")
        with self.assertRaises(TaskOrchestrationError):
            orchestrator.pause("TASK-RUN-TEST")
        with self.assertRaises(TaskOrchestrationError):
            orchestrator.cancel("TASK-RUN-TEST")
