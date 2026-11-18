# workflow_runtime

通用开发任务编排运行时。它不依赖校园业务，输入任何符合 B01 任务计划契约的 `plan.json`，即可校验 DAG、计算可执行任务、派发执行器、接收验收结果并保存事件快照。

```powershell
cd workflow_runtime
python -m workflow_runtime ..\b01-spec-planner\docs\plan.json --run
```

输出包含：

- `states`：每个任务的最终状态
- `events`：`TASK_READY`、`TASK_DISPATCHED`、`TASK_REVIEW_REQUESTED`、`TASK_ACCEPTED` 等事件
- `completed` / `total`：完成任务数和总任务数

当前默认执行器是演示适配器；接入真实 B04 Agent 时，传入实现 `executor(task) -> result` 的适配器即可。B02 负责控制调度，B03 保存快照，B06 应在 `REVIEWING` 阶段返回正式证据后再接受任务。
