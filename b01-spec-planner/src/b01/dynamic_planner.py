"""B01 动态计划：由任意规格推导任务、DAG、拓扑序与追溯表。

与 `task_planner` 的区别：`task_planner` 生成固定的 T01—T11 参考任务包；
本模块按规格中的需求、设计条目与契约动态生成任务，任务数量与编号不固定。

生成规则（可预测、可复核，不做隐式猜测）：

1. 每个设计条目 → 一个实现任务（`B04`/`BE-01` 或按模块判定）。
2. 每个契约操作 → 一个契约任务。
3. 每个需求 → 一个验收任务（`QA-01`）。
4. 收尾：集成回归任务 + 交付任务。
5. 依赖按“模块 → 契约 → 验收 → 集成 → 交付”分层推导，保证无环。
"""

from __future__ import annotations

import copy
import hashlib
import hashlib
import heapq
import json
from datetime import datetime, timezone
from typing import Any, Iterable

from .spec_input import SpecDocument

PLAN_SCHEMA_VERSION = "b01.plan.v1"
DEFAULT_TIMEOUT = 600
DEFAULT_RETRY_BUDGET = 2
MAX_TIMEOUT = 3600

CAPABILITY_BY_MODULE = {
    "身份": "BE-01",
    "用户": "BE-01",
    "认证": "BE-01",
    "搜索": "BE-01",
    "接口": "BE-01",
    "后端": "BE-01",
    "数据库": "DB-01",
    "数据": "DB-01",
    "模型": "DB-01",
    "前端": "FE-01",
    "界面": "FE-01",
    "交互": "FE-01",
    "页面": "FE-01",
    "测试": "QA-01",
    "质量": "QA-01",
}

BLOCK_BY_CAPABILITY = {
    "orchestrator": "B01",
    "DB-01": "B03",
    "BE-01": "B04",
    "FE-01": "B05",
    "QA-01": "B06",
}

ROLE_BY_CAPABILITY = {
    "orchestrator": "SpecPlannerAgent",
    "DB-01": "BlackboardAgent",
    "BE-01": "AgentIntegrationAgent",
    "FE-01": "InterfaceAgent",
    "QA-01": "QualityConsensusAgent",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_hash(value: Any) -> str:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def topological_sort(tasks: Iterable[dict[str, Any]]) -> list[str]:
    """Kahn 算法；同层按 task_id 稳定排序。存在环或缺失依赖时抛出 ValueError。"""
    items = list(tasks)
    registry = {task["task_id"]: task for task in items}
    if len(registry) != len(items):
        raise ValueError("duplicate task_id")

    indegree = {tid: 0 for tid in registry}
    successors: dict[str, list[str]] = {tid: [] for tid in registry}
    for task in items:
        tid = task["task_id"]
        for dep in task.get("depends_on", []):
            if dep not in registry:
                raise ValueError(f"{tid}: missing dependency {dep}")
            if dep == tid:
                raise ValueError(f"{tid}: self dependency")
            indegree[tid] += 1
            successors[dep].append(tid)

    ready = [tid for tid, degree in indegree.items() if degree == 0]
    heapq.heapify(ready)
    order: list[str] = []
    while ready:
        tid = heapq.heappop(ready)
        order.append(tid)
        for child in successors[tid]:
            indegree[child] -= 1
            if indegree[child] == 0:
                heapq.heappush(ready, child)
    if len(order) != len(registry):
        raise ValueError("dependency cycle")
    return order


def _capability_for_module(module: str) -> str:
    for keyword, capability in CAPABILITY_BY_MODULE.items():
        if keyword in module:
            return capability
    return "BE-01"


def _slug(text: str) -> str:
    """生成用于领域路径和产物名称的 slug，保留 Unicode 领域名称。"""
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in str(text or ""))
    return "-".join(part for part in cleaned.split("-") if part).lower() or "item"


def _project_slug(text: str) -> str:
    """生成符合计划版本契约的 ASCII 项目标识。"""
    source = str(text or "").strip()
    ascii_part = "".join(ch if (ch.isascii() and ch.isalnum()) else "-" for ch in source)
    slug = "-".join(part for part in ascii_part.split("-") if part).lower()
    if source.isascii() and slug:
        return slug
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}" if slug else f"project-{digest}"


def _task(
    *,
    task_id: str,
    capability: str,
    goal: str,
    req_refs: list[str],
    design_refs: list[str],
    inputs: list[str],
    outputs: list[str],
    depends_on: list[str],
    write_scope: list[str],
    acceptance: list[str],
    required_test_ids: list[str],
    spec: SpecDocument,
    priority: str = "P0",
    timeout: int = DEFAULT_TIMEOUT,
    retry_budget: int = DEFAULT_RETRY_BUDGET,
    escalation_target: str = "B02 WorkflowAgent",
) -> dict[str, Any]:
    block_id = BLOCK_BY_CAPABILITY[capability]
    return {
        "role_id": ROLE_BY_CAPABILITY[capability],
        "block_id": block_id,
        "task_id": task_id,
        "project_id": spec.project_id,
        "req_refs": list(req_refs),
        "design_refs": list(design_refs),
        "spec_version": spec.spec_version,
        "contract_version": spec.contract_version,
        "goal": goal,
        "inputs": list(inputs),
        "outputs": list(outputs),
        "depends_on": list(depends_on),
        "capability": capability,
        "write_scope": list(write_scope),
        "acceptance": list(acceptance),
        "required_test_ids": list(required_test_ids),
        "priority": priority,
        "timeout": max(1, min(int(timeout), MAX_TIMEOUT)),
        "retry_budget": max(0, int(retry_budget)),
        "escalation_target": escalation_target,
    }


def _design_tasks(spec: SpecDocument, counter: "list[int]") -> tuple[list[dict[str, Any]], dict[str, str]]:
    """每个设计条目 → 实现任务；返回 (任务列表, design_id -> task_id)。"""
    tasks: list[dict[str, Any]] = []
    by_design: dict[str, str] = {}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in spec.design_entries:
        grouped.setdefault(entry["module"], []).append(entry)
    for module, entries in grouped.items():
        for entry in entries:
            counter[0] += 1
            task_id = f"T{counter[0]:02d}"
            by_design[entry["design_id"]] = task_id
            capability = _capability_for_module(module)
            tasks.append(
                _task(
                    task_id=task_id,
                    capability=capability,
                    goal=f"实现 {entry['design_id']}（{module}）：{entry['rule']}",
                    req_refs=entry["req_refs"],
                    design_refs=[entry["design_id"]],
                    inputs=[f"{spec.spec_version}", f"{spec.contract_version}", *entry["contract_refs"]],
                    outputs=[f"artifact-{_slug(entry['design_id'])}"],
                    depends_on=[],
                    write_scope=[f"src/{_slug(module)}/{_slug(entry['design_id'])}.*"],
                    acceptance=[entry["rule"]] or [f"{entry['design_id']} 行为符合设计条目"],
                    required_test_ids=entry["test_refs"] or [f"TC-{_slug(entry['design_id']).upper()}"],
                    spec=spec,
                    priority="P0",
                )
            )
    return tasks, by_design


def _contract_tasks(
    spec: SpecDocument,
    counter: "list[int]",
    implementation_tasks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """每个契约操作 → 一个契约任务，依赖覆盖其 source design 的实现任务。"""
    operations = spec.contract.get("operations") or []
    if not operations:
        return []
    # 建立 req -> 实现任务映射，用于推导依赖。
    by_req: dict[str, list[str]] = {}
    for task in implementation_tasks:
        for req in task["req_refs"]:
            by_req.setdefault(req, []).append(task["task_id"])
    tasks: list[dict[str, Any]] = []
    for operation in operations:
        counter[0] += 1
        task_id = f"T{counter[0]:02d}"
        # 契约操作声明的 required 字段决定它归哪一类需求：优先使用全部上游实现任务。
        depends = sorted({dep for deps in by_req.values() for dep in deps})
        req_refs = sorted({req for task in implementation_tasks for req in task["req_refs"]})
        tasks.append(
            _task(
                task_id=task_id,
                capability="BE-01",
                goal=(
                    f"实现契约操作 {operation['name']}"
                    f"（{operation['method']} {operation['path']}，认证={operation['authenticated']}）"
                ),
                req_refs=req_refs or [spec.requirements[0]["req_id"]],
                design_refs=[],
                inputs=[spec.contract_version, f"operation:{operation['name']}"],
                outputs=[f"contract-{_slug(operation['name'])}"],
                depends_on=depends,
                write_scope=[f"src/api/{_slug(operation['name'])}.*"],
                acceptance=[
                    f"{operation['method']} {operation['path']} 返回 {operation['status']}",
                    "必填字段缺失时返回字段级错误",
                    "响应字段与契约一致",
                ],
                required_test_ids=[f"TC-C-{_slug(operation['name']).upper()}"],
                spec=spec,
                priority="P0",
            )
        )
    return tasks


def _requirement_tasks(
    spec: SpecDocument,
    counter: "list[int]",
    implementation_tasks: list[dict[str, Any]],
    contract_tasks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """每个需求 → 一个验收任务，依赖覆盖该需求的实现与契约任务。"""
    upstream = implementation_tasks + contract_tasks
    tasks: list[dict[str, Any]] = []
    for requirement in spec.requirements:
        covering = [task for task in upstream if requirement["req_id"] in task["req_refs"]]
        depends = sorted({task["task_id"] for task in covering})
        counter[0] += 1
        task_id = f"T{counter[0]:02d}"
        acceptance = list(requirement["acceptance"]) or [f"{requirement['title']} 行为符合需求"]
        if requirement["constraints"]:
            acceptance.append("满足约束: " + "；".join(requirement["constraints"]))
        tasks.append(
            _task(
                task_id=task_id,
                capability="QA-01",
                goal=f"验收需求 {requirement['req_id']}：{requirement['title']}",
                req_refs=[requirement["req_id"]],
                design_refs=sorted(
                    {
                        entry["design_id"]
                        for entry in spec.design_entries
                        if requirement["req_id"] in entry["req_refs"]
                    }
                ),
                inputs=[spec.spec_version, requirement["req_id"], "候选产物"],
                outputs=[f"evidence-{_slug(requirement['req_id'])}"],
                depends_on=depends,
                write_scope=[f"evidence/{_slug(requirement['req_id'])}/*"],
                acceptance=acceptance,
                required_test_ids=[
                    f"TC-{requirement['req_id'].upper()}-POS",
                    f"TC-{requirement['req_id'].upper()}-NEG",
                ],
                spec=spec,
                priority="P0" if requirement["priority"] == "must" else "P1",
            )
        )
    return tasks


def _integration_and_delivery(
    spec: SpecDocument,
    counter: "list[int]",
    implementation_tasks: list[dict[str, Any]],
    contract_tasks: list[dict[str, Any]],
    acceptance_tasks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    integration_upstream = [task["task_id"] for task in implementation_tasks + contract_tasks + acceptance_tasks]
    counter[0] += 1
    integration_id = f"T{counter[0]:02d}"
    counter[0] += 1
    delivery_id = f"T{counter[0]:02d}"
    requirement_ids = spec.requirement_ids()
    return [
        _task(
            task_id=integration_id,
            capability="QA-01",
            goal="执行联调回归并登记缺陷",
            req_refs=requirement_ids,
            design_refs=spec.design_ids(),
            inputs=["全部候选产物", "验收证据", "环境说明"],
            outputs=[f"integration-report-{_slug(spec.project_id)}"],
            depends_on=sorted(set(integration_upstream)),
            write_scope=[f"evidence/{_slug(spec.project_id)}/integration/*"],
            acceptance=["规定用例全部通过", "无未关闭阻断缺陷", "报告对应同一组产物版本"],
            required_test_ids=[f"TC-REG-{_slug(spec.project_id).upper()}", "TC-FLOW-01"],
            spec=spec,
            priority="P0",
            timeout=1200,
        ),
        _task(
            task_id=delivery_id,
            capability="orchestrator",
            goal="汇总交付包、运行说明与需求追溯表",
            req_refs=requirement_ids,
            design_refs=spec.design_ids(),
            inputs=["集成报告", "全部任务产物", "追溯表"],
            outputs=[f"delivery-package-{_slug(spec.project_id)}"],
            depends_on=[integration_id],
            write_scope=[f"docs/delivery-{_slug(spec.project_id)}.md"],
            acceptance=["必需文件齐全", "可按说明复现", "版本对应已验收快照"],
            required_test_ids=["TC-DELIVERY-01"],
            spec=spec,
            priority="P0",
            escalation_target="B01 SpecPlannerAgent",
        ),
    ]


def build_tasks_from_spec(spec: SpecDocument) -> list[dict[str, Any]]:
    """按规格动态生成任务列表（顺序确定，不依赖字典遍历顺序）。"""
    counter = [0]
    implementation_tasks, _ = _design_tasks(spec, counter)
    contract_tasks = _contract_tasks(spec, counter, implementation_tasks)
    acceptance_tasks = _requirement_tasks(spec, counter, implementation_tasks, contract_tasks)
    closing = _integration_and_delivery(spec, counter, implementation_tasks, contract_tasks, acceptance_tasks)
    tasks = implementation_tasks + contract_tasks + acceptance_tasks + closing
    if not tasks:
        raise ValueError("规格未生成任何任务")
    return tasks


def next_plan_version(previous: str | None, project_id: str) -> str:
    """基于上一版本递增；无上一版本时从 v1.0 开始。"""
    if not previous:
        return f"PLAN-{_project_slug(project_id)}-v1.0"
    marker = "-v"
    if marker not in previous:
        return f"PLAN-{_project_slug(project_id)}-v1.0"
    head, _, tail = previous.rpartition(marker)
    major_text, _, minor_text = tail.partition(".")
    try:
        major = int(major_text)
        minor = int(minor_text or 0)
    except ValueError:
        return f"PLAN-{_project_slug(project_id)}-v1.0"
    minor += 1
    if minor >= 10:
        major += 1
        minor = 0
    return f"{head}{marker}{major}.{minor}"


def spec_input_snapshot(spec: SpecDocument) -> dict[str, Any]:
    contract_hash = canonical_hash(spec.contract)
    return {
        "spec_source": "inline" if spec.source_format == "json" else "file",
        "spec_format": spec.source_format,
        "spec_sha256": spec.source_sha256,
        "contract_sha256": contract_hash,
        "spec_version": spec.spec_version,
        "contract_version": spec.contract_version,
        "requirement_count": len(spec.requirements),
        "design_entry_count": len(spec.design_entries),
        "contract_operation_count": len(spec.contract.get("operations") or []),
        "schema_version": spec.schema_version,
    }


def build_plan_from_spec(
    spec: SpecDocument,
    *,
    plan_version: str | None = None,
    previous_plan_version: str | None = None,
    generated_by: str = "B01 SpecPlannerAgent dynamic planner",
    generated_at: str | None = None,
) -> dict[str, Any]:
    """生成符合 docs/TASK_ORCHESTRATION.md §2 的计划文档。

    `generated_at` 可显式传入，使**同一规格 + 同一版本**的计划可逐字节复现；
    CLI 生成入库 fixture 时应带上它，避免每次运行都产生仅时间戳不同的 diff。
    """
    tasks = build_tasks_from_spec(spec)
    order = topological_sort(tasks)
    version = plan_version or next_plan_version(previous_plan_version, spec.project_id)
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "project_id": spec.project_id,
        "title": spec.title,
        "spec_version": spec.spec_version,
        "contract_version": spec.contract_version,
        "plan_version": version,
        "generated_by": generated_by,
        "generated_at": generated_at or _now(),
        "required_requirements": spec.requirement_ids(),
        "tasks": copy.deepcopy(tasks),
        "topological_order": order,
        "input_snapshot": spec_input_snapshot(spec),
    }


def build_traceability(plan: dict[str, Any], spec: SpecDocument) -> dict[str, Any]:
    """生成 design_id → req_refs → contract_refs → test_refs → task_refs 追溯表。"""
    task_by_design: dict[str, list[str]] = {}
    task_by_req: dict[str, list[str]] = {}
    for task in plan["tasks"]:
        for design_id in task.get("design_refs", []):
            task_by_design.setdefault(design_id, []).append(task["task_id"])
        for req_id in task.get("req_refs", []):
            task_by_req.setdefault(req_id, []).append(task["task_id"])

    items = []
    for entry in spec.design_entries:
        items.append(
            {
                "design_id": entry["design_id"],
                "module": entry["module"],
                "req_refs": entry["req_refs"],
                "contract_refs": entry["contract_refs"],
                "test_refs": entry["test_refs"],
                "task_refs": sorted(set(task_by_design.get(entry["design_id"], []))),
                "rule": entry["rule"],
            }
        )
    requirements = []
    for requirement in spec.requirements:
        requirements.append(
            {
                "req_id": requirement["req_id"],
                "title": requirement["title"],
                "priority": requirement["priority"],
                "design_refs": sorted(
                    {
                        entry["design_id"]
                        for entry in spec.design_entries
                        if requirement["req_id"] in entry["req_refs"]
                    }
                ),
                "task_refs": sorted(set(task_by_req.get(requirement["req_id"], []))),
            }
        )
    covered = sorted({req for task in plan["tasks"] for req in task.get("req_refs", [])})
    return {
        "plan_version": plan["plan_version"],
        "spec_version": plan["spec_version"],
        "contract_version": plan["contract_version"],
        "project_id": plan["project_id"],
        "input_snapshot": plan["input_snapshot"],
        "format": "design_id -> req_refs -> contract_refs -> test_refs -> task_refs",
        "items": items,
        "requirements": requirements,
        "covered_requirements": covered,
        "uncovered_requirements": sorted(set(plan["required_requirements"]) - set(covered)),
    }
