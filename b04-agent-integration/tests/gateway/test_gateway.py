"""B04-D21/D22/D23: unified task-execution gateway contract.

Contract: ``docs/TASK_ORCHESTRATION.md`` §5 — every call returns the same
``task_id``/``attempt_id`` it was given, ``status`` ∈ {SUCCEEDED, FAILED,
BLOCKED}, artifacts/logs use relative POSIX paths, and the gateway never
touches B02/B03 state.

The module is a pure adapter: these tests only exercise request/result
validation, the deterministic mock executor and the runner adapter. No network
and no writes outside the process.
"""

from __future__ import annotations

import hashlib
import re

import pytest

from b04.gateway import (
    COMMIT_PATTERN,
    EXECUTION_MODES,
    GatewayError,
    ISSUE_SEVERITIES,
    MockScenario,
    MockTaskExecutor,
    RESULT_STATUSES,
    RunnerTaskExecutor,
    TaskExecutionGateway,
    artifacts_digest,
    validate_request,
    validate_result,
)

SPEC_SHA = "a" * 64
ARTIFACT_SHA = "b" * 64
LOG_SHA = "c" * 64


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def make_request(**overrides) -> dict:
    """A §5 TaskExecutionRequest (B02 -> B04)."""
    request = {
        "task_id": "T01",
        "attempt_id": 1,
        "plan_version": "PLAN-demo-v1.0",
        "input_snapshot": SPEC_SHA,
        "run_id": "TASK-RUN-TEST",
        "role_id": "AgentIntegrationAgent",
        "block_id": "B04",
        "goal": "implement DS-AUTH-01",
        "inputs": [{"ref": "S1-v1.0", "kind": "declared_input"}],
        "outputs": ["artifact-ds-auth-01"],
        "acceptance": ["password_hash never leaves the service"],
        "required_test_ids": ["TC-AUTH-01", "TC-AUTH-02"],
        "write_scope": ["src/auth"],
        "timeout": 60,
        "retry_budget": 0,
        "candidate_base_commit": None,
    }
    request.update(overrides)
    return request


def make_result(request: dict | None = None, **overrides) -> dict:
    """A §5 TaskExecutionResult (B04 -> B02) echoing the request identity."""
    request = request or make_request()
    result = {
        "task_id": request["task_id"],
        "attempt_id": request["attempt_id"],
        "status": "SUCCEEDED",
        "artifacts": [{"path": "artifacts/T01/auth.md", "kind": "document", "sha256": ARTIFACT_SHA}],
        "candidate_commit": "abc1234",
        "evidence_refs": ["evidence:1"],
        "known_issues": [],
        "logs": [{"name": "logs/T01/run.log", "sha256": LOG_SHA}],
        "test_results": [
            {"test_id": "TC-AUTH-01", "status": "PASS", "log_sha256": LOG_SHA, "detail": "ok"}
        ],
        "started_at": "2026-11-18T00:00:00Z",
        "finished_at": "2026-11-18T00:00:01Z",
        "duration_seconds": 1.0,
    }
    result.update(overrides)
    return result


# --------------------------------------------------------------------------- #
# validate_request
# --------------------------------------------------------------------------- #


REQUIRED_REQUEST_FIELDS = (
    "task_id",
    "attempt_id",
    "plan_version",
    "input_snapshot",
    "goal",
    "inputs",
    "outputs",
    "acceptance",
    "write_scope",
)


def test_validate_request_accepts_a_complete_contract_request():
    request = make_request()
    assert validate_request(request) is request


def test_validate_request_rejects_missing_required_field():
    for field in REQUIRED_REQUEST_FIELDS:
        with pytest.raises(GatewayError):
            validate_request({key: value for key, value in make_request().items() if key != field})


def test_validate_request_rejects_non_object():
    for value in ([], "request", 5, None):
        with pytest.raises(GatewayError):
            validate_request(value)


def test_validate_request_rejects_blank_identity_strings():
    for field in ("task_id", "plan_version", "input_snapshot", "goal"):
        for value in ("", "   ", None, 5):
            with pytest.raises(GatewayError):
                validate_request(make_request(**{field: value}))


def test_validate_request_rejects_non_int_attempt_id():
    for value in (0, -1, -10, "1", 1.0, True, None):
        with pytest.raises(GatewayError):
            validate_request(make_request(attempt_id=value))


def test_validate_request_accepts_attempt_id_one_and_above():
    for value in (1, 2, 99):
        assert validate_request(make_request(attempt_id=value))["attempt_id"] == value


def test_validate_request_rejects_non_list_collection_fields():
    for field in ("inputs", "outputs", "acceptance", "write_scope"):
        for value in (None, {}, "x", 5, ("a",)):
            with pytest.raises(GatewayError):
                validate_request(make_request(**{field: value}))


def test_validate_request_rejects_absolute_parent_and_drive_write_scope():
    for scope in ("/etc/passwd", "../escape", "a/../b", "C:/Windows", "D:\\data", "src/../../x"):
        with pytest.raises(GatewayError):
            validate_request(make_request(write_scope=[scope]))


def test_validate_request_rejects_write_scope_with_non_string_entry():
    for scope in (5, None, {"path": "src"}, ["src"]):
        with pytest.raises(GatewayError):
            validate_request(make_request(write_scope=[scope]))


def test_validate_request_accepts_relative_posix_write_scope():
    request = make_request(write_scope=["src/auth", "docs/api/auth.md", "tests/auth/test_login.py"])
    assert validate_request(request)["write_scope"] == request["write_scope"]


def test_validate_request_rejects_bad_timeout():
    for value in (0, -1, "60", 1.0, True, None):
        with pytest.raises(GatewayError):
            validate_request(make_request(timeout=value))


def test_validate_request_rejects_bad_retry_budget():
    for value in (-1, -5, "0", 1.0, True):
        with pytest.raises(GatewayError):
            validate_request(make_request(retry_budget=value))


def test_validate_request_does_not_fill_optional_defaults():
    """"不做隐式补全" (gateway.validate_request docstring): defaults are only used
    for validation; they are not written back into the returned request."""
    request = make_request()
    request.pop("timeout")
    request.pop("retry_budget")
    validated = validate_request(request)
    assert validated is request
    assert "timeout" not in validated
    assert "retry_budget" not in validated
    # ... yet the mock executor tolerates the omission.
    assert MockTaskExecutor()(request)["status"] == "SUCCEEDED"


# --------------------------------------------------------------------------- #
# validate_result
# --------------------------------------------------------------------------- #


def test_validate_result_accepts_a_complete_contract_result():
    request = validate_request(make_request())
    result = make_result(request)
    assert validate_result(result, request) is result


def test_validate_result_rejects_missing_required_field():
    request = validate_request(make_request())
    for field in ("task_id", "attempt_id", "status", "artifacts", "evidence_refs", "known_issues"):
        with pytest.raises(GatewayError):
            validate_result(
                {key: value for key, value in make_result(request).items() if key != field}, request
            )


def test_validate_result_rejects_non_object():
    request = validate_request(make_request())
    for value in ([], "result", None, 5):
        with pytest.raises(GatewayError):
            validate_result(value, request)


def test_validate_result_rejects_wrong_task_id():
    request = validate_request(make_request())
    with pytest.raises(GatewayError) as raised:
        validate_result(make_result(request, task_id="T99"), request)
    assert "T99" in str(raised.value)


def test_validate_result_rejects_wrong_attempt_id():
    request = validate_request(make_request())
    with pytest.raises(GatewayError) as raised:
        validate_result(make_result(request, attempt_id=2), request)
    assert "attempt_id" in str(raised.value)


def test_validate_result_rejects_unknown_status():
    request = validate_request(make_request())
    for status in ("DONE", "SUCCESS", "PASSED", "", None, 5):
        with pytest.raises(GatewayError):
            validate_result(make_result(request, status=status), request)


def test_validate_result_accepts_all_contract_statuses_for_a_matching_lowercase_commit():
    request = validate_request(make_request())
    for status in RESULT_STATUSES:
        result = make_result(request, status=status, candidate_commit="abc1234")
        assert validate_result(result, request)["status"] == status


def test_validate_result_rejects_non_list_collection_fields():
    request = validate_request(make_request())
    for field in ("artifacts", "evidence_refs", "known_issues"):
        for value in (None, {}, "x", 5):
            with pytest.raises(GatewayError):
                validate_result(make_result(request, **{field: value}), request)


def test_validate_result_rejects_bad_artifact_path():
    request = validate_request(make_request())
    for path in ("../secret.md", "/etc/passwd", "C:/x.md", "a/../../b.md", ""):
        with pytest.raises(GatewayError):
            validate_result(
                make_result(request, artifacts=[{"path": path, "kind": "document", "sha256": ARTIFACT_SHA}]),
                request,
            )


def test_validate_result_rejects_artifact_without_sha256():
    request = validate_request(make_request())
    for artifact in ({"path": "a.md"}, {"path": "a.md", "sha256": ""}, {"sha256": ARTIFACT_SHA}, "a.md", 5):
        with pytest.raises(GatewayError):
            validate_result(make_result(request, artifacts=[artifact]), request)


def test_validate_result_rejects_bad_sha256_format():
    request = validate_request(make_request())
    for digest in ("zz", "xyz", "B" * 64, ARTIFACT_SHA[:63], "g" * 64):
        with pytest.raises(GatewayError):
            validate_result(
                make_result(request, artifacts=[{"path": "a.md", "kind": "document", "sha256": digest}]),
                request,
            )


def test_validate_result_requires_candidate_commit_when_succeeded():
    request = validate_request(make_request())
    for commit in (None, "", "xyz", "abc", "ABCDEF1", "abc123g"):
        with pytest.raises(GatewayError):
            validate_result(make_result(request, candidate_commit=commit), request)


def test_validate_result_allows_missing_candidate_commit_when_not_succeeded():
    request = validate_request(make_request())
    for status in ("FAILED", "BLOCKED"):
        result = make_result(request, status=status, candidate_commit=None)
        assert validate_result(result, request)["candidate_commit"] is None


def test_validate_result_accepts_short_and_long_hex_commits():
    request = validate_request(make_request())
    for commit in ("abc1234", "0" * 40, "f" * 7):
        assert COMMIT_PATTERN.match(commit)
        assert validate_result(make_result(request, candidate_commit=commit), request)["candidate_commit"] == commit


def test_validate_result_rejects_bad_known_issues_severity():
    request = validate_request(make_request())
    for severity in ("huge", "BLOCKING", "", None, 5):
        with pytest.raises(GatewayError):
            validate_result(
                make_result(request, known_issues=[{"code": "X", "message": "m", "severity": severity}]),
                request,
            )


def test_validate_result_accepts_all_contract_severities():
    request = validate_request(make_request())
    result = make_result(
        request,
        known_issues=[
            {"code": code, "message": "m", "severity": severity}
            for code, severity in zip("ABC", ISSUE_SEVERITIES)
        ],
    )
    assert validate_result(result, request)["known_issues"] == result["known_issues"]


def test_validate_result_rejects_bad_test_results_status():
    request = validate_request(make_request())
    for status in ("OK", "PASSED", "passed", "", None, 5):
        with pytest.raises(GatewayError):
            validate_result(
                make_result(request, test_results=[{"test_id": "TC-1", "status": status}]), request
            )


def test_validate_result_rejects_test_result_without_test_id():
    request = validate_request(make_request())
    for case in ({"status": "PASS"}, {"test_id": 5, "status": "PASS"}, "TC-1"):
        with pytest.raises(GatewayError):
            validate_result(make_result(request, test_results=[case]), request)


def test_validate_result_skips_test_validation_when_test_results_absent():
    request = validate_request(make_request())
    result = make_result(request)
    result.pop("test_results")
    assert validate_result(result, request) is result


# --------------------------------------------------------------------------- #
# MockTaskExecutor
# --------------------------------------------------------------------------- #


def test_mock_executor_is_deterministic_for_the_same_request():
    executor = MockTaskExecutor()
    first = executor(make_request())
    second = executor(make_request())

    assert first["candidate_commit"] == second["candidate_commit"]
    assert [item["sha256"] for item in first["artifacts"]] == [
        item["sha256"] for item in second["artifacts"]
    ]
    assert first["test_results"] == second["test_results"]
    assert first["logs"] == second["logs"]
    assert first["artifacts"] == second["artifacts"]


def test_mock_executor_is_deterministic_across_instances():
    """确定性只覆盖“同一请求 → 同一证据”，时间戳按设计每次都不同。"""
    request = make_request()
    first = MockTaskExecutor()(request)
    second = MockTaskExecutor()(request)
    for field in ("task_id", "attempt_id", "status", "artifacts", "candidate_commit",
                  "evidence_refs", "known_issues", "test_results", "logs",
                  "duration_seconds", "execution_mode", "simulated"):
        assert first[field] == second[field], field
    # 时间戳来自真实时钟（毫秒精度，可能相同），只校验格式。
    for field in ("started_at", "finished_at"):
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", first[field]), field


def test_mock_executor_changes_commit_and_digests_per_attempt():
    executor = MockTaskExecutor()
    first = executor(make_request(attempt_id=1))
    second = executor(make_request(attempt_id=2))

    assert first["candidate_commit"] != second["candidate_commit"]
    assert [a["sha256"] for a in first["artifacts"]] != [a["sha256"] for a in second["artifacts"]]
    assert first["task_id"] == second["task_id"] == "T01"


def test_mock_executor_echoes_request_identity_and_marks_simulation():
    request = make_request(attempt_id=3)
    result = MockTaskExecutor()(request)
    assert result["task_id"] == "T01"
    assert result["attempt_id"] == 3
    assert result["status"] == "SUCCEEDED"
    assert result["execution_mode"] == "mock"
    assert result["simulated"] is True
    assert result["candidate_commit"] == hashlib.sha256(b"mock:T01:3").hexdigest()[:12]


def test_mock_executor_produces_a_test_result_for_every_required_test():
    request = make_request(required_test_ids=["TC-1", "TC-2", "TC-3"])
    result = MockTaskExecutor()(request)
    assert [case["test_id"] for case in result["test_results"]] == ["TC-1", "TC-2", "TC-3"]
    assert {case["status"] for case in result["test_results"]} == {"PASS"}
    assert all(len(case["log_sha256"]) == 64 for case in result["test_results"])


def test_mock_executor_produces_an_artifact_per_declared_output():
    request = make_request(outputs=["artifact-a", "artifact-b"])
    result = MockTaskExecutor()(request)
    assert [item["path"] for item in result["artifacts"]] == [
        "artifacts/T01/artifact-a.md",
        "artifacts/T01/artifact-b.md",
    ]
    assert all(item["kind"] == "document" for item in result["artifacts"])
    assert all(len(item["sha256"]) == 64 for item in result["artifacts"])


def test_mock_executor_falls_back_to_a_named_output_when_outputs_empty():
    result = MockTaskExecutor()(make_request(outputs=[]))
    assert [item["path"] for item in result["artifacts"]] == ["artifacts/T01/T01-output.md"]


def test_mock_executor_respects_fail_tasks_scenario():
    executor = MockTaskExecutor(MockScenario.from_config({"fail_tasks": {"T01": "compile error"}}))
    result = executor(make_request())
    assert result["status"] == "FAILED"
    assert result["error"] == "compile error"
    assert result["candidate_commit"] is None
    assert result["artifacts"] == []
    assert result["known_issues"] == [
        {"code": "SIMULATED_FAILURE", "message": "compile error", "severity": "blocking"}
    ]


def test_mock_executor_respects_block_tasks_scenario():
    executor = MockTaskExecutor(MockScenario.from_config({"block_tasks": ["T01"]}))
    result = executor(make_request())
    assert result["status"] == "BLOCKED"
    assert result["candidate_commit"] is None
    assert [issue["code"] for issue in result["known_issues"]] == ["DEPENDENCY_UNAVAILABLE"]
    assert all(issue["severity"] == "blocking" for issue in result["known_issues"])


def test_mock_executor_only_applies_scenario_to_the_listed_task():
    executor = MockTaskExecutor(
        MockScenario.from_config({"fail_tasks": {"T02": "boom"}, "block_tasks": ["T03"]})
    )
    assert executor(make_request(task_id="T01"))["status"] == "SUCCEEDED"
    assert executor(make_request(task_id="T02"))["status"] == "FAILED"
    assert executor(make_request(task_id="T03"))["status"] == "BLOCKED"


def test_mock_executor_respects_missing_tests_scenario():
    executor = MockTaskExecutor(
        MockScenario.from_config({"missing_tests": {"T01": ["TC-AUTH-02"]}})
    )
    result = executor(make_request(required_test_ids=["TC-AUTH-01", "TC-AUTH-02"]))
    statuses = {case["test_id"]: case["status"] for case in result["test_results"]}
    assert statuses == {"TC-AUTH-01": "PASS", "TC-AUTH-02": "FAIL"}
    # Execution still succeeds: acceptance is B06's decision, not B04's.
    assert result["status"] == "SUCCEEDED"


def test_mock_executor_emits_a_log_per_required_test():
    result = MockTaskExecutor()(make_request(required_test_ids=["TC-1", "TC-2"]))
    assert [log["name"] for log in result["logs"]] == ["logs/T01/TC-1.log", "logs/T01/TC-2.log"]


def test_mock_executor_result_passes_gateway_result_validation():
    request = validate_request(make_request())
    result = MockTaskExecutor()(request)
    validate_result(result, request)


def test_mock_scenario_defaults_are_empty_and_deterministic():
    scenario = MockScenario.from_config(None)
    assert scenario.fail_tasks == {}
    assert scenario.block_tasks == set()
    assert scenario.missing_tests == {}
    assert scenario.duration_seconds == 0.0
    assert scenario.commit_salt == "mock"


def test_mock_scenario_normalises_config_types():
    scenario = MockScenario.from_config(
        {
            "fail_tasks": {"T01": 500},
            "block_tasks": ["T02"],
            "missing_tests": {"T03": ("TC-1",)},
            "duration_seconds": 2,
            "commit_salt": 7,
        }
    )
    assert scenario.fail_tasks == {"T01": "500"}
    assert scenario.block_tasks == {"T02"}
    assert scenario.missing_tests == {"T03": ["TC-1"]}
    assert scenario.duration_seconds == 2.0
    assert scenario.commit_salt == "7"


def test_mock_scenario_duration_is_reported():
    executor = MockTaskExecutor(MockScenario.from_config({"duration_seconds": 1.5}))
    assert executor(make_request())["duration_seconds"] == 1.5


# --------------------------------------------------------------------------- #
# Runner adapter and gateway
# --------------------------------------------------------------------------- #


def test_runner_executor_requires_a_callable():
    for value in (None, 5, "callable", {}):
        with pytest.raises(GatewayError):
            RunnerTaskExecutor(value)


def test_runner_executor_rejects_non_dict_return():
    executor = RunnerTaskExecutor(lambda request: ["not", "a", "result"])
    with pytest.raises(GatewayError):
        executor(make_request())


def test_runner_executor_fills_identity_and_mode_defaults():
    def runner(request):
        return {
            "status": "SUCCEEDED",
            "artifacts": [],
            "candidate_commit": "abc1234",
            "evidence_refs": [],
            "known_issues": [],
        }

    result = RunnerTaskExecutor(runner)(make_request(attempt_id=4))
    assert result["task_id"] == "T01"
    assert result["attempt_id"] == 4
    assert result["execution_mode"] == "runner"
    assert result["simulated"] is False


def test_runner_executor_validates_the_incoming_request():
    calls = []
    executor = RunnerTaskExecutor(lambda request: calls.append(request) or {})
    with pytest.raises(GatewayError):
        executor(make_request(attempt_id=0))
    assert calls == []


def test_gateway_rejects_unknown_mode():
    for mode in ("real", "", None, "MOCK"):
        with pytest.raises(GatewayError):
            TaskExecutionGateway(mode=mode)


def test_gateway_mock_mode_works_end_to_end():
    gateway = TaskExecutionGateway(mode="mock")
    assert gateway.mode == "mock"
    assert gateway.simulated is True
    result = gateway(make_request())
    assert result["status"] == "SUCCEEDED"
    assert result["simulated"] is True
    assert result["error"] is None
    # The result must already satisfy the §5 result contract.
    validate_result(result, validate_request(make_request()))


def test_gateway_mock_mode_applies_scenario_config():
    gateway = TaskExecutionGateway(mode="mock", scenario={"fail_tasks": {"T01": "boom"}})
    result = gateway(make_request())
    assert result["status"] == "FAILED"
    assert result["error"] == "boom"


def test_gateway_mock_mode_is_deterministic():
    first = TaskExecutionGateway(mode="mock")(make_request())
    second = TaskExecutionGateway(mode="mock")(make_request())
    assert first["candidate_commit"] == second["candidate_commit"]
    assert first["artifacts"] == second["artifacts"]


def test_gateway_rejects_invalid_request_before_executing():
    gateway = TaskExecutionGateway(mode="mock")
    for request in (
        {key: value for key, value in make_request().items() if key != "task_id"},
        make_request(attempt_id=0),
        make_request(write_scope=["../escape"]),
    ):
        with pytest.raises(GatewayError):
            gateway(request)


def test_gateway_runner_mode_requires_runner_callable():
    with pytest.raises(GatewayError):
        TaskExecutionGateway(mode="runner")


def test_gateway_runner_mode_uses_the_injected_callable():
    seen = []

    def runner(request):
        seen.append(request)
        return {
            "status": "SUCCEEDED",
            "artifacts": [],
            "candidate_commit": "abc1234",
            "evidence_refs": ["evidence:runner"],
            "known_issues": [],
        }

    gateway = TaskExecutionGateway(mode="runner", runner_callable=runner)
    result = gateway(make_request())

    assert seen and seen[0]["task_id"] == "T01"
    assert result["status"] == "SUCCEEDED"
    assert result["evidence_refs"] == ["evidence:runner"]
    assert result["simulated"] is False
    assert gateway.simulated is False


def test_gateway_runner_mode_rejects_non_dict_return():
    gateway = TaskExecutionGateway(mode="runner", runner_callable=lambda request: "SUCCEEDED")
    with pytest.raises(GatewayError):
        gateway(make_request())


def test_gateway_runner_mode_rejects_identity_mismatch():
    def runner(request):
        return {
            "task_id": "T99",
            "attempt_id": request["attempt_id"],
            "status": "SUCCEEDED",
            "artifacts": [],
            "candidate_commit": "abc1234",
            "evidence_refs": [],
            "known_issues": [],
        }

    gateway = TaskExecutionGateway(mode="runner", runner_callable=runner)
    with pytest.raises(GatewayError):
        gateway(make_request())


def test_gateway_runner_mode_rejects_success_without_commit():
    def runner(request):
        return {
            "task_id": request["task_id"],
            "attempt_id": request["attempt_id"],
            "status": "SUCCEEDED",
            "artifacts": [],
            "candidate_commit": None,
            "evidence_refs": [],
            "known_issues": [],
        }

    gateway = TaskExecutionGateway(mode="runner", runner_callable=runner)
    with pytest.raises(GatewayError):
        gateway(make_request())


def test_gateway_simulated_property_matches_the_mode():
    assert TaskExecutionGateway(mode="mock").simulated is True
    assert TaskExecutionGateway(mode="mock").describe()["simulated"] is True
    runner_gateway = TaskExecutionGateway(mode="runner", runner_callable=lambda request: {})
    assert runner_gateway.simulated is False
    assert runner_gateway.describe()["simulated"] is False


def test_gateway_describe_reports_the_result_contract():
    description = TaskExecutionGateway(mode="mock").describe()
    assert description == {
        "mode": "mock",
        "simulated": True,
        "result_contract": "b04.task-result.v1",
    }
    assert set(EXECUTION_MODES) == {"mock", "runner"}


def test_gateway_error_is_a_value_error():
    assert issubclass(GatewayError, ValueError)


# --------------------------------------------------------------------------- #
# artifacts_digest
# --------------------------------------------------------------------------- #


def test_artifacts_digest_is_order_independent():
    artifacts = [
        {"path": "artifacts/b.md", "sha256": "2" * 64},
        {"path": "artifacts/a.md", "sha256": "1" * 64},
        {"path": "artifacts/c.md", "sha256": "3" * 64},
    ]
    assert artifacts_digest(artifacts) == artifacts_digest(list(reversed(artifacts)))
    assert artifacts_digest(artifacts) == artifacts_digest(artifacts[1:] + artifacts[:1])


def test_artifacts_digest_changes_when_content_changes():
    base = [{"path": "artifacts/a.md", "sha256": "1" * 64}]
    changed = [{"path": "artifacts/a.md", "sha256": "9" * 64}]
    assert artifacts_digest(base) != artifacts_digest(changed)


def test_artifacts_digest_changes_when_a_path_changes():
    base = [{"path": "artifacts/a.md", "sha256": "1" * 64}]
    renamed = [{"path": "artifacts/b.md", "sha256": "1" * 64}]
    assert artifacts_digest(base) != artifacts_digest(renamed)


def test_artifacts_digest_changes_when_an_artifact_is_added():
    base = [{"path": "artifacts/a.md", "sha256": "1" * 64}]
    extended = base + [{"path": "artifacts/b.md", "sha256": "2" * 64}]
    assert artifacts_digest(base) != artifacts_digest(extended)


def test_artifacts_digest_is_a_hex_sha256():
    digest = artifacts_digest([{"path": "a", "sha256": "1" * 64}])
    assert len(digest) == 64
    assert digest == digest.lower()
    assert int(digest, 16) >= 0


def test_artifacts_digest_of_empty_collection_is_the_sha256_of_nothing():
    assert artifacts_digest([]) == hashlib.sha256(b"").hexdigest()


def test_artifacts_digest_accepts_any_iterable():
    artifacts = [{"path": "a", "sha256": "1" * 64}]
    assert artifacts_digest(iter(artifacts)) == artifacts_digest(artifacts)
