"""第二阶段闭环演示：规格 → 计划 → 编排 → 执行 → 验收 → 持久化 → DAG → 回放。

用法（仓库根目录）：

```powershell
python -m workflow_runtime pipeline                       # 成功闭环
python -m workflow_runtime pipeline --scenario retry      # 失败→有限重试→阻断
python -m workflow_runtime pipeline --scenario blocked     # 执行阻断传播
python -m workflow_runtime pipeline --scenario evidence    # 缺证据被质量门驳回
```

输出为 JSON（便于核对与截图），并在最后给出关键结论摘要。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from workflow_runtime.bootstrap import WorkflowStack  # noqa: E402

FIXTURES = REPO / "b01-spec-planner" / "docs" / "fixtures"

SCENARIOS = {
    "success": {
        "label": "全部任务验收通过",
        "execution": {},
    },
    "retry": {
        "label": "根任务持续失败 → 有限重试耗尽 → 下游阻塞",
        "execution": {"fail_tasks": {"T02": "注入编译失败"}},
    },
    "blocked": {
        "label": "执行被阻断 → 阻断向下游传播",
        "execution": {"block_tasks": {"T01"}},
    },
    "evidence": {
        "label": "缺少规定测试证据 → B06 驳回并登记缺陷",
        # 不依赖具体测试编号：让根任务的第一条规定测试被判为未执行。
        "execution": {"missing_first_test_tasks": ["T01", "T02"]},
    },
}


def run_pipeline(
    *,
    spec_path: Path,
    scenario: str,
    state_dir: Path,
    run_id: str | None = None,
    verbose: bool = True,
) -> dict[str, Any]:
    config = SCENARIOS[scenario]
    if state_dir.exists():
        shutil.rmtree(state_dir)
    stack = WorkflowStack(
        data_dir=state_dir,
        execution_mode="mock",
        scenario=config["execution"],
    )

    # 1. 规格 → 计划（B01）
    plan = stack.plan_from_spec_path(spec_path)
    # 2. 计划登记 + 运行创建（B02 + B03）
    loaded = stack.load_plan(plan)
    run = stack.start_run(loaded["plan_id"], run_id=run_id)
    # 3. 推进（B02 派发 → B04 执行 → B06 验收 → B03 落盘）
    outcome = stack.run(run["run_id"], max_steps=len(plan["tasks"]) * 3 + 20)
    snapshot = outcome["run"]
    # 4. 视图与一致性
    dag = stack.dag(run["run_id"])
    events = stack.events(run["run_id"], limit=100000)["events"]
    replay = stack.replay(run["run_id"])
    restored = stack.restore(run["run_id"])

    result = {
        "scenario": scenario,
        "label": config["label"],
        "spec": spec_path.name,
        "plan": {
            "plan_version": plan["plan_version"],
            "spec_version": plan["spec_version"],
            "contract_version": plan["contract_version"],
            "tasks": len(plan["tasks"]),
            "input_snapshot": plan["input_snapshot"],
        },
        "run": {
            "run_id": snapshot["run_id"],
            "status": snapshot["status"],
            "completed": snapshot["completed"],
            "total": snapshot["total"],
            "counts": {key: value for key, value in snapshot["counts"].items() if value},
            "events": len(events),
        },
        "dag": {
            "nodes": len(dag["nodes"]),
            "edges": len(dag["edges"]),
            "blocked": [node["id"] for node in dag["nodes"] if node["state"] == "BLOCKED"],
        },
        "tasks": [
            {
                "task_id": task["task_id"],
                "state": task["state"],
                "attempts": task["attempt_id"],
                "retry_count": task["retry_count"],
                "retry_budget": task["retry_budget"],
                "candidate_commit": task["candidate_commit"],
                "blocked_reason": task["blocked_reason"],
                "review": (task.get("review") or {}).get("decision"),
                "findings": [item["code"] for item in (task.get("review") or {}).get("findings") or []],
            }
            for task in snapshot["tasks"]
        ],
        "persistence": {
            "checkpoint_sequence": restored["checkpoint"]["last_event_sequence"],
            "completed_tasks": len(restored["completed_tasks"]),
            "resumable_tasks": restored["resumable_tasks"],
        },
        "replay": {
            "consistent": replay["consistent"],
            "mismatches": replay["mismatches"],
            "last_sequence": replay["last_sequence"],
        },
        "event_types": sorted({event["type"] for event in events}),
    }
    if verbose:
        _print_summary(result)
    return result


def _print_summary(result: dict[str, Any]) -> None:
    run = result["run"]
    print(f"\n=== {result['scenario']} · {result['label']} ===")
    print(f"规格   {result['spec']} → 计划 {result['plan']['plan_version']} "
          f"（{result['plan']['tasks']} 个任务）")
    print(f"运行   {run['run_id']} 状态 {run['status']}  "
          f"完成 {run['completed']}/{run['total']}  事件 {run['events']}")
    print(f"DAG    {result['dag']['nodes']} 节点 / {result['dag']['edges']} 边  "
          f"阻塞 {len(result['dag']['blocked'])} 个")
    print(f"恢复   检查点事件序号 {result['persistence']['checkpoint_sequence']}  "
          f"可恢复任务 {len(result['persistence']['resumable_tasks'])}")
    print(f"回放   一致 = {result['replay']['consistent']}")
    print(f"事件类型 {', '.join(result['event_types'])}")
    failed = [item for item in result["tasks"] if item["state"] in ("FAILED", "BLOCKED")]
    if failed:
        print("失败/阻塞任务：")
        for item in failed[:6]:
            detail = item["blocked_reason"] or ",".join(item["findings"]) or ""
            print(f"  - {item['task_id']} {item['state']} retry={item['retry_count']}/{item['retry_budget']} {detail}")
        if len(failed) > 6:
            print(f"  … 其余 {len(failed) - 6} 个")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="workflow_runtime", description="B01-B06 第二阶段闭环演示")
    parser.add_argument("command", nargs="?", default="pipeline", choices=["pipeline"])
    parser.add_argument("--scenario", default="success", choices=sorted(SCENARIOS))
    parser.add_argument("--spec", default=str(FIXTURES / "campus-lost-found.spec.json"))
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--json", action="store_true", help="只输出机器可读 JSON")
    args = parser.parse_args(argv)

    spec_path = Path(args.spec)
    if not spec_path.is_absolute():
        spec_path = REPO / spec_path
    state_dir = Path(args.state_dir) if args.state_dir else REPO / "work" / "demo-state"
    if not state_dir.is_absolute():
        state_dir = REPO / state_dir

    result = run_pipeline(
        spec_path=spec_path,
        scenario=args.scenario,
        state_dir=state_dir,
        run_id=args.run_id,
        verbose=not args.json,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["replay"]["consistent"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
