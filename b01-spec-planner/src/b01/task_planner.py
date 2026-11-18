"""B01 task_planner：从设计条目生成 T01—T11 标准任务包与无环任务图。

生成结果同时兼容 B06 的只读 plan validator：
plan.spec_version / plan.contract_version / plan.tasks[]，且每个 task 至少包含
task_id、goal、req_refs、depends_on、acceptance、required_test_ids、outputs。

本模块是“固定参考任务包”；从任意规格动态生成任务请使用 `dynamic_planner`。
"""

from __future__ import annotations

import copy
import heapq
from datetime import datetime, timezone
from typing import Any, Iterable

from .design import (
    CONTRACT_VERSION,
    DESIGN_ENTRIES,
    PLAN_VERSION,
    PROJECT_ID,
    REQUIRED_REQUIREMENTS,
    SPEC_VERSION,
)


def topological_sort(tasks: Iterable[dict[str, Any]]) -> list[str]:
    """Kahn 算法；同层按 task_id 稳定排序。存在环时抛出 ValueError。"""
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


def _task(
    *,
    task_id: str,
    block_id: str,
    role_id: str,
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
    priority: str = "P0",
    timeout: int = 600,
    retry_budget: int = 2,
    escalation_target: str = "B02 WorkflowAgent",
) -> dict[str, Any]:
    return {
        "role_id": role_id,
        "block_id": block_id,
        "task_id": task_id,
        "project_id": PROJECT_ID,
        "req_refs": req_refs,
        "design_refs": design_refs,
        "spec_version": SPEC_VERSION,
        "contract_version": CONTRACT_VERSION,
        "goal": goal,
        "inputs": inputs,
        "outputs": outputs,
        "depends_on": depends_on,
        "capability": capability,
        "write_scope": write_scope,
        "acceptance": acceptance,
        "required_test_ids": required_test_ids,
        "priority": priority,
        "timeout": timeout,
        "retry_budget": retry_budget,
        "escalation_target": escalation_target,
    }


def build_tasks() -> list[dict[str, Any]]:
    """按团队文档表 3、表 4、表 5 生成标准任务记录。"""
    all_reqs = list(REQUIRED_REQUIREMENTS)
    return [
        _task(
            task_id="T01",
            block_id="B01",
            role_id="SpecPlannerAgent",
            capability="orchestrator",
            goal="校验、登记并冻结已批准的 R1/S1/C1 开发基线",
            req_refs=all_reqs,
            design_refs=[entry["design_id"] for entry in DESIGN_ENTRIES],
            inputs=["R1-REFERENCE-v1.0", "S1-v1.0", "C1-v1.0", "六人确认记录", "契约测试清单"],
            outputs=["b01-baseline-manifest"],
            depends_on=[],
            write_scope=["b01-spec-planner/docs/baseline_manifest.json"],
            acceptance=[
                "审批与引用有效或显式标注为参考基线",
                "全部行为已定义",
                "LR-01—05 及规定用例无遗漏",
                "遇歧义退回设计而不自行补规则",
            ],
            required_test_ids=["TC-BASE-01", "TC-BASE-02"],
            timeout=300,
        ),
        _task(
            task_id="T02",
            block_id="B03",
            role_id="BlackboardAgent",
            capability="DB-01",
            goal="设计用户、失物、申请模型及迁移和测试数据",
            req_refs=all_reqs,
            design_refs=["DS-B03-01", "DS-B03-02", "DS-B03-07", "DS-B03-08"],
            inputs=["S1-v1.0", "C1-v1.0", "T01 基线清单"],
            outputs=["b03-campus-schema"],
            depends_on=["T01"],
            write_scope=["b03/b03_implementation/migrations/*", "b03/b03_implementation/campus_db.py"],
            acceptance=[
                "标识和关联有效",
                "lost_at 非空",
                "凭据字段受保护",
                "申请防重复约束可验证",
            ],
            required_test_ids=["TC-DATA-01", "TC-B03-07", "TC-B03-08"],
        ),
        _task(
            task_id="T03",
            block_id="B04",
            role_id="AgentIntegrationAgent",
            capability="BE-01",
            goal="开发注册登录、身份校验及本人资料与记录访问",
            req_refs=["LR-01", "LR-05"],
            design_refs=["DS-AUTH-01", "DS-USER-01"],
            inputs=["C1-v1.0", "T02 数据结构", "T01 基线清单"],
            outputs=["b04-auth-profile"],
            depends_on=["T02"],
            write_scope=["b04-agent-integration/src/b04/reference_backend/auth_profile.py"],
            acceptance=[
                "重复账号与错误凭据拒绝",
                "读写仅限本人",
                "敏感凭据不返回",
            ],
            required_test_ids=["TC-AUTH-01", "TC-AUTH-02", "TC-USER-01"],
        ),
        _task(
            task_id="T04",
            block_id="B04",
            role_id="AgentIntegrationAgent",
            capability="BE-01",
            goal="开发失物发布、搜索与详情处理逻辑",
            req_refs=["LR-02", "LR-03"],
            design_refs=["DS-LOST-01", "DS-LOST-02"],
            inputs=["C1-v1.0", "T02 模型", "T03 身份校验", "T01 基线清单"],
            outputs=["b04-lost-service"],
            depends_on=["T03"],
            write_scope=["b04-agent-integration/src/b04/reference_backend/lost.py"],
            acceptance=[
                "TC-P01/TC-P02/TC-P03 通过",
                "隐私用例通过",
                "缺失、空、非法时间拒绝且不写入",
                "缺规定证据不可成功",
            ],
            required_test_ids=["TC-P01", "TC-P02", "TC-P03", "TC-SEARCH-01"],
        ),
        _task(
            task_id="T05",
            block_id="B04",
            role_id="AgentIntegrationAgent",
            capability="BE-01",
            goal="开发认领申请提交与本人申请查询",
            req_refs=["LR-04"],
            design_refs=["DS-CLAIM-01"],
            inputs=["C1-v1.0", "T02 申请模型", "T04 失物访问能力"],
            outputs=["b04-claim-service"],
            depends_on=["T04"],
            write_scope=["b04-agent-integration/src/b04/reference_backend/claims.py"],
            acceptance=[
                "禁止认领本人信息和重复申请",
                "对象存在且待认领",
                "结果可由本人查询",
            ],
            required_test_ids=["TC-CLAIM-01", "TC-CLAIM-02"],
        ),
        _task(
            task_id="T06",
            block_id="B05",
            role_id="InterfaceAgent",
            capability="FE-01",
            goal="开发注册登录页、个人页与本人记录入口",
            req_refs=["LR-01", "LR-05"],
            design_refs=["DS-AUTH-01", "DS-USER-01"],
            inputs=["C1-v1.0", "LR-01", "LR-05", "交互要求", "T01 基线清单"],
            outputs=["b05-auth-ui"],
            depends_on=["T01"],
            write_scope=["frontend/src/components/*Auth*", "frontend/src/api/controlApi.ts"],
            acceptance=[
                "表单与身份状态正确",
                "仅修改允许字段",
                "接口字段与 C1 一致",
            ],
            required_test_ids=["TC-UI-01"],
            priority="P1",
        ),
        _task(
            task_id="T07",
            block_id="B05",
            role_id="InterfaceAgent",
            capability="FE-01",
            goal="开发发布表单、搜索列表与详情页",
            req_refs=["LR-02", "LR-03"],
            design_refs=["DS-LOST-01", "DS-LOST-02"],
            inputs=["C1-v1.0", "LR-02", "LR-03", "交互要求", "T01 基线清单"],
            outputs=["b05-lost-ui"],
            depends_on=["T01"],
            write_scope=["frontend/src/components/CaseSamples.tsx", "frontend/src/components/*Lost*"],
            acceptance=[
                "lost_at 必填并提示",
                "搜索与空结果可展示",
                "错误标识可转成明确提示",
            ],
            required_test_ids=["TC-UI-02"],
            priority="P1",
        ),
        _task(
            task_id="T08",
            block_id="B05",
            role_id="InterfaceAgent",
            capability="FE-01",
            goal="开发认领申请表单、结果提示与本人申请列表",
            req_refs=["LR-04"],
            design_refs=["DS-CLAIM-01"],
            inputs=["C1-v1.0", "LR-04", "交互要求", "T01 基线清单"],
            outputs=["b05-claim-ui"],
            depends_on=["T01"],
            write_scope=["frontend/src/components/*Claim*"],
            acceptance=[
                "申请内容可提交",
                "禁止与失败状态有提示",
                "不泄露他人申请信息",
            ],
            required_test_ids=["TC-UI-03"],
            priority="P1",
        ),
        _task(
            task_id="T09",
            block_id="B06",
            role_id="QualityConsensusAgent",
            capability="QA-01",
            goal="设计功能、接口、权限与边界测试用例及虚构数据",
            req_refs=all_reqs,
            design_refs=[entry["design_id"] for entry in DESIGN_ENTRIES if not entry["design_id"].startswith("DS-B03")],
            inputs=["S1-v1.0", "C1-v1.0", "五项业务规则", "T01 基线清单"],
            outputs=["b06-test-design"],
            depends_on=["T01"],
            write_scope=["b06-quality-consensus/tests/*", "b01-spec-planner/docs/traceability.json"],
            acceptance=[
                "每项 LR 至少有正常与失败检查",
                "包含直接调用接口的缺失时间测试 TC-P02",
                "覆盖全部错误映射",
            ],
            required_test_ids=["TC-P01", "TC-P02", "TC-P03", "TC-C01", "TC-FLOW-01"],
            escalation_target="B01 SpecPlannerAgent（交叉复核）",
        ),
        _task(
            task_id="T10",
            block_id="B06",
            role_id="QualityConsensusAgent",
            capability="QA-01",
            goal="真实接口替换模拟数据，执行联调回归并登记缺陷",
            req_refs=all_reqs,
            design_refs=[entry["design_id"] for entry in DESIGN_ENTRIES],
            inputs=["T03—T08 候选成果", "T09 用例", "环境说明", "T01 基线清单"],
            outputs=["b06-integration-report"],
            depends_on=["T03", "T04", "T05", "T06", "T07", "T08", "T09"],
            write_scope=["b06-quality-consensus/evidence/*"],
            acceptance=[
                "真实接口替换模拟数据",
                "规定用例通过",
                "无未关闭阻断缺陷",
                "报告对应同一组产物版本",
            ],
            required_test_ids=["TC-FLOW-01", "TC-REG-01", "TC-DELIVERY-01"],
            timeout=1200,
        ),
        _task(
            task_id="T11",
            block_id="B01",
            role_id="SpecPlannerAgent",
            capability="orchestrator",
            goal="汇总最终交付包、运行说明与需求追溯表",
            req_refs=all_reqs,
            design_refs=[entry["design_id"] for entry in DESIGN_ENTRIES],
            inputs=["T10 通过报告", "全任务产物引用", "T01 基线清单"],
            outputs=["b01-delivery-package"],
            depends_on=["T10"],
            write_scope=["b01-spec-planner/docs/delivery_manifest.md"],
            acceptance=[
                "必需文件齐全",
                "QA-01 可按说明复现",
                "版本对应已验收快照",
            ],
            required_test_ids=["TC-DELIVERY-01"],
            escalation_target="B01 SpecPlannerAgent",
        ),
    ]


REFERENCE_GENERATED_AT = "2026-11-18T00:00:00+00:00"


def build_plan(
    *,
    spec_version: str = SPEC_VERSION,
    contract_version: str = CONTRACT_VERSION,
    plan_version: str | None = None,
    tasks: list[dict[str, Any]] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    selected = copy.deepcopy(tasks) if tasks is not None else build_tasks()
    version = plan_version or PLAN_VERSION
    return {
        "schema_version": "b01.plan.v1",
        "project_id": PROJECT_ID,
        "title": "校园失物招领系统（参考固定任务包）",
        "spec_version": spec_version,
        "contract_version": contract_version,
        "plan_version": version,
        "generated_by": "B01 SpecPlannerAgent reference implementation",
        # 固定参考包必须可逐字节复现，因此使用稳定时间戳而非当前时间。
        "generated_at": generated_at or REFERENCE_GENERATED_AT,
        "required_requirements": list(REQUIRED_REQUIREMENTS),
        "tasks": selected,
        "topological_order": topological_sort(selected),
        "input_snapshot": _reference_snapshot(spec_version, contract_version, selected),
    }


def _reference_snapshot(spec_version: str, contract_version: str, tasks: list[dict[str, Any]]) -> dict[str, Any]:
    """固定参考包的输入快照；用于与通用动态计划保持同一结构。"""
    import hashlib
    import json

    payload = json.dumps(
        {"spec_version": spec_version, "contract_version": contract_version, "tasks": tasks},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "spec_source": "reference",
        "spec_format": "json",
        "spec_sha256": hashlib.sha256(payload).hexdigest(),
        "contract_sha256": hashlib.sha256(
            json.dumps({"contract_version": contract_version}, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "spec_version": spec_version,
        "contract_version": contract_version,
        "requirement_count": len(REQUIRED_REQUIREMENTS),
        "design_entry_count": len(DESIGN_ENTRIES),
        "contract_operation_count": 0,
        "schema_version": "b01.spec.reference",
    }


def build_traceability(plan: dict[str, Any]) -> dict[str, Any]:
    """生成 design_id → 需求/契约/测试/任务 的追溯映射。"""
    task_by_design: dict[str, list[str]] = {}
    task_by_req: dict[str, list[str]] = {}
    for task in plan["tasks"]:
        for design_id in task["design_refs"]:
            task_by_design.setdefault(design_id, []).append(task["task_id"])
        for req_id in task.get("req_refs", []):
            task_by_req.setdefault(req_id, []).append(task["task_id"])

    items = []
    for entry in DESIGN_ENTRIES:
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
    for req_id in REQUIRED_REQUIREMENTS:
        requirements.append(
            {
                "req_id": req_id,
                "title": req_id,
                "priority": "must",
                "design_refs": sorted(
                    entry["design_id"] for entry in DESIGN_ENTRIES if req_id in entry["req_refs"]
                ),
                "task_refs": sorted(set(task_by_req.get(req_id, []))),
            }
        )
    covered = sorted({req for task in plan["tasks"] for req in task.get("req_refs", [])})
    return {
        "plan_version": plan["plan_version"],
        "spec_version": plan["spec_version"],
        "contract_version": plan["contract_version"],
        "project_id": plan.get("project_id"),
        "input_snapshot": plan.get("input_snapshot"),
        "format": "design_id -> req_refs -> contract_refs -> test_refs -> task_refs",
        "items": items,
        "requirements": requirements,
        "covered_requirements": covered,
        "uncovered_requirements": sorted(set(plan["required_requirements"]) - set(covered)),
    }
