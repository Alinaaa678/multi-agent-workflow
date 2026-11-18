"""B03-D18/D19：任务运行、执行状态、尝试历史、事件与断点恢复。"""

import json
import tempfile
import unittest
from pathlib import Path

from b03_implementation.errors import InvalidState, NotFound, VersionConflict
from b03_implementation.task_state import TASK_STATES, TaskStateStore

TASKS = [
    {"task_id": "T01", "goal": "基线"},
    {"task_id": "T02", "goal": "实现", "depends_on": ["T01"]},
    {"task_id": "T03", "goal": "验收", "depends_on": ["T02"]},
]


def plan_document(plan_version="PLAN-demo-v1.0"):
    return {
        "project_id": "demo",
        "spec_version": "S1-v1.0",
        "contract_version": "C1-v1.0",
        "plan_version": plan_version,
        "tasks": TASKS,
        "topological_order": ["T01", "T02", "T03"],
        "input_snapshot": {"spec_sha256": "a" * 64, "spec_version": "S1-v1.0", "contract_version": "C1-v1.0"},
    }


class TaskStateStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = TaskStateStore(Path(self.temp.name) / "task.db")
        self.store.register_plan("PLAN-demo-v1.0", plan_document())
        self.store.create_run(
            run_id="RUN-1",
            plan_id="PLAN-demo-v1.0",
            plan_version="PLAN-demo-v1.0",
            project_id="demo",
            spec_version="S1-v1.0",
            contract_version="C1-v1.0",
            input_snapshot="a" * 64,
            tasks=TASKS,
        )

    def test_nine_states_are_declared(self):
        self.assertEqual(
            set(TASK_STATES),
            {
                "PENDING",
                "READY",
                "RUNNING",
                "WAITING_DEPENDENCY",
                "REVIEWING",
                "FAILED",
                "RETRYING",
                "BLOCKED",
                "SUCCEEDED",
            },
        )

    def test_plan_registration_rejects_conflicting_content(self):
        with self.assertRaises(VersionConflict):
            self.store.register_plan("PLAN-demo-v1.0", {**plan_document(), "project_id": "other"})

    def test_plan_registration_is_idempotent_for_same_content(self):
        self.store.register_plan("PLAN-demo-v1.0", plan_document())
        listed = self.store.list_plans()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["task_count"], 3)

    def test_run_creation_registers_all_tasks_as_pending(self):
        states = self.store.list_task_states("RUN-1")
        self.assertEqual([item["state"] for item in states], ["PENDING", "PENDING", "PENDING"])
        self.assertEqual(self.store.get_run("RUN-1")["status"], "READY")

    def test_duplicate_run_is_rejected(self):
        with self.assertRaises(VersionConflict):
            self.store.create_run(
                run_id="RUN-1",
                plan_id="PLAN-demo-v1.0",
                plan_version="PLAN-demo-v1.0",
                project_id="demo",
                spec_version="S1-v1.0",
                contract_version="C1-v1.0",
                input_snapshot="a" * 64,
                tasks=TASKS,
            )

    def test_state_update_increments_rev(self):
        updated = self.store.update_task_state("RUN-1", "T01", state="READY")
        self.assertEqual(updated["rev"], 1)
        again = self.store.update_task_state("RUN-1", "T01", state="RUNNING", attempt_id=1)
        self.assertEqual(again["rev"], 2)

    def test_invalid_state_is_rejected(self):
        with self.assertRaises(InvalidState):
            self.store.update_task_state("RUN-1", "T01", state="DONE")

    def test_cas_conflict_is_rejected(self):
        self.store.update_task_state("RUN-1", "T01", state="READY")
        with self.assertRaises(VersionConflict):
            self.store.update_task_state("RUN-1", "T01", state="RUNNING", expected_rev=0)

    def test_unknown_task_is_not_found(self):
        with self.assertRaises(NotFound):
            self.store.update_task_state("RUN-1", "T99", state="READY")

    def test_json_columns_round_trip(self):
        artifacts = [{"path": "a.py", "kind": "source", "sha256": "b" * 64}]
        issues = [{"code": "X", "message": "m", "severity": "minor"}]
        updated = self.store.update_task_state(
            "RUN-1",
            "T02",
            state="REVIEWING",
            artifacts=artifacts,
            known_issues=issues,
            evidence_refs=["ev-1"],
            result={"status": "SUCCEEDED"},
            review={"decision": "ACCEPT"},
        )
        self.assertEqual(updated["artifacts"], artifacts)
        self.assertEqual(updated["known_issues"], issues)
        self.assertEqual(updated["evidence_refs"], ["ev-1"])
        self.assertEqual(updated["result"], {"status": "SUCCEEDED"})
        self.assertEqual(updated["review"], {"decision": "ACCEPT"})

    def test_attempt_history_preserves_old_attempts(self):
        self.store.record_attempt("RUN-1", "T02", attempt_id=1, retry_count=0, state="FAILED", reason="boom")
        self.store.record_attempt("RUN-1", "T02", attempt_id=2, retry_count=1, state="SUCCEEDED")
        attempts = self.store.list_attempts("RUN-1", "T02")
        self.assertEqual([item["attempt_id"] for item in attempts], [1, 2])
        self.assertEqual(attempts[0]["state"], "FAILED")
        self.assertEqual(attempts[0]["reason"], "boom")
        self.assertEqual(attempts[1]["retry_count"], 1)

    def test_event_sequence_and_idempotency(self):
        base = {
            "run_id": "RUN-1",
            "plan_version": "PLAN-demo-v1.0",
            "input_snapshot": "a" * 64,
            "type": "TASK_READY",
            "task_id": "T01",
            "attempt_id": 1,
            "event_id": "TEV-1",
        }
        first = self.store.append_run_event(base)
        duplicate = self.store.append_run_event(dict(base))
        self.assertEqual(first["sequence"], 1)
        self.assertEqual(duplicate["sequence"], 1)
        second = self.store.append_run_event({**base, "event_id": "TEV-2", "type": "TASK_DISPATCHED"})
        self.assertEqual(second["sequence"], 2)
        self.assertEqual(len(self.store.list_run_events("RUN-1")), 2)

    def test_event_cursor_pagination(self):
        for index in range(5):
            self.store.append_run_event(
                {
                    "run_id": "RUN-1",
                    "plan_version": "PLAN-demo-v1.0",
                    "input_snapshot": "a" * 64,
                    "type": "TASK_DISPATCHED",
                    "event_id": f"TEV-{index}",
                }
            )
        page = self.store.list_run_events("RUN-1", after_sequence=2, limit=10)
        self.assertEqual([item["sequence"] for item in page], [3, 4, 5])

    def test_checkpoint_and_restore_do_not_replay_completed_tasks(self):
        self.store.update_task_state("RUN-1", "T01", state="SUCCEEDED", attempt_id=1)
        self.store.update_task_state(
            "RUN-1", "T02", state="RUNNING", attempt_id=1, artifacts=[{"path": "a"}]
        )
        self.store.update_task_state("RUN-1", "T03", state="WAITING_DEPENDENCY")
        self.store.append_run_event(
            {
                "run_id": "RUN-1",
                "plan_version": "PLAN-demo-v1.0",
                "input_snapshot": "a" * 64,
                "type": "TASK_ACCEPTED",
                "task_id": "T01",
                "attempt_id": 1,
            }
        )
        checkpoint = self.store.build_run_checkpoint("RUN-1")
        self.store.write_run_checkpoint("RUN-1", checkpoint)
        restored = self.store.restore_run("RUN-1")
        self.assertEqual(restored["completed_tasks"], ["T01"])
        self.assertEqual(restored["in_flight_tasks"], ["T02"])
        self.assertIn("T03", restored["resumable_tasks"])
        self.assertEqual(restored["checkpoint"]["last_event_sequence"], 1)
        self.assertEqual(restored["checkpoint"]["artifacts"]["T02"], [{"path": "a"}])

    def test_checkpoint_is_rebuilt_when_missing(self):
        self.store.update_task_state("RUN-1", "T01", state="SUCCEEDED", attempt_id=1)
        restored = self.store.restore_run("RUN-1")
        self.assertEqual(restored["checkpoint"]["plan_version"], "PLAN-demo-v1.0")
        self.assertEqual(restored["completed_tasks"], ["T01"])

    def test_run_view_aggregates_counts(self):
        self.store.update_task_state("RUN-1", "T01", state="SUCCEEDED")
        self.store.update_task_state("RUN-1", "T02", state="BLOCKED", blocked_reason="upstream_failed")
        view = self.store.get_run_view("RUN-1")
        self.assertEqual(view["counts"]["SUCCEEDED"], 1)
        self.assertEqual(view["counts"]["BLOCKED"], 1)
        self.assertEqual(view["counts"]["PENDING"], 1)
        self.assertEqual(len(view["tasks"]), 3)

    def test_find_plan_by_version(self):
        found = self.store.find_plan_by_version("PLAN-demo-v1.0")
        self.assertIsNotNone(found)
        self.assertEqual(found["project_id"], "demo")
        self.assertIsNone(self.store.find_plan_by_version("PLAN-missing-v9.9"))

    def test_state_survives_reopen(self):
        path = Path(self.temp.name) / "task.db"
        self.store.update_task_state("RUN-1", "T01", state="SUCCEEDED")
        reopened = TaskStateStore(path)
        self.assertEqual(reopened.get_task_state("RUN-1", "T01")["state"], "SUCCEEDED")
        self.assertEqual(reopened.get_run("RUN-1")["plan_version"], "PLAN-demo-v1.0")


if __name__ == "__main__":
    unittest.main()
