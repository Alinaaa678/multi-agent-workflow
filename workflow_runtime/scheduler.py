from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
import json
from typing import Callable

class TaskState(str, Enum):
    PENDING="PENDING"; READY="READY"; RUNNING="RUNNING"; REVIEWING="REVIEWING"; SUCCEEDED="SUCCEEDED"; FAILED="FAILED"; BLOCKED="BLOCKED"

@dataclass
class TaskRecord:
    task: dict
    state: TaskState = TaskState.PENDING
    attempt_id: int = 0
    retry_count: int = 0
    result: dict | None = None
    history: list[dict] = field(default_factory=list)

class TaskScheduler:
    """通用 DAG 调度器；不关心具体领域，也不允许执行器自行改变状态。"""
    def __init__(self, plan: dict, executor: Callable[[dict], dict] | None = None):
        self.plan = plan; self.executor = executor or (lambda task: {"status":"SUCCEEDED"})
        self.records = {t["task_id"]: TaskRecord(t) for t in plan.get("tasks", [])}
        if not self.records: raise ValueError("plan.tasks must not be empty")
        self._validate()
        self.events=[]

    @classmethod
    def from_file(cls, path, executor=None):
        return cls(json.loads(Path(path).read_text(encoding="utf-8")), executor)

    def _validate(self):
        ids=set(self.records)
        for tid,r in self.records.items():
            deps=r.task.get("depends_on",[])
            if any(d not in ids for d in deps): raise ValueError(f"{tid}: unknown dependency")
        visiting=set(); done=set()
        def visit(tid):
            if tid in visiting: raise ValueError("dependency cycle")
            if tid in done:return
            visiting.add(tid)
            for dep in self.records[tid].task.get("depends_on",[]): visit(dep)
            visiting.remove(tid); done.add(tid)
        for tid in ids: visit(tid)

    def _emit(self, event, tid, **payload):
        item={"event":event,"task_id":tid,"payload":payload}; self.events.append(item); self.records[tid].history.append(item)

    def ready(self):
        result=[]
        for tid,r in self.records.items():
            if r.state != TaskState.PENDING: continue
            deps=[self.records[d].state for d in r.task.get("depends_on",[])]
            if any(s in (TaskState.FAILED,TaskState.BLOCKED) for s in deps):
                r.state=TaskState.BLOCKED; self._emit("TASK_BLOCKED",tid,reason="dependency_failed"); continue
            if all(s==TaskState.SUCCEEDED for s in deps):
                r.state=TaskState.READY; self._emit("TASK_READY",tid); result.append(tid)
        return result

    def dispatch_one(self, tid):
        r=self.records[tid]
        if r.state != TaskState.READY: raise ValueError(f"{tid} is not READY")
        r.state=TaskState.RUNNING; r.attempt_id+=1; self._emit("TASK_DISPATCHED",tid,attempt_id=r.attempt_id)
        try: result=self.executor({**r.task,"attempt_id":r.attempt_id})
        except Exception as exc: result={"status":"FAILED","error":str(exc)}
        r.result=result
        if result.get("status") in ("SUCCEEDED","PASSED"):
            r.state=TaskState.REVIEWING; self._emit("TASK_REVIEW_REQUESTED",tid,attempt_id=r.attempt_id)
        else:
            r.retry_count+=1
            if r.retry_count <= int(r.task.get("retry_budget",0)):
                r.state=TaskState.PENDING; self._emit("TASK_RETRY_SCHEDULED",tid,retry_count=r.retry_count)
            else:
                r.state=TaskState.FAILED; self._emit("TASK_FAILED",tid,error=result.get("error"))
        return result

    def accept(self, tid, accepted=True, evidence=None):
        r=self.records[tid]
        if r.state != TaskState.REVIEWING: raise ValueError(f"{tid} is not REVIEWING")
        if accepted:
            r.state=TaskState.SUCCEEDED; self._emit("TASK_ACCEPTED",tid,evidence_refs=evidence or [])
        else:
            r.state=TaskState.FAILED; self._emit("TASK_REJECTED",tid,evidence_refs=evidence or [])

    def run(self, max_steps=1000):
        steps=0
        while steps < max_steps:
            steps+=1; ready=self.ready()
            if not ready:
                if all(r.state in (TaskState.SUCCEEDED,TaskState.BLOCKED,TaskState.FAILED) for r in self.records.values()): break
                if any(r.state==TaskState.REVIEWING for r in self.records.values()):
                    for tid,r in self.records.items():
                        if r.state==TaskState.REVIEWING:self.accept(tid,True,[f"runtime:{tid}:{r.attempt_id}"])
                    continue
                break
            for tid in ready:self.dispatch_one(tid)
        return self.snapshot()

    def snapshot(self):
        return {"plan_version":self.plan.get("plan_version"),"states":{tid:r.state.value for tid,r in self.records.items()},"events":self.events,"completed":sum(r.state==TaskState.SUCCEEDED for r in self.records.values()),"total":len(self.records)}
