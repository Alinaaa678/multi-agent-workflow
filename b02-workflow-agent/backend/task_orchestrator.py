"""B02 开发任务编排器（D15/D16）：状态机、依赖检查、派发、重试与阻断。

职责边界：
- 本模块是**唯一**允许推进任务状态的组件（docs/TASK_ORCHESTRATION.md §1）。
- B04 只返回执行结果，B06 只返回验收结论与事件；二者都不得改写状态。
- 状态落盘委托给 B03 `TaskStateStore`；本模块只决定“下一步做什么”。

关键不变量：
1. 只有依赖全部 `SUCCEEDED` 的任务才能进入 `READY`。
2. 同一任务在 `RUNNING`/`REVIEWING` 期间不得重复派发。
3. `attempt_id` 单调递增；`retry_count` 只在失败重派时递增。
4. 上游 `FAILED`/`BLOCKED` 时下游进入 `BLOCKED`，并记录 `blocked_reason`。
5. 每个状态变化都产生带 `task_id/attempt_id/plan_version/input_snapshot` 的事件。
"""

from __future__ import annotations

import queue
import re
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Iterable
from uuid import uuid4

from .errors import WorkflowError

TASK_STATES = (
    "PENDING",
    "READY",
    "RUNNING",
    "WAITING_DEPENDENCY",
    "REVIEWING",
    "FAILED",
    "RETRYING",
    "BLOCKED",
    "SUCCEEDED",
)

# docs/TASK_ORCHESTRATION.md §1 的合法迁移表。
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "PENDING": frozenset({"READY", "WAITING_DEPENDENCY", "BLOCKED"}),
    "WAITING_DEPENDENCY": frozenset({"READY", "BLOCKED"}),
    "READY": frozenset({"RUNNING", "BLOCKED"}),
    "RUNNING": frozenset({"REVIEWING", "FAILED", "RETRYING", "BLOCKED", "READY"}),
    "REVIEWING": frozenset({"SUCCEEDED", "FAILED", "RETRYING", "BLOCKED", "READY"}),
    "RETRYING": frozenset({"READY", "FAILED", "BLOCKED"}),
    "FAILED": frozenset({"BLOCKED", "READY"}),
    "BLOCKED": frozenset({"READY"}),
    "SUCCEEDED": frozenset(),
}

TERMINAL_STATES = frozenset({"SUCCEEDED"})
FAILURE_STATES = frozenset({"FAILED", "BLOCKED"})

EVENT_TYPES = (
    "TASK_READY",
    "TASK_DISPATCHED",
    "TASK_STARTED",
    "TASK_OUTPUT_SUBMITTED",
    "TASK_REVIEW_REQUESTED",
    "TASK_ACCEPTED",
    "TASK_REJECTED",
    "TASK_RETRY_SCHEDULED",
    "TASK_RETRY_REQUESTED",
    "TASK_BLOCKED",
    "TASK_COMPLETED",
    "TASK_FAILED",
    # 依赖未满足时发出的显式事件；没有它，回放无法区分
    # “从未评估过（PENDING）”与“已评估但依赖未就绪（WAITING_DEPENDENCY）”。
    "TASK_WAITING_DEPENDENCY",
    # 运行级控制事件。它们不改变任务状态，但为人工操作提供审计轨迹。
    "RUN_PAUSED",
    "RUN_RESUMED",
    "RUN_CANCELLED",
    "RUN_RECOVERED",
)

CONTROLLED_RUN_STATES = frozenset({"PAUSED", "CANCELLED"})

# 这些事件必须同时携带 task_id/attempt_id/plan_version/input_snapshot。
BINDING_EVENTS = frozenset(
    {
        "TASK_DISPATCHED",
        "TASK_STARTED",
        "TASK_OUTPUT_SUBMITTED",
        "TASK_REVIEW_REQUESTED",
        "TASK_ACCEPTED",
        "TASK_REJECTED",
        "TASK_RETRY_SCHEDULED",
        "TASK_BLOCKED",
        "TASK_COMPLETED",
        "TASK_FAILED",
    }
)

# 这些事件必须带 candidate_commit。
COMMIT_EVENTS = frozenset({"TASK_OUTPUT_SUBMITTED", "TASK_ACCEPTED"})

EVENT_TO_STATE = {
    "TASK_READY": "READY",
    "TASK_DISPATCHED": "RUNNING",
    "TASK_STARTED": "RUNNING",
    "TASK_OUTPUT_SUBMITTED": "REVIEWING",
    "TASK_REVIEW_REQUESTED": "REVIEWING",
    "TASK_ACCEPTED": "SUCCEEDED",
    "TASK_REJECTED": "FAILED",
    "TASK_RETRY_SCHEDULED": "RETRYING",
    "TASK_BLOCKED": "BLOCKED",
    "TASK_COMPLETED": "SUCCEEDED",
    "TASK_FAILED": "FAILED",
    "TASK_WAITING_DEPENDENCY": "WAITING_DEPENDENCY",
}


class TaskOrchestrationError(WorkflowError):
    """编排前置条件不满足（未知任务、重复派发、非法迁移等）。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message, status)


# candidate_commit 必须是小写十六进制短/长哈希；与 B06 `COMMIT_PATTERN` 一致。
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{7,40}$")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def validate_plan_document(plan: dict[str, Any]) -> None:
    """B02 侧最小校验：不重复实现 B01 的完整校验，但拒绝无法编排的计划。"""
    if not isinstance(plan, dict):
        raise TaskOrchestrationError("plan 必须是对象。")
    for field in ("project_id", "spec_version", "contract_version", "plan_version"):
        if not isinstance(plan.get(field), str) or not plan[field].strip():
            raise TaskOrchestrationError(f"plan.{field} 必须是非空字符串。")
    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise TaskOrchestrationError("plan.tasks 必须是非空数组。")
    seen: set[str] = set()
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            raise TaskOrchestrationError(f"plan.tasks[{index}] 必须是对象。")
        task_id = task.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise TaskOrchestrationError(f"plan.tasks[{index}].task_id 必须是非空字符串。")
        if task_id in seen:
            raise TaskOrchestrationError(f"plan.tasks 任务编号重复: {task_id}")
        seen.add(task_id)
        depends = task.get("depends_on")
        if depends is not None and not isinstance(depends, list):
            raise TaskOrchestrationError(f"{task_id}.depends_on 必须是数组。")
    for task in tasks:
        for dep in task.get("depends_on") or []:
            if dep not in seen:
                raise TaskOrchestrationError(f"{task['task_id']}: 依赖不存在 {dep}")
    _assert_acyclic(tasks)


def _assert_acyclic(tasks: Iterable[dict[str, Any]]) -> None:
    registry = {task["task_id"]: task for task in tasks}
    visiting: set[str] = set()
    done: set[str] = set()
    for start in registry:
        if start in done:
            continue
        stack = [(start, iter(registry[start].get("depends_on") or []))]
        visiting.add(start)
        while stack:
            node, children = stack[-1]
            advanced = False
            for dep in children:
                if dep in done:
                    continue
                if dep in visiting:
                    raise TaskOrchestrationError(f"依赖环: {dep} 已在当前路径上")
                visiting.add(dep)
                stack.append((dep, iter(registry[dep].get("depends_on") or [])))
                advanced = True
                break
            if not advanced:
                stack.pop()
                visiting.discard(node)
                done.add(node)


class TaskOrchestrator:
    """基于 B03 `TaskStateStore` 的任务编排状态机。"""

    def __init__(
        self,
        store,
        *,
        executor: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        reviewer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ):
        self.store = store
        self.executor = executor or _default_executor
        self.reviewer = reviewer or _default_reviewer
        self._lock = threading.RLock()
        # 串行化调度步，但执行 B04 时不持有状态锁，暂停/取消/恢复仍可操作。
        self._schedule_lock = threading.RLock()

    # ---------- 计划与运行 ----------

    def load_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            validate_plan_document(plan)
            plan_id = str(plan.get("plan_id") or plan["plan_version"])
            self.store.register_plan(plan_id, plan)
            stored = self.store.get_plan(plan_id)
            assert stored is not None
            return stored

    def create_run(self, plan_id: str, *, run_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            plan = self.store.get_plan_document(plan_id)
            if plan is None:
                raise TaskOrchestrationError(f"未知计划: {plan_id}", 404)
            validate_plan_document(plan)
            identifier = run_id or "TASK-RUN-" + uuid4().hex[:8].upper()
            if not identifier.startswith("TASK-RUN-"):
                raise TaskOrchestrationError("run_id 必须以 TASK-RUN- 开头。")
            snapshot = (plan.get("input_snapshot") or {}).get("spec_sha256") or ""
            self.store.create_run(
                run_id=identifier,
                plan_id=plan_id,
                plan_version=plan["plan_version"],
                project_id=plan["project_id"],
                spec_version=plan["spec_version"],
                contract_version=plan["contract_version"],
                input_snapshot=snapshot,
                tasks=plan["tasks"],
            )
            run = self._require_run(identifier)
            tasks = self._task_map(plan)
            self._refresh_dependencies(run, plan, tasks)
            self._sync_run_status(run)
            return self.snapshot(identifier)

    def _require_run(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run is None:
            raise TaskOrchestrationError(f"未知任务运行: {run_id}", 404)
        return run

    def _plan_of(self, run: dict[str, Any]) -> dict[str, Any]:
        plan = self.store.get_plan_document(run["plan_id"])
        if plan is None:
            raise TaskOrchestrationError(f"运行 {run['run_id']} 的计划已不存在: {run['plan_id']}", 404)
        return plan

    @staticmethod
    def _task_map(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {task["task_id"]: task for task in plan["tasks"]}

    # ---------- 事件 ----------

    def _emit(
        self,
        run: dict[str, Any],
        event_type: str,
        task_id: str | None = None,
        *,
        attempt_id: int | None = None,
        candidate_commit: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if event_type not in EVENT_TYPES:
            raise TaskOrchestrationError(f"未知任务事件类型: {event_type}")
        event = {
            "run_id": run["run_id"],
            "type": event_type,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "plan_version": run["plan_version"],
            "input_snapshot": run["input_snapshot"],
            "candidate_commit": candidate_commit,
            "producer": "B02",
            "payload": payload or {},
            "occurred_at": now_iso(),
        }
        return self.store.append_run_event(event)

    def _transition(
        self,
        run: dict[str, Any],
        task_id: str,
        next_state: str,
        *,
        event_type: str | None = None,
        payload: dict[str, Any] | None = None,
        candidate_commit: str | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        current = self.store.get_task_state(run["run_id"], task_id)
        if current is None:
            raise TaskOrchestrationError(f"运行中没有任务 {task_id}")
        if next_state != current["state"]:
            allowed = ALLOWED_TRANSITIONS.get(current["state"], frozenset())
            if next_state not in allowed:
                raise TaskOrchestrationError(
                    f"{task_id}: 非法状态迁移 {current['state']} -> {next_state}"
                )
        updated = self.store.update_task_state(
            run["run_id"],
            task_id,
            state=next_state,
            expected_rev=current["rev"],
            **fields,
        )
        if event_type:
            self._emit(
                run,
                event_type,
                task_id,
                attempt_id=updated["attempt_id"],
                candidate_commit=candidate_commit,
                payload=payload,
            )
        return updated

    # ---------- 依赖与就绪 ----------

    def _refresh_dependencies(
        self,
        run: dict[str, Any],
        plan: dict[str, Any],
        tasks: dict[str, dict[str, Any]],
        *,
        emit: bool = True,
    ) -> list[str]:
        """评估依赖，返回本次进入 READY 的任务。

        `emit=False` 是**只读预览**（供 `ready_tasks` 使用）：此时不写状态、不发事件，
        否则一次只读查询会凭空产生 `TASK_READY` 事件，破坏事件回放一致性。
        """
        if not emit:
            # 只读预览：仅返回当前已处于 READY 的任务，不写状态、不发事件。
            return [
                item["task_id"]
                for item in self.store.list_task_states(run["run_id"])
                if item["state"] == "READY"
            ]
        states = {item["task_id"]: item for item in self.store.list_task_states(run["run_id"])}
        ready: list[str] = []
        # 按拓扑序评估，保证同一 step 内的传播是确定的。
        order = plan.get("topological_order") or [task["task_id"] for task in plan["tasks"]]
        for task_id in order:
            task = tasks[task_id]
            record = states[task_id]
            # 只有“上游失败”造成的 BLOCKED 可在上游人工重试成功后自动恢复。
            # 执行器主动报告的 BLOCKED 不会被这里静默重派。
            recoverable = record["state"] == "BLOCKED" and str(
                record.get("blocked_reason") or ""
            ).startswith("upstream_failed:")
            if record["state"] not in ("PENDING", "WAITING_DEPENDENCY") and not recoverable:
                continue
            dependency_states = [states[dep]["state"] for dep in task.get("depends_on") or []]
            failed = [dep for dep in task.get("depends_on") or [] if states[dep]["state"] in FAILURE_STATES]
            if failed:
                if record["state"] != "BLOCKED":
                    reason = "upstream_failed:" + ",".join(sorted(failed))
                    self._transition(
                        run,
                        task_id,
                        "BLOCKED",
                        event_type="TASK_BLOCKED",
                        payload={"reason": reason, "blocked_by": sorted(failed)},
                        blocked_reason=reason,
                    )
                continue
            if all(state == "SUCCEEDED" for state in dependency_states):
                self._transition(
                    run,
                    task_id,
                    "READY",
                    event_type="TASK_READY",
                    payload={
                        "depends_on": list(task.get("depends_on") or []),
                        "recovered_after_upstream_retry": recoverable,
                    },
                    blocked_reason=None,
                    error=None,
                )
                ready.append(task_id)
            elif record["state"] == "PENDING":
                waiting_on = sorted(
                    dep for dep in (task.get("depends_on") or []) if states[dep]["state"] != "SUCCEEDED"
                )
                self._transition(
                    run,
                    task_id,
                    "WAITING_DEPENDENCY",
                    event_type="TASK_WAITING_DEPENDENCY",
                    payload={"waiting_on": waiting_on},
                )
        return ready

    def ready_tasks(self, run_id: str) -> list[str]:
        """只读查询：按当前状态推算“本可以派发”的任务。

        纯计算，不写状态、不发事件（GET 语义）。判定规则与 `_refresh_dependencies`
        一致：任务尚在 `PENDING`/`WAITING_DEPENDENCY`/`READY`，且依赖全部 `SUCCEEDED`
        且没有上游处于 `FAILED`/`BLOCKED`。
        若需要真正推进依赖并记录事件，请调用 `step()`。
        """
        with self._lock:
            run = self._require_run(run_id)
            plan = self._plan_of(run)
            states = {item["task_id"]: item["state"] for item in self.store.list_task_states(run_id)}
            tasks = self._task_map(plan)
            candidates = ("PENDING", "WAITING_DEPENDENCY", "READY")
            ready: list[str] = []
            order = plan.get("topological_order") or [task["task_id"] for task in plan["tasks"]]
            for task_id in order:
                state = states.get(task_id)
                if state not in candidates:
                    continue
                depends = list(tasks[task_id].get("depends_on") or [])
                dependency_states = [states.get(dep, "PENDING") for dep in depends]
                if any(item in FAILURE_STATES for item in dependency_states):
                    continue
                if all(item == "SUCCEEDED" for item in dependency_states):
                    ready.append(task_id)
            return ready

    # ---------- 派发 / 执行 / 验收 ----------

    def dispatch(self, run_id: str, task_id: str) -> dict[str, Any]:
        """派发给 B04；B02 独立计时，超时结果进入现有限重试流程。"""
        with self._lock:
            run = self._require_run(run_id)
            self._assert_dispatchable(run)
            plan = self._plan_of(run)
            tasks = self._task_map(plan)
            self._refresh_dependencies(run, plan, tasks)
            record = self.store.get_task_state(run_id, task_id)
            if record is None:
                raise TaskOrchestrationError(f"运行中没有任务 {task_id}")
            if record["state"] in ("RUNNING", "REVIEWING"):
                raise TaskOrchestrationError(f"{task_id} 正在 {record['state']}，拒绝重复派发")
            if record["state"] != "READY":
                raise TaskOrchestrationError(f"{task_id} 当前 {record['state']}，不是 READY")

            attempt_id = record["attempt_id"] + 1
            started = now_iso()
            request = self.build_execution_request(run, tasks[task_id], attempt_id)
            self._timeout_seconds(request.get("timeout"))
            self._transition(
                run,
                task_id,
                "RUNNING",
                event_type="TASK_DISPATCHED",
                payload={"role_id": tasks[task_id].get("role_id"), "block_id": tasks[task_id].get("block_id")},
                attempt_id=attempt_id,
                started_at=started,
                finished_at=None,
                error=None,
                blocked_reason=None,
            )
            self._emit(
                run,
                "TASK_STARTED",
                task_id,
                attempt_id=attempt_id,
                payload={"goal": tasks[task_id].get("goal")},
            )

        # 不持有状态锁等待同步执行器：重复派发仍会被 RUNNING 状态拒绝，
        # 同时慢任务不会卡住暂停、取消、快照和崩溃恢复操作。
        result = self._execute_with_timeout(request)
        with self._lock:
            run = self._require_run(run_id)
            plan = self._plan_of(run)
            task = self._task_map(plan)[task_id]
            latest = self.store.get_task_state(run_id, task_id)
            if (
                latest is None
                or latest["attempt_id"] != attempt_id
                or latest["state"] not in ("RUNNING", "REVIEWING")
            ):
                # 恢复或人工干预已经推进了现场；旧 attempt 的结果只能丢弃。
                return {**result, "stale": True, "ignored_by_orchestrator": True}
            self._apply_result(run, plan, task, attempt_id, result, started)
            return result

    @staticmethod
    def _timeout_seconds(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise TaskOrchestrationError("task.timeout 必须是正整数秒。")
        return value

    def _execute_with_timeout(self, request: dict[str, Any]) -> dict[str, Any]:
        """在守护线程中调用同步执行器；迟到结果只进队列，不再触碰状态。"""
        timeout = self._timeout_seconds(request.get("timeout"))
        results: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)

        def execute() -> None:
            try:
                result = self.executor(request)
                if not isinstance(result, dict):
                    raise TypeError("executor 必须返回字典结果")
            except Exception as error:  # 执行器异常按失败处理，不吞掉原因
                result = {
                    "task_id": request["task_id"],
                    "attempt_id": request["attempt_id"],
                    "status": "FAILED",
                    "error": f"{type(error).__name__}: {error}",
                    "artifacts": [],
                    "evidence_refs": [],
                    "known_issues": [
                        {"code": "EXECUTOR_ERROR", "message": str(error), "severity": "blocking"}
                    ],
                }
            results.put_nowait(result)

        threading.Thread(
            target=execute,
            name=f"B02-{request['run_id']}-{request['task_id']}-{request['attempt_id']}",
            daemon=True,
        ).start()
        try:
            return results.get(timeout=timeout)
        except queue.Empty:
            message = f"TASK_TIMEOUT: 执行超过 {timeout} 秒"
            return {
                "task_id": request["task_id"],
                "attempt_id": request["attempt_id"],
                "status": "FAILED",
                "error": message,
                "artifacts": [],
                "evidence_refs": [],
                "known_issues": [
                    {
                        "code": "TASK_TIMEOUT",
                        "message": message,
                        "severity": "blocking",
                        "timeout_seconds": timeout,
                    }
                ],
            }

    @staticmethod
    def build_execution_request(
        run: dict[str, Any], task: dict[str, Any], attempt_id: int
    ) -> dict[str, Any]:
        """构造 docs/TASK_ORCHESTRATION.md §5 的 TaskExecutionRequest。"""
        return {
            "task_id": task["task_id"],
            "attempt_id": attempt_id,
            "plan_version": run["plan_version"],
            "input_snapshot": run["input_snapshot"],
            "run_id": run["run_id"],
            "role_id": task.get("role_id"),
            "block_id": task.get("block_id"),
            "goal": task.get("goal"),
            "inputs": [{"ref": item, "kind": "declared_input"} for item in task.get("inputs") or []],
            "outputs": list(task.get("outputs") or []),
            "acceptance": list(task.get("acceptance") or []),
            "required_test_ids": list(task.get("required_test_ids") or []),
            "write_scope": list(task.get("write_scope") or []),
            "timeout": task.get("timeout", 600),
            "retry_budget": task.get("retry_budget", 0),
            "candidate_base_commit": task.get("candidate_base_commit"),
        }

    def _apply_result(
        self,
        run: dict[str, Any],
        plan: dict[str, Any],
        task: dict[str, Any],
        attempt_id: int,
        result: dict[str, Any],
        started_at: str,
    ) -> None:
        task_id = task["task_id"]
        if result.get("task_id") not in (None, task_id):
            self._fail_attempt(
                run, task, attempt_id, started_at,
                reason=f"executor returned task_id={result.get('task_id')}",
            )
            return
        if result.get("attempt_id") not in (None, attempt_id):
            self._fail_attempt(
                run, task, attempt_id, started_at,
                reason=f"executor returned attempt_id={result.get('attempt_id')}",
            )
            return
        candidate_commit = result.get("candidate_commit")
        if result.get("status") == "BLOCKED":
            reason = result.get("error") or "executor_blocked"
            self._transition(
                run, task_id, "BLOCKED",
                event_type="TASK_BLOCKED",
                payload={"reason": reason, "known_issues": result.get("known_issues") or []},
                blocked_reason=reason,
                finished_at=now_iso(),
                known_issues=result.get("known_issues") or [],
            )
            self._record_attempt(run, task_id, attempt_id, "BLOCKED", started_at, reason)
            return
        if result.get("status") != "SUCCEEDED":
            self._fail_attempt(
                run,
                task,
                attempt_id,
                started_at,
                reason=result.get("error") or "execution_failed",
                findings=result.get("known_issues") or [],
            )
            return
        # 契约要求：TASK_OUTPUT_SUBMITTED 必带 candidate_commit。
        # 执行器声称成功却没有提交引用时按失败处理，而不是发出缺字段的事件。
        if not isinstance(candidate_commit, str) or not COMMIT_PATTERN.match(candidate_commit):
            self._fail_attempt(
                run, task, attempt_id, started_at,
                reason="missing_candidate_commit",
                findings=[
                    {
                        "code": "INVALID_CANDIDATE_COMMIT",
                        "severity": "blocking",
                        "message": "SUCCEEDED 结果缺少合法的 candidate_commit",
                    }
                ],
            )
            return
        # 执行成功 → REVIEWING → B06 验收
        record = self.store.get_task_state(run["run_id"], task_id)
        assert record is not None
        self.store.update_task_state(
            run["run_id"],
            task_id,
            expected_rev=record["rev"],
            state="REVIEWING",
            candidate_commit=candidate_commit,
            artifacts=result.get("artifacts") or [],
            known_issues=result.get("known_issues") or [],
            evidence_refs=result.get("evidence_refs") or [],
            result=result,
        )
        self._emit(
            run, "TASK_OUTPUT_SUBMITTED", task_id,
            attempt_id=attempt_id,
            candidate_commit=candidate_commit,
            payload={
                "artifacts": result.get("artifacts") or [],
                "logs": result.get("logs") or [],
                "test_results": result.get("test_results") or [],
            },
        )
        self._emit(
            run, "TASK_REVIEW_REQUESTED", task_id,
            attempt_id=attempt_id,
            candidate_commit=candidate_commit,
            payload={"required_test_ids": list(task.get("required_test_ids") or [])},
        )
        review = self.reviewer(
            self.build_review_request(
                run, plan, task, attempt_id, result, self._defect_history(run["run_id"], task_id)
            )
        )
        self._apply_review(run, task, attempt_id, result, review, started_at)

    GATE_EVENT_TYPES = frozenset({"DEFECT_CREATED", "DEFECT_CLOSED"})

    def _ingest_gate_events(self, run: dict[str, Any], events: list[dict[str, Any]]) -> None:
        """记录 B06 的缺陷事件。验收/驳回事件由 B02 自己的迁移事件表达，避免重复。"""
        for item in events:
            event_type = item.get("type")
            if event_type not in self.GATE_EVENT_TYPES:
                continue
            self.store.append_run_event(
                {
                    "run_id": run["run_id"],
                    "type": event_type,
                    "task_id": item.get("task_id"),
                    "attempt_id": item.get("attempt_id"),
                    "plan_version": run["plan_version"],
                    "input_snapshot": run["input_snapshot"],
                    "candidate_commit": item.get("candidate_commit"),
                    "producer": "B06",
                    "payload": item.get("payload") or {},
                    "occurred_at": now_iso(),
                }
            )

    def _defect_history(self, run_id: str, task_id: str) -> list[dict[str, Any]]:
        """该任务仍未关闭的缺陷（按事件重放，只追加不覆盖）。"""
        open_defects: dict[str, dict[str, Any]] = {}
        for event in self.store.list_run_events(run_id, limit=1_000_000):
            payload = event.get("payload") or {}
            if event.get("task_id") != task_id:
                continue
            if event["type"] == "DEFECT_CREATED":
                defect_id = payload.get("defect_id")
                if defect_id:
                    open_defects[defect_id] = {
                        "defect_id": defect_id,
                        "task_id": task_id,
                        "severity": payload.get("severity", "blocking"),
                        "status": "open",
                        "code": payload.get("code"),
                        "message": payload.get("message"),
                    }
            elif event["type"] == "DEFECT_CLOSED":
                # DEFECT_CLOSED 可能带也可能不带 defect_id：带则定向关闭，不带则关闭该任务全部。
                defect_id = payload.get("defect_id")
                if defect_id:
                    if defect_id in open_defects:
                        open_defects[defect_id]["status"] = "closed"
                else:
                    for defect in open_defects.values():
                        defect["status"] = "closed"
        return list(open_defects.values())

    @staticmethod
    def build_review_request(
        run: dict[str, Any],
        plan: dict[str, Any],
        task: dict[str, Any],
        attempt_id: int,
        result: dict[str, Any],
        history: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """构造 docs/TASK_ORCHESTRATION.md §6 的 B06 输入。"""
        return {
            "plan": plan,
            "task": task,
            "run": {
                "run_id": run["run_id"],
                "plan_version": run["plan_version"],
                "input_snapshot": run["input_snapshot"],
                "spec_version": run["spec_version"],
                "contract_version": run["contract_version"],
            },
            "result": result,
            "history": list(history or []),
            "attempt_id": attempt_id,
        }

    def _apply_review(
        self,
        run: dict[str, Any],
        task: dict[str, Any],
        attempt_id: int,
        result: dict[str, Any],
        review: dict[str, Any],
        started_at: str,
    ) -> None:
        task_id = task["task_id"]
        decision = review.get("decision")
        candidate_commit = result.get("candidate_commit")
        findings = review.get("findings") or []
        defects = review.get("defects") or []
        blocking = [item for item in findings if item.get("severity") == "blocking"]
        # 验收判定来源：B02 是唯一状态写入者，但结论来自 B06，必须可追溯。
        gate_ref = {
            "gate": "B06",
            "decision": decision,
            "finding_count": len(findings),
            "blocking_count": len(blocking),
            "defect_ids": [item.get("defect_id") for item in defects if item.get("defect_id")],
        }
        self.store.update_task_state(
            run["run_id"],
            task_id,
            review={"decision": decision, "findings": findings, "defects": defects},
        )
        # B06 只返回事件，由 B02 负责落库（B06 不写状态）。
        # 说明：TASK_ACCEPTED/TASK_REJECTED 由 B02 作为唯一状态写入者发出，
        # 以免同一语义在事件流里出现两次；B06 的结论通过 payload.gate 与
        # 独立的 DEFECT_CREATED/DEFECT_CLOSED 事件体现。
        self._ingest_gate_events(run, review.get("events") or [])
        if decision == "ACCEPT":
            record = self.store.get_task_state(run["run_id"], task_id)
            assert record is not None
            finished = now_iso()
            duration = _duration_seconds(started_at, finished)
            self.store.update_task_state(
                run["run_id"],
                task_id,
                expected_rev=record["rev"],
                state="SUCCEEDED",
                finished_at=finished,
                duration_seconds=duration,
                evidence_refs=result.get("evidence_refs") or [],
                candidate_commit=candidate_commit,
            )
            self._emit(
                run, "TASK_ACCEPTED", task_id,
                attempt_id=attempt_id,
                candidate_commit=candidate_commit,
                payload={
                    "evidence_refs": result.get("evidence_refs") or [],
                    "findings": findings,
                    "defects": defects,
                    "gate": gate_ref,
                },
            )
            self._emit(
                run, "TASK_COMPLETED", task_id,
                attempt_id=attempt_id,
                candidate_commit=candidate_commit,
                payload={"duration_seconds": duration, "gate": gate_ref},
            )
            self._record_attempt(run, task_id, attempt_id, "SUCCEEDED", started_at, None, candidate_commit)
            return
        if decision == "REVALIDATION_REQUIRED":
            self._retry_or_fail(
                run, task, attempt_id, started_at,
                reason="revalidation_required",
                findings=findings,
                candidate_commit=candidate_commit,
                gate_ref=gate_ref,
            )
            return
        self._retry_or_fail(
            run, task, attempt_id, started_at,
            reason="review_rejected",
            findings=findings or blocking,
            candidate_commit=candidate_commit,
            gate_ref=gate_ref,
        )

    def _fail_attempt(
        self,
        run: dict[str, Any],
        task: dict[str, Any],
        attempt_id: int,
        started_at: str,
        *,
        reason: str,
        findings: list[dict[str, Any]] | None = None,
        candidate_commit: str | None = None,
    ) -> None:
        self._retry_or_fail(
            run, task, attempt_id, started_at,
            reason=reason,
            findings=findings or [],
            candidate_commit=candidate_commit,
        )

    def _retry_or_fail(
        self,
        run: dict[str, Any],
        task: dict[str, Any],
        attempt_id: int,
        started_at: str,
        *,
        reason: str,
        findings: list[dict[str, Any]],
        candidate_commit: str | None,
        gate_ref: dict[str, Any] | None = None,
    ) -> None:
        """有限重试：预算内进入 RETRYING→READY，超预算进入 FAILED。"""
        task_id = task["task_id"]
        record = self.store.get_task_state(run["run_id"], task_id)
        assert record is not None
        budget = int(task.get("retry_budget", 0) or 0)
        retried = record["retry_count"]
        payload = {
            "reason": reason,
            "findings": findings,
            "retry_count": retried,
            "retry_budget": budget,
        }
        if gate_ref is not None:
            payload["gate"] = gate_ref
        self._emit(
            run, "TASK_REJECTED", task_id,
            attempt_id=attempt_id,
            candidate_commit=candidate_commit,
            payload=payload,
        )
        finished = now_iso()
        if retried < budget:
            self.store.update_task_state(
                run["run_id"],
                task_id,
                expected_rev=record["rev"],
                state="RETRYING",
                retry_count=retried + 1,
                finished_at=finished,
                duration_seconds=_duration_seconds(started_at, finished),
                error=reason,
            )
            self._emit(
                run, "TASK_RETRY_SCHEDULED", task_id,
                attempt_id=attempt_id,
                payload={"retry_count": retried + 1, "retry_budget": budget, "reason": reason},
            )
            self._record_attempt(run, task_id, attempt_id, "RETRYING", started_at, reason, candidate_commit)
            # 重试排队后立即回到 READY，等待下一次派发。
            # 该迁移同样必须有事件，否则回放会停在 RETRYING 而与快照不一致。
            latest = self.store.get_task_state(run["run_id"], task_id)
            assert latest is not None
            self.store.update_task_state(
                run["run_id"],
                task_id,
                expected_rev=latest["rev"],
                state="READY",
                error=reason,
            )
            self._emit(
                run, "TASK_READY", task_id,
                attempt_id=attempt_id,
                payload={
                    "depends_on": list(task.get("depends_on") or []),
                    "requeued_after_retry": True,
                    "retry_count": retried + 1,
                },
            )
            return
        self.store.update_task_state(
            run["run_id"],
            task_id,
            expected_rev=record["rev"],
            state="FAILED",
            finished_at=finished,
            duration_seconds=_duration_seconds(started_at, finished),
            error=reason,
        )
        self._emit(
            run, "TASK_FAILED", task_id,
            attempt_id=attempt_id,
            candidate_commit=candidate_commit,
            payload={"reason": reason, "retry_count": retried, "retry_budget": budget, "findings": findings},
        )
        self._record_attempt(run, task_id, attempt_id, "FAILED", started_at, reason, candidate_commit)

    def _record_attempt(
        self,
        run: dict[str, Any],
        task_id: str,
        attempt_id: int,
        state: str,
        started_at: str,
        reason: str | None,
        candidate_commit: str | None = None,
    ) -> None:
        record = self.store.get_task_state(run["run_id"], task_id)
        self.store.record_attempt(
            run["run_id"],
            task_id,
            attempt_id=attempt_id,
            retry_count=(record or {}).get("retry_count", 0),
            state=state,
            started_at=started_at,
            finished_at=now_iso(),
            reason=reason,
            candidate_commit=candidate_commit,
        )

    # ---------- 人工执行控制 ----------

    @staticmethod
    def _control_ack(
        action: str,
        request_id: str | None,
        operator: str,
        message: str,
    ) -> dict[str, Any]:
        return {
            "request_id": request_id or "CTRL-" + uuid4().hex[:12].upper(),
            "action": action,
            "accepted": True,
            "operator": operator,
            "message": message,
            "timestamp": now_iso(),
        }

    @staticmethod
    def _assert_dispatchable(run: dict[str, Any]) -> None:
        if run["status"] == "PAUSED":
            raise TaskOrchestrationError("运行已暂停，请先继续运行。", 409)
        if run["status"] == "CANCELLED":
            raise TaskOrchestrationError("运行已取消，不能继续派发任务。", 409)

    def pause(
        self,
        run_id: str,
        *,
        request_id: str | None = None,
        operator: str = "human-operator",
        reason: str = "",
    ) -> dict[str, Any]:
        """暂停后续派发；同步执行中的当前任务不会被强行终止。"""
        with self._lock:
            run = self._require_run(run_id)
            if run["status"] == "PAUSED":
                return {
                    "ack": self._control_ack("pause", request_id, operator, "运行已处于暂停状态。"),
                    "run": self.snapshot(run_id),
                }
            if run["status"] in ("SUCCEEDED", "FAILED", "BLOCKED", "CANCELLED"):
                raise TaskOrchestrationError(f"运行状态 {run['status']} 不能暂停。", 409)
            self.store.set_run_status(run_id, "PAUSED")
            run["status"] = "PAUSED"
            self._emit(run, "RUN_PAUSED", payload={"operator": operator, "reason": reason})
            return {
                "ack": self._control_ack("pause", request_id, operator, "已暂停后续任务派发。"),
                "run": self.snapshot(run_id),
            }

    def resume(
        self,
        run_id: str,
        *,
        request_id: str | None = None,
        operator: str = "human-operator",
        reason: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            run = self._require_run(run_id)
            if run["status"] != "PAUSED":
                raise TaskOrchestrationError("只有已暂停的运行才能继续。", 409)
            # 先脱离受控状态，再根据任务快照恢复真实运行状态。
            self.store.set_run_status(run_id, "RUNNING")
            run["status"] = "RUNNING"
            self._sync_run_status(run)
            self._emit(run, "RUN_RESUMED", payload={"operator": operator, "reason": reason})
            return {
                "ack": self._control_ack("resume", request_id, operator, "运行已继续，可再次单步或运行至完成。"),
                "run": self.snapshot(run_id),
            }

    def cancel(
        self,
        run_id: str,
        *,
        request_id: str | None = None,
        operator: str = "human-operator",
        reason: str = "",
    ) -> dict[str, Any]:
        """取消整个运行；保留任务现场和事件，不伪造任务成功或失败。"""
        with self._lock:
            run = self._require_run(run_id)
            if run["status"] == "CANCELLED":
                return {
                    "ack": self._control_ack("cancel", request_id, operator, "运行已经取消。"),
                    "run": self.snapshot(run_id),
                }
            if run["status"] == "SUCCEEDED":
                raise TaskOrchestrationError("已完成的运行不能取消。", 409)
            self.store.set_run_status(run_id, "CANCELLED")
            run["status"] = "CANCELLED"
            self._emit(run, "RUN_CANCELLED", payload={"operator": operator, "reason": reason})
            return {
                "ack": self._control_ack("cancel", request_id, operator, "运行已取消，任务现场已保留。"),
                "run": self.snapshot(run_id),
            }

    def recover(
        self,
        run_id: str,
        *,
        request_id: str | None = None,
        operator: str = "human-operator",
        reason: str = "",
    ) -> dict[str, Any]:
        """把进程崩溃遗留的 RUNNING/REVIEWING 任务重新入队，不自动重跑。"""
        with self._lock:
            run = self._require_run(run_id)
            if run["status"] == "CANCELLED":
                raise TaskOrchestrationError("运行已取消，不能恢复任务。", 409)
            plan = self._plan_of(run)
            tasks = self._task_map(plan)
            records = self.store.list_task_states(run_id)
            states = {item["task_id"]: item["state"] for item in records}
            interrupted = [item for item in records if item["state"] in ("RUNNING", "REVIEWING")]
            # 先检查全部候选，避免恢复一半才发现依赖不满足。
            for record in interrupted:
                task_id = record["task_id"]
                unfinished = [
                    dep for dep in tasks[task_id].get("depends_on") or []
                    if states.get(dep) != "SUCCEEDED"
                ]
                if unfinished:
                    raise TaskOrchestrationError(
                        f"{task_id} 的上游尚未全部通过: {', '.join(sorted(unfinished))}", 409
                    )

            recovered: list[str] = []
            recovery_reason = reason or "PROCESS_RESTART_RECOVERY"
            for record in interrupted:
                task_id = record["task_id"]
                previous_state = record["state"]
                previous_attempt = record["attempt_id"]
                self._record_attempt(
                    run,
                    task_id,
                    previous_attempt,
                    "INTERRUPTED",
                    record.get("started_at"),
                    recovery_reason,
                    record.get("candidate_commit"),
                )
                self._transition(
                    run,
                    task_id,
                    "READY",
                    event_type="TASK_READY",
                    payload={
                        "depends_on": list(tasks[task_id].get("depends_on") or []),
                        "recovered_after_restart": True,
                        "previous_state": previous_state,
                        "interrupted_attempt_id": previous_attempt,
                        "reason": recovery_reason,
                        "operator": operator,
                    },
                    started_at=None,
                    finished_at=None,
                    duration_seconds=None,
                    error=None,
                    blocked_reason=None,
                    candidate_commit=None,
                    artifacts=[],
                    evidence_refs=[],
                    known_issues=[],
                    result=None,
                    review=None,
                )
                recovered.append(task_id)

            message = (
                f"已恢复 {len(recovered)} 个中断任务；请继续单步或运行，不会自动重跑。"
                if recovered else "没有需要恢复的中断任务。"
            )
            ack = self._control_ack("recover", request_id, operator, message)
            if recovered:
                self._emit(
                    run,
                    "RUN_RECOVERED",
                    payload={
                        "request_id": ack["request_id"],
                        "operator": operator,
                        "reason": recovery_reason,
                        "recovered_tasks": recovered,
                    },
                )
                self._sync_run_status(run)
            return {"ack": ack, "recovered_tasks": recovered, "run": self.snapshot(run_id)}

    def retry_task(
        self,
        run_id: str,
        task_id: str,
        *,
        request_id: str | None = None,
        operator: str = "human-operator",
        reason: str = "",
    ) -> dict[str, Any]:
        """人工重派失败/阻塞任务；依赖未通过时拒绝绕过编排规则。"""
        with self._schedule_lock:
            with self._lock:
                run = self._require_run(run_id)
                self._assert_dispatchable(run)
                plan = self._plan_of(run)
                tasks = self._task_map(plan)
                record = self.store.get_task_state(run_id, task_id)
                if record is None:
                    raise TaskOrchestrationError(f"运行中没有任务 {task_id}", 404)
                if record["state"] not in ("FAILED", "BLOCKED"):
                    raise TaskOrchestrationError(
                        f"{task_id} 当前 {record['state']}，只有失败或阻塞任务可人工重试。", 409
                    )
                states = {
                    item["task_id"]: item["state"]
                    for item in self.store.list_task_states(run_id)
                }
                unfinished = [
                    dep for dep in tasks[task_id].get("depends_on") or []
                    if states.get(dep) != "SUCCEEDED"
                ]
                if unfinished:
                    raise TaskOrchestrationError(
                        f"{task_id} 的上游尚未全部通过: {', '.join(sorted(unfinished))}", 409
                    )
                self._emit(
                    run,
                    "TASK_RETRY_REQUESTED",
                    task_id,
                    attempt_id=record["attempt_id"],
                    payload={"operator": operator, "reason": reason, "previous_state": record["state"]},
                )
                self._transition(
                    run,
                    task_id,
                    "READY",
                    event_type="TASK_READY",
                    payload={"depends_on": list(tasks[task_id].get("depends_on") or []), "manual_retry": True},
                    error=None,
                    blocked_reason=None,
                    finished_at=None,
                    candidate_commit=None,
                    artifacts=[],
                    evidence_refs=[],
                    known_issues=[],
                    review=None,
                )
                self._sync_run_status(run)
            result = self.dispatch(run_id, task_id)
            with self._lock:
                latest = self._require_run(run_id)
                self._refresh_dependencies(latest, plan, tasks)
                self._sync_run_status(latest)
                return {
                    "ack": self._control_ack("retry", request_id, operator, f"{task_id} 已重派，本次结果：{result.get('status')}。"),
                    "run": self.snapshot(run_id),
                    "result": result,
                }

    # ---------- 单步 / 整体推进 ----------

    def step(self, run_id: str) -> dict[str, Any]:
        """推进一个调度步：刷新依赖 + 派发一个 READY 任务。"""
        with self._schedule_lock:
            with self._lock:
                run = self._require_run(run_id)
                self._assert_dispatchable(run)
                plan = self._plan_of(run)
                tasks = self._task_map(plan)
                self._refresh_dependencies(run, plan, tasks)
                self._sync_run_status(run)
                ready = [
                    item["task_id"]
                    for item in self.store.list_task_states(run_id)
                    if item["state"] == "READY"
                ]
                transitions_before = len(self.store.list_run_events(run_id, limit=1_000_000))
            if ready:
                self.dispatch(run_id, ready[0])
            with self._lock:
                if ready:
                    self._refresh_dependencies(self._require_run(run_id), plan, tasks)
                    self._sync_run_status(self._require_run(run_id))
                events = self.store.list_run_events(run_id, after_sequence=transitions_before, limit=1_000_000)
                return {
                    "run": self.snapshot(run_id),
                    "dispatched": ready[:1],
                    "transitions": events,
                }

    def run(self, run_id: str, *, max_steps: int = 200) -> dict[str, Any]:
        if not isinstance(max_steps, int) or max_steps < 1:
            raise TaskOrchestrationError("max_steps 必须是正整数。")
        with self._schedule_lock:
            with self._lock:
                run = self._require_run(run_id)
                self._assert_dispatchable(run)
                events_before = len(self.store.list_run_events(run_id, limit=1_000_000))
            steps = 0
            while steps < max_steps:
                with self._lock:
                    snapshot = self.snapshot(run_id)
                    before = len(self.store.list_run_events(run_id, limit=1_000_000))
                if snapshot["status"] in ("PAUSED", "CANCELLED", "SUCCEEDED", "FAILED", "BLOCKED"):
                    break
                if not any(item["state"] in ("READY", "PENDING", "WAITING_DEPENDENCY", "RETRYING")
                           for item in snapshot["tasks"]):
                    break
                self.step(run_id)
                with self._lock:
                    after = len(self.store.list_run_events(run_id, limit=1_000_000))
                steps += 1
                if after == before:
                    break
            with self._lock:
                events = self.store.list_run_events(run_id, after_sequence=events_before, limit=1_000_000)
                return {
                    "run": self.snapshot(run_id),
                    "steps": steps,
                    "transitions": events,
                }

    def _sync_run_status(self, run: dict[str, Any]) -> None:
        if run["status"] in CONTROLLED_RUN_STATES:
            return
        states = [item["state"] for item in self.store.list_task_states(run["run_id"])]
        if not states:
            status = "READY"
        elif all(state == "SUCCEEDED" for state in states):
            status = "SUCCEEDED"
        elif any(state in ("RUNNING", "REVIEWING", "READY", "RETRYING", "WAITING_DEPENDENCY", "PENDING") for state in states):
            status = "RUNNING"
        elif any(state == "FAILED" for state in states) and any(state == "BLOCKED" for state in states):
            # 失败与阻断并存时以 BLOCKED 表达“需要人工介入解除阻断”。
            status = "BLOCKED"
        elif any(state == "BLOCKED" for state in states):
            status = "BLOCKED"
        else:
            status = "FAILED"
        if run["status"] != status:
            self.store.set_run_status(run["run_id"], status)
            run["status"] = status

    # ---------- 快照 / DAG ----------

    def snapshot(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            run = self._require_run(run_id)
            plan = self._plan_of(run)
            tasks = self._task_map(plan)
            records = {item["task_id"]: item for item in self.store.list_task_states(run_id)}
            attempts = self.store.list_attempts(run_id)
            attempts_by_task: dict[str, list[dict[str, Any]]] = {}
            for attempt in attempts:
                attempts_by_task.setdefault(attempt["task_id"], []).append(attempt)
            view: list[dict[str, Any]] = []
            for plan_task in plan["tasks"]:
                task_id = plan_task["task_id"]
                record = records[task_id]
                view.append(
                    {
                        "task_id": task_id,
                        "goal": plan_task.get("goal"),
                        "block_id": plan_task.get("block_id"),
                        "role_id": plan_task.get("role_id"),
                        "capability": plan_task.get("capability"),
                        "priority": plan_task.get("priority"),
                        "depends_on": list(plan_task.get("depends_on") or []),
                        "req_refs": list(plan_task.get("req_refs") or []),
                        "design_refs": list(plan_task.get("design_refs") or []),
                        "outputs": list(plan_task.get("outputs") or []),
                        "acceptance": list(plan_task.get("acceptance") or []),
                        "required_test_ids": list(plan_task.get("required_test_ids") or []),
                        "inputs": list(plan_task.get("inputs") or []),
                        "write_scope": list(plan_task.get("write_scope") or []),
                        "timeout": plan_task.get("timeout"),
                        "retry_budget": plan_task.get("retry_budget"),
                        "state": record["state"],
                        "attempt_id": record["attempt_id"],
                        "retry_count": record["retry_count"],
                        "candidate_commit": record["candidate_commit"],
                        "evidence_refs": record["evidence_refs"],
                        "known_issues": record["known_issues"],
                        "blocked_reason": record["blocked_reason"],
                        "error": record["error"],
                        "started_at": record["started_at"],
                        "finished_at": record["finished_at"],
                        "duration_seconds": record["duration_seconds"],
                        "artifacts": record["artifacts"],
                        "review": record["review"],
                        # B05 执行观测需要读取 B04 已持久化的测试与日志证据；
                        # 只读投影不改变任务状态，也不让前端重新执行 Agent。
                        "result": record["result"],
                        "attempts": attempts_by_task.get(task_id, []),
                    }
                )
            counts = {state: 0 for state in TASK_STATES}
            for item in view:
                counts[item["state"]] = counts.get(item["state"], 0) + 1
            return {
                "run_id": run["run_id"],
                "plan_id": run["plan_id"],
                "plan_version": run["plan_version"],
                "input_snapshot": run["input_snapshot"],
                "project_id": run["project_id"],
                "spec_version": run["spec_version"],
                "contract_version": run["contract_version"],
                "status": run["status"],
                "created_at": run["created_at"],
                "updated_at": run["updated_at"],
                "counts": counts,
                "completed": counts.get("SUCCEEDED", 0),
                "total": len(view),
                "events_count": len(self.store.list_run_events(run_id, limit=1_000_000)),
                "tasks": view,
                "topological_order": list(plan.get("topological_order") or []),
            }

    def dag(self, run_id: str) -> dict[str, Any]:
        snapshot = self.snapshot(run_id)
        nodes = [
            {
                "id": task["task_id"],
                "label": task["task_id"],
                "goal": task["goal"],
                "block_id": task["block_id"],
                "role_id": task["role_id"],
                "state": task["state"],
                "priority": task["priority"],
                "attempt_id": task["attempt_id"],
                "retry_count": task["retry_count"],
                "duration_seconds": task["duration_seconds"],
                "blocked_reason": task["blocked_reason"],
                "acceptance": task["acceptance"],
                "required_test_ids": task["required_test_ids"],
                "candidate_commit": task["candidate_commit"],
            }
            for task in snapshot["tasks"]
        ]
        edges = [
            {"from": dep, "to": task["task_id"], "revalidation": False}
            for task in snapshot["tasks"]
            for dep in task["depends_on"]
        ]
        return {
            "run_id": run_id,
            "plan_version": snapshot["plan_version"],
            "nodes": nodes,
            "edges": edges,
            "counts": snapshot["counts"],
            "topological_order": snapshot["topological_order"],
        }

    def events(self, run_id: str, *, after: int | None = None, limit: int = 500) -> dict[str, Any]:
        self._require_run(run_id)
        return {
            "run_id": run_id,
            "events": self.store.list_run_events(run_id, after_sequence=after, limit=limit),
        }

    # ---------- 回放一致性 ----------

    def replay(self, run_id: str) -> dict[str, Any]:
        """按事件重放状态，并与当前快照逐任务比对。"""
        with self._lock:
            snapshot = self.snapshot(run_id)
            events = self.store.list_run_events(run_id, limit=1_000_000)
            replayed: dict[str, str] = {task["task_id"]: "PENDING" for task in snapshot["tasks"]}
            for event in events:
                task_id = event.get("task_id")
                target = EVENT_TO_STATE.get(event["type"])
                if task_id in replayed and target is not None:
                    replayed[task_id] = target
            mismatches = [
                {"task_id": task["task_id"], "snapshot": task["state"], "replayed": replayed[task["task_id"]]}
                for task in snapshot["tasks"]
                if replayed[task["task_id"]] != task["state"]
            ]
            return {
                "run_id": run_id,
                "consistent": not mismatches,
                "mismatches": mismatches,
                "event_count": len(events),
                "last_sequence": events[-1]["sequence"] if events else 0,
                "replayed_states": replayed,
            }


def _duration_seconds(started_at: str | None, finished_at: str | None) -> float | None:
    if not started_at or not finished_at:
        return None
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        finish = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return round(max(0.0, (finish - start).total_seconds()), 3)


def _default_executor(request: dict[str, Any]) -> dict[str, Any]:
    """未配置执行器时的确定性占位实现（显式标注为 simulated）。"""
    return {
        "task_id": request["task_id"],
        "attempt_id": request["attempt_id"],
        "status": "SUCCEEDED",
        "artifacts": [{"path": f"artifacts/{request['task_id']}.json", "kind": "placeholder", "sha256": "0" * 64}],
        "candidate_commit": "0" * 7,
        "evidence_refs": [],
        "known_issues": [],
        "logs": [],
        "test_results": [
            {"test_id": test_id, "status": "PASS", "log_sha256": "0" * 64, "detail": "simulated"}
            for test_id in request.get("required_test_ids") or []
        ],
        "started_at": now_iso(),
        "finished_at": now_iso(),
        "duration_seconds": 0.0,
        "simulated": True,
    }


def _default_reviewer(payload: dict[str, Any]) -> dict[str, Any]:
    """未配置质量门时的确定性占位实现：只做最小自洽检查。"""
    result = payload.get("result") or {}
    task = payload.get("task") or {}
    findings = []
    if not result.get("candidate_commit"):
        findings.append({"code": "MISSING_COMMIT", "severity": "blocking", "message": "缺少 candidate_commit"})
    produced = {item.get("test_id") for item in result.get("test_results") or []}
    for test_id in task.get("required_test_ids") or []:
        if test_id not in produced:
            findings.append(
                {"code": "MISSING_TEST", "severity": "blocking", "message": f"缺少测试 {test_id}", "ref": test_id}
            )
    decision = "REJECT" if any(item["severity"] == "blocking" for item in findings) else "ACCEPT"
    return {"decision": decision, "findings": findings, "defects": [], "events": []}
