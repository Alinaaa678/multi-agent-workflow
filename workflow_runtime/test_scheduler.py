import json
from .scheduler import TaskScheduler,TaskState
def test_dag_dispatch_and_review():
    p={"plan_version":"P1","tasks":[{"task_id":"A","depends_on":[],"retry_budget":0},{"task_id":"B","depends_on":["A"],"retry_budget":0}]}
    s=TaskScheduler(p); out=s.run(); assert out["completed"]==2; assert out["states"]=={"A":"SUCCEEDED","B":"SUCCEEDED"}
def test_cycle_is_rejected():
    try: TaskScheduler({"tasks":[{"task_id":"A","depends_on":["B"]},{"task_id":"B","depends_on":["A"]}]})
    except ValueError as e: assert "cycle" in str(e)
    else: assert False
