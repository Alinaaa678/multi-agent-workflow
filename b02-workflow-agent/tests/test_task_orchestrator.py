"""B02-D15/D16: task orchestration state machine, dispatch, retry and blocking.

Contract: ``docs/TASK_ORCHESTRATION.md`` §1 (states/transitions), §2 (plan
document), §3.1 (run snapshot), §3.2 (run events) and §3.3 (DAG view).

The orchestrator is driven with a temporary :class:`TaskStateStore` and lambda
executors/reviewers only: no network, no shared database, no wall-clock
assertions.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = PROJECT_ROOT.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(REPO_ROOT / "b03"))

from b03_implementation.task_state import TaskStateStore  # noqa: E402
from backend.task_orchestrator import (  # noqa: E402
    ALLOWED_TRANSITIONS,
    BINDING_EVENTS,
    COMMIT_EVENTS,
    EVENT_TYPES,
    TASK_STATES,
    TaskOrchestrationError,
    TaskOrchestrator,
    validate_plan_document,
)

SPEC_SHA = "a" * 64
LOG_SHA = "c" * 64
ARTIFACT_SHA = "b" * 64


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def make_task(
    task_id: str = "T01",
    *,
    depends_on: tuple[str, ...] = (),
    retry_budget: int = 0,
    required_test_ids: tuple[str, ...] = ("TC-1",),
    **overrides,
) -> dict:
    """A §2 task with every mandatory field present."""
    task = {
        "role_id": "AgentIntegrationAgent",
        "block_id": "B04",
        "task_id": task_id,
        "project_id": "demo",
        "req_refs": ["R1"],
        "design_refs": ["DS-1"],
        "spec_version": "S1-v1.0",
        "contract_version": "C1-v1.0",
        "goal": f"goal of {task_id}",
        "inputs": ["S1-v1.0"],
        "outputs": [f"artifact-{task_id}"],
        "depends_on": list(depends_on),
        "capability": "BE-01",
        "write_scope": ["src/generated"],
        "acceptance": [f"{task_id} acceptance"],
        "required_test_ids": list(required_test_ids),
        "priority": "P0",
        "timeout": 60,
        "retry_budget": retry_budget,
        "escalation_target": "B02 WorkflowAgent",
    }
    task.update(overrides)
    return task


def make_plan(tasks: list[dict], *, plan_version: str = "PLAN-demo-v1.0", **overrides) -> dict:
    """A §2 plan document whose topological order is the given task order."""
    plan = {
        "project_id": "demo",
        "spec_version": "S1-v1.0",
        "contract_version": "C1-v1.0",
        "plan_version": plan_version,
        "generated_by": "B01",
        "generated_at": "2026-11-18T00:00:00Z",
        "required_requirements": ["R1"],
        "tasks": tasks,
        "topological_order": [task["task_id"] for task in tasks],
        "input_snapshot": {
            "spec_source": "inline",
            "spec_format": "json",
            "spec_sha256": SPEC_SHA,
            "contract_sha256": "d" * 64,
            "spec_version": "S1-v1.0",
            "contract_version": "C1-v1.0",
            "requirement_count": 1,
            "design_entry_count": 1,
        },
    }
    plan.update(overrides)
    return plan


def execution_result(request: dict, *, status: str = "SUCCEEDED", **overrides) -> dict:
    """A §5 TaskExecutionResult echoing the request identity."""
    result = {
        "task_id": request["task_id"],
        "attempt_id": request["attempt_id"],
        "status": status,
        "artifacts": [
            {
                "path": f"artifacts/{request['task_id']}.md",
                "kind": "document",
                "sha256": ARTIFACT_SHA,
            }
        ],
        "candidate_commit": "abc1234" if status == "SUCCEEDED" else None,
        "evidence_refs": ["evidence-1"],
        "known_issues": [],
        "logs": [],
        "test_results": [
            {"test_id": test_id, "status": "PASS", "log_sha256": LOG_SHA}
            for test_id in request.get("required_test_ids") or []
        ],
    }
    result.update(overrides)
    return result


def accepting_reviewer(_payload: dict) -> dict:
    return {"decision": "ACCEPT", "findings": [], "defects": [], "events": []}


class RecordingStore(TaskStateStore):
    """Captures the states written at registration time, before any refresh."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.states_at_registration: dict[str, str] = {}

    def create_run(self, **kwargs):
        run = super().create_run(**kwargs)
        self.states_at_registration = {
            item["task_id"]: item["state"] for item in self.list_task_states(kwargs["run_id"])
        }
        return run


class OrchestratorTestCase(unittest.TestCase):
    """Shared harness: fresh temporary TaskStateStore per test."""

    store_class = TaskStateStore

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.db_path = Path(self._temp.name) / "task_state.db"
        self.store = self.store_class(self.db_path)

    def build(self, *, executor=None, reviewer=None) -> TaskOrchestrator:
        return TaskOrchestrator(
            self.store,
            executor=executor if executor is not None else (lambda request: execution_result(request)),
            reviewer=reviewer if reviewer is not None else accepting_reviewer,
        )

    def loaded_run(
        self,
        tasks: list[dict],
        *,
        run_id: str = "TASK-RUN-TEST",
        plan_version: str = "PLAN-demo-v1.0",
        executor=None,
        reviewer=None,
        plan_overrides: dict | None = None,
    ) -> tuple[TaskOrchestrator, dict]:
        orchestrator = self.build(executor=executor, reviewer=reviewer)
        orchestrator.load_plan(make_plan(tasks, plan_version=plan_version, **(plan_overrides or {})))
        snapshot = orchestrator.create_run(plan_version, run_id=run_id)
        return orchestrator, snapshot

    def task(self, orchestrator: TaskOrchestrator, run_id: str, task_id: str) -> dict:
        return next(item for item in orchestrator.snapshot(run_id)["tasks"] if item["task_id"] == task_id)

    def all_events(self, run_id: str) -> list[dict]:
        return self.store.list_run_events(run_id, limit=1_000_000)

    def events_of_type(self, run_id: str, event_type: str) -> list[dict]:
        return [event for event in self.all_events(run_id) if event["type"] == event_type]


# --------------------------------------------------------------------------- #
# 1. Plan loading / validation
# --------------------------------------------------------------------------- #


class PlanValidationTests(OrchestratorTestCase):
    def test_plan_load_rejects_missing_top_level_fields(self):
        for field in ("project_id", "spec_version", "contract_version", "plan_version"):
            with self.subTest(field=field):
                document = make_plan([make_task()])
                document.pop(field)
                with self.assertRaises(TaskOrchestrationError):
                    self.build().load_plan(document)

    def test_plan_load_rejects_blank_top_level_fields(self):
        for field in ("project_id", "spec_version", "contract_version", "plan_version"):
            with self.subTest(field=field):
                document = make_plan([make_task()])
                document[field] = "   "
                with self.assertRaises(TaskOrchestrationError):
                    self.build().load_plan(document)

    def test_plan_load_rejects_missing_or_empty_tasks(self):
        for tasks in ([], None, {"T01": {}}):
            with self.subTest(tasks=tasks):
                document = make_plan([make_task()])
                document["tasks"] = tasks
                with self.assertRaises(TaskOrchestrationError):
                    self.build().load_plan(document)

    def test_plan_load_rejects_non_object_plan(self):
        with self.assertRaises(TaskOrchestrationError):
            validate_plan_document(["not", "a", "plan"])

    def test_plan_load_rejects_duplicate_task_id(self):
        document = make_plan([make_task("T01"), make_task("T01", depends_on=())])
        with self.assertRaises(TaskOrchestrationError) as raised:
            self.build().load_plan(document)
        self.assertIn("T01", str(raised.exception))

    def test_plan_load_rejects_unknown_dependency(self):
        document = make_plan([make_task("T01", depends_on=("T99",))])
        with self.assertRaises(TaskOrchestrationError) as raised:
            self.build().load_plan(document)
        self.assertIn("T99", str(raised.exception))

    def test_plan_load_rejects_dependency_cycle(self):
        document = make_plan(
            [
                make_task("T01", depends_on=("T02",)),
                make_task("T02", depends_on=("T01",)),
            ]
        )
        with self.assertRaises(TaskOrchestrationError):
            self.build().load_plan(document)

    def test_plan_load_rejects_self_dependency(self):
        document = make_plan([make_task("T01", depends_on=("T01",))])
        with self.assertRaises(TaskOrchestrationError):
            self.build().load_plan(document)

    def test_plan_load_rejects_depends_on_that_is_not_a_list(self):
        document = make_plan([make_task("T01", depends_on="T02")])
        with self.assertRaises(TaskOrchestrationError):
            self.build().load_plan(document)

    def test_plan_load_exposes_contract_summary(self):
        orchestrator = self.build()
        loaded = orchestrator.load_plan(make_plan([make_task("T01"), make_task("T02", depends_on=("T01",))]))
        self.assertEqual("PLAN-demo-v1.0", loaded["plan_id"])
        self.assertEqual("PLAN-demo-v1.0", loaded["plan_version"])
        self.assertEqual(2, loaded["task_count"])
        self.assertEqual(["T01", "T02"], loaded["topological_order"])
        self.assertEqual(SPEC_SHA, loaded["input_snapshot"])

    def test_plan_load_is_idempotent_for_identical_document(self):
        orchestrator = self.build()
        document = make_plan([make_task("T01")])
        orchestrator.load_plan(document)
        orchestrator.load_plan(make_plan([make_task("T01")]))
        self.assertEqual(1, len(self.store.list_plans()))

    def test_plan_load_rejects_conflicting_document_for_same_plan_version(self):
        orchestrator = self.build()
        orchestrator.load_plan(make_plan([make_task("T01")]))
        with self.assertRaises(Exception):
            orchestrator.load_plan(make_plan([make_task("T01")], project_id="other-project"))

    def test_plan_without_input_snapshot_is_accepted_with_empty_snapshot(self):
        """Characterisation: B02 does minimal validation (module docstring).

        §2 marks ``input_snapshot`` as mandatory, yet a plan without it loads and
        the run then carries ``""`` into every event.  The current behaviour is
        recorded here rather than asserted as correct.
        """
        document = make_plan([make_task("T01")])
        document.pop("input_snapshot")
        orchestrator = self.build()
        orchestrator.load_plan(document)
        snapshot = orchestrator.create_run("PLAN-demo-v1.0", run_id="TASK-RUN-EMPTY")
        self.assertEqual("", snapshot["input_snapshot"])
        self.assertEqual({""}, {event["input_snapshot"] for event in self.all_events("TASK-RUN-EMPTY")})


# --------------------------------------------------------------------------- #
# 2. Run creation and dependency refresh
# --------------------------------------------------------------------------- #


class RunCreationTests(OrchestratorTestCase):
    store_class = RecordingStore

    def test_create_run_registers_all_tasks_pending_then_refreshes_dependencies(self):
        tasks = [
            make_task("T01"),
            make_task("T02"),
            make_task("T03", depends_on=("T01",)),
            make_task("T04", depends_on=("T01", "T03")),
        ]
        orchestrator = self.build()
        orchestrator.load_plan(make_plan(tasks))
        snapshot = orchestrator.create_run("PLAN-demo-v1.0", run_id="TASK-RUN-DAG")

        # Registration itself is PENDING for every task.
        self.assertEqual(
            {"T01": "PENDING", "T02": "PENDING", "T03": "PENDING", "T04": "PENDING"},
            self.store.states_at_registration,
        )

        states = {task["task_id"]: task["state"] for task in snapshot["tasks"]}
        self.assertEqual({"T01": "READY", "T02": "READY", "T03": "WAITING_DEPENDENCY", "T04": "WAITING_DEPENDENCY"}, states)
        self.assertEqual(2, snapshot["counts"]["READY"])
        self.assertEqual(2, snapshot["counts"]["WAITING_DEPENDENCY"])
        self.assertEqual(0, snapshot["counts"]["PENDING"])
        self.assertEqual(4, snapshot["total"])
        self.assertEqual(0, snapshot["completed"])

    def test_create_run_emits_task_ready_only_for_roots(self):
        orchestrator, _ = self.loaded_run([make_task("T01"), make_task("T02", depends_on=("T01",))])
        ready_events = self.events_of_type("TASK-RUN-TEST", "TASK_READY")
        self.assertEqual(["T01"], [event["task_id"] for event in ready_events])

    def test_create_run_rejects_unknown_plan(self):
        with self.assertRaises(TaskOrchestrationError):
            self.build().create_run("PLAN-missing-v9.9", run_id="TASK-RUN-X")

    def test_create_run_requires_task_run_prefix(self):
        orchestrator = self.build()
        orchestrator.load_plan(make_plan([make_task("T01")]))
        with self.assertRaises(TaskOrchestrationError):
            orchestrator.create_run("PLAN-demo-v1.0", run_id="RUN-1")
        with self.assertRaises(TaskOrchestrationError):
            orchestrator.create_run("PLAN-demo-v1.0", run_id="task-run-1")

    def test_create_run_rejects_duplicate_run_id(self):
        orchestrator, _ = self.loaded_run([make_task("T01")])
        with self.assertRaises(Exception):
            orchestrator.create_run("PLAN-demo-v1.0", run_id="TASK-RUN-TEST")

    def test_unknown_run_is_rejected(self):
        orchestrator = self.build()
        with self.assertRaises(TaskOrchestrationError):
            orchestrator.snapshot("TASK-RUN-NOPE")


# --------------------------------------------------------------------------- #
# 3. Dispatch rules
# --------------------------------------------------------------------------- #


class DispatchTests(OrchestratorTestCase):
    def test_dispatch_rejects_task_that_is_not_ready(self):
        orchestrator, _ = self.loaded_run([make_task("T01"), make_task("T02", depends_on=("T01",))])
        with self.assertRaises(TaskOrchestrationError) as raised:
            orchestrator.dispatch("TASK-RUN-TEST", "T02")
        self.assertIn("WAITING_DEPENDENCY", str(raised.exception))
        self.assertEqual("WAITING_DEPENDENCY", self.task(orchestrator, "TASK-RUN-TEST", "T02")["state"])

    def test_dispatch_rejects_unknown_task(self):
        orchestrator, _ = self.loaded_run([make_task("T01")])
        with self.assertRaises(TaskOrchestrationError):
            orchestrator.dispatch("TASK-RUN-TEST", "T99")

    def test_dispatch_rejects_duplicate_while_running(self):
        captured: dict[str, str] = {}
        orchestrator: TaskOrchestrator

        def executor(request):
            try:
                orchestrator.dispatch("TASK-RUN-TEST", request["task_id"])
            except TaskOrchestrationError as error:
                captured["error"] = str(error)
            return execution_result(request)

        orchestrator = self.build(executor=executor)
        orchestrator.load_plan(make_plan([make_task("T01")]))
        orchestrator.create_run("PLAN-demo-v1.0", run_id="TASK-RUN-TEST")
        orchestrator.step("TASK-RUN-TEST")

        self.assertIn("RUNNING", captured.get("error", ""))
        self.assertEqual("SUCCEEDED", self.task(orchestrator, "TASK-RUN-TEST", "T01")["state"])
        # Exactly one dispatch happened even though the executor tried again.
        self.assertEqual(1, len(self.events_of_type("TASK-RUN-TEST", "TASK_DISPATCHED")))

    def test_dispatch_rejects_duplicate_while_reviewing(self):
        captured: dict[str, str] = {}

        def reviewer(payload):
            try:
                orchestrator.dispatch("TASK-RUN-TEST", payload["task"]["task_id"])
            except TaskOrchestrationError as error:
                captured["error"] = str(error)
            return {"decision": "ACCEPT", "findings": [], "defects": [], "events": []}

        orchestrator = self.build(reviewer=reviewer)
        orchestrator.load_plan(make_plan([make_task("T01")]))
        orchestrator.create_run("PLAN-demo-v1.0", run_id="TASK-RUN-TEST")
        orchestrator.step("TASK-RUN-TEST")

        self.assertIn("REVIEWING", captured.get("error", ""))
        self.assertEqual(1, len(self.events_of_type("TASK-RUN-TEST", "TASK_DISPATCHED")))

    def test_dispatch_on_a_running_task_is_rejected_through_the_state_machine(self):
        orchestrator, _ = self.loaded_run([make_task("T01"), make_task("T02")])
        self.store.update_task_state("TASK-RUN-TEST", "T02", state="RUNNING", attempt_id=1)
        with self.assertRaises(TaskOrchestrationError) as raised:
            orchestrator.dispatch("TASK-RUN-TEST", "T02")
        self.assertIn("RUNNING", str(raised.exception))

    def test_dispatch_forwards_the_execution_request_and_returns_the_result(self):
        seen: list[dict] = []

        def executor(request):
            seen.append(request)
            return execution_result(request)

        orchestrator = self.build(executor=executor)
        orchestrator.load_plan(make_plan([make_task("T01")]))
        orchestrator.create_run("PLAN-demo-v1.0", run_id="TASK-RUN-TEST")
        result = orchestrator.dispatch("TASK-RUN-TEST", "T01")

        self.assertEqual("SUCCEEDED", result["status"])
        request = seen[0]
        self.assertEqual("T01", request["task_id"])
        self.assertEqual(1, request["attempt_id"])
        self.assertEqual("PLAN-demo-v1.0", request["plan_version"])
        self.assertEqual(SPEC_SHA, request["input_snapshot"])
        self.assertEqual("TASK-RUN-TEST", request["run_id"])
        self.assertEqual(["artifact-T01"], request["outputs"])
        self.assertEqual(["TC-1"], request["required_test_ids"])
        self.assertEqual(60, request["timeout"])
        self.assertEqual(0, request["retry_budget"])

    def test_step_dispatches_at_most_one_task_per_call(self):
        orchestrator, _ = self.loaded_run(
            [make_task("T01"), make_task("T02"), make_task("T03", depends_on=("T01", "T02"))]
        )
        step = orchestrator.step("TASK-RUN-TEST")
        self.assertEqual(1, len(step["dispatched"]))
        self.assertEqual({"run", "dispatched", "transitions"}, set(step))
        transitions = step["transitions"]
        self.assertEqual("TASK_DISPATCHED", transitions[0]["type"])
        self.assertEqual(step["dispatched"][0], transitions[0]["task_id"])
        # T02 is still READY: a single step must not run it.
        states = {item["task_id"]: item["state"] for item in step["run"]["tasks"]}
        self.assertEqual("READY", states["T02"])

    def test_step_on_fully_blocked_run_produces_no_transitions(self):
        orchestrator, _ = self.loaded_run([make_task("T01")])
        self.store.update_task_state("TASK-RUN-TEST", "T01", state="BLOCKED", blocked_reason="manual")
        before = len(self.all_events("TASK-RUN-TEST"))
        step = orchestrator.step("TASK-RUN-TEST")
        self.assertEqual([], step["dispatched"])
        self.assertEqual(before, len(self.all_events("TASK-RUN-TEST")))
        self.assertEqual("BLOCKED", step["run"]["status"])


# --------------------------------------------------------------------------- #
# 4. State machine
# --------------------------------------------------------------------------- #


class TransitionTableTests(OrchestratorTestCase):
    EXPECTED = {
        "PENDING": {"READY", "WAITING_DEPENDENCY", "BLOCKED"},
        "WAITING_DEPENDENCY": {"READY", "BLOCKED"},
        "READY": {"RUNNING", "BLOCKED"},
        "RUNNING": {"REVIEWING", "FAILED", "RETRYING", "BLOCKED"},
        "REVIEWING": {"SUCCEEDED", "FAILED", "RETRYING", "BLOCKED"},
        "RETRYING": {"READY", "FAILED", "BLOCKED"},
        "FAILED": {"BLOCKED", "READY"},
        "BLOCKED": {"READY"},
        "SUCCEEDED": set(),
    }

    def test_allowed_transitions_table_matches_the_contract(self):
        self.assertEqual(set(TASK_STATES), set(ALLOWED_TRANSITIONS))
        for state, expected in self.EXPECTED.items():
            with self.subTest(state=state):
                self.assertEqual(expected, set(ALLOWED_TRANSITIONS[state]))

    def test_every_transition_target_is_a_known_state(self):
        for state, targets in ALLOWED_TRANSITIONS.items():
            for target in targets:
                with self.subTest(source=state, target=target):
                    self.assertIn(target, TASK_STATES)

    def test_succeeded_is_terminal(self):
        self.assertEqual(frozenset(), ALLOWED_TRANSITIONS["SUCCEEDED"])

    def test_illegal_transition_raises_and_leaves_state_untouched(self):
        orchestrator, _ = self.loaded_run([make_task("T01")])
        orchestrator.step("TASK-RUN-TEST")
        self.assertEqual("SUCCEEDED", self.task(orchestrator, "TASK-RUN-TEST", "T01")["state"])

        run = self.store.get_run("TASK-RUN-TEST")
        for target in ("READY", "PENDING", "WAITING_DEPENDENCY", "RUNNING", "REVIEWING"):
            with self.subTest(target=target):
                with self.assertRaises(TaskOrchestrationError) as raised:
                    orchestrator._transition(run, "T01", target)
                self.assertIn("非法状态迁移", str(raised.exception))
                self.assertEqual("SUCCEEDED", self.task(orchestrator, "TASK-RUN-TEST", "T01")["state"])

    def test_pending_cannot_jump_straight_to_running(self):
        orchestrator = self.build()
        orchestrator.load_plan(make_plan([make_task("T01")]))
        self.store.create_run(
            run_id="TASK-RUN-RAW",
            plan_id="PLAN-demo-v1.0",
            plan_version="PLAN-demo-v1.0",
            project_id="demo",
            spec_version="S1-v1.0",
            contract_version="C1-v1.0",
            input_snapshot=SPEC_SHA,
            tasks=[make_task("T01")],
        )
        run = self.store.get_run("TASK-RUN-RAW")
        with self.assertRaises(TaskOrchestrationError):
            orchestrator._transition(run, "T01", "RUNNING")
        self.assertEqual("PENDING", self.store.get_task_state("TASK-RUN-RAW", "T01")["state"])

    def test_legal_transition_is_accepted(self):
        orchestrator = self.build()
        orchestrator.load_plan(make_plan([make_task("T01")]))
        self.store.create_run(
            run_id="TASK-RUN-RAW",
            plan_id="PLAN-demo-v1.0",
            plan_version="PLAN-demo-v1.0",
            project_id="demo",
            spec_version="S1-v1.0",
            contract_version="C1-v1.0",
            input_snapshot=SPEC_SHA,
            tasks=[make_task("T01")],
        )
        run = self.store.get_run("TASK-RUN-RAW")
        updated = orchestrator._transition(run, "T01", "WAITING_DEPENDENCY")
        self.assertEqual("WAITING_DEPENDENCY", updated["state"])
        updated = orchestrator._transition(run, "T01", "READY")
        self.assertEqual("READY", updated["state"])

    def test_unknown_event_type_is_rejected(self):
        orchestrator, _ = self.loaded_run([make_task("T01")])
        run = self.store.get_run("TASK-RUN-TEST")
        with self.assertRaises(TaskOrchestrationError):
            orchestrator._emit(run, "TASK_DONE", "T01")


# --------------------------------------------------------------------------- #
# 5. Attempts, retry budget and failure modes
# --------------------------------------------------------------------------- #


class AttemptAndRetryTests(OrchestratorTestCase):
    def test_attempt_id_increments_monotonically_and_retry_count_only_on_failure(self):
        calls: list[tuple[str, int]] = []

        def executor(request):
            calls.append((request["task_id"], request["attempt_id"]))
            return execution_result(request, status="FAILED", error="boom")

        orchestrator, _ = self.loaded_run([make_task("T01", retry_budget=2)], executor=executor)
        orchestrator.run("TASK-RUN-TEST", max_steps=20)

        task = self.task(orchestrator, "TASK-RUN-TEST", "T01")
        self.assertEqual("FAILED", task["state"])
        self.assertEqual([1, 2, 3], [attempt for _, attempt in calls])
        self.assertEqual(2, task["retry_count"])
        self.assertEqual([1, 2, 3], [item["attempt_id"] for item in task["attempts"]])
        self.assertEqual([1, 2, 2], [item["retry_count"] for item in task["attempts"]])
        self.assertEqual(["RETRYING", "RETRYING", "FAILED"], [item["state"] for item in task["attempts"]])
        dispatched = [event["attempt_id"] for event in self.events_of_type("TASK-RUN-TEST", "TASK_DISPATCHED")]
        started = [event["attempt_id"] for event in self.events_of_type("TASK-RUN-TEST", "TASK_STARTED")]
        self.assertEqual([1, 2, 3], dispatched)
        self.assertEqual(dispatched, started)

    def test_retry_count_is_not_incremented_on_success(self):
        orchestrator, _ = self.loaded_run([make_task("T01", retry_budget=3)])
        orchestrator.run("TASK-RUN-TEST", max_steps=10)
        task = self.task(orchestrator, "TASK-RUN-TEST", "T01")
        self.assertEqual("SUCCEEDED", task["state"])
        self.assertEqual(0, task["retry_count"])
        self.assertEqual(1, task["attempt_id"])

    def test_bounded_retry_with_budget_one_dispatches_twice_then_fails(self):
        calls: list[int] = []

        def executor(request):
            calls.append(request["attempt_id"])
            return execution_result(request, status="FAILED", error="compile error")

        orchestrator, _ = self.loaded_run([make_task("T01", retry_budget=1)], executor=executor)
        orchestrator.run("TASK-RUN-TEST", max_steps=10)

        task = self.task(orchestrator, "TASK-RUN-TEST", "T01")
        self.assertEqual("FAILED", task["state"])
        self.assertEqual([1, 2], calls)
        self.assertEqual(1, task["retry_count"])
        self.assertEqual("compile error", task["error"])

        types = [event["type"] for event in self.all_events("TASK-RUN-TEST")]
        self.assertEqual(
            [
                "TASK_READY",
                "TASK_DISPATCHED",
                "TASK_STARTED",
                "TASK_REJECTED",
                "TASK_RETRY_SCHEDULED",
                # 重试排队后立即回到 READY，该迁移同样必须有事件，
                # 否则中途 replay 会与快照不一致。
                "TASK_READY",
                "TASK_DISPATCHED",
                "TASK_STARTED",
                "TASK_REJECTED",
                "TASK_FAILED",
            ],
            types,
        )
        requeued = [e for e in self.all_events("TASK-RUN-TEST") if e["type"] == "TASK_READY"][1]
        self.assertTrue(requeued["payload"].get("requeued_after_retry"))
        retry_event = self.events_of_type("TASK-RUN-TEST", "TASK_RETRY_SCHEDULED")[0]
        self.assertEqual(1, retry_event["payload"]["retry_count"])
        self.assertEqual(1, retry_event["payload"]["retry_budget"])

    def test_retry_budget_zero_fails_on_the_first_attempt(self):
        orchestrator, _ = self.loaded_run([make_task("T01", retry_budget=0)],
                                          executor=lambda request: execution_result(request, status="FAILED"))
        orchestrator.run("TASK-RUN-TEST", max_steps=5)
        task = self.task(orchestrator, "TASK-RUN-TEST", "T01")
        self.assertEqual("FAILED", task["state"])
        self.assertEqual(0, task["retry_count"])
        self.assertEqual(1, task["attempt_id"])
        self.assertEqual([], self.events_of_type("TASK-RUN-TEST", "TASK_RETRY_SCHEDULED"))

    def test_review_rejection_uses_the_retry_budget(self):
        def rejecting_reviewer(_payload):
            return {
                "decision": "REJECT",
                "findings": [{"code": "MISSING_TEST", "severity": "blocking", "message": "missing"}],
                "defects": [
                    {"defect_id": "D-T01-A1-001", "task_id": "T01", "severity": "blocking", "status": "open", "message": "m"}
                ],
                "events": [
                    {
                        "type": "DEFECT_CREATED",
                        "task_id": "T01",
                        "attempt_id": 1,
                        "candidate_commit": "abc1234",
                        "payload": {"defect_id": "D-T01-A1-001", "code": "MISSING_TEST", "severity": "blocking"},
                    }
                ],
            }

        orchestrator, _ = self.loaded_run([make_task("T01", retry_budget=1)], reviewer=rejecting_reviewer)
        orchestrator.run("TASK-RUN-TEST", max_steps=10)

        task = self.task(orchestrator, "TASK-RUN-TEST", "T01")
        self.assertEqual("FAILED", task["state"])
        self.assertEqual(1, task["retry_count"])
        self.assertEqual("review_rejected", task["error"])
        self.assertEqual("REJECT", task["review"]["decision"])
        # B06 only supplies defect events; B02 records them with producer B06.
        defect_events = self.events_of_type("TASK-RUN-TEST", "DEFECT_CREATED")
        self.assertEqual(2, len(defect_events))
        self.assertEqual({"B06"}, {event["producer"] for event in defect_events})

    def test_executor_exception_is_recorded_as_a_failed_attempt(self):
        def boom(_request):
            raise RuntimeError("kaboom")

        orchestrator, _ = self.loaded_run([make_task("T01", retry_budget=0)], executor=boom)
        orchestrator.run("TASK-RUN-TEST", max_steps=5)
        task = self.task(orchestrator, "TASK-RUN-TEST", "T01")
        self.assertEqual("FAILED", task["state"])
        self.assertIn("kaboom", task["error"])


# --------------------------------------------------------------------------- #
# 6. Executor result identity binding
# --------------------------------------------------------------------------- #


class ResultIdentityTests(OrchestratorTestCase):
    def test_mismatched_task_id_result_is_rejected(self):
        def executor(request):
            return execution_result(request, task_id="T99")

        orchestrator, _ = self.loaded_run([make_task("T01")], executor=executor)
        orchestrator.step("TASK-RUN-TEST")
        task = self.task(orchestrator, "TASK-RUN-TEST", "T01")

        self.assertNotEqual("SUCCEEDED", task["state"])
        self.assertEqual("FAILED", task["state"])
        self.assertIn("T99", task["error"])
        self.assertIsNone(task["candidate_commit"])
        self.assertEqual([], self.events_of_type("TASK-RUN-TEST", "TASK_ACCEPTED"))
        self.assertEqual([], self.events_of_type("TASK-RUN-TEST", "TASK_OUTPUT_SUBMITTED"))

    def test_mismatched_attempt_id_result_is_rejected(self):
        def executor(request):
            return execution_result(request, attempt_id=request["attempt_id"] + 1)

        orchestrator, _ = self.loaded_run([make_task("T01")], executor=executor)
        orchestrator.step("TASK-RUN-TEST")
        task = self.task(orchestrator, "TASK-RUN-TEST", "T01")

        self.assertEqual("FAILED", task["state"])
        self.assertIn("attempt_id", task["error"])
        self.assertEqual([], self.events_of_type("TASK-RUN-TEST", "TASK_ACCEPTED"))

    def test_rejected_identity_is_retried_within_budget(self):
        def executor(request):
            if request["attempt_id"] == 1:
                return execution_result(request, task_id="T99")
            return execution_result(request)

        orchestrator, _ = self.loaded_run([make_task("T01", retry_budget=1)], executor=executor)
        orchestrator.run("TASK-RUN-TEST", max_steps=10)
        task = self.task(orchestrator, "TASK-RUN-TEST", "T01")
        self.assertEqual("SUCCEEDED", task["state"])
        self.assertEqual(1, task["retry_count"])
        self.assertEqual(2, task["attempt_id"])


# --------------------------------------------------------------------------- #
# 7. Upstream failure / blocking propagation
# --------------------------------------------------------------------------- #


class BlockingTests(OrchestratorTestCase):
    def test_failed_upstream_blocks_downstream_with_reason(self):
        orchestrator, _ = self.loaded_run(
            [make_task("T01"), make_task("T02", depends_on=("T01",)), make_task("T03", depends_on=("T02",))],
            executor=lambda request: execution_result(request, status="FAILED", error="broken"),
        )
        orchestrator.run("TASK-RUN-TEST", max_steps=20)

        first = self.task(orchestrator, "TASK-RUN-TEST", "T01")
        second = self.task(orchestrator, "TASK-RUN-TEST", "T02")
        third = self.task(orchestrator, "TASK-RUN-TEST", "T03")
        self.assertEqual("FAILED", first["state"])
        self.assertEqual("BLOCKED", second["state"])
        self.assertEqual("upstream_failed:T01", second["blocked_reason"])
        self.assertEqual("BLOCKED", third["state"])
        self.assertEqual("upstream_failed:T02", third["blocked_reason"])

        blocked_events = self.events_of_type("TASK-RUN-TEST", "TASK_BLOCKED")
        self.assertEqual(["T02", "T03"], [event["task_id"] for event in blocked_events])
        self.assertEqual(["T01"], blocked_events[0]["payload"]["blocked_by"])
        self.assertEqual("upstream_failed:T01", blocked_events[0]["payload"]["reason"])
        self.assertEqual("BLOCKED", orchestrator.snapshot("TASK-RUN-TEST")["status"])

    def test_blocked_execution_result_blocks_the_task_and_downstream(self):
        def executor(request):
            if request["task_id"] == "T01":
                return execution_result(
                    request,
                    status="BLOCKED",
                    error="dependency unavailable",
                    known_issues=[{"code": "DEPENDENCY_UNAVAILABLE", "message": "down", "severity": "blocking"}],
                )
            return execution_result(request)

        orchestrator, _ = self.loaded_run(
            [make_task("T01"), make_task("T02", depends_on=("T01",))], executor=executor
        )
        orchestrator.run("TASK-RUN-TEST", max_steps=10)

        first = self.task(orchestrator, "TASK-RUN-TEST", "T01")
        second = self.task(orchestrator, "TASK-RUN-TEST", "T02")
        self.assertEqual("BLOCKED", first["state"])
        self.assertEqual("dependency unavailable", first["blocked_reason"])
        self.assertEqual("BLOCKED", second["state"])
        self.assertEqual("upstream_failed:T01", second["blocked_reason"])
        self.assertEqual("BLOCKED", orchestrator.snapshot("TASK-RUN-TEST")["status"])

    def test_blocked_downstream_does_not_consume_an_attempt(self):
        orchestrator, _ = self.loaded_run(
            [make_task("T01"), make_task("T02", depends_on=("T01",))],
            executor=lambda request: execution_result(request, status="FAILED"),
        )
        orchestrator.run("TASK-RUN-TEST", max_steps=10)
        second = self.task(orchestrator, "TASK-RUN-TEST", "T02")
        self.assertEqual(0, second["attempt_id"])
        self.assertEqual([], second["attempts"])
        self.assertEqual([], self.events_of_type("TASK-RUN-TEST", "TASK_DISPATCHED")[1:])

    def test_run_status_is_failed_when_only_failures_remain(self):
        orchestrator, _ = self.loaded_run(
            [make_task("T01")],
            executor=lambda request: execution_result(request, status="FAILED"),
        )
        orchestrator.run("TASK-RUN-TEST", max_steps=5)
        snapshot = orchestrator.snapshot("TASK-RUN-TEST")
        self.assertEqual("FAILED", snapshot["status"])
        self.assertEqual(1, snapshot["counts"]["FAILED"])


# --------------------------------------------------------------------------- #
# 8. Events (binding, commit, sequencing, idempotency)
# --------------------------------------------------------------------------- #


class EventContractTests(OrchestratorTestCase):
    def _mixed_run(self) -> TaskOrchestrator:
        def executor(request):
            if request["task_id"] == "T03":
                return execution_result(request, status="BLOCKED", error="blocked on purpose")
            return execution_result(request)

        orchestrator, _ = self.loaded_run(
            [
                make_task("T01"),
                make_task("T02", depends_on=("T01",)),
                make_task("T03", depends_on=("T02",)),
                make_task("T04", depends_on=("T03",)),
            ],
            executor=executor,
        )
        orchestrator.run("TASK-RUN-TEST", max_steps=30)
        return orchestrator

    def test_binding_events_carry_task_attempt_plan_and_snapshot(self):
        self._mixed_run()
        events = self.all_events("TASK-RUN-TEST")
        binding = [event for event in events if event["type"] in BINDING_EVENTS]
        self.assertTrue(binding)
        for event in binding:
            with self.subTest(event=event["type"], sequence=event["sequence"]):
                self.assertTrue(event["task_id"])
                self.assertIsInstance(event["attempt_id"], int)
                self.assertEqual("PLAN-demo-v1.0", event["plan_version"])
                self.assertEqual(SPEC_SHA, event["input_snapshot"])
                self.assertIn(event["producer"], {"B02", "B06"})

    def test_binding_events_of_a_dispatched_attempt_use_a_positive_attempt_id(self):
        self._mixed_run()
        dispatched_events = {
            "TASK_DISPATCHED",
            "TASK_STARTED",
            "TASK_OUTPUT_SUBMITTED",
            "TASK_REVIEW_REQUESTED",
            "TASK_ACCEPTED",
            "TASK_COMPLETED",
            "TASK_RETRY_SCHEDULED",
            "TASK_FAILED",
        }
        for event in self.all_events("TASK-RUN-TEST"):
            if event["type"] not in dispatched_events:
                continue
            with self.subTest(event=event["type"], sequence=event["sequence"]):
                self.assertGreaterEqual(event["attempt_id"], 1)

    def test_upstream_blocked_event_reports_attempt_zero(self):
        self._mixed_run()
        blocked = [event for event in self.events_of_type("TASK-RUN-TEST", "TASK_BLOCKED")
                   if event["task_id"] == "T04"]
        self.assertEqual(1, len(blocked))
        self.assertEqual(0, blocked[0]["attempt_id"])
        self.assertTrue(blocked[0]["payload"]["blocked_by"])

    def test_commit_events_carry_candidate_commit(self):
        self._mixed_run()
        events = self.all_events("TASK-RUN-TEST")
        commit_events = [event for event in events if event["type"] in COMMIT_EVENTS]
        self.assertTrue(commit_events)
        for event in commit_events:
            with self.subTest(event=event["type"], sequence=event["sequence"]):
                self.assertEqual("abc1234", event["candidate_commit"])

    def test_every_emitted_event_type_is_part_of_the_contract_enum(self):
        self._mixed_run()
        emitted = {event["type"] for event in self.all_events("TASK-RUN-TEST")}
        self.assertTrue(emitted <= set(EVENT_TYPES))
        self.assertIn("TASK_ACCEPTED", emitted)
        self.assertIn("TASK_BLOCKED", emitted)

    def test_event_sequence_is_unique_and_monotonic(self):
        self._mixed_run()
        sequences = [event["sequence"] for event in self.all_events("TASK-RUN-TEST")]
        self.assertEqual(sorted(sequences), sequences)
        self.assertEqual(len(sequences), len(set(sequences)))
        self.assertEqual(1, sequences[0])

    def test_event_append_is_idempotent_per_event_id(self):
        self._mixed_run()
        events = self.all_events("TASK-RUN-TEST")
        before = len(events)
        replay_event = dict(events[0])
        replay_event["event_id"] = events[0]["event_id"]
        again = self.store.append_run_event(replay_event)
        self.assertEqual(events[0]["event_id"], again["event_id"])
        self.assertEqual(events[0]["sequence"], again["sequence"])
        self.assertEqual(before, len(self.all_events("TASK-RUN-TEST")))

    def test_events_endpoint_supports_cursor_pagination(self):
        self._mixed_run()
        everything = self.all_events("TASK-RUN-TEST")
        first_page = self.build().events("TASK-RUN-TEST", after=None, limit=3)
        self.assertEqual(3, len(first_page["events"]))
        self.assertEqual(everything[:3], first_page["events"])

        second_page = self.build().events("TASK-RUN-TEST", after=first_page["events"][-1]["sequence"], limit=3)
        self.assertEqual(everything[3:6], second_page["events"])

    def test_task_ready_event_precedes_dispatch(self):
        orchestrator, _ = self.loaded_run([make_task("T01")])
        orchestrator.step("TASK-RUN-TEST")
        types = [event["type"] for event in self.all_events("TASK-RUN-TEST")]
        self.assertLess(types.index("TASK_READY"), types.index("TASK_DISPATCHED"))
        self.assertLess(types.index("TASK_DISPATCHED"), types.index("TASK_OUTPUT_SUBMITTED"))
        self.assertLess(types.index("TASK_OUTPUT_SUBMITTED"), types.index("TASK_ACCEPTED"))

    def test_dispatch_payload_names_role_and_block(self):
        orchestrator, _ = self.loaded_run([make_task("T01")])
        orchestrator.step("TASK-RUN-TEST")
        dispatched = self.events_of_type("TASK-RUN-TEST", "TASK_DISPATCHED")[0]
        self.assertEqual("AgentIntegrationAgent", dispatched["payload"]["role_id"])
        self.assertEqual("B04", dispatched["payload"]["block_id"])


# --------------------------------------------------------------------------- #
# 9. Replay consistency
# --------------------------------------------------------------------------- #


class ReplayTests(OrchestratorTestCase):
    def test_replay_is_consistent_after_a_successful_run(self):
        orchestrator, _ = self.loaded_run(
            [
                make_task("T01"),
                make_task("T02", depends_on=("T01",)),
                make_task("T03", depends_on=("T01",)),
                make_task("T04", depends_on=("T02", "T03")),
            ]
        )
        orchestrator.run("TASK-RUN-TEST", max_steps=30)
        replay = orchestrator.replay("TASK-RUN-TEST")
        self.assertTrue(replay["consistent"], replay["mismatches"])
        self.assertEqual([], replay["mismatches"])
        self.assertEqual(4, len(replay["replayed_states"]))
        self.assertEqual({"SUCCEEDED"}, set(replay["replayed_states"].values()))
        self.assertEqual(len(self.all_events("TASK-RUN-TEST")), replay["event_count"])
        self.assertEqual(self.all_events("TASK-RUN-TEST")[-1]["sequence"], replay["last_sequence"])

    def test_replay_is_consistent_after_a_failed_and_blocked_run(self):
        orchestrator, _ = self.loaded_run(
            [make_task("T01"), make_task("T02", depends_on=("T01",)), make_task("T03", depends_on=("T02",))],
            executor=lambda request: execution_result(request, status="FAILED", error="nope"),
        )
        orchestrator.run("TASK-RUN-TEST", max_steps=20)
        replay = orchestrator.replay("TASK-RUN-TEST")
        self.assertTrue(replay["consistent"], replay["mismatches"])
        self.assertEqual(
            {"T01": "FAILED", "T02": "BLOCKED", "T03": "BLOCKED"},
            replay["replayed_states"],
        )

    def test_replay_is_consistent_after_a_bounded_retry_run(self):
        orchestrator, _ = self.loaded_run(
            [make_task("T01", retry_budget=2)],
            executor=lambda request: execution_result(request, status="FAILED", error="flaky"),
        )
        orchestrator.run("TASK-RUN-TEST", max_steps=20)
        replay = orchestrator.replay("TASK-RUN-TEST")
        self.assertTrue(replay["consistent"], replay["mismatches"])
        self.assertEqual({"T01": "FAILED"}, replay["replayed_states"])

    def test_replay_reports_mismatches_for_a_tampered_snapshot_state(self):
        orchestrator, _ = self.loaded_run([make_task("T01")])
        orchestrator.step("TASK-RUN-TEST")
        self.store.update_task_state("TASK-RUN-TEST", "T01", state="REVIEWING")
        replay = orchestrator.replay("TASK-RUN-TEST")
        self.assertFalse(replay["consistent"])
        self.assertEqual("T01", replay["mismatches"][0]["task_id"])
        self.assertEqual("REVIEWING", replay["mismatches"][0]["snapshot"])
        self.assertEqual("SUCCEEDED", replay["mismatches"][0]["replayed"])


# --------------------------------------------------------------------------- #
# 10. Snapshot / DAG contract shapes
# --------------------------------------------------------------------------- #


class SnapshotAndDagTests(OrchestratorTestCase):
    def _run_diamond(self) -> TaskOrchestrator:
        orchestrator, _ = self.loaded_run(
            [
                make_task("T01"),
                make_task("T02", depends_on=("T01",)),
                make_task("T03", depends_on=("T01",)),
                make_task("T04", depends_on=("T02", "T03")),
            ]
        )
        orchestrator.run("TASK-RUN-TEST", max_steps=30)
        return orchestrator

    def test_snapshot_matches_the_contract_shape(self):
        orchestrator = self._run_diamond()
        snapshot = orchestrator.snapshot("TASK-RUN-TEST")

        for field in (
            "run_id",
            "plan_id",
            "plan_version",
            "input_snapshot",
            "project_id",
            "spec_version",
            "contract_version",
            "status",
            "created_at",
            "updated_at",
            "counts",
            "completed",
            "total",
            "events_count",
            "tasks",
            "topological_order",
        ):
            with self.subTest(field=field):
                self.assertIn(field, snapshot)

        self.assertEqual("TASK-RUN-TEST", snapshot["run_id"])
        self.assertEqual("PLAN-demo-v1.0", snapshot["plan_id"])
        self.assertEqual(SPEC_SHA, snapshot["input_snapshot"])
        self.assertEqual(set(TASK_STATES), set(snapshot["counts"]))
        self.assertEqual(4, snapshot["total"])
        self.assertEqual(4, snapshot["completed"])
        self.assertEqual(4, snapshot["counts"]["SUCCEEDED"])
        self.assertEqual(len(self.all_events("TASK-RUN-TEST")), snapshot["events_count"])
        self.assertEqual(["T01", "T02", "T03", "T04"], snapshot["topological_order"])
        self.assertEqual("SUCCEEDED", snapshot["status"])

    def test_snapshot_task_matches_the_contract_shape(self):
        orchestrator = self._run_diamond()
        snapshot = orchestrator.snapshot("TASK-RUN-TEST")
        task = next(item for item in snapshot["tasks"] if item["task_id"] == "T04")

        required = {
            "task_id",
            "goal",
            "block_id",
            "role_id",
            "capability",
            "priority",
            "depends_on",
            "req_refs",
            "outputs",
            "acceptance",
            "required_test_ids",
            "write_scope",
            "state",
            "attempt_id",
            "retry_count",
            "candidate_commit",
            "evidence_refs",
            "known_issues",
            "blocked_reason",
            "error",
            "started_at",
            "finished_at",
            "duration_seconds",
            "artifacts",
            "attempts",
        }
        self.assertEqual(set(), required - set(task))
        self.assertEqual(["T02", "T03"], task["depends_on"])
        self.assertEqual("SUCCEEDED", task["state"])
        self.assertEqual(1, task["attempt_id"])
        self.assertEqual("abc1234", task["candidate_commit"])
        self.assertEqual([{"path": "artifacts/T04.md", "kind": "document", "sha256": ARTIFACT_SHA}], task["artifacts"])
        self.assertEqual(["evidence-1"], task["evidence_refs"])
        self.assertIsNone(task["blocked_reason"])
        self.assertEqual(1, len(task["attempts"]))
        attempt = task["attempts"][0]
        for field in ("attempt_id", "retry_count", "state", "started_at", "finished_at", "reason"):
            with self.subTest(attempt_field=field):
                self.assertIn(field, attempt)

    def test_dag_matches_the_contract_shape(self):
        orchestrator = self._run_diamond()
        dag = orchestrator.dag("TASK-RUN-TEST")

        self.assertEqual({"run_id", "plan_version", "nodes", "edges", "counts", "topological_order"}, set(dag))
        self.assertEqual("TASK-RUN-TEST", dag["run_id"])
        self.assertEqual("PLAN-demo-v1.0", dag["plan_version"])
        self.assertEqual(4, len(dag["nodes"]))
        self.assertEqual(["T01", "T02", "T03", "T04"], dag["topological_order"])
        self.assertEqual(orchestrator.snapshot("TASK-RUN-TEST")["counts"], dag["counts"])

        node = dag["nodes"][0]
        for field in (
            "id",
            "label",
            "block_id",
            "state",
            "priority",
            "attempt_id",
            "retry_count",
            "duration_seconds",
            "blocked_reason",
            "acceptance",
            "required_test_ids",
        ):
            with self.subTest(node_field=field):
                self.assertIn(field, node)
        self.assertEqual("T01", node["id"])
        self.assertEqual("SUCCEEDED", node["state"])

        self.assertEqual(
            {("T01", "T02"), ("T01", "T03"), ("T02", "T04"), ("T03", "T04")},
            {(edge["from"], edge["to"]) for edge in dag["edges"]},
        )
        for edge in dag["edges"]:
            self.assertFalse(edge["revalidation"])

    def test_dag_shows_blocked_reason(self):
        orchestrator, _ = self.loaded_run(
            [make_task("T01"), make_task("T02", depends_on=("T01",))],
            executor=lambda request: execution_result(request, status="FAILED"),
        )
        orchestrator.run("TASK-RUN-TEST", max_steps=10)
        dag = orchestrator.dag("TASK-RUN-TEST")
        node = next(item for item in dag["nodes"] if item["id"] == "T02")
        self.assertEqual("BLOCKED", node["state"])
        self.assertEqual("upstream_failed:T01", node["blocked_reason"])
        self.assertEqual(1, dag["counts"]["FAILED"])
        self.assertEqual(1, dag["counts"]["BLOCKED"])


# --------------------------------------------------------------------------- #
# 11. Behaviour that contradicts the contract (documented, not fixed)
# --------------------------------------------------------------------------- #


class KnownDefectsTests(OrchestratorTestCase):
    def test_ready_tasks_does_not_emit_events(self):
        orchestrator, _ = self.loaded_run([make_task("T01"), make_task("T02", depends_on=("T01",))])
        self.store.update_task_state("TASK-RUN-TEST", "T01", state="SUCCEEDED")
        before = len(self.all_events("TASK-RUN-TEST"))

        ready = orchestrator.ready_tasks("TASK-RUN-TEST")

        self.assertEqual(["T02"], ready)
        self.assertEqual(before, len(self.all_events("TASK-RUN-TEST")))

    def test_replay_is_consistent_before_the_run_finishes(self):
        orchestrator, _ = self.loaded_run(
            [make_task("T01", retry_budget=1), make_task("T02", depends_on=("T01",))],
            executor=lambda request: execution_result(request, status="FAILED"),
        )
        self.assertTrue(orchestrator.replay("TASK-RUN-TEST")["consistent"])
        orchestrator.step("TASK-RUN-TEST")
        self.assertTrue(orchestrator.replay("TASK-RUN-TEST")["consistent"])

    def test_output_submitted_always_carries_candidate_commit(self):
        def executor(request):
            return {
                "task_id": request["task_id"],
                "attempt_id": request["attempt_id"],
                "status": "SUCCEEDED",
                "artifacts": [],
                "evidence_refs": [],
                "known_issues": [],
                "test_results": [],
            }

        orchestrator, _ = self.loaded_run([make_task("T01")], executor=executor)
        orchestrator.step("TASK-RUN-TEST")
        for event in self.events_of_type("TASK-RUN-TEST", "TASK_OUTPUT_SUBMITTED"):
            self.assertIsNotNone(event["candidate_commit"])
        self.assertNotEqual("SUCCEEDED", self.task(orchestrator, "TASK-RUN-TEST", "T01")["state"])

    def test_task_recovers_after_a_fixed_attempt(self):
        sys.path.insert(0, str(REPO_ROOT / "b06-quality-consensus"))
        from multiagent_quality.task_gate import review_task  # noqa: PLC0415

        attempts = {"count": 0}

        def executor(request):
            attempts["count"] += 1
            passing = [{"test_id": "TC-1", "status": "PASS"}]
            failing = [{"test_id": "TC-1", "status": "FAIL"}]
            return execution_result(request, test_results=failing if attempts["count"] == 1 else passing)

        orchestrator, _ = self.loaded_run(
            [make_task("T01", retry_budget=2)], executor=executor, reviewer=review_task
        )
        orchestrator.run("TASK-RUN-TEST", max_steps=20)
        task = self.task(orchestrator, "TASK-RUN-TEST", "T01")
        self.assertEqual("SUCCEEDED", task["state"], task["error"])
        self.assertEqual(2, task["attempt_id"])


if __name__ == "__main__":
    unittest.main()
