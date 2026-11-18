"""B03-D18/D19：开发任务的注册、执行状态、尝试历史与运行级持久化。

职责边界（与 blackboard.py 一致）：
- **B02 独占状态迁移决策**：本模块只提供原子落盘与只读查询，不判断业务迁移是否合法。
- 事件、产物、证据、缺陷只追加；更正通过新记录表达。
- 断点恢复所需的一切（计划版本、任务状态、已处理事件、产物清单）可一次性读写。

本模块复用 `blackboard.db`，与讨论流程的黑板数据共用同一数据库文件，
但表结构互相独立，避免把两类过程数据混在一起。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator

from .config import BLACKBOARD_DB
from .errors import InvalidState, NotFound, VersionConflict

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

TERMINAL_STATES = frozenset({"SUCCEEDED"})
ACTIVE_STATES = frozenset({"RUNNING", "REVIEWING"})
FAILURE_STATES = frozenset({"FAILED", "BLOCKED"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _uuid() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True)
class TaskStateRecord:
    task_id: str
    state: str
    attempt_id: int
    retry_count: int
    rev: int


@dataclass(frozen=True)
class EventRecord:
    event_id: str
    sequence: int
    event_type: str
    run_id: str | None
    task_id: str | None
    attempt_id: int | None
    occurred_at: str


class TaskStateStore:
    """任务运行注册表、执行状态、尝试历史与运行事件。"""

    def __init__(self, db_path=BLACKBOARD_DB):
        self.db_path = str(db_path)
        self.initialize()

    # ---------- 基础设施 ----------

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    def initialize(self) -> None:
        ddl = """
        CREATE TABLE IF NOT EXISTS task_plans (
            plan_id TEXT PRIMARY KEY,
            plan_version TEXT NOT NULL,
            project_id TEXT NOT NULL,
            spec_version TEXT NOT NULL,
            contract_version TEXT NOT NULL,
            input_snapshot TEXT NOT NULL,
            task_count INTEGER NOT NULL,
            topological_order_json TEXT NOT NULL DEFAULT '[]',
            document_json TEXT NOT NULL,
            loaded_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS task_runs (
            run_id TEXT PRIMARY KEY,
            plan_id TEXT NOT NULL,
            plan_version TEXT NOT NULL,
            project_id TEXT NOT NULL,
            spec_version TEXT NOT NULL,
            contract_version TEXT NOT NULL,
            input_snapshot TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS task_run_tasks (
            run_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            state TEXT NOT NULL,
            attempt_id INTEGER NOT NULL DEFAULT 0,
            retry_count INTEGER NOT NULL DEFAULT 0,
            candidate_commit TEXT,
            blocked_reason TEXT,
            error TEXT,
            started_at TEXT,
            finished_at TEXT,
            duration_seconds REAL,
            evidence_refs_json TEXT NOT NULL DEFAULT '[]',
            known_issues_json TEXT NOT NULL DEFAULT '[]',
            artifacts_json TEXT NOT NULL DEFAULT '[]',
            result_json TEXT,
            review_json TEXT,
            rev INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (run_id, task_id)
        );
        CREATE TABLE IF NOT EXISTS task_attempts (
            run_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            attempt_id INTEGER NOT NULL,
            retry_count INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            reason TEXT,
            candidate_commit TEXT,
            result_json TEXT,
            PRIMARY KEY (run_id, task_id, attempt_id)
        );
        CREATE TABLE IF NOT EXISTS task_run_events (
            event_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            task_id TEXT,
            attempt_id INTEGER,
            plan_version TEXT NOT NULL,
            input_snapshot TEXT NOT NULL,
            candidate_commit TEXT,
            producer TEXT,
            payload_json TEXT NOT NULL DEFAULT '{}',
            occurred_at TEXT NOT NULL,
            UNIQUE (run_id, sequence)
        );
        CREATE TABLE IF NOT EXISTS task_run_checkpoints (
            run_id TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_run_tasks_run ON task_run_tasks(run_id);
        CREATE INDEX IF NOT EXISTS idx_run_events_run ON task_run_events(run_id, sequence);
        CREATE INDEX IF NOT EXISTS idx_task_attempts ON task_attempts(run_id, task_id);
        """
        with self._connect() as conn:
            conn.executescript(ddl)
            conn.commit()

    # ---------- 计划登记 ----------

    def register_plan(self, plan_id: str, document: dict[str, Any]) -> None:
        """登记计划文档；同一 plan_id 内容不同则拒绝（版本必须由 plan_version 区分）。"""
        payload = json.dumps(document, ensure_ascii=False, sort_keys=True)
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT document_json FROM task_plans WHERE plan_id=?", (plan_id,)
            ).fetchone()
            if existing is not None and existing["document_json"] != payload:
                raise VersionConflict(f"plan {plan_id} 已存在且内容不同，请使用新的 plan_version")
            conn.execute(
                """INSERT OR REPLACE INTO task_plans
                   (plan_id, plan_version, project_id, spec_version, contract_version,
                    input_snapshot, task_count, topological_order_json, document_json, loaded_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    plan_id,
                    document["plan_version"],
                    document["project_id"],
                    document["spec_version"],
                    document["contract_version"],
                    (document.get("input_snapshot") or {}).get("spec_sha256", ""),
                    len(document.get("tasks") or []),
                    json.dumps(document.get("topological_order") or [], ensure_ascii=False),
                    payload,
                    _now(),
                ),
            )

    def get_plan(self, plan_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM task_plans WHERE plan_id=?", (plan_id,)).fetchone()
            return self._plan_row(row) if row else None

    def get_plan_document(self, plan_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT document_json FROM task_plans WHERE plan_id=?", (plan_id,)).fetchone()
            return json.loads(row["document_json"]) if row else None

    def list_plans(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM task_plans ORDER BY loaded_at DESC").fetchall()
            return [self._plan_row(row) for row in rows]

    def find_plan_by_version(self, plan_version: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM task_plans WHERE plan_version=? ORDER BY loaded_at DESC LIMIT 1",
                (plan_version,),
            ).fetchone()
            return self._plan_row(row) if row else None

    @staticmethod
    def _plan_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "plan_id": row["plan_id"],
            "plan_version": row["plan_version"],
            "project_id": row["project_id"],
            "spec_version": row["spec_version"],
            "contract_version": row["contract_version"],
            "input_snapshot": row["input_snapshot"],
            "task_count": row["task_count"],
            "topological_order": json.loads(row["topological_order_json"]),
            "loaded_at": row["loaded_at"],
        }

    # ---------- 运行 ----------

    def create_run(
        self,
        *,
        run_id: str,
        plan_id: str,
        plan_version: str,
        project_id: str,
        spec_version: str,
        contract_version: str,
        input_snapshot: str,
        tasks: list[dict[str, Any]],
        status: str = "READY",
    ) -> dict[str, Any]:
        """原子创建运行与全部任务初始状态；重复 run_id 拒绝。"""
        now = _now()
        with self._tx() as conn:
            existing = conn.execute("SELECT run_id FROM task_runs WHERE run_id=?", (run_id,)).fetchone()
            if existing is not None:
                raise VersionConflict(f"run {run_id} 已存在")
            conn.execute(
                """INSERT INTO task_runs
                   (run_id, plan_id, plan_version, project_id, spec_version, contract_version,
                    input_snapshot, status, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    plan_id,
                    plan_version,
                    project_id,
                    spec_version,
                    contract_version,
                    input_snapshot,
                    status,
                    now,
                    now,
                ),
            )
            for task in tasks:
                conn.execute(
                    """INSERT INTO task_run_tasks
                       (run_id, task_id, state, attempt_id, retry_count, rev)
                       VALUES (?,?,?,?,?,?)""",
                    (run_id, task["task_id"], "PENDING", 0, 0, 0),
                )
        return self.get_run(run_id) or {}

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM task_runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                return None
            return dict(row)

    def list_runs(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT run_id, plan_id, plan_version, project_id, status, created_at, updated_at "
                "FROM task_runs ORDER BY created_at DESC"
            ).fetchall()
            return [dict(row) for row in rows]

    def set_run_status(self, run_id: str, status: str) -> None:
        with self._tx() as conn:
            updated = conn.execute(
                "UPDATE task_runs SET status=?, updated_at=? WHERE run_id=?",
                (status, _now(), run_id),
            ).rowcount
            if not updated:
                raise NotFound(run_id)

    def touch_run(self, run_id: str) -> None:
        with self._tx() as conn:
            conn.execute("UPDATE task_runs SET updated_at=? WHERE run_id=?", (_now(), run_id))

    # ---------- 任务状态 ----------

    def list_task_states(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM task_run_tasks WHERE run_id=? ORDER BY task_id", (run_id,)
            ).fetchall()
            return [self._task_row(row) for row in rows]

    def get_task_state(self, run_id: str, task_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM task_run_tasks WHERE run_id=? AND task_id=?", (run_id, task_id)
            ).fetchone()
            return self._task_row(row) if row else None

    @staticmethod
    def _task_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "run_id": row["run_id"],
            "task_id": row["task_id"],
            "state": row["state"],
            "attempt_id": row["attempt_id"],
            "retry_count": row["retry_count"],
            "candidate_commit": row["candidate_commit"],
            "blocked_reason": row["blocked_reason"],
            "error": row["error"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "duration_seconds": row["duration_seconds"],
            "evidence_refs": json.loads(row["evidence_refs_json"]),
            "known_issues": json.loads(row["known_issues_json"]),
            "artifacts": json.loads(row["artifacts_json"]),
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "review": json.loads(row["review_json"]) if row["review_json"] else None,
            "rev": row["rev"],
        }

    def update_task_state(
        self,
        run_id: str,
        task_id: str,
        *,
        expected_rev: int | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        """原子更新任务状态（可选 CAS）。B02 负责判断迁移合法性。"""
        state = fields.get("state")
        if state is not None and state not in TASK_STATES:
            raise InvalidState(state)
        json_fields = {
            "evidence_refs": "evidence_refs_json",
            "known_issues": "known_issues_json",
            "artifacts": "artifacts_json",
            "result": "result_json",
            "review": "review_json",
        }
        assignments: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            column = json_fields.get(key, key)
            assignments.append(f"{column}=?")
            values.append(json.dumps(value, ensure_ascii=False) if key in json_fields else value)
        assignments.append("rev=rev+1")
        with self._tx() as conn:
            row = conn.execute(
                "SELECT rev FROM task_run_tasks WHERE run_id=? AND task_id=?", (run_id, task_id)
            ).fetchone()
            if row is None:
                raise NotFound(f"{run_id}/{task_id}")
            if expected_rev is not None and row["rev"] != expected_rev:
                raise VersionConflict(
                    f"{task_id}: expected_rev={expected_rev}, current_rev={row['rev']}"
                )
            conn.execute(
                f"UPDATE task_run_tasks SET {', '.join(assignments)} WHERE run_id=? AND task_id=?",
                (*values, run_id, task_id),
            )
            conn.execute("UPDATE task_runs SET updated_at=? WHERE run_id=?", (_now(), run_id))
        updated = self.get_task_state(run_id, task_id)
        assert updated is not None
        return updated

    # ---------- 尝试历史 ----------

    def record_attempt(
        self,
        run_id: str,
        task_id: str,
        *,
        attempt_id: int,
        retry_count: int,
        state: str,
        started_at: str | None = None,
        finished_at: str | None = None,
        reason: str | None = None,
        candidate_commit: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO task_attempts
                   (run_id, task_id, attempt_id, retry_count, state, started_at, finished_at,
                    reason, candidate_commit, result_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    task_id,
                    attempt_id,
                    retry_count,
                    state,
                    started_at,
                    finished_at,
                    reason,
                    candidate_commit,
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                ),
            )

    def list_attempts(self, run_id: str, task_id: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if task_id is None:
                rows = conn.execute(
                    "SELECT * FROM task_attempts WHERE run_id=? ORDER BY task_id, attempt_id",
                    (run_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM task_attempts WHERE run_id=? AND task_id=? ORDER BY attempt_id",
                    (run_id, task_id),
                ).fetchall()
            return [
                {
                    "task_id": row["task_id"],
                    "attempt_id": row["attempt_id"],
                    "retry_count": row["retry_count"],
                    "state": row["state"],
                    "started_at": row["started_at"],
                    "finished_at": row["finished_at"],
                    "reason": row["reason"],
                    "candidate_commit": row["candidate_commit"],
                }
                for row in rows
            ]

    # ---------- 运行事件 ----------

    def append_run_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """追加运行事件；同一 event_id 幂等，sequence 唯一。"""
        event_id = event.get("event_id") or _uuid()
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT * FROM task_run_events WHERE event_id=?", (event_id,)
            ).fetchone()
            if existing is not None:
                return self._event_row(existing)
            sequence = event.get("sequence")
            if sequence is None:
                row = conn.execute(
                    "SELECT COALESCE(MAX(sequence), 0) AS m FROM task_run_events WHERE run_id=?",
                    (event["run_id"],),
                ).fetchone()
                sequence = int(row["m"]) + 1
            conn.execute(
                """INSERT INTO task_run_events
                   (event_id, run_id, sequence, event_type, task_id, attempt_id, plan_version,
                    input_snapshot, candidate_commit, producer, payload_json, occurred_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event_id,
                    event["run_id"],
                    sequence,
                    event["type"],
                    event.get("task_id"),
                    event.get("attempt_id"),
                    event["plan_version"],
                    event["input_snapshot"],
                    event.get("candidate_commit"),
                    event.get("producer", "B02"),
                    json.dumps(event.get("payload") or {}, ensure_ascii=False),
                    event.get("occurred_at") or _now(),
                ),
            )
            stored = conn.execute(
                "SELECT * FROM task_run_events WHERE event_id=?", (event_id,)
            ).fetchone()
        assert stored is not None
        return self._event_row(stored)

    @staticmethod
    def _event_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "event_id": row["event_id"],
            "sequence": row["sequence"],
            "type": row["event_type"],
            "run_id": row["run_id"],
            "task_id": row["task_id"],
            "attempt_id": row["attempt_id"],
            "plan_version": row["plan_version"],
            "input_snapshot": row["input_snapshot"],
            "candidate_commit": row["candidate_commit"],
            "producer": row["producer"],
            "payload": json.loads(row["payload_json"]),
            "occurred_at": row["occurred_at"],
        }

    def list_run_events(
        self,
        run_id: str,
        *,
        after_sequence: int | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if after_sequence is None:
                rows = conn.execute(
                    "SELECT * FROM task_run_events WHERE run_id=? ORDER BY sequence LIMIT ?",
                    (run_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM task_run_events WHERE run_id=? AND sequence>? ORDER BY sequence LIMIT ?",
                    (run_id, after_sequence, limit),
                ).fetchall()
            return [self._event_row(row) for row in rows]

    # ---------- 检查点 / 断点恢复 ----------

    def write_run_checkpoint(self, run_id: str, payload: dict[str, Any]) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO task_run_checkpoints (run_id, payload_json, updated_at) VALUES (?,?,?)",
                (run_id, json.dumps(payload, ensure_ascii=False, sort_keys=True), _now()),
            )

    def load_run_checkpoint(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM task_run_checkpoints WHERE run_id=?", (run_id,)
            ).fetchone()
            return json.loads(row["payload_json"]) if row else None

    def build_run_checkpoint(self, run_id: str) -> dict[str, Any]:
        """由当前库内状态重建检查点：计划版本、任务状态、最后事件序号、产物清单。"""
        run = self.get_run(run_id)
        if run is None:
            raise NotFound(run_id)
        tasks = self.list_task_states(run_id)
        events = self.list_run_events(run_id, limit=1_000_000)
        artifacts: dict[str, list[dict[str, Any]]] = {}
        for task in tasks:
            if task["artifacts"]:
                artifacts[task["task_id"]] = task["artifacts"]
        return {
            "run_id": run_id,
            "plan_id": run["plan_id"],
            "plan_version": run["plan_version"],
            "input_snapshot": run["input_snapshot"],
            "status": run["status"],
            "last_event_sequence": events[-1]["sequence"] if events else 0,
            "task_states": {
                task["task_id"]: {
                    "state": task["state"],
                    "attempt_id": task["attempt_id"],
                    "retry_count": task["retry_count"],
                }
                for task in tasks
            },
            "artifacts": artifacts,
            "saved_at": _now(),
        }

    def restore_run(self, run_id: str) -> dict[str, Any]:
        """断点恢复：返回可重新驱动的状态，不重复执行已完成任务。"""
        checkpoint = self.load_run_checkpoint(run_id) or self.build_run_checkpoint(run_id)
        states = self.list_task_states(run_id)
        resumable = [
            task["task_id"]
            for task in states
            if task["state"] in ("READY", "RETRYING", "WAITING_DEPENDENCY", "PENDING")
        ]
        completed = [task["task_id"] for task in states if task["state"] == "SUCCEEDED"]
        in_flight = [
            task["task_id"]
            for task in states
            if task["state"] in ACTIVE_STATES
        ]
        return {
            "checkpoint": checkpoint,
            "resumable_tasks": resumable,
            "completed_tasks": completed,
            "in_flight_tasks": in_flight,
        }

    # ---------- 只读聚合（供 B05 与 B06） ----------

    def get_run_view(self, run_id: str) -> dict[str, Any]:
        run = self.get_run(run_id)
        if run is None:
            raise NotFound(run_id)
        tasks = self.list_task_states(run_id)
        counts = {state: 0 for state in TASK_STATES}
        for task in tasks:
            counts[task["state"]] = counts.get(task["state"], 0) + 1
        return {
            "run": run,
            "tasks": tasks,
            "counts": counts,
            "attempts": self.list_attempts(run_id),
            "events": self.list_run_events(run_id),
        }
