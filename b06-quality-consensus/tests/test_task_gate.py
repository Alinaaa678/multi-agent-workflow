"""B06-D27/D28/D29/D30: task quality gate (docs/TASK_ORCHESTRATION.md §6).

The gate is a pure decision function: it returns ``decision``/``findings``/
``defects``/``events`` and never touches B02 state. Every blocking finding must
materialise as a ``DEFECT_CREATED`` event, and the only event types it may emit
are ``TASK_ACCEPTED``, ``TASK_REJECTED``, ``TASK_REVALIDATION_REQUIRED``,
``DEFECT_CREATED`` and ``DEFECT_CLOSED``.
"""

from __future__ import annotations

import copy
import hashlib

import pytest

from multiagent_quality.task_gate import (
    DECISION_ACCEPT,
    DECISION_REJECT,
    DECISION_REVALIDATION,
    RESULT_EVENTS,
    SEVERITIES,
    review_task,
    verify_artifacts,
    verify_plan,
)

SPEC_SHA = "a" * 64
ARTIFACT_BODY = "artifact body"
ARTIFACT_SHA = hashlib.sha256(ARTIFACT_BODY.encode("utf-8")).hexdigest()
LOG_BODY = "log body"
LOG_SHA = hashlib.sha256(LOG_BODY.encode("utf-8")).hexdigest()
COMMIT = "abc1234"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def make_task(task_id: str = "T01", **overrides) -> dict:
    """One §2 task entry with every mandatory field present."""
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
        "depends_on": [],
        "capability": "BE-01",
        "write_scope": ["src/auth"],
        "acceptance": [f"{task_id} acceptance"],
        "required_test_ids": ["TC-1"],
        "priority": "P0",
        "timeout": 60,
        "retry_budget": 0,
        "escalation_target": "B02 WorkflowAgent",
    }
    task.update(overrides)
    return task


def make_plan(tasks: list[dict] | None = None, **overrides) -> dict:
    tasks = tasks if tasks is not None else [make_task()]
    plan = {
        "project_id": "demo",
        "spec_version": "S1-v1.0",
        "contract_version": "C1-v1.0",
        "plan_version": "PLAN-demo-v1.0",
        "required_requirements": ["R1"],
        "tasks": tasks,
        "topological_order": [task["task_id"] for task in tasks],
        "input_snapshot": {"spec_sha256": SPEC_SHA},
    }
    plan.update(overrides)
    return plan


def make_run(**overrides) -> dict:
    run = {
        "run_id": "TASK-RUN-TEST",
        "plan_version": "PLAN-demo-v1.0",
        "input_snapshot": SPEC_SHA,
        "spec_version": "S1-v1.0",
        "contract_version": "C1-v1.0",
    }
    run.update(overrides)
    return run


def make_result(**overrides) -> dict:
    """A §5 TaskExecutionResult that satisfies every gate check."""
    result = {
        "task_id": "T01",
        "attempt_id": 1,
        "status": "SUCCEEDED",
        "artifacts": [{"path": "artifacts/T01.md", "kind": "document", "sha256": ARTIFACT_SHA}],
        "candidate_commit": COMMIT,
        "evidence_refs": ["evidence:1"],
        "known_issues": [],
        "logs": [{"name": "logs/T01.log", "sha256": LOG_SHA}],
        "test_results": [{"test_id": "TC-1", "status": "PASS", "log_sha256": LOG_SHA}],
        "started_at": "2026-11-18T00:00:00Z",
        "finished_at": "2026-11-18T00:00:01Z",
        "duration_seconds": 1.0,
    }
    result.update(overrides)
    return result


def make_payload(**overrides) -> dict:
    payload = {
        "plan": make_plan(),
        "task": make_task(),
        "run": make_run(),
        "result": make_result(),
        "history": [],
    }
    payload.update(overrides)
    return payload


def codes(report: dict) -> list[str]:
    return [item["code"] for item in report["findings"]]


def event_types(report: dict) -> list[str]:
    return [event["type"] for event in report["events"]]


def blocking_codes(report: dict) -> list[str]:
    return [item["code"] for item in report["findings"] if item["severity"] == "blocking"]


def assert_only_contract_events(report: dict) -> None:
    for event in report["events"]:
        assert event["type"] in RESULT_EVENTS
        assert event["task_id"] == "T01"
        assert event["attempt_id"] == 1


# --------------------------------------------------------------------------- #
# 1. Clean acceptance
# --------------------------------------------------------------------------- #


def test_clean_result_is_accepted_without_defects():
    report = review_task(make_payload())

    assert report["decision"] == DECISION_ACCEPT
    assert report["findings"] == []
    assert report["defects"] == []
    assert event_types(report) == ["TASK_ACCEPTED"]
    assert_only_contract_events(report)

    accepted = report["events"][0]
    assert accepted["candidate_commit"] == COMMIT
    assert accepted["payload"]["evidence_refs"] == ["evidence:1"]
    assert accepted["payload"]["findings"] == []


def test_clean_accept_does_not_emit_defect_closed_without_history():
    report = review_task(make_payload(history=[]))
    assert "DEFECT_CLOSED" not in event_types(report)


def test_clean_accept_emits_defect_closed_when_history_had_an_open_non_blocking_defect():
    history = [
        {"task_id": "T01", "attempt_id": 1, "status": "open", "severity": "minor", "defect_id": "D-OLD-1"}
    ]
    report = review_task(make_payload(history=history))

    assert report["decision"] == DECISION_ACCEPT
    assert event_types(report) == ["DEFECT_CLOSED", "TASK_ACCEPTED"]
    closed = report["events"][0]
    assert closed["candidate_commit"] == COMMIT
    assert closed["payload"]["resolution"] == "evidence_revalidated"
    assert_only_contract_events(report)


def test_clean_accept_emits_defect_closed_when_history_defect_is_only_closed_by_status():
    """Only *open* history defects are closed; already-closed ones are silent."""
    history = [{"task_id": "T01", "attempt_id": 1, "status": "closed", "severity": "blocking", "defect_id": "D-OLD-1"}]
    report = review_task(make_payload(history=history))
    assert report["decision"] == DECISION_ACCEPT
    assert event_types(report) == ["TASK_ACCEPTED"]


def test_accept_with_artifact_and_log_contents_matching_is_clean():
    report = review_task(
        make_payload(),
        artifact_contents={"artifacts/T01.md": ARTIFACT_BODY},
        log_contents={"logs/T01.log": LOG_BODY},
    )
    assert report["decision"] == DECISION_ACCEPT
    assert report["findings"] == []


def test_report_echoes_task_and_attempt_identity():
    report = review_task(make_payload(result=make_result(attempt_id=4)))
    assert report["task_id"] == "T01"
    assert report["attempt_id"] == 4


# --------------------------------------------------------------------------- #
# 2. Blocking checks -> REJECT + DEFECT_CREATED
# --------------------------------------------------------------------------- #


def test_missing_task_field_is_rejected_with_a_defect():
    task = make_task()
    task.pop("capability")
    report = review_task(make_payload(task=task))

    assert report["decision"] == DECISION_REJECT
    assert "MISSING_TASK_FIELD" in blocking_codes(report)
    assert [item["ref"] for item in report["findings"] if item["code"] == "MISSING_TASK_FIELD"] == ["capability"]
    assert len(report["defects"]) == 1
    assert report["defects"][0]["code"] == "MISSING_TASK_FIELD"
    assert report["defects"][0]["status"] == "open"
    assert report["defects"][0]["severity"] == "blocking"
    assert event_types(report) == ["DEFECT_CREATED", "TASK_REJECTED"]
    assert_only_contract_events(report)
    assert report["events"][1]["payload"]["defect_ids"] == [report["defects"][0]["defect_id"]]


def test_invalid_task_field_types_are_rejected():
    task = make_task(timeout=0, retry_budget=-1, req_refs="R1", goal="")
    report = review_task(make_payload(task=task))
    assert report["decision"] == DECISION_REJECT
    assert "INVALID_TASK_FIELD" in blocking_codes(report)


def test_uncovered_requirement_is_rejected_with_a_defect():
    plan = make_plan(required_requirements=["R1", "R2"])
    report = review_task(make_payload(plan=plan))

    assert report["decision"] == DECISION_REJECT
    assert "UNCOVERED_REQUIREMENT" in blocking_codes(report)
    uncovered = [item for item in report["findings"] if item["code"] == "UNCOVERED_REQUIREMENT"]
    assert [item["ref"] for item in uncovered] == ["R2"]
    assert [defect["code"] for defect in report["defects"]] == ["UNCOVERED_REQUIREMENT"]
    assert "DEFECT_CREATED" in event_types(report)


def test_empty_required_requirements_is_rejected():
    report = review_task(make_payload(plan=make_plan(required_requirements=[])))
    assert report["decision"] == DECISION_REJECT
    assert "MISSING_REQUIREMENTS" in blocking_codes(report)


def test_dependency_cycle_is_rejected_with_a_defect():
    first = make_task("T01", depends_on=["T02"])
    second = make_task("T02", depends_on=["T01"])
    report = review_task(make_payload(plan=make_plan([first, second]), task=first))

    assert report["decision"] == DECISION_REJECT
    assert "DEPENDENCY_CYCLE" in blocking_codes(report)
    assert [defect["code"] for defect in report["defects"]] == ["DEPENDENCY_CYCLE"]
    assert "DEFECT_CREATED" in event_types(report)


def test_unknown_dependency_is_rejected():
    task = make_task("T01", depends_on=["T99"])
    report = review_task(make_payload(plan=make_plan([task]), task=task))
    assert report["decision"] == DECISION_REJECT
    assert "UNKNOWN_DEPENDENCY" in blocking_codes(report)


def test_self_dependency_is_rejected():
    task = make_task("T01", depends_on=["T01"])
    report = review_task(make_payload(plan=make_plan([task]), task=task))
    assert report["decision"] == DECISION_REJECT
    assert "SELF_DEPENDENCY" in blocking_codes(report)


def test_duplicate_output_owner_is_rejected_with_a_defect():
    first = make_task("T01", outputs=["shared-artifact"])
    second = make_task("T02", outputs=["shared-artifact"], depends_on=["T01"])
    report = review_task(make_payload(plan=make_plan([first, second]), task=first))

    assert report["decision"] == DECISION_REJECT
    assert "DUPLICATE_OUTPUT" in blocking_codes(report)
    duplicate = [item for item in report["findings"] if item["code"] == "DUPLICATE_OUTPUT"]
    assert [item["ref"] for item in duplicate] == ["shared-artifact"]
    assert [defect["code"] for defect in report["defects"]] == ["DUPLICATE_OUTPUT"]
    assert "DEFECT_CREATED" in event_types(report)


def test_missing_required_test_evidence_is_rejected():
    task = make_task(required_test_ids=["TC-1", "TC-2"])
    report = review_task(make_payload(task=task))

    assert report["decision"] == DECISION_REJECT
    assert "MISSING_TEST" in blocking_codes(report)
    missing = [item for item in report["findings"] if item["code"] == "MISSING_TEST"]
    assert [item["ref"] for item in missing] == ["TC-2"]
    assert [defect["code"] for defect in report["defects"]] == ["MISSING_TEST"]
    assert event_types(report) == ["DEFECT_CREATED", "TASK_REJECTED"]


def test_failing_required_test_is_rejected():
    result = make_result(test_results=[{"test_id": "TC-1", "status": "FAIL", "log_sha256": LOG_SHA}])
    report = review_task(make_payload(result=result))

    assert report["decision"] == DECISION_REJECT
    assert "TEST_NOT_PASSED" in blocking_codes(report)
    assert [defect["code"] for defect in report["defects"]] == ["TEST_NOT_PASSED"]
    assert "FAIL" in report["findings"][0]["message"]


def test_empty_test_results_are_rejected_when_tests_are_required():
    report = review_task(make_payload(result=make_result(test_results=[])))
    assert report["decision"] == DECISION_REJECT
    assert "MISSING_TEST" in blocking_codes(report)


def test_invalid_candidate_commit_is_rejected():
    for commit in (None, "", "abc", "xyz1234", "ABC1234", "abc123g"):
        report = review_task(make_payload(result=make_result(candidate_commit=commit)))
        assert report["decision"] == DECISION_REJECT
        assert "INVALID_CANDIDATE_COMMIT" in blocking_codes(report)
        assert "DEFECT_CREATED" in event_types(report)


def test_valid_short_and_long_hex_commits_are_accepted():
    for commit in ("abc1234", "0" * 40, "abcdef1234567890"):
        report = review_task(make_payload(result=make_result(candidate_commit=commit)))
        assert report["decision"] == DECISION_ACCEPT, (commit, codes(report))


def test_changed_artifact_hash_is_rejected_via_artifact_contents():
    report = review_task(
        make_payload(),
        artifact_contents={"artifacts/T01.md": "tampered body"},
    )

    assert report["decision"] == DECISION_REJECT
    assert "CHANGED_ARTIFACT" in blocking_codes(report)
    assert [defect["code"] for defect in report["defects"]] == ["CHANGED_ARTIFACT"]
    assert event_types(report) == ["DEFECT_CREATED", "TASK_REJECTED"]


def test_invalid_artifact_hash_format_is_rejected():
    result = make_result(artifacts=[{"path": "artifacts/T01.md", "sha256": "zz"}])
    report = review_task(make_payload(result=result))
    assert report["decision"] == DECISION_REJECT
    assert "INVALID_ARTIFACT_HASH" in blocking_codes(report)


def test_duplicate_artifact_path_is_rejected():
    artifact = {"path": "artifacts/T01.md", "kind": "document", "sha256": ARTIFACT_SHA}
    report = review_task(make_payload(result=make_result(artifacts=[artifact, dict(artifact)])))
    assert report["decision"] == DECISION_REJECT
    assert "DUPLICATE_ARTIFACT" in blocking_codes(report)


def test_changed_log_hash_is_rejected_via_log_contents():
    report = review_task(
        make_payload(),
        log_contents={"logs/T01.log": "tampered log"},
    )

    assert report["decision"] == DECISION_REJECT
    assert "CHANGED_LOG" in blocking_codes(report)
    assert "DEFECT_CREATED" in event_types(report)


def test_invalid_log_hash_format_is_rejected():
    result = make_result(logs=[{"name": "logs/T01.log", "sha256": "not-a-hash"}])
    report = review_task(make_payload(result=result))
    assert report["decision"] == DECISION_REJECT
    assert "INVALID_LOG_HASH" in blocking_codes(report)


def test_changed_log_is_blocking_and_materialises_as_a_defect():
    report = review_task(make_payload(), log_contents={"logs/T01.log": "tampered log"})
    assert report["decision"] == DECISION_REJECT
    assert "CHANGED_LOG" in blocking_codes(report)
    assert [defect["code"] for defect in report["defects"]] == ["CHANGED_LOG"]
    assert event_types(report) == ["DEFECT_CREATED", "TASK_REJECTED"]


# --------------------------------------------------------------------------- #
# 3. Version / snapshot staleness -> REVALIDATION_REQUIRED
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "run_overrides",
    [
        {"plan_version": "PLAN-demo-v2.0"},
        {"spec_version": "S2-v1.0"},
        {"contract_version": "C2-v1.0"},
        {"input_snapshot": "e" * 64},
    ],
    ids=["plan_version", "spec_version", "contract_version", "input_snapshot"],
)
def test_version_or_snapshot_mismatch_requires_revalidation(run_overrides):
    report = review_task(make_payload(run=make_run(**run_overrides)))

    assert report["decision"] == DECISION_REVALIDATION
    assert report["defects"] == []
    assert event_types(report) == ["TASK_REVALIDATION_REQUIRED"]
    assert_only_contract_events(report)
    event = report["events"][0]
    assert event["candidate_commit"] == COMMIT
    assert event["payload"]["reasons"]
    assert {"VERSION_MISMATCH", "STALE_INPUT_SNAPSHOT"} & set(codes(report))


def test_task_level_version_mismatch_requires_revalidation():
    task = make_task(spec_version="S9-v9.9")
    report = review_task(make_payload(task=task))
    assert report["decision"] == DECISION_REVALIDATION
    assert "VERSION_MISMATCH" in codes(report)


def test_no_revalidation_when_every_version_matches():
    report = review_task(make_payload())
    assert report["decision"] == DECISION_ACCEPT
    assert "TASK_REVALIDATION_REQUIRED" not in event_types(report)
    assert not {"VERSION_MISMATCH", "STALE_INPUT_SNAPSHOT"} & set(codes(report))


def test_blocking_finding_wins_over_revalidation():
    task = make_task()
    task.pop("capability")
    report = review_task(make_payload(task=task, run=make_run(plan_version="PLAN-demo-v2.0")))

    assert report["decision"] == DECISION_REJECT
    assert "VERSION_MISMATCH" in codes(report)
    assert "TASK_REJECTED" in event_types(report)
    assert "TASK_REVALIDATION_REQUIRED" not in event_types(report)


# --------------------------------------------------------------------------- #
# 4. Non-SUCCEEDED execution status
# --------------------------------------------------------------------------- #


def test_failed_result_is_rejected():
    report = review_task(make_payload(result=make_result(status="FAILED", candidate_commit=None, error="compile error")))
    assert report["decision"] == DECISION_REJECT
    assert "EXECUTION_FAILED" in blocking_codes(report)
    assert "DEFECT_CREATED" in event_types(report)
    assert any("compile error" in item["message"] for item in report["findings"])


def test_blocked_result_is_rejected():
    report = review_task(make_payload(result=make_result(status="BLOCKED", candidate_commit=None, error="no database")))
    assert report["decision"] == DECISION_REJECT
    assert "EXECUTION_BLOCKED" in blocking_codes(report)
    assert event_types(report)[-1] == "TASK_REJECTED"


def test_unknown_execution_status_is_treated_as_failure():
    report = review_task(make_payload(result=make_result(status="DONE", candidate_commit=None)))
    assert report["decision"] == DECISION_REJECT
    assert "EXECUTION_FAILED" in blocking_codes(report)


# --------------------------------------------------------------------------- #
# 5. Unclosed blocking defects from history
# --------------------------------------------------------------------------- #


def test_unclosed_blocking_defect_in_history_is_rejected():
    history = [
        {"task_id": "T01", "attempt_id": 1, "status": "open", "severity": "blocking", "defect_id": "D-OLD-1"}
    ]
    report = review_task(make_payload(history=history))

    assert report["decision"] == DECISION_REJECT
    assert "UNCLOSED_BLOCKING_DEFECT" in blocking_codes(report)
    finding = next(item for item in report["findings"] if item["code"] == "UNCLOSED_BLOCKING_DEFECT")
    assert finding["ref"] == "D-OLD-1"
    assert event_types(report) == ["TASK_REJECTED"]
    # The回指 finding must not spawn a self-feeding new defect.
    assert report["defects"] == []


def test_open_minor_defect_in_history_does_not_block():
    history = [
        {"task_id": "T01", "attempt_id": 1, "status": "open", "severity": "minor", "defect_id": "D-OLD-1"}
    ]
    report = review_task(make_payload(history=history))
    assert report["decision"] == DECISION_ACCEPT
    assert event_types(report) == ["DEFECT_CLOSED", "TASK_ACCEPTED"]


def test_closed_blocking_defect_in_history_does_not_block():
    history = [
        {"task_id": "T01", "attempt_id": 1, "status": "closed", "severity": "blocking", "defect_id": "D-OLD-1"}
    ]
    report = review_task(make_payload(history=history))
    assert report["decision"] == DECISION_ACCEPT


def test_multiple_unclosed_blocking_defects_all_appear_in_findings():
    history = [
        {"task_id": "T01", "attempt_id": 1, "status": "open", "severity": "blocking", "defect_id": "D-OLD-1"},
        {"task_id": "T01", "attempt_id": 2, "status": "open", "severity": "blocking", "defect_id": "D-OLD-2"},
        {"task_id": "T01", "attempt_id": 3, "status": "closed", "severity": "blocking", "defect_id": "D-OLD-3"},
    ]
    report = review_task(make_payload(history=history))
    refs = [item["ref"] for item in report["findings"] if item["code"] == "UNCLOSED_BLOCKING_DEFECT"]
    assert refs == ["D-OLD-1", "D-OLD-2"]


def test_non_mapping_history_entries_are_ignored():
    report = review_task(make_payload(history=["D-OLD-1", 5, None]))
    assert report["decision"] == DECISION_ACCEPT


# --------------------------------------------------------------------------- #
# 6. Non-blocking findings
# --------------------------------------------------------------------------- #


def test_only_major_and_minor_known_issues_still_accept():
    """Non-blocking self-reported issues never block acceptance.

    Characterisation: B06 currently never produces ``major``/``minor``
    *findings* (every ``finding()`` call uses the default ``blocking``
    severity); ``result.known_issues`` are not inspected by the gate at all, so
    they neither block nor reappear in ``findings``.
    """
    result = make_result(
        known_issues=[
            {"code": "SLOW_QUERY", "message": "p95 regression", "severity": "major"},
            {"code": "STYLE", "message": "naming", "severity": "minor"},
        ]
    )
    report = review_task(make_payload(result=result))
    assert report["decision"] == DECISION_ACCEPT
    assert report["findings"] == []
    assert report["defects"] == []
    assert event_types(report) == ["TASK_ACCEPTED"]


def test_issue_severities_enumeration_is_the_contract_one():
    assert set(SEVERITIES) == {"blocking", "major", "minor"}


def test_only_major_and_minor_findings_are_accepted(monkeypatch):
    """§6 decision rule: non-blocking findings must not reject the task."""
    from multiagent_quality import task_gate

    def fake_check(task, result, findings, defects, **_kwargs):
        findings.append(task_gate.finding("SLOW_QUERY", "p95 regression", severity="major"))
        findings.append(task_gate.finding("STYLE", "naming", severity="minor"))

    monkeypatch.setattr(task_gate, "_check_result_hash_and_tests", fake_check)
    # Remove the other blocking sources so only the injected findings remain.
    monkeypatch.setattr(task_gate, "_check_requirement_coverage", lambda *args, **kwargs: None)
    monkeypatch.setattr(task_gate, "_check_dependency_cycle", lambda *args, **kwargs: None)
    monkeypatch.setattr(task_gate, "_check_duplicate_outputs", lambda *args, **kwargs: None)

    report = review_task(make_payload())

    assert [item["severity"] for item in report["findings"]] == ["major", "minor"]
    assert report["decision"] == DECISION_ACCEPT
    assert report["defects"] == []
    assert event_types(report) == ["TASK_ACCEPTED"]
    assert [item["code"] for item in report["events"][0]["payload"]["findings"]] == ["SLOW_QUERY", "STYLE"]


# --------------------------------------------------------------------------- #
# 7. Event vocabulary
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "payload_overrides",
    [
        {},
        {"history": [{"task_id": "T01", "attempt_id": 1, "status": "open", "severity": "blocking", "defect_id": "D-1"}]},
        {"result": make_result(test_results=[])},
        {"run": make_run(plan_version="PLAN-demo-v2.0")},
        {"result": make_result(status="FAILED", candidate_commit=None)},
        {"plan": make_plan(required_requirements=["R1", "R2"])},
    ],
    ids=["accept", "accept-close", "missing-test", "revalidation", "failed", "uncovered"],
)
def test_events_only_use_the_five_allowed_types(payload_overrides):
    report = review_task(make_payload(**payload_overrides))
    assert report["events"]
    assert set(event_types(report)) <= set(RESULT_EVENTS)


def test_reject_event_carries_findings_and_defect_ids():
    report = review_task(make_payload(result=make_result(test_results=[])))
    rejected = report["events"][-1]
    assert rejected["type"] == "TASK_REJECTED"
    assert rejected["payload"]["findings"] == report["findings"]
    assert rejected["payload"]["defect_ids"] == [defect["defect_id"] for defect in report["defects"]]


def test_defect_created_events_precede_the_decision_event():
    report = review_task(make_payload(result=make_result(test_results=[])))
    assert event_types(report) == ["DEFECT_CREATED", "TASK_REJECTED"]
    created = report["events"][0]
    assert created["payload"]["defect_id"] == report["defects"][0]["defect_id"]
    assert created["payload"]["severity"] == "blocking"
    assert created["payload"]["code"] == report["defects"][0]["code"]
    assert created["candidate_commit"] == COMMIT


# --------------------------------------------------------------------------- #
# 8. verify_plan / verify_artifacts
# --------------------------------------------------------------------------- #


def test_verify_plan_accepts_a_clean_plan_read_only():
    plan = make_plan()
    before = copy.deepcopy(plan)
    report = verify_plan(plan)

    assert report["mode"] == "read_only"
    assert report["accepted"] is True
    assert report["findings"] == []
    assert report["task_count"] == 1
    assert plan == before


def test_verify_plan_reports_unknown_dependency():
    task = make_task("T01", depends_on=["T99"])
    report = verify_plan(make_plan([task]))
    assert report["accepted"] is False
    assert "UNKNOWN_DEPENDENCY" in [item["code"] for item in report["findings"]]


def test_verify_plan_reports_cycle_uncovered_requirement_and_duplicate_output():
    first = make_task("T01", depends_on=["T02"], outputs=["shared"])
    second = make_task("T02", depends_on=["T01"], outputs=["shared"])
    report = verify_plan(make_plan([first, second], required_requirements=["R1", "R2"]))

    assert report["accepted"] is False
    found = {item["code"] for item in report["findings"]}
    assert {"DEPENDENCY_CYCLE", "UNCOVERED_REQUIREMENT", "DUPLICATE_OUTPUT"} <= found
    assert report["task_count"] == 2


def test_verify_plan_reports_missing_task_fields():
    task = make_task()
    task.pop("escalation_target")
    report = verify_plan(make_plan([task]))
    assert report["accepted"] is False
    missing = [item for item in report["findings"] if item["code"] == "MISSING_TASK_FIELD"]
    assert [item["ref"] for item in missing] == ["escalation_target"]


def test_verify_plan_rejects_empty_plan_document():
    report = verify_plan({"tasks": [], "required_requirements": []})
    assert report["accepted"] is False
    assert "MISSING_REQUIREMENTS" in [item["code"] for item in report["findings"]]
    assert report["task_count"] == 0


def test_verify_artifacts_accepts_matching_hashes():
    artifacts = [{"path": "artifacts/T01.md", "sha256": ARTIFACT_SHA}]
    assert verify_artifacts(artifacts, {"artifacts/T01.md": ARTIFACT_BODY}) == []


def test_verify_artifacts_reports_changed_hash():
    artifacts = [{"path": "artifacts/T01.md", "sha256": "0" * 64}]
    report = verify_artifacts(artifacts, {"artifacts/T01.md": ARTIFACT_BODY})
    assert [item["code"] for item in report] == ["CHANGED_ARTIFACT"]
    assert report[0]["ref"] == "artifacts/T01.md"


def test_verify_artifacts_reports_missing_content():
    report = verify_artifacts([{"path": "artifacts/missing.md", "sha256": ARTIFACT_SHA}], {})
    assert [item["code"] for item in report] == ["MISSING_ARTIFACT"]


def test_verify_artifacts_reports_each_artifact_independently():
    artifacts = [
        {"path": "a.md", "sha256": ARTIFACT_SHA},
        {"path": "b.md", "sha256": "0" * 64},
        {"path": "c.md", "sha256": ARTIFACT_SHA},
    ]
    report = verify_artifacts(
        artifacts, {"a.md": ARTIFACT_BODY, "b.md": ARTIFACT_BODY, "c.md": "other"}
    )
    assert [item["code"] for item in report] == ["CHANGED_ARTIFACT", "CHANGED_ARTIFACT"]
    assert [item["ref"] for item in report] == ["b.md", "c.md"]


# --------------------------------------------------------------------------- #
# 9. Defect identity
# --------------------------------------------------------------------------- #


def test_defect_ids_are_unique_across_two_attempts_of_the_same_task():
    first = review_task(make_payload(result=make_result(test_results=[])))
    second = review_task(make_payload(result=make_result(test_results=[], attempt_id=2)))

    assert first["decision"] == second["decision"] == DECISION_REJECT
    first_ids = {defect["defect_id"] for defect in first["defects"]}
    second_ids = {defect["defect_id"] for defect in second["defects"]}
    assert first_ids
    assert second_ids
    assert first_ids.isdisjoint(second_ids)
    assert all("A1" in defect_id for defect_id in first_ids)
    assert all("A2" in defect_id for defect_id in second_ids)


def test_defect_ids_are_unique_within_a_single_report():
    task = make_task()
    for field in ("capability", "priority", "escalation_target"):
        task.pop(field)
    report = review_task(make_payload(task=task))
    ids = [defect["defect_id"] for defect in report["defects"]]
    assert len(ids) == len(set(ids))
    assert len(ids) == sum(1 for item in report["findings"] if item["code"] == "MISSING_TASK_FIELD")


def test_defect_carries_task_code_severity_and_message():
    report = review_task(make_payload(result=make_result(test_results=[])))
    defect = report["defects"][0]
    assert defect["task_id"] == "T01"
    assert defect["code"] == "MISSING_TEST"
    assert defect["severity"] == "blocking"
    assert defect["status"] == "open"
    assert defect["message"]


# --------------------------------------------------------------------------- #
# 10. Regressions found by review
# --------------------------------------------------------------------------- #


def test_invalid_commit_creates_exactly_one_defect():
    """单一违规只能产生一条缺陷（避免同一检查重复登记）。"""
    report = review_task(make_payload(result=make_result(candidate_commit="zz")))
    assert len(report["defects"]) == 1
    assert event_types(report) == ["DEFECT_CREATED", "TASK_REJECTED"]


def test_each_distinct_missing_test_creates_its_own_defect():
    """同一 code 但不同 ref 的不同违规，仍应各自登记（不能过度去重）。"""
    payload = make_payload()
    payload["result"]["test_results"] = []
    report = review_task(payload)
    missing = [item for item in report["defects"] if item["code"] == "MISSING_TEST"]
    required = payload["task"]["required_test_ids"]
    assert len(missing) == len(required)
    assert {item.get("ref") for item in missing} == set(required)
    assert len({item["defect_id"] for item in missing}) == len(required)
