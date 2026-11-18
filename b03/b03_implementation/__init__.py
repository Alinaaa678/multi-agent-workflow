"""B03 黑板数据 Agent 实现包。

负责人：陈怡雯 · 角色标识：BlackboardAgent
模块：
  - blackboard.py     task_store：状态、租约、快照、事件、检查点
  - artifact_store.py artifact_store：版本化产物
  - campus_db.py      案例 T02 数据层
  - task_state.py     task_state_store：任务运行/状态/尝试历史/运行事件（D18/D19）
"""

from __future__ import annotations

__all__ = ["TASK_STATES", "TaskStateStore"]

TASK_STATES = (
    "PENDING",
    "READY",
    "RUNNING",
    "WAITING_DEPENDENCY",
    "REVIEWING",
    "FAILED",
    "RETRYING",
    "BLOCKED",
    "SUCCEEDED",
)


def __getattr__(name: str):
    """延迟导入，避免仅为读取 TASK_STATES 就打开数据库。"""
    if name == "TaskStateStore":
        from .task_state import TaskStateStore as _cls

        return _cls
    raise AttributeError(name)
