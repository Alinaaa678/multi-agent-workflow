"""闭环一致性检查：B01 计划 ↔ B02 编排 ↔ B03 状态 ↔ B04 执行 ↔ B06 验收。

供 CI 与本地复用：`python workflow_runtime/verify_contracts.py`
检查项：
1. B01 生成的计划在 B01 自己的校验器与 B06 的只读校验器下都通过。
2. B02 载入后：拓扑序与计划一致、任务数与计划一致。
3. 全流程跑完：全部 SUCCEEDED；事件回放与快照一致。
4. 关键事件字段绑定完整（task_id/attempt_id/plan_version/input_snapshot）。
5. 失败场景：重试次数不超过 retry_budget，下游被阻断，无任务 SUCCEEDED 越权。
6. 断点恢复：不重复执行已完成任务。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from workflow_runtime.bootstrap import WorkflowStack  # noqa: E402

FIXTURES = REPO / "b01-spec-planner" / "docs" / "fixtures"
SPECS = [
    FIXTURES / "campus-lost-found.spec.json",
    FIXTURES / "lab-equipment-loan.spec.md",
]

BINDING_EVENTS = {
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


class CheckFailure(AssertionError):
    pass


def check(condition: Any, message: str) -> None:
    if not condition:
        raise CheckFailure(message)


def fresh_stack(root: Path, scenario: dict | None = None) -> WorkflowStack:
    return WorkflowStack(
        data_dir=root,
        execution_mode="mock",
        scenario=scenario or {},
    )


def verify_success_path(spec_path: Path, root: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(dir=root) as temp:
        stack = fresh_stack(Path(temp))
        plan = stack.plan_from_spec_path(spec_path)
        # B01 自身校验
        from b01.plan_validator import validate_plan as b01_validate

        b01_result = b01_validate(plan)
        check(b01_result.ok, f"B01 校验失败: {b01_result.reasons}")
        check(plan["topological_order"] == b01_result.topological_order,
              "B01 拓扑序与重算结果不一致")
        # B06 只读计划校验
        gate_plan = stack.gate_module.verify_plan(plan)
        check(gate_plan["accepted"], f"B06 计划校验失败: {gate_plan['findings']}")

        loaded = stack.load_plan(plan)
        run = stack.start_run(loaded["plan_id"])
        run_id = run["run_id"]
        check(run["total"] == len(plan["tasks"]), "运行任务数与计划不一致")
        check(run["topological_order"] == plan["topological_order"], "运行拓扑序与计划不一致")

        outcome = stack.run(run_id, max_steps=len(plan["tasks"]) * 3 + 10)
        snapshot = outcome["run"]
        check(snapshot["status"] == "SUCCEEDED", f"运行未成功: {snapshot['status']} {snapshot['counts']}")
        check(snapshot["completed"] == snapshot["total"], "并非全部任务完成")

        # B03 持久化
        stored = stack.store.get_run(run_id)
        check(stored is not None, "B03 未保存运行")
        check(stored["plan_version"] == plan["plan_version"], "B03 计划版本不一致")
        events = stack.events(run_id, limit=100000)["events"]
        for event in events:
            if event["type"] in BINDING_EVENTS:
                check(event["task_id"], f"{event['type']} 缺 task_id")
                check(event["attempt_id"] is not None, f"{event['type']} 缺 attempt_id")
                check(event["plan_version"], f"{event['type']} 缺 plan_version")
                check(event["input_snapshot"], f"{event['type']} 缺 input_snapshot")
        replay = stack.replay(run_id)
        check(replay["consistent"], f"回放不一致: {replay['mismatches']}")

        restored = stack.restore(run_id)
        check(len(restored["completed_tasks"]) == snapshot["total"], "恢复视图完成数不对")
        check(not restored["resumable_tasks"], "已完成运行不应有可恢复任务")
        check(restored["checkpoint"]["last_event_sequence"] == replay["last_sequence"],
              "检查点事件序号与回放不一致")

        dag = stack.dag(run_id)
        check(len(dag["nodes"]) == snapshot["total"], "DAG 节点数不对")
        expected_edges = sum(len(task["depends_on"]) for task in plan["tasks"])
        check(len(dag["edges"]) == expected_edges, "DAG 边数不对")
        return {
            "spec": spec_path.name,
            "plan_version": plan["plan_version"],
            "tasks": len(plan["tasks"]),
            "events": len(events),
            "dag_edges": len(dag["edges"]),
            "status": snapshot["status"],
        }


def verify_failure_path(spec_path: Path, root: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(dir=root) as temp:
        # 让根任务失败，下游应被阻断
        probe = fresh_stack(Path(temp) / "probe")
        plan = probe.plan_from_spec_path(spec_path)
        roots = [task["task_id"] for task in plan["tasks"] if not task["depends_on"]]
        check(roots, "计划没有根任务")
        target = roots[0]
        budget = next(task["retry_budget"] for task in plan["tasks"] if task["task_id"] == target)

        stack = fresh_stack(Path(temp) / "run", {"fail_tasks": {target: "注入失败"}})
        loaded = stack.load_plan(plan)
        run = stack.start_run(loaded["plan_id"])
        outcome = stack.run(run["run_id"], max_steps=len(plan["tasks"]) * 3 + 10)
        snapshot = outcome["run"]
        failed = next(task for task in snapshot["tasks"] if task["task_id"] == target)
        check(failed["state"] == "FAILED", f"{target} 应 FAILED，实际 {failed['state']}")
        check(failed["retry_count"] == budget,
              f"重试次数应等于预算 {budget}，实际 {failed['retry_count']}")
        attempts = failed["attempts"]
        check(len(attempts) == budget + 1,
              f"尝试次数应为 {budget + 1}，实际 {len(attempts)}")

        downstream = [
            task
            for task in snapshot["tasks"]
            if target in (task["depends_on"] or [])
        ]
        for task in downstream:
            check(task["state"] == "BLOCKED", f"下游 {task['task_id']} 应 BLOCKED，实际 {task['state']}")
            check(task["blocked_reason"], f"下游 {task['task_id']} 缺 blocked_reason")

        # 阻断传播不能产生“越权成功”
        for task in snapshot["tasks"]:
            if task["state"] == "SUCCEEDED":
                check(
                    not any(dep in {failed["task_id"]} for dep in task["depends_on"]),
                    f"{task['task_id']} 依赖失败任务却 SUCCEEDED",
                )
        retry_events = [e for e in stack.events(run["run_id"], limit=100000)["events"]
                        if e["type"] == "TASK_RETRY_SCHEDULED"]
        check(len(retry_events) == budget,
              f"重试事件应有 {budget} 条，实际 {len(retry_events)}")
        return {
            "spec": spec_path.name,
            "failed_task": target,
            "retry_budget": budget,
            "attempts": len(attempts),
            "blocked_downstream": len(downstream),
            "status": snapshot["status"],
        }


def verify_missing_evidence(root: Path) -> dict[str, Any]:
    """缺规定测试证据时必须 REJECT，不能 ACCEPT。"""
    spec_path = FIXTURES / "lab-equipment-loan.spec.md"
    with tempfile.TemporaryDirectory(dir=root) as temp:
        probe = fresh_stack(Path(temp) / "probe")
        plan = probe.plan_from_spec_path(spec_path)
        target = plan["tasks"][0]["task_id"]
        missing = plan["tasks"][0]["required_test_ids"][:1]
        check(missing, "首个任务没有规定测试")
        stack = fresh_stack(Path(temp) / "run", {"missing_tests": {target: missing}})
        loaded = stack.load_plan(plan)
        run = stack.start_run(loaded["plan_id"])
        stack.run(run["run_id"], max_steps=len(plan["tasks"]) * 3 + 10)
        snapshot = stack.snapshot(run["run_id"])
        task = next(item for item in snapshot["tasks"] if item["task_id"] == target)
        check(task["state"] in ("FAILED", "BLOCKED"), f"{target} 缺证据却 {task['state']}")
        findings = (task.get("review") or {}).get("findings") or []
        codes = {item["code"] for item in findings}
        check("TEST_NOT_PASSED" in codes, f"缺少 TEST_NOT_PASSED 判定: {codes}")
        defect_events = [e for e in stack.events(run["run_id"], limit=100000)["events"]
                         if e["type"] == "DEFECT_CREATED"]
        check(defect_events, "缺证据未登记缺陷")
        return {"task": target, "state": task["state"], "codes": sorted(codes),
                "defects": len(defect_events)}


def verify_stale_versions(root: Path) -> dict[str, Any]:
    """版本/快照不一致必须 REVALIDATION_REQUIRED，而不是 ACCEPT。"""
    with tempfile.TemporaryDirectory(dir=root) as temp:
        stack = fresh_stack(Path(temp))
        plan = stack.plan_from_spec_path(FIXTURES / "lab-equipment-loan.spec.md")
        task = plan["tasks"][0]
        result = {
            "task_id": task["task_id"],
            "attempt_id": 1,
            "status": "SUCCEEDED",
            "artifacts": [{"path": "artifacts/x.md", "kind": "document", "sha256": "a" * 64}],
            "candidate_commit": "abcdef1234",
            "evidence_refs": ["ev-1"],
            "known_issues": [],
            "logs": [],
            "test_results": [
                {"test_id": test_id, "status": "PASS", "log_sha256": "b" * 64, "detail": ""}
                for test_id in task["required_test_ids"]
            ],
        }
        good_run = {
            "run_id": "TASK-RUN-VERIFY",
            "plan_version": plan["plan_version"],
            "input_snapshot": plan["input_snapshot"]["spec_sha256"],
            "spec_version": plan["spec_version"],
            "contract_version": plan["contract_version"],
        }
        base = {"plan": plan, "task": task, "result": result, "history": []}

        accepted = stack.gate_module.review_task({**base, "run": good_run})
        check(accepted["decision"] == "ACCEPT",
              f"一致版本应 ACCEPT，实际 {accepted['decision']}: {accepted['findings']}")

        stale_run = {**good_run, "plan_version": "PLAN-lab-equipment-loan-v9.9"}
        stale = stack.gate_module.review_task({**base, "run": stale_run})
        check(stale["decision"] == "REVALIDATION_REQUIRED",
              f"计划版本漂移应 REVALIDATION_REQUIRED，实际 {stale['decision']}")
        check(any(event["type"] == "TASK_REVALIDATION_REQUIRED" for event in stale["events"]),
              "缺少 TASK_REVALIDATION_REQUIRED 事件")

        snapshot_run = {**good_run, "input_snapshot": "f" * 64}
        drifted = stack.gate_module.review_task({**base, "run": snapshot_run})
        check(drifted["decision"] == "REVALIDATION_REQUIRED",
              f"输入快照漂移应 REVALIDATION_REQUIRED，实际 {drifted['decision']}")

        # 校验：B06 不得产生规定以外的行为
        moved = {event["type"] for event in stale["events"] + drifted["events"]}
        allowed = {
            "TASK_ACCEPTED", "TASK_REJECTED", "TASK_REVALIDATION_REQUIRED",
            "DEFECT_CREATED", "DEFECT_CLOSED",
        }
        check(moved <= allowed, f"质量门产生了越权事件: {sorted(moved - allowed)}")
        return {
            "accepted_decision": accepted["decision"],
            "stale_decision": stale["decision"],
            "snapshot_decision": drifted["decision"],
            "events": sorted(moved),
        }


def main() -> int:
    root = REPO / "work" / "_verify"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    results: list[dict[str, Any]] = []
    failures: list[str] = []
    for spec_path in SPECS:
        try:
            results.append(verify_success_path(spec_path, root))
        except CheckFailure as error:
            failures.append(f"success[{spec_path.name}]: {error}")
        try:
            results.append(verify_failure_path(spec_path, root))
        except CheckFailure as error:
            failures.append(f"failure[{spec_path.name}]: {error}")
    try:
        results.append(verify_missing_evidence(root))
    except CheckFailure as error:
        failures.append(f"missing-evidence: {error}")
    try:
        results.append(verify_stale_versions(root))
    except CheckFailure as error:
        failures.append(f"stale-versions: {error}")

    print(json.dumps({"results": results, "failures": failures}, ensure_ascii=False, indent=2))
    if failures:
        print(f"FAILED ({len(failures)} 项)", file=sys.stderr)
        return 1
    print(f"OK: {len(results)} 项闭环检查全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
