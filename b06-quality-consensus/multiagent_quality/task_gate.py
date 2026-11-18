"""B06-D27/D28/D29/D30：开发任务质量门。

职责边界：
- 本模块**只返回判定与事件**，不写任何 B02 状态（不导入 B02/B03 状态模块）。
- `ACCEPT` 表示“证据满足规定门槛”，不表示“模型说了通过”。
- 判定优先级：blocking finding / 未关闭阻断缺陷 → REJECT；
  版本或证据过期 → REVALIDATION_REQUIRED；只有 major/minor → ACCEPT。

必查项（对应任务书 D27—D30）：
1. 任务字段完整性  2. 需求覆盖  3. 依赖环  4. 重复产物
5. 版本一致性      6. 日志哈希  7. 产物哈希  8. 测试编号  9. 提交引用
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable

RESULT_EVENTS = (
    "TASK_ACCEPTED",
    "TASK_REJECTED",
    "TASK_REVALIDATION_REQUIRED",
    "DEFECT_CREATED",
    "DEFECT_CLOSED",
)

SEVERITIES = ("blocking", "major", "minor")

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

COMMIT_PATTERN = re.compile(r"^[0-9a-f]{7,40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

DECISION_ACCEPT = "ACCEPT"
DECISION_REJECT = "REJECT"
DECISION_REVALIDATION = "REVALIDATION_REQUIRED"


def finding(code: str, message: str, *, severity: str = "blocking", ref: str | None = None) -> dict[str, Any]:
    if severity not in SEVERITIES:
        raise ValueError(f"unknown severity: {severity}")
    item: dict[str, Any] = {"code": code, "severity": severity, "message": message}
    if ref is not None:
        item["ref"] = ref
    return item


class _DefectBook:
    """本次判定内生成可追踪的缺陷编号。

    编号必须跨尝试唯一：同一任务的不同 attempt 会重复触发同一 finding，
    若沿用 `D-0001` 这类进程内序号，事件回放时无法区分新旧缺陷。
    """

    def __init__(self, task_id: str | None = None, attempt_id: int | None = None) -> None:
        self.items: list[dict[str, Any]] = []
        self._prefix = f"D-{task_id or 'TASK'}-A{attempt_id if attempt_id is not None else 0}"

    def open(
        self,
        task_id: str,
        code: str,
        message: str,
        severity: str = "blocking",
        ref: str | None = None,
    ) -> dict[str, Any]:
        defect = {
            "defect_id": f"{self._prefix}-{len(self.items) + 1:03d}",
            "task_id": task_id,
            "code": code,
            "severity": severity,
            "status": "open",
            "message": message,
        }
        if ref is not None:
            defect["ref"] = ref
        self.items.append(defect)
        return defect


def _check_task_fields(task: dict[str, Any], findings: list[dict[str, Any]]) -> None:
    """D27：任务字段完整性。"""
    for name in REQUIRED_TASK_FIELDS:
        if name not in task:
            findings.append(finding("MISSING_TASK_FIELD", f"任务缺少字段 {name}", ref=name))
    for name in ("req_refs", "design_refs", "outputs", "acceptance", "required_test_ids", "depends_on"):
        value = task.get(name)
        if name in task and not isinstance(value, list):
            findings.append(finding("INVALID_TASK_FIELD", f"{name} 必须是数组", ref=name))
    for name in ("task_id", "goal", "spec_version", "contract_version", "role_id", "block_id", "capability"):
        value = task.get(name)
        if name in task and (not isinstance(value, str) or not value.strip()):
            findings.append(finding("INVALID_TASK_FIELD", f"{name} 必须是非空字符串", ref=name))
    timeout = task.get("timeout")
    if "timeout" in task and (not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 1):
        findings.append(finding("INVALID_TASK_FIELD", "timeout 必须是正整数", ref="timeout"))
    budget = task.get("retry_budget")
    if "retry_budget" in task and (
        not isinstance(budget, int) or isinstance(budget, bool) or budget < 0
    ):
        findings.append(finding("INVALID_TASK_FIELD", "retry_budget 必须是非负整数", ref="retry_budget"))


def _check_requirement_coverage(plan: dict[str, Any], findings: list[dict[str, Any]]) -> None:
    """D28：需求覆盖。"""
    required = plan.get("required_requirements") or []
    if not isinstance(required, list) or not required:
        findings.append(finding("MISSING_REQUIREMENTS", "plan.required_requirements 必须是非空数组"))
        return
    covered = {
        ref
        for task in plan.get("tasks") or []
        if isinstance(task, dict)
        for ref in (task.get("req_refs") or [])
    }
    for req_id in sorted(set(required) - covered):
        findings.append(finding("UNCOVERED_REQUIREMENT", f"需求未被任何任务覆盖: {req_id}", ref=req_id))


def _check_dependency_cycle(plan: dict[str, Any], findings: list[dict[str, Any]]) -> None:
    """D28：依赖环与悬空依赖。"""
    tasks = [task for task in plan.get("tasks") or [] if isinstance(task, dict) and task.get("task_id")]
    registry = {str(task["task_id"]): task for task in tasks}
    indegree = {tid: 0 for tid in registry}
    successors: dict[str, list[str]] = {tid: [] for tid in registry}
    for tid, task in registry.items():
        for dep in task.get("depends_on") or []:
            if dep not in registry:
                findings.append(finding("UNKNOWN_DEPENDENCY", f"{tid} 依赖不存在的任务 {dep}", ref=tid))
                continue
            if dep == tid:
                findings.append(finding("SELF_DEPENDENCY", f"{tid} 依赖自身", ref=tid))
                continue
            indegree[tid] += 1
            successors[dep].append(tid)
    ready = [tid for tid, degree in indegree.items() if degree == 0]
    visited = 0
    while ready:
        tid = ready.pop()
        visited += 1
        for child in successors[tid]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
    if visited != len(registry):
        findings.append(finding("DEPENDENCY_CYCLE", "任务计划存在依赖环"))


def _check_duplicate_outputs(plan: dict[str, Any], findings: list[dict[str, Any]]) -> None:
    """D29：重复产物（两个任务声明同一产物）。"""
    owner: dict[str, str] = {}
    for task in plan.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        tid = task.get("task_id")
        for output in task.get("outputs") or []:
            if output in owner and owner[output] != tid:
                findings.append(
                    finding(
                        "DUPLICATE_OUTPUT",
                        f"产物 {output} 同时属于 {owner[output]} 与 {tid}",
                        ref=str(output),
                    )
                )
            else:
                owner[output] = str(tid)


def _check_version_consistency(
    plan: dict[str, Any],
    task: dict[str, Any],
    run: dict[str, Any],
    findings: list[dict[str, Any]],
    *,
    stale: list[dict[str, Any]],
) -> None:
    """D30：版本一致性；不匹配时进入 stale（→ REVALIDATION_REQUIRED）。"""
    pairs = [
        ("plan_version", plan.get("plan_version"), run.get("plan_version")),
        ("spec_version", plan.get("spec_version"), run.get("spec_version")),
        ("contract_version", plan.get("contract_version"), run.get("contract_version")),
        ("spec_version", task.get("spec_version"), plan.get("spec_version")),
        ("contract_version", task.get("contract_version"), plan.get("contract_version")),
    ]
    for name, left, right in pairs:
        if left is not None and right is not None and left != right:
            stale.append(
                finding(
                    "VERSION_MISMATCH",
                    f"{name} 不一致: {left!r} != {right!r}",
                    severity="blocking",
                    ref=name,
                )
            )


def _check_snapshot(plan: dict[str, Any], run: dict[str, Any], findings: list[dict[str, Any]], stale: list[dict[str, Any]]) -> None:
    declared = (plan.get("input_snapshot") or {}).get("spec_sha256")
    current = run.get("input_snapshot")
    if declared and current and declared != current:
        stale.append(
            finding(
                "STALE_INPUT_SNAPSHOT",
                f"运行输入快照与计划不一致: {current} != {declared}",
                ref="input_snapshot",
            )
        )


def _check_result_hash_and_tests(
    task: dict[str, Any],
    result: dict[str, Any],
    findings: list[dict[str, Any]],
    defects: _DefectBook,
    *,
    artifact_contents: dict[str, str] | None = None,
    log_contents: dict[str, str] | None = None,
) -> None:
    """D29/D30：产物哈希、日志哈希、测试编号与提交引用。"""
    task_id = str(task.get("task_id"))
    artifacts = result.get("artifacts") or []
    seen_paths: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            findings.append(finding("INVALID_ARTIFACT", "artifacts 条目必须是对象", ref=task_id))
            continue
        path = artifact.get("path")
        digest = artifact.get("sha256")
        if not isinstance(path, str) or not path:
            findings.append(finding("INVALID_ARTIFACT", "产物缺少 path", ref=task_id))
            continue
        if path in seen_paths:
            findings.append(finding("DUPLICATE_ARTIFACT", f"同一产物被重复提交: {path}", ref=path))
        seen_paths.add(path)
        if not isinstance(digest, str) or not SHA256_PATTERN.match(digest):
            findings.append(
                finding("INVALID_ARTIFACT_HASH", f"{path} 的 sha256 格式非法", ref=path)
            )
            continue
        if artifact_contents is not None and path in artifact_contents:
            actual = hashlib.sha256(artifact_contents[path].encode("utf-8")).hexdigest()
            if actual != digest:
                # 缺陷登记统一由 _open_defects_for_findings 完成，避免同一违规产生两条缺陷。
                findings.append(
                    finding("CHANGED_ARTIFACT", f"{path} 内容与声明哈希不一致", ref=path)
                )

    logs = result.get("logs") or []
    seen_logs: set[str] = set()
    for log in logs:
        if not isinstance(log, dict):
            findings.append(finding("INVALID_LOG", "logs 条目必须是对象", ref=task_id))
            continue
        name = log.get("name")
        digest = log.get("sha256")
        if not isinstance(name, str) or not name:
            findings.append(finding("INVALID_LOG", "日志缺少 name", ref=task_id))
            continue
        if name in seen_logs:
            findings.append(finding("DUPLICATE_LOG", f"同一日志被重复提交: {name}", ref=name))
        seen_logs.add(name)
        if not isinstance(digest, str) or not SHA256_PATTERN.match(digest):
            findings.append(finding("INVALID_LOG_HASH", f"{name} 的 sha256 格式非法", ref=name))
            continue
        if log_contents is not None and name in log_contents:
            actual = hashlib.sha256(log_contents[name].encode("utf-8")).hexdigest()
            if actual != digest:
                findings.append(finding("CHANGED_LOG", f"{name} 内容与声明哈希不一致", ref=name))

    produced: dict[str, str] = {}
    for case in result.get("test_results") or []:
        if not isinstance(case, dict):
            findings.append(finding("INVALID_TEST_RESULT", "test_results 条目必须是对象", ref=task_id))
            continue
        test_id = case.get("test_id")
        if not isinstance(test_id, str) or not test_id:
            findings.append(finding("INVALID_TEST_ID", "test_results 缺少 test_id", ref=task_id))
            continue
        if test_id in produced:
            findings.append(finding("DUPLICATE_TEST_RESULT", f"测试 {test_id} 重复提交", ref=test_id))
        produced[test_id] = str(case.get("status"))
    required = [test_id for test_id in task.get("required_test_ids") or []]
    for test_id in required:
        status = produced.get(test_id)
        if status is None:
            findings.append(finding("MISSING_TEST", f"缺少规定测试证据: {test_id}", ref=test_id))
        elif status != "PASS":
            findings.append(finding("TEST_NOT_PASSED", f"测试 {test_id} 未通过: {status}", ref=test_id))

    commit = result.get("candidate_commit")
    if not isinstance(commit, str) or not COMMIT_PATTERN.match(commit):
        # 缺陷登记统一由 _open_defects_for_findings 完成。
        findings.append(finding("INVALID_CANDIDATE_COMMIT", "candidate_commit 必须是合法的十六进制哈希", ref=task_id))


def _check_status(task: dict[str, Any], result: dict[str, Any], findings: list[dict[str, Any]]) -> None:
    """执行状态本身不是证据；非 SUCCEEDED 直接构成阻断 finding。"""
    status = result.get("status")
    if status == "SUCCEEDED":
        return
    if status == "BLOCKED":
        findings.append(
            finding(
                "EXECUTION_BLOCKED",
                f"执行被阻断: {result.get('error') or 'unknown'}",
                severity="blocking",
                ref=str(task.get("task_id")),
            )
        )
    else:
        findings.append(
            finding(
                "EXECUTION_FAILED",
                f"执行失败: {result.get('error') or status}",
                severity="blocking",
                ref=str(task.get("task_id")),
            )
        )


def _open_defects_for_findings(
    task: dict[str, Any], findings: list[dict[str, Any]], defects: _DefectBook
) -> None:
    """每条阻断 finding 对应一条可追踪缺陷；同一 (code, ref) 只登记一次。

    - 用 `(code, ref)` 而非完整 message 做去重键：同一违规若被两个检查器
      以不同措辞报告，仍应只产生一条缺陷。
    - `UNCLOSED_BLOCKING_DEFECT` 是对既有缺陷的回指，不再派生成新缺陷，
      否则会形成自反馈循环。
    """
    task_id = str(task.get("task_id"))
    registered = {(item["code"], item.get("ref")) for item in defects.items}
    for item in findings:
        if item["severity"] != "blocking":
            continue
        if item["code"] == "UNCLOSED_BLOCKING_DEFECT":
            continue
        key = (item["code"], item.get("ref"))
        if key in registered:
            continue
        registered.add(key)
        defects.open(task_id, item["code"], item["message"], item["severity"], item.get("ref"))


def review_task(
    payload: dict[str, Any],
    *,
    artifact_contents: dict[str, str] | None = None,
    log_contents: dict[str, str] | None = None,
) -> dict[str, Any]:
    """对一个任务候选结果执行质量门。输入见 docs/TASK_ORCHESTRATION.md §6。

    返回 `{decision, findings, defects, events}`；事件由 B02 消费后推进状态。
    """
    plan = payload.get("plan") or {}
    task = payload.get("task") or {}
    run = payload.get("run") or {}
    result = payload.get("result") or {}
    history = payload.get("history") or []

    findings: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    defects = _DefectBook(task.get("task_id"), result.get("attempt_id"))

    _check_task_fields(task, findings)
    _check_requirement_coverage(plan, findings)
    _check_dependency_cycle(plan, findings)
    _check_duplicate_outputs(plan, findings)
    _check_version_consistency(plan, task, run, findings, stale=stale)
    _check_snapshot(plan, run, findings, stale)
    _check_status(task, result, findings)
    _check_result_hash_and_tests(
        task,
        result,
        findings,
        defects,
        artifact_contents=artifact_contents,
        log_contents=log_contents,
    )

    # 历史缺陷处理：
    # - 若本次尝试**不再复现**同一 code，说明缺陷已被修复 → 关闭（DEFECT_CLOSED）。
    #   非阻断缺陷同样会关闭：它们的“修复”由本轮证据证明，不该永久悬空。
    # - 阻断缺陷若仍然复现，则保持开放并阻断本次验收。
    # 这样任务才能在重试预算内真正恢复，而不是被一次旧缺陷永久卡住。
    reproduced_codes = {item["code"] for item in findings}
    still_open: list[dict[str, Any]] = []
    closed_ids: list[str] = []
    for item in history:
        if not isinstance(item, dict) or item.get("status") != "open":
            continue
        code = str(item.get("code") or "")
        defect_id = item.get("defect_id")
        severity = item.get("severity")
        # 能判定“已修复”的两种情形：
        #   1) 有 code 且本次未再复现同一 code；
        #   2) 无 code 且本身非阻断：无法比对，本轮阻断项清零后即视为已处理。
        # 阻断缺陷缺少 code 时不得默认关闭——那是“无法证明已修复”，必须继续阻断。
        resolved = (bool(code) and code not in reproduced_codes) or (
            not code and severity != "blocking" and not any(f["severity"] == "blocking" for f in findings)
        )
        if resolved:
            if defect_id:
                closed_ids.append(str(defect_id))
            continue
        if severity != "blocking":
            # 非阻断且无法判定时既不阻断验收，也不重复登记。
            continue
        still_open.append(item)
        # 同一 code 已被本轮 finding 覆盖时不再追加“未关闭缺陷”，
        # 否则同一问题会在 findings 里出现两遍，掩盖真正的根因。
        if code and code in reproduced_codes:
            continue
        findings.append(
            finding(
                "UNCLOSED_BLOCKING_DEFECT",
                f"存在未关闭阻断缺陷: {defect_id or code}",
                ref=str(defect_id or ""),
            )
        )

    # 阻断物化：每条阻断 finding 都登记成缺陷，供 B02 落库并可关闭。
    _open_defects_for_findings(task, findings, defects)

    blocking = [item for item in findings if item["severity"] == "blocking"]
    if blocking:
        decision = DECISION_REJECT
    elif stale:
        decision = DECISION_REVALIDATION
    else:
        decision = DECISION_ACCEPT

    # 版本/快照不匹配属于“需要重验”，即使同时有 blocking 也优先按 REJECT 处理，
    # 但 REVALIDATION_REQUIRED 只在没有 blocking 时单独成立。
    if decision == DECISION_REVALIDATION:
        findings.extend(stale)
    elif stale:
        findings.extend(item for item in stale if item not in findings)

    events = _build_events(decision, task, result, findings, defects, stale, history, closed_ids)
    return {
        "decision": decision,
        "findings": findings,
        "defects": list(defects.items),
        "closed_defect_ids": closed_ids,
        "events": events,
        "task_id": task.get("task_id"),
        "attempt_id": result.get("attempt_id"),
    }


def _build_events(
    decision: str,
    task: dict[str, Any],
    result: dict[str, Any],
    findings: list[dict[str, Any]],
    defects: _DefectBook,
    stale: list[dict[str, Any]],
    history: list[dict[str, Any]],
    closed_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    task_id = task.get("task_id")
    attempt_id = result.get("attempt_id")
    commit = result.get("candidate_commit")
    events: list[dict[str, Any]] = []
    for defect in defects.items:
        events.append(
            {
                "type": "DEFECT_CREATED",
                "task_id": task_id,
                "attempt_id": attempt_id,
                "candidate_commit": commit,
                "payload": {
                    "defect_id": defect["defect_id"],
                    "code": defect["code"],
                    "severity": defect["severity"],
                    "message": defect["message"],
                },
            }
        )
    # 已修复的历史缺陷：先关闭，再给出本轮判定。
    for defect_id in closed_ids or []:
        events.append(
            {
                "type": "DEFECT_CLOSED",
                "task_id": task_id,
                "attempt_id": attempt_id,
                "candidate_commit": commit,
                "payload": {
                    "defect_id": defect_id,
                    # 兼容既有契约用词：关闭理由表示“本轮证据已复核，缺陷不再复现”。
                    "resolution": "evidence_revalidated",
                },
            }
        )
    if decision == DECISION_ACCEPT:
        if defects.items:
            # 本次验收仍有新缺陷时不得 ACCEPT（防御性断言）。
            raise ValueError("ACCEPT 判定不得同时产生新缺陷")
        events.append(
            {
                "type": "TASK_ACCEPTED",
                "task_id": task_id,
                "attempt_id": attempt_id,
                "candidate_commit": commit,
                "payload": {
                    "evidence_refs": result.get("evidence_refs") or [],
                    "findings": findings,
                    "closed_defect_ids": list(closed_ids or []),
                },
            }
        )
    elif decision == DECISION_REVALIDATION:
        events.append(
            {
                "type": "TASK_REVALIDATION_REQUIRED",
                "task_id": task_id,
                "attempt_id": attempt_id,
                "candidate_commit": commit,
                "payload": {"reasons": [item["message"] for item in stale]},
            }
        )
    else:
        events.append(
            {
                "type": "TASK_REJECTED",
                "task_id": task_id,
                "attempt_id": attempt_id,
                "candidate_commit": commit,
                "payload": {
                    "findings": findings,
                    "defect_ids": [item["defect_id"] for item in defects.items],
                },
            }
        )
    for event in events:
        if event["type"] not in RESULT_EVENTS:
            raise ValueError(f"质量门不得产生事件 {event['type']}")
    return events


def verify_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """整个计划级的只读质量校验（不含执行证据）。"""
    findings: list[dict[str, Any]] = []
    _check_requirement_coverage(plan, findings)
    _check_dependency_cycle(plan, findings)
    _check_duplicate_outputs(plan, findings)
    for task in plan.get("tasks") or []:
        if isinstance(task, dict):
            _check_task_fields(task, findings)
    blocking = [item for item in findings if item["severity"] == "blocking"]
    return {
        "mode": "read_only",
        "accepted": not blocking,
        "findings": findings,
        "task_count": len(plan.get("tasks") or []),
    }


def verify_artifacts(artifacts: Iterable[dict[str, Any]], contents: dict[str, str]) -> list[dict[str, Any]]:
    """独立校验一组产物的声明哈希（供离线复核使用）。"""
    results: list[dict[str, Any]] = []
    for artifact in artifacts:
        path = artifact.get("path")
        declared = artifact.get("sha256")
        body = contents.get(path) if isinstance(path, str) else None
        if body is None:
            results.append(finding("MISSING_ARTIFACT", f"缺少产物内容: {path}", ref=str(path)))
            continue
        actual = hashlib.sha256(body.encode("utf-8")).hexdigest()
        if actual != declared:
            results.append(finding("CHANGED_ARTIFACT", f"{path} 哈希不匹配", ref=str(path)))
    return results
