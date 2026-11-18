"""跨块闭环测试：B01 → B02 → B03 → B04 → B06 的契约一致性。

这些测试用真实模块（不是 mock 替身），只把外部副作用限制在临时目录：
- 执行模式为 `mock`（B04 的确定性执行器），不发起网络/模型调用。
- 每个测试一个独立临时目录，互不干扰。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from workflow_runtime.bootstrap import WorkflowStack  # noqa: E402

FIXTURES = REPO / "b01-spec-planner" / "docs" / "fixtures"
CAMPUS = FIXTURES / "campus-lost-found.spec.json"
LAB = FIXTURES / "lab-equipment-loan.spec.md"


class WorkflowStackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=REPO)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def stack(self, scenario=None, name="state"):
        return WorkflowStack(data_dir=self.root / name, execution_mode="mock", scenario=scenario or {})

    # ---------- 规格 → 计划 ----------

    def test_json_and_markdown_specs_both_produce_valid_plans(self):
        stack = self.stack()
        for spec_path in (CAMPUS, LAB):
            with self.subTest(spec=spec_path.name):
                plan = stack.plan_from_spec_path(spec_path)
                self.assertEqual(sorted(plan["topological_order"]),
                                 sorted(task["task_id"] for task in plan["tasks"]))
                self.assertTrue(plan["input_snapshot"]["spec_sha256"])

    def test_plan_differs_between_specs(self):
        """动态计划：不同规格必须得到不同任务数，而不是固定 T01—T11。"""
        stack = self.stack()
        campus_plan = stack.plan_from_spec_path(CAMPUS)
        lab_plan = stack.plan_from_spec_path(LAB)
        self.assertNotEqual(len(campus_plan["tasks"]), len(lab_plan["tasks"]))
        self.assertNotEqual(campus_plan["plan_version"], lab_plan["plan_version"])

    # ---------- 计划 → 运行 ----------

    def test_load_plan_and_create_run(self):
        stack = self.stack()
        plan = stack.plan_from_spec_path(LAB)
        loaded = stack.load_plan(plan)
        run = stack.start_run(loaded["plan_id"])
        self.assertEqual(run["total"], len(plan["tasks"]))
        self.assertEqual(run["plan_version"], plan["plan_version"])
        self.assertEqual(run["topological_order"], plan["topological_order"])
        roots = sum(1 for task in plan["tasks"] if not task["depends_on"])
        self.assertEqual(run["counts"]["READY"], roots)

    def test_success_run_reaches_all_succeeded(self):
        stack = self.stack()
        plan = stack.plan_from_spec_path(LAB)
        loaded = stack.load_plan(plan)
        run = stack.start_run(loaded["plan_id"])
        outcome = stack.run(run["run_id"], max_steps=200)
        self.assertEqual(outcome["run"]["status"], "SUCCEEDED")
        self.assertEqual(outcome["run"]["completed"], outcome["run"]["total"])
        for task in outcome["run"]["tasks"]:
            self.assertEqual(task["state"], "SUCCEEDED")
            self.assertTrue(task["candidate_commit"])

    def test_success_run_is_replay_consistent(self):
        stack = self.stack()
        plan = stack.plan_from_spec_path(LAB)
        loaded = stack.load_plan(plan)
        run = stack.start_run(loaded["plan_id"])
        stack.run(run["run_id"], max_steps=200)
        replay = stack.replay(run["run_id"])
        self.assertTrue(replay["consistent"], replay["mismatches"])
        self.assertGreater(replay["last_sequence"], 0)

    def test_b06_verdict_is_traceable_in_acceptance_event(self):
        """B02 是唯一状态写入者；B06 的结论必须以 payload.gate 体现，避免不可追溯。"""
        stack = self.stack()
        plan = stack.plan_from_spec_path(LAB)
        loaded = stack.load_plan(plan)
        run = stack.start_run(loaded["plan_id"])
        stack.run(run["run_id"], max_steps=200)
        events = stack.events(run["run_id"], limit=10000)["events"]
        producers = {event["producer"] for event in events}
        self.assertEqual(producers, {"B02"}, "状态事件只应由 B02 产生")
        accepted = [event for event in events if event["type"] == "TASK_ACCEPTED"]
        self.assertTrue(accepted)
        for event in accepted:
            gate = event["payload"].get("gate")
            self.assertIsNotNone(gate, "TASK_ACCEPTED 缺少 B06 判定来源")
            self.assertEqual(gate["gate"], "B06")
            self.assertEqual(gate["decision"], "ACCEPT")

    # ---------- 失败 / 重试 / 阻断 ----------

    def test_failure_consumes_retry_budget_then_blocks_downstream(self):
        probe = self.stack(name="probe")
        plan = probe.plan_from_spec_path(LAB)
        target = plan["tasks"][0]["task_id"]
        budget = next(task["retry_budget"] for task in plan["tasks"] if task["task_id"] == target)

        stack = self.stack({"fail_tasks": {target: "注入失败"}}, name="run")
        loaded = stack.load_plan(plan)
        run = stack.start_run(loaded["plan_id"])
        snapshot = stack.run(run["run_id"], max_steps=200)["run"]

        failed = next(task for task in snapshot["tasks"] if task["task_id"] == target)
        self.assertEqual(failed["state"], "FAILED")
        self.assertEqual(failed["retry_count"], budget)
        self.assertEqual(len(failed["attempts"]), budget + 1)

        downstream = [task for task in snapshot["tasks"] if target in task["depends_on"]]
        self.assertTrue(downstream)
        for task in downstream:
            self.assertEqual(task["state"], "BLOCKED")
            self.assertTrue(task["blocked_reason"])

        retries = [
            event
            for event in stack.events(run["run_id"], limit=10000)["events"]
            if event["type"] == "TASK_RETRY_SCHEDULED"
        ]
        self.assertEqual(len(retries), budget)

    def test_blocked_execution_propagates_and_run_reports_blocked(self):
        probe = self.stack(name="probe")
        plan = probe.plan_from_spec_path(LAB)
        target = plan["tasks"][0]["task_id"]

        stack = self.stack({"block_tasks": {target}}, name="run")
        loaded = stack.load_plan(plan)
        run = stack.start_run(loaded["plan_id"])
        snapshot = stack.run(run["run_id"], max_steps=200)["run"]

        blocked = next(task for task in snapshot["tasks"] if task["task_id"] == target)
        self.assertEqual(blocked["state"], "BLOCKED")
        self.assertIn("模拟环境依赖不可用", blocked["blocked_reason"])
        self.assertEqual(snapshot["status"], "BLOCKED")

    def test_missing_evidence_is_rejected_not_accepted(self):
        probe = self.stack(name="probe")
        plan = probe.plan_from_spec_path(LAB)
        target = plan["tasks"][0]["task_id"]

        stack = self.stack({"missing_first_test_tasks": [target]}, name="run")
        loaded = stack.load_plan(plan)
        run = stack.start_run(loaded["plan_id"])
        snapshot = stack.run(run["run_id"], max_steps=200)["run"]

        task = next(item for item in snapshot["tasks"] if item["task_id"] == target)
        self.assertNotEqual(task["state"], "SUCCEEDED")
        codes = {item["code"] for item in (task.get("review") or {}).get("findings") or []}
        self.assertIn("TEST_NOT_PASSED", codes)

        defects = [
            event
            for event in stack.events(run["run_id"], limit=10000)["events"]
            if event["type"] == "DEFECT_CREATED"
        ]
        self.assertTrue(defects)
        for event in defects:
            self.assertEqual(event["task_id"], target)
            self.assertIn("defect_id", event["payload"])

    # ---------- 持久化 / 恢复 ----------

    def test_checkpoint_and_restore(self):
        stack = self.stack()
        plan = stack.plan_from_spec_path(LAB)
        loaded = stack.load_plan(plan)
        run = stack.start_run(loaded["plan_id"])
        snapshot = stack.run(run["run_id"], max_steps=200)["run"]

        restored = stack.restore(run["run_id"])
        self.assertEqual(len(restored["completed_tasks"]), snapshot["total"])
        self.assertEqual(restored["resumable_tasks"], [])
        self.assertEqual(restored["in_flight_tasks"], [])
        self.assertEqual(restored["checkpoint"]["plan_version"], plan["plan_version"])
        self.assertEqual(
            restored["checkpoint"]["last_event_sequence"],
            stack.replay(run["run_id"])["last_sequence"],
        )

    def test_state_survives_new_stack_instance(self):
        """同一个数据目录重建 stack 后，运行与事件仍在（B03 持久化）。"""
        stack = self.stack(name="persist")
        plan = stack.plan_from_spec_path(LAB)
        loaded = stack.load_plan(plan)
        run = stack.start_run(loaded["plan_id"])
        stack.run(run["run_id"], max_steps=200)
        snapshot = stack.snapshot(run["run_id"])

        reopened = WorkflowStack(data_dir=self.root / "persist", execution_mode="mock")
        again = reopened.snapshot(run["run_id"])
        self.assertEqual(again["status"], snapshot["status"])
        self.assertEqual(again["completed"], snapshot["completed"])

    # ---------- DAG ----------

    def test_dag_matches_plan_edges(self):
        stack = self.stack()
        plan = stack.plan_from_spec_path(CAMPUS)
        loaded = stack.load_plan(plan)
        run = stack.start_run(loaded["plan_id"])
        dag = stack.dag(run["run_id"])
        self.assertEqual(len(dag["nodes"]), len(plan["tasks"]))
        self.assertEqual(
            len(dag["edges"]),
            sum(len(task["depends_on"]) for task in plan["tasks"]),
        )
        node_ids = {node["id"] for node in dag["nodes"]}
        for edge in dag["edges"]:
            self.assertIn(edge["from"], node_ids)
            self.assertIn(edge["to"], node_ids)
        for node in dag["nodes"]:
            for field in ("id", "label", "block_id", "state", "attempt_id", "retry_count",
                          "acceptance", "required_test_ids"):
                self.assertIn(field, node)


class ContractVerificationTests(unittest.TestCase):
    def test_verify_contracts_script_passes(self):
        """CI 直接跑同一个脚本；这里保证它在本地也可复现。"""
        from workflow_runtime import verify_contracts

        with tempfile.TemporaryDirectory(dir=REPO) as temp:
            root = Path(temp)
            for spec_path in (CAMPUS, LAB):
                verify_contracts.verify_success_path(spec_path, root)
                verify_contracts.verify_failure_path(spec_path, root)
            verify_contracts.verify_missing_evidence(root)
            verify_contracts.verify_stale_versions(root)


if __name__ == "__main__":
    unittest.main()
