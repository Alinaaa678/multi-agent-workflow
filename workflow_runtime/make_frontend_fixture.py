"""生成 B05 所需的真实 DAG 快照 fixture（离线演示用）。

产物：frontend/src/data/taskRunFixture.json
内容形状与 B02 `GET /api/task-runs/{run_id}`、`/dag`、`/events` 完全一致，
因此界面在「模拟数据」模式下展示的是**真实编排器产物**，而非手工编造。
"""

import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from workflow_runtime.bootstrap import WorkflowStack  # noqa: E402

OUT = REPO / "frontend" / "src" / "data" / "taskRunFixture.json"
STATE = REPO / "work" / "_fixture_state"


def _trim_snapshot(snapshot: dict) -> dict:
    """只保留界面需要的字段，避免把完整事件负载塞进仓库。"""
    keep = {
        "run_id", "plan_id", "plan_version", "input_snapshot", "project_id",
        "spec_version", "contract_version", "status", "created_at", "updated_at",
        "counts", "completed", "total", "events_count", "topological_order",
    }
    trimmed = {key: snapshot[key] for key in keep if key in snapshot}
    trimmed["tasks"] = [
        {
            "task_id": task["task_id"],
            "goal": task["goal"],
            "block_id": task["block_id"],
            "role_id": task["role_id"],
            "capability": task["capability"],
            "priority": task["priority"],
            "depends_on": task["depends_on"],
            "req_refs": task["req_refs"],
            "design_refs": task["design_refs"],
            "inputs": task["inputs"],
            "outputs": task["outputs"],
            "acceptance": task["acceptance"],
            "required_test_ids": task["required_test_ids"],
            "write_scope": task["write_scope"],
            "state": task["state"],
            "attempt_id": task["attempt_id"],
            "retry_count": task["retry_count"],
            "candidate_commit": task["candidate_commit"],
            "evidence_refs": task["evidence_refs"][:3],
            "known_issues": task["known_issues"][:3],
            "blocked_reason": task["blocked_reason"],
            "error": task["error"],
            "started_at": task["started_at"],
            "finished_at": task["finished_at"],
            "duration_seconds": task["duration_seconds"],
            "artifacts": task["artifacts"][:3],
            "review": {
                "decision": (task.get("review") or {}).get("decision"),
                "findings": ((task.get("review") or {}).get("findings") or [])[:5],
            },
            "attempts": [
                {k: attempt[k] for k in ("attempt_id", "retry_count", "state", "started_at", "finished_at", "reason") if k in attempt}
                for attempt in task["attempts"]
            ],
        }
        for task in snapshot["tasks"]
    ]
    return trimmed


def _trim_events(events: list[dict]) -> list[dict]:
    """事件压缩为「界面展示必需」字段。

    注意：这是**仅用于前端 fixture 的展示压缩**，不是契约变化。
    真实接口 `GET /api/task-runs/{run_id}/events` 仍返回完整绑定字段
    （plan_version / input_snapshot / candidate_commit / payload），
    该契约由 b02-workflow-agent/tests/test_task_api.py 覆盖。
    """
    trimmed = []
    for event in events:
        trimmed.append(
            {
                "event_id": event["event_id"],
                "sequence": event["sequence"],
                "type": event["type"],
                "task_id": event["task_id"],
                "attempt_id": event["attempt_id"],
                "candidate_commit": event["candidate_commit"],
                "producer": event["producer"],
                "occurred_at": event["occurred_at"],
                "payload": {},
            }
        )
    return trimmed


def build(run_id: str, scenario: dict, label: str):
    if STATE.exists():
        shutil.rmtree(STATE)
    stack = WorkflowStack(data_dir=STATE, execution_mode="mock", scenario=scenario)
    plan = stack.plan_from_spec_path(
        REPO / "b01-spec-planner" / "docs" / "fixtures" / "campus-lost-found.spec.json"
    )
    loaded = stack.load_plan(plan)
    stack.start_run(loaded["plan_id"], run_id=run_id)
    stack.run(run_id, max_steps=200)
    snapshot = stack.snapshot(run_id)
    dag = stack.dag(run_id)
    events = stack.events(run_id)["events"]
    return {
        "label": label,
        "run": _trim_snapshot(snapshot),
        "dag": dag,
        "events": _trim_events(events),
    }


fixtures = {
    "generated_at": "2026-11-18T00:00:00Z",
    "note": "由 workflow_runtime 真实编排器（B02+B03+B04+B06）生成，非手工编造。",
    "runs": {
        "success": build("TASK-RUN-FX-OK", {}, "全部任务验收通过"),
        "failure": build(
            "TASK-RUN-FX-FAIL",
            {"fail_tasks": {"T03": "编译失败：缺少依赖"}, "block_tasks": {"T05": "模拟服务不可用"}},
            "T03 失败耗尽重试预算、T05 阻断，下游被阻塞",
        ),
    },
}

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(fixtures, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("wrote", OUT, OUT.stat().st_size, "bytes")
for name, item in fixtures["runs"].items():
    counts = {k: v for k, v in item["run"]["counts"].items() if v}
    print(name, len(item["dag"]["nodes"]), "nodes", len(item["dag"]["edges"]), "edges", counts)
