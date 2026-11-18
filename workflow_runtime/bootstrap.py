"""跨工作块装配：把 B01/B02/B03/B04/B06 接成可运行的闭环。

设计约束：
- 本模块只做“依赖注入 + 路径解析”，不含业务规则。
- 各工作块保持独立可测：B04 不导入 B02/B03，B06 不导入 B02/B03。
- 允许在未安装包的情况下从仓库根目录直接运行（使用相对路径 + sys.path 注入）。
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

BLOCK_PATHS = {
    "b01": REPO_ROOT / "b01-spec-planner" / "src",
    "b02": REPO_ROOT / "b02-workflow-agent",
    "b03": REPO_ROOT / "b03",
    "b04": REPO_ROOT / "b04-agent-integration" / "src",
    "b06": REPO_ROOT / "b06-quality-consensus",
}


class WiringError(RuntimeError):
    """跨块装配失败：路径缺失或依赖未安装。"""


def ensure_path(block: str) -> Path:
    if block not in BLOCK_PATHS:
        raise WiringError(f"未知工作块: {block}")
    path = BLOCK_PATHS[block]
    if not path.is_dir():
        raise WiringError(f"{block} 路径不存在: {path}")
    entry = str(path)
    if entry not in sys.path:
        sys.path.insert(0, entry)
    return path


def load_module(block: str, dotted: str) -> ModuleType:
    ensure_path(block)
    try:
        return importlib.import_module(dotted)
    except ModuleNotFoundError as error:
        raise WiringError(f"无法载入 {dotted}: {error}") from error


class WorkflowStack:
    """一个进程内的完整闭环装配。"""

    def __init__(
        self,
        *,
        data_dir: str | Path | None = None,
        execution_mode: str = "mock",
        scenario: dict[str, Any] | None = None,
        runner_callable=None,
    ):
        self.data_dir = Path(data_dir) if data_dir else REPO_ROOT / "work" / "runtime-state"
        self.data_dir.mkdir(parents=True, exist_ok=True)

        spec_input = load_module("b01", "b01.spec_input")
        dynamic_planner = load_module("b01", "b01.dynamic_planner")
        b03_module = load_module("b03", "b03_implementation.task_state")
        b02_module = load_module("b02", "backend.task_orchestrator")
        gateway_module = load_module("b04", "b04.gateway")
        gate_module = load_module("b06", "multiagent_quality.task_gate")

        self.spec_input = spec_input
        self.dynamic_planner = dynamic_planner
        self.task_state_module = b03_module
        self.orchestrator_module = b02_module
        self.gateway_module = gateway_module
        self.gate_module = gate_module

        self.store = b03_module.TaskStateStore(self.data_dir / "task_state.db")
        self.execution_gateway = gateway_module.TaskExecutionGateway(
            mode=execution_mode, scenario=scenario, runner_callable=runner_callable
        )
        self.orchestrator = b02_module.TaskOrchestrator(
            self.store,
            executor=self.execution_gateway,
            reviewer=self._review,
        )

    # ---------- 闭环步骤 ----------

    def plan_from_spec_path(self, spec_path: str | Path, *, plan_version: str | None = None) -> dict[str, Any]:
        spec = self.spec_input.load_spec(spec_path)
        return self.dynamic_planner.build_plan_from_spec(spec, plan_version=plan_version)

    def plan_from_spec_text(self, text: str, *, spec_format: str | None = None,
                            plan_version: str | None = None) -> dict[str, Any]:
        spec = self.spec_input.load_spec_from_text(text, spec_format=spec_format)
        return self.dynamic_planner.build_plan_from_spec(spec, plan_version=plan_version)

    def load_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        return self.orchestrator.load_plan(plan)

    def start_run(self, plan_id: str, *, run_id: str | None = None) -> dict[str, Any]:
        return self.orchestrator.create_run(plan_id, run_id=run_id)

    def step(self, run_id: str) -> dict[str, Any]:
        return self.orchestrator.step(run_id)

    def run(self, run_id: str, *, max_steps: int = 200) -> dict[str, Any]:
        return self.orchestrator.run(run_id, max_steps=max_steps)

    def snapshot(self, run_id: str) -> dict[str, Any]:
        return self.orchestrator.snapshot(run_id)

    def dag(self, run_id: str) -> dict[str, Any]:
        return self.orchestrator.dag(run_id)

    def events(self, run_id: str, **kwargs) -> dict[str, Any]:
        return self.orchestrator.events(run_id, **kwargs)

    def replay(self, run_id: str) -> dict[str, Any]:
        return self.orchestrator.replay(run_id)

    def restore(self, run_id: str) -> dict[str, Any]:
        return self.store.restore_run(run_id)

    # ---------- B06 接入 ----------

    def _review(self, payload: dict[str, Any]) -> dict[str, Any]:
        """B02 已按 §6 组装好 plan/task/run/result/history，这里只转交 B06。"""
        return self.gate_module.review_task(payload)
