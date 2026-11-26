# B02 异常处理与恢复测试报告

## 1. 交付范围

本报告对应 B02 工作流编排与任务调度中的异常处理交付物，覆盖：

- 任务执行超时、有限重试、预算耗尽与下游阻塞；
- 执行器异常和迟到结果隔离；
- 进程崩溃后 `RUNNING` / `REVIEWING` 任务的恢复；
- 暂停、继续、取消与恢复之间的约束；
- 任务尝试历史、运行事件、DAG 快照和事件回放一致性。

实现入口为 `b02-workflow-agent/backend/task_orchestrator.py`，HTTP 适配位于
`task_api.py` 与 `app.py`。B02 仍是唯一任务状态写入者。

## 2. 超时处理

B02 按任务计划中的正整数 `timeout` 秒独立计时，不再只是把字段转交给 B04。
超时后生成标准失败结果：

```json
{
  "status": "FAILED",
  "error": "TASK_TIMEOUT: 执行超过 1 秒",
  "known_issues": [{"code": "TASK_TIMEOUT", "severity": "blocking"}]
}
```

该结果复用现有 `_fail_attempt()` 流程：预算内 `RETRYING → READY`，预算耗尽后
`FAILED`，下游因 `upstream_failed:<task_id>` 进入 `BLOCKED`。每次拒绝、重试、
失败都保留 `attempt_id`、原因和结构化问题。

执行期间不持有状态锁，因此慢任务不会阻塞暂停、取消和快照查询。执行器在隔离的
守护线程中运行，迟到结果只写入本次调用的队列；若 attempt 已变化或任务已被恢复，
旧结果会标记 `stale` 并被编排器丢弃，不覆盖新状态。

**边界：超时只停止 B02 等待，不保证强杀外部同步进程，也不能撤销其副作用。**
需要硬取消时，应由具体 runner 提供进程终止或取消令牌能力。

## 3. 崩溃恢复

### 只读检查

```http
GET /api/task-runs/{run_id}/restore
```

返回检查点、可继续任务、已完成任务和中断任务，不修改任何状态。

### 显式恢复

```http
POST /api/task-runs/{run_id}/recover
Content-Type: application/json

{"request_id":"CTRL-RECOVER-1","operator":"teacher","reason":"PROCESS_RESTART_RECOVERY"}
```

处理规则：

1. 找出 `RUNNING` / `REVIEWING` 任务，并先确认其上游全部 `SUCCEEDED`。
2. 将旧 attempt 记录为 `INTERRUPTED`，保留原 `retry_count`，不消耗重试预算。
3. 清除旧 candidate、review、错误和执行现场，迁移为 `READY`。
4. 发出带 `recovered_after_restart=true` 的 `TASK_READY` 和运行级 `RUN_RECOVERED`。
5. 返回 `ack`、`recovered_tasks` 和最新快照，**不自动重跑**；调用方再执行 `/step` 或 `/run`。

`SUCCEEDED` 是终态，不会重复派发。暂停运行恢复后仍为 `PAUSED`，必须 `/resume`
才能继续；已取消运行拒绝恢复。依赖未全部成功时，整个恢复请求返回 `409`，不会只恢复一部分。

## 4. 新增异常测试

测试文件：`b02-workflow-agent/tests/test_task_recovery.py`，另有一项真实 HTTP 接口测试位于
`test_task_api.py`。

| 场景 | 验证点 | 结果 |
| --- | --- | --- |
| 连续超时且重试预算为 1 | 共执行 2 次；首次重排队，最终失败；下游阻塞；事件回放一致 | 通过 |
| 超时线程随后返回成功 | 迟到结果不改变最终 `FAILED`，不增加 attempt，不破坏回放 | 通过 |
| `timeout=0` | 派发前拒绝，任务保持 `READY`，attempt 不增加 | 通过 |
| `RUNNING` 与 `REVIEWING` 崩溃现场 | 旧尝试记为 `INTERRUPTED`，重新进入 `READY`，重试计数不变 | 通过 |
| 恢复后继续运行 | 新 attempt 单调增加，已成功任务不重复执行，`RUN_RECOVERED` 可回放 | 通过 |
| 暂停与取消约束 | 暂停恢复后仍禁止派发；取消运行返回 `409` | 通过 |
| 中断任务依赖未成功 | 拒绝部分恢复，原状态保持不变 | 通过 |
| 真实 HTTP `/recover` | 只读 `/restore` 不改状态；恢复返回 200，随后可运行至成功 | 通过 |

## 5. 验证命令与结果

```bash
cd b02-workflow-agent
python -m unittest discover -s tests -v
```

原有 111 项回归与新增 6 项测试均已通过，合计 **117 项**。新增超时测试使用真实 1 秒
超时和迟到结果，不以 mock 状态断言代替调度行为；HTTP 测试使用临时数据库和真实本地服务。

## 6. 交付边界

- 恢复是进程重启后的显式操作，不会在服务启动时未经确认自动重跑任务。
- 当前执行器是课堂原型的同步适配器；守护线程提供状态隔离，不等于生产级进程 supervisor。
- 本地 SQLite 与同步执行模型适合单机演示，不宣称支持分布式多实例调度。
- 所有异常结论均可通过任务快照、attempt 历史和有序事件流核验，不伪造成功结果。
