"""B04-D21/D22/D23：统一任务执行 Gateway。

契约（docs/TASK_ORCHESTRATION.md §5）：

- 输入 `TaskExecutionRequest`：task_id、attempt_id、plan_version、input_snapshot、
  inputs、acceptance、write_scope 等。
- 输出 `TaskExecutionResult`：task_id、attempt_id、status、artifacts、
  candidate_commit、evidence_refs、known_issues 等。
- **不得改变 B02 状态**：本模块不导入也不写任何 B02/B03 状态表。
- `status=SUCCEEDED` 只代表“执行完成并自检通过”，不代表验收通过。

支持两种模式：
- `mock`：确定性模拟执行器，可注入失败/超时/缺证据，用于闭环演示与回归测试。
- `runner`：把请求转发给真实 B04 `AgentRunner` 的适配层（需外部注入可调用对象）。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

EXECUTION_MODES = ("mock", "runner")
RESULT_STATUSES = ("SUCCEEDED", "FAILED", "BLOCKED")
ISSUE_SEVERITIES = ("blocking", "major", "minor")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{7,40}$")
# 与 B06 `task_gate.SHA256_PATTERN` 保持一致：小写十六进制 64 位。
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

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


class GatewayError(ValueError):
    """执行请求或执行结果不符合 §5 契约。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate_request(request: Any) -> dict[str, Any]:
    """严格校验 TaskExecutionRequest；不做隐式补全。"""
    if not isinstance(request, dict):
        raise GatewayError("执行请求必须是对象")
    for name in REQUIRED_REQUEST_FIELDS:
        if name not in request:
            raise GatewayError(f"执行请求缺少字段 {name}")
    for name in ("task_id", "plan_version", "input_snapshot", "goal"):
        value = request[name]
        if not isinstance(value, str) or not value.strip():
            raise GatewayError(f"执行请求 {name} 必须是非空字符串")
    attempt_id = request["attempt_id"]
    if not isinstance(attempt_id, int) or isinstance(attempt_id, bool) or attempt_id < 1:
        raise GatewayError("执行请求 attempt_id 必须是 >=1 的整数")
    for name in ("inputs", "outputs", "acceptance", "write_scope"):
        if not isinstance(request[name], list):
            raise GatewayError(f"执行请求 {name} 必须是数组")
    timeout = request.get("timeout", 600)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 1:
        raise GatewayError("执行请求 timeout 必须是正整数")
    retry_budget = request.get("retry_budget", 0)
    if not isinstance(retry_budget, int) or isinstance(retry_budget, bool) or retry_budget < 0:
        raise GatewayError("执行请求 retry_budget 必须是非负整数")
    for scope in request["write_scope"]:
        if not isinstance(scope, str) or ".." in scope or scope.startswith("/") or ":" in scope:
            raise GatewayError(f"write_scope 只能使用相对 POSIX 路径: {scope!r}")
    return request


def validate_result(result: Any, request: dict[str, Any]) -> dict[str, Any]:
    """校验 TaskExecutionResult 与请求的身份一致性。"""
    if not isinstance(result, dict):
        raise GatewayError("执行结果必须是对象")
    for name in ("task_id", "attempt_id", "status", "artifacts", "evidence_refs", "known_issues"):
        if name not in result:
            raise GatewayError(f"执行结果缺少字段 {name}")
    if result["task_id"] != request["task_id"]:
        raise GatewayError(
            f"执行结果 task_id={result['task_id']!r} 与请求 {request['task_id']!r} 不一致"
        )
    if result["attempt_id"] != request["attempt_id"]:
        raise GatewayError(
            f"执行结果 attempt_id={result['attempt_id']!r} 与请求 {request['attempt_id']!r} 不一致"
        )
    if result["status"] not in RESULT_STATUSES:
        raise GatewayError(f"执行结果 status 必须是 {list(RESULT_STATUSES)}")
    if not isinstance(result["artifacts"], list):
        raise GatewayError("执行结果 artifacts 必须是数组")
    if not isinstance(result["evidence_refs"], list):
        raise GatewayError("执行结果 evidence_refs 必须是数组")
    if not isinstance(result["known_issues"], list):
        raise GatewayError("执行结果 known_issues 必须是数组")
    for issue in result["known_issues"]:
        if not isinstance(issue, dict) or issue.get("severity") not in ISSUE_SEVERITIES:
            raise GatewayError(f"known_issues 条目必须含 severity ∈ {list(ISSUE_SEVERITIES)}")
    for artifact in result["artifacts"]:
        if not isinstance(artifact, dict):
            raise GatewayError("artifacts 条目必须是对象")
        for name in ("path", "sha256"):
            if not isinstance(artifact.get(name), str) or not artifact[name]:
                raise GatewayError(f"artifacts 条目缺少 {name}")
        if ".." in artifact["path"] or artifact["path"].startswith("/") or ":" in artifact["path"]:
            raise GatewayError(f"artifacts.path 必须是相对 POSIX 路径: {artifact['path']!r}")
        # 必须与 B06 task_gate.SHA256_PATTERN 一致：小写十六进制 64 位。
        # 否则 B04 会放过 B06 必然拒绝的产物摘要，把失败推迟到验收阶段。
        if not SHA256_PATTERN.match(artifact["sha256"]):
            raise GatewayError(
                f"artifacts.sha256 必须是小写十六进制 64 位: {artifact['sha256']!r}"
            )
    for log in result.get("logs") or []:
        if not isinstance(log, dict):
            raise GatewayError("logs 条目必须是对象")
        for name in ("name", "sha256"):
            if not isinstance(log.get(name), str) or not log[name]:
                raise GatewayError(f"logs 条目缺少 {name}")
        if not SHA256_PATTERN.match(log["sha256"]):
            raise GatewayError(f"logs.sha256 必须是小写十六进制 64 位: {log['sha256']!r}")
    for case in result.get("test_results") or []:
        if not isinstance(case, dict) or not isinstance(case.get("test_id"), str):

            raise GatewayError("test_results 条目必须含 test_id")
        if case.get("status") not in ("PASS", "FAIL"):
            raise GatewayError("test_results[].status 必须是 PASS 或 FAIL")
        digest = case.get("log_sha256")
        if digest is not None and (
            not isinstance(digest, str) or not SHA256_PATTERN.match(digest)
        ):
            raise GatewayError(f"test_results[].log_sha256 必须是小写十六进制 64 位: {digest!r}")
    if not isinstance(result.get("test_results", []), list):
        raise GatewayError("执行结果 test_results 必须是数组")
    if not isinstance(result.get("logs", []), list):
        raise GatewayError("执行结果 logs 必须是数组")
    if result["status"] == "SUCCEEDED":
        commit = result.get("candidate_commit")
        if not isinstance(commit, str) or not COMMIT_PATTERN.match(commit):
            raise GatewayError("status=SUCCEEDED 时必须提供合法的 candidate_commit")
    return result


@dataclass
class MockScenario:
    """模拟执行的确定性设定；用于演示失败、有限重试与阻断。"""

    fail_tasks: dict[str, str] = field(default_factory=dict)
    block_tasks: set[str] = field(default_factory=set)
    missing_tests: dict[str, list[str]] = field(default_factory=dict)
    # 按请求自省：这些任务的**第一条**规定测试会被刻意判为未执行。
    # 用于演示「缺证据被质量门驳回」，无需事先知道具体测试编号。
    missing_first_test_tasks: set[str] = field(default_factory=set)
    duration_seconds: float = 0.0
    commit_salt: str = "mock"

    @classmethod
    def from_config(cls, config: dict[str, Any] | None) -> "MockScenario":
        data = config or {}
        return cls(
            fail_tasks={str(k): str(v) for k, v in (data.get("fail_tasks") or {}).items()},
            block_tasks=set(data.get("block_tasks") or ()),
            missing_tests={str(k): list(v) for k, v in (data.get("missing_tests") or {}).items()},
            missing_first_test_tasks=set(data.get("missing_first_test_tasks") or ()),
            duration_seconds=float(data.get("duration_seconds") or 0.0),
            commit_salt=str(data.get("commit_salt") or "mock"),
        )

    def omitted_tests(self, task_id: str, required: list[str]) -> set[str]:
        """本次应被判为“未执行”的测试编号。"""
        omitted = set(self.missing_tests.get(task_id) or ())
        if task_id in self.missing_first_test_tasks and required:
            omitted.add(required[0])
        return omitted


class MockTaskExecutor:
    """确定性模拟执行器：产生可复现的产物、日志与测试结果。"""

    def __init__(self, scenario: MockScenario | None = None):
        self.scenario = scenario or MockScenario()

    def __call__(self, request: dict[str, Any]) -> dict[str, Any]:
        request = validate_request(request)
        task_id = request["task_id"]
        attempt_id = request["attempt_id"]
        started = _now()
        if task_id in self.scenario.block_tasks:
            return self._result(
                request,
                status="BLOCKED",
                started=started,
                error="模拟环境依赖不可用（blocked）",
                issues=[{"code": "DEPENDENCY_UNAVAILABLE", "message": "模拟环境依赖不可用", "severity": "blocking"}],
            )
        if task_id in self.scenario.fail_tasks:
            return self._result(
                request,
                status="FAILED",
                started=started,
                error=self.scenario.fail_tasks[task_id],
                issues=[{"code": "SIMULATED_FAILURE", "message": self.scenario.fail_tasks[task_id], "severity": "blocking"}],
            )
        commit = _sha256(f"{self.scenario.commit_salt}:{task_id}:{attempt_id}")[:12]
        artifacts = [
            {
                "path": f"artifacts/{task_id}/{name}.md",
                "kind": "document",
                "sha256": _sha256(f"{task_id}:{name}:{attempt_id}"),
            }
            for name in (request["outputs"] or [f"{task_id}-output"])
        ]
        missing = self.scenario.omitted_tests(task_id, list(request.get("required_test_ids") or []))
        test_results = [
            {
                "test_id": test_id,
                "status": "FAIL" if test_id in missing else "PASS",
                "log_sha256": _sha256(f"log:{task_id}:{test_id}:{attempt_id}"),
                "detail": "未执行" if test_id in missing else "模拟通过",
            }
            for test_id in request.get("required_test_ids") or []
        ]
        logs = [
            {
                "name": f"logs/{task_id}/{test_id}.log",
                "sha256": _sha256(f"log:{task_id}:{test_id}:{attempt_id}"),
            }
            for test_id in request.get("required_test_ids") or []
        ]
        return self._result(
            request,
            status="SUCCEEDED",
            started=started,
            artifacts=artifacts,
            commit=commit,
            evidence=[f"evidence:{task_id}:{attempt_id}:{index}" for index in range(len(test_results))],
            tests=test_results,
            logs=logs,
        )

    def _result(
        self,
        request: dict[str, Any],
        *,
        status: str,
        started: str,
        artifacts: list[dict[str, Any]] | None = None,
        commit: str | None = None,
        evidence: list[str] | None = None,
        issues: list[dict[str, Any]] | None = None,
        tests: list[dict[str, Any]] | None = None,
        logs: list[dict[str, Any]] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        finished = _now()
        return {
            "task_id": request["task_id"],
            "attempt_id": request["attempt_id"],
            "status": status,
            "artifacts": artifacts or [],
            "candidate_commit": commit,
            "evidence_refs": evidence or [],
            "known_issues": issues or [],
            "logs": logs or [],
            "test_results": tests or [],
            "started_at": started,
            "finished_at": finished,
            "duration_seconds": self.scenario.duration_seconds,
            "error": error,
            "execution_mode": "mock",
            "simulated": True,
        }


class RunnerTaskExecutor:
    """把 §5 请求适配到真实 B04 `AgentRunner`。

    调用方需注入 `runner_callable(request) -> dict`，由它负责构造 B04 envelope、
    调用 `AgentRunner.invoke_agent`、等待终态并把 manifest 转成 §5 结果。
    本适配层只做 §5 契约校验，不解释业务。
    """

    def __init__(self, runner_callable: Callable[[dict[str, Any]], dict[str, Any]]):
        if not callable(runner_callable):
            raise GatewayError("runner_callable 必须可调用")
        self.runner_callable = runner_callable

    def __call__(self, request: dict[str, Any]) -> dict[str, Any]:
        request = validate_request(request)
        produced = self.runner_callable(request)
        if not isinstance(produced, dict):
            raise GatewayError("真实 runner 必须返回 §5 结果对象")
        produced.setdefault("task_id", request["task_id"])
        produced.setdefault("attempt_id", request["attempt_id"])
        produced.setdefault("execution_mode", "runner")
        produced.setdefault("simulated", False)
        return produced


class TaskExecutionGateway:
    """B04 统一执行入口：模式切换 + 结果契约校验。"""

    def __init__(
        self,
        *,
        mode: str = "mock",
        scenario: dict[str, Any] | None = None,
        runner_callable: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ):
        if mode not in EXECUTION_MODES:
            raise GatewayError(f"mode 必须是 {list(EXECUTION_MODES)}")
        self.mode = mode
        if mode == "mock":
            self.executor: Callable[[dict[str, Any]], dict[str, Any]] = MockTaskExecutor(
                MockScenario.from_config(scenario)
            )
        else:
            if runner_callable is None:
                raise GatewayError("runner 模式必须提供 runner_callable")
            self.executor = RunnerTaskExecutor(runner_callable)

    def __call__(self, request: dict[str, Any]) -> dict[str, Any]:
        validated = validate_request(request)
        result = self.executor(validated)
        validated_result = validate_result(result, validated)
        if "error" not in validated_result:
            validated_result["error"] = None
        return validated_result

    @property
    def simulated(self) -> bool:
        return self.mode == "mock"

    def describe(self) -> dict[str, Any]:
        return {"mode": self.mode, "simulated": self.simulated, "result_contract": "b04.task-result.v1"}


def artifacts_digest(artifacts: Iterable[dict[str, Any]]) -> str:
    """产物集合摘要；B06 用它检查“重复产物”。"""
    joined = "|".join(sorted(f"{item['path']}:{item['sha256']}" for item in artifacts))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()
