"""B01 计划校验（通用版）：结构、需求覆盖、产物归属、DAG 与版本一致性。

输出 `ok / reasons / warnings / topological_order / covered_requirements`。
与旧版的关键差异：
- 不再要求固定 T01—T11、固定 capability 集合或固定关键链；
- 额外校验 `plan_version` 格式、`input_snapshot`、跨块引用与追溯一致性；
- 保留 `validate_plan(plan, required_requirements)` 兼容签名。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .dynamic_planner import topological_sort

REQUIRED_PLAN_FIELDS = (
    "project_id",
    "spec_version",
    "contract_version",
    "plan_version",
    "required_requirements",
    "tasks",
    "topological_order",
)

REQUIRED_TASK_FIELDS = (
    "role_id",
    "block_id",
    "task_id",
    "project_id",
    "req_refs",
    "design_refs",
    "spec_version",
    "contract_version",
    "goal",
    "inputs",
    "outputs",
    "depends_on",
    "capability",
    "write_scope",
    "acceptance",
    "required_test_ids",
    "priority",
    "timeout",
    "retry_budget",
    "escalation_target",
)

STRING_FIELDS = (
    "role_id",
    "block_id",
    "task_id",
    "project_id",
    "spec_version",
    "contract_version",
    "goal",
    "capability",
    "priority",
    "escalation_target",
)

LIST_FIELDS = (
    "req_refs",
    "design_refs",
    "inputs",
    "outputs",
    "depends_on",
    "write_scope",
    "acceptance",
    "required_test_ids",
)

ALLOWED_PRIORITIES = {"P0", "P1", "P2"}
PLAN_VERSION_PATTERN = re.compile(r"^PLAN-[A-Za-z0-9._-]+-v[0-9]+\.[0-9]+$")
MAX_TIMEOUT = 3600


@dataclass
class ValidationReport:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    topological_order: list[str] = field(default_factory=list)
    covered_requirements: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reasons": self.reasons,
            "warnings": self.warnings,
            "topological_order": self.topological_order,
            "covered_requirements": self.covered_requirements,
        }


def _nonempty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _unique_strings(value: Any, *, allow_empty: bool = False) -> bool:
    if not isinstance(value, list):
        return False
    if not value and not allow_empty:
        return False
    seen: set[str] = set()
    for item in value:
        if not _nonempty_text(item):
            return False
        if item in seen:
            return False
        seen.add(item)
    return True


def validate_plan(
    plan: dict[str, Any],
    required_requirements: Iterable[str] | None = None,
) -> ValidationReport:
    reasons: list[str] = []
    warnings: list[str] = []
    report = ValidationReport(ok=False, reasons=reasons, warnings=warnings)

    if not isinstance(plan, dict):
        reasons.append("plan must be an object")
        return report

    for name in REQUIRED_PLAN_FIELDS:
        if name not in plan:
            reasons.append(f"plan: missing field {name}")

    required = list(required_requirements or plan.get("required_requirements") or [])
    if not _unique_strings(required):
        reasons.append("required_requirements must be a unique non-empty string list")
        required = list(dict.fromkeys(required)) if isinstance(required, list) else []

    spec_version = plan.get("spec_version")
    contract_version = plan.get("contract_version")
    plan_version = plan.get("plan_version")
    for label, value in (
        ("spec_version", spec_version),
        ("contract_version", contract_version),
        ("plan_version", plan_version),
        ("project_id", plan.get("project_id")),
    ):
        if not _nonempty_text(value):
            reasons.append(f"plan.{label} must be a non-empty string")
    if _nonempty_text(plan_version) and not PLAN_VERSION_PATTERN.match(plan_version):
        reasons.append(f"plan.plan_version must match PLAN-<project>-v<major>.<minor>, got {plan_version}")

    snapshot = plan.get("input_snapshot")
    if snapshot is not None:
        if not isinstance(snapshot, dict):
            reasons.append("plan.input_snapshot must be an object")
        else:
            for name in ("spec_sha256", "spec_version", "contract_version"):
                if not _nonempty_text(snapshot.get(name)):
                    reasons.append(f"plan.input_snapshot.{name} must be a non-empty string")
            if snapshot.get("spec_version") not in (None, spec_version):
                reasons.append("plan.input_snapshot.spec_version does not match plan.spec_version")
            if snapshot.get("contract_version") not in (None, contract_version):
                reasons.append("plan.input_snapshot.contract_version does not match plan.contract_version")

    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        reasons.append("plan.tasks must be a non-empty list")
        return report

    registry: dict[str, dict[str, Any]] = {}
    output_owner: dict[str, str] = {}
    covered: set[str] = set()

    for index, task in enumerate(tasks):
        label = f"tasks[{index}]"
        if not isinstance(task, dict):
            reasons.append(f"{label} must be an object")
            continue

        for name in REQUIRED_TASK_FIELDS:
            if name not in task:
                reasons.append(f"{label}: missing field {name}")

        tid = task.get("task_id")
        if not _nonempty_text(tid):
            reasons.append(f"{label}: invalid task_id")
            continue
        label = str(tid)
        if tid in registry:
            reasons.append(f"{label}: duplicate task_id")
            continue
        registry[tid] = task

        for name in STRING_FIELDS:
            if name in task and not _nonempty_text(task[name]):
                reasons.append(f"{label}: {name} must be a non-empty string")

        for name in LIST_FIELDS:
            if name not in task:
                continue
            allow_empty = name in ("depends_on", "design_refs")
            if not _unique_strings(task[name], allow_empty=allow_empty):
                suffix = "" if allow_empty else " and non-empty"
                reasons.append(f"{label}: {name} must be a unique string list{suffix}")

        if task.get("priority") not in ALLOWED_PRIORITIES:
            reasons.append(f"{label}: priority must be one of {sorted(ALLOWED_PRIORITIES)}")
        if task.get("project_id") != plan.get("project_id"):
            reasons.append(f"{label}: project_id does not match plan")
        for version_field, expected in (("spec_version", spec_version), ("contract_version", contract_version)):
            if task.get(version_field) != expected:
                reasons.append(f"{label}: {version_field} does not match plan")

        refs = task.get("req_refs") if isinstance(task.get("req_refs"), list) else []
        unknown = sorted(set(refs) - set(required))
        if unknown:
            reasons.append(f"{label}: unknown requirements {unknown}")
        covered.update(refs)

        timeout = task.get("timeout")
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= MAX_TIMEOUT:
            reasons.append(f"{label}: timeout must be an integer within 1..{MAX_TIMEOUT}")
        retry_budget = task.get("retry_budget")
        if not isinstance(retry_budget, int) or isinstance(retry_budget, bool) or retry_budget < 0:
            reasons.append(f"{label}: retry_budget must be a non-negative integer")

        for output in task.get("outputs") if isinstance(task.get("outputs"), list) else []:
            if output in output_owner:
                reasons.append(f"{label}: output has two owners: {output} (also {output_owner[output]})")
            else:
                output_owner[output] = label

    for req_id in required:
        if req_id not in covered:
            reasons.append(f"uncovered requirement: {req_id}")

    missing_dependency = False
    for tid, task in registry.items():
        for dep in task.get("depends_on") if isinstance(task.get("depends_on"), list) else []:
            if dep not in registry:
                reasons.append(f"{tid}: missing dependency {dep}")
                missing_dependency = True
            elif dep == tid:
                reasons.append(f"{tid}: self dependency")
                missing_dependency = True

    order: list[str] = []
    if not missing_dependency:
        try:
            order = topological_sort(registry.values())
        except ValueError as exc:
            reasons.append(str(exc))

    declared_order = plan.get("topological_order")
    if declared_order is not None:
        if not isinstance(declared_order, list) or sorted(declared_order) != sorted(registry):
            reasons.append("plan.topological_order must list every task exactly once")
        elif order and declared_order != order:
            warnings.append("plan.topological_order differs from recomputed deterministic order")

    report.ok = not reasons
    report.topological_order = order or (declared_order if isinstance(declared_order, list) else [])
    report.covered_requirements = sorted(covered)
    return report


def validate_reference_profile(plan: dict[str, Any]) -> ValidationReport:
    """校验“校园失物招领参考固定任务包”的额外约束。

    这些约束只属于 fixture（`task_planner.build_plan()` 的 T01—T11 参考包），
    不适用于任意规格的通用计划；通用计划请使用 `validate_plan`。
    """
    reasons: list[str] = []
    warnings: list[str] = []
    report = ValidationReport(ok=False, reasons=reasons, warnings=warnings)
    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        reasons.append("plan.tasks must be a non-empty list")
        return report
    registry = {task.get("task_id"): task for task in tasks if isinstance(task, dict)}

    order = validate_plan(plan).topological_order
    if order:
        positions = {tid: index for index, tid in enumerate(order)}
        for left, right in zip(REFERENCE_CRITICAL_CHAIN, REFERENCE_CRITICAL_CHAIN[1:]):
            if left in positions and right in positions and positions[left] >= positions[right]:
                reasons.append(f"critical chain violated: {left} must precede {right}")

    t01 = registry.get("T01")
    if t01 and t01.get("depends_on") != []:
        reasons.append("T01: must have no dependencies")

    t04 = registry.get("T04")
    if t04:
        t04_tests = set(t04.get("required_test_ids") or [])
        for test_id in ("TC-P01", "TC-P02", "TC-P03"):
            if test_id not in t04_tests:
                reasons.append(f"T04: required test missing {test_id}")

    t09 = registry.get("T09")
    if t09 and "TC-P02" not in set(t09.get("required_test_ids") or []):
        reasons.append("T09: required direct-call test TC-P02 missing")

    t10 = registry.get("T10")
    if t10:
        expected_deps = {"T03", "T04", "T05", "T06", "T07", "T08", "T09"}
        missing = sorted(expected_deps - set(t10.get("depends_on") or []))
        if missing:
            reasons.append(f"T10: missing dependencies {missing}")

    report.ok = not reasons
    report.topological_order = order
    return report


REFERENCE_CRITICAL_CHAIN = ["T01", "T02", "T03", "T04", "T05", "T10", "T11"]


def validate_traceability(plan: dict[str, Any], traceability: dict[str, Any]) -> ValidationReport:
    """校验追溯表与计划一致：任务引用存在、需求覆盖一致。"""
    reasons: list[str] = []
    warnings: list[str] = []
    task_ids = {task["task_id"] for task in plan.get("tasks", []) if isinstance(task, dict)}
    items = traceability.get("items")
    if not isinstance(items, list):
        reasons.append("traceability.items must be a list")
        items = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            reasons.append(f"traceability.items[{index}] must be an object")
            continue
        for ref in item.get("task_refs") or []:
            if ref not in task_ids:
                reasons.append(f"traceability.items[{index}]: unknown task_ref {ref}")
    if traceability.get("plan_version") != plan.get("plan_version"):
        reasons.append("traceability.plan_version does not match plan")
    uncovered = traceability.get("uncovered_requirements")
    if uncovered:
        reasons.append(f"traceability.uncovered_requirements must be empty, got {uncovered}")
    if traceability.get("spec_version") != plan.get("spec_version"):
        reasons.append("traceability.spec_version does not match plan")
    report = ValidationReport(ok=not reasons, reasons=reasons, warnings=warnings)
    report.covered_requirements = list(traceability.get("covered_requirements") or [])
    return report
