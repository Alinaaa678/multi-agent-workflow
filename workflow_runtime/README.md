# workflow_runtime

B01-B06 第二阶段的**闭环装配与演示入口**。

它不是新工作块，而是把各工作块的公开接口接起来，用来证明：

```text
设计规格输入 → B01 任务分解 → B02 任务编排 → B03 状态持久化
→ B04 Agent 执行 → B06 证据验收 → B05 DAG 可视化 → 集成发布
```

## 内容

| 文件 | 作用 |
| --- | --- |
| `bootstrap.py` | 跨块装配（`WorkflowStack`）：把 B01/B02/B03/B04/B06 注入成一个可运行闭环 |
| `pipeline.py` | 一键演示：规格 → 计划 → 运行 → 派发 → 执行 → 验收 → DAG → 回放 |
| `verify_contracts.py` | 契约一致性检查，CI 直接运行 |
| `cli.py` / `__main__.py` | `python -m workflow_runtime` 入口 |
| `make_frontend_fixture.py` | 用真实编排器生成 B05 的离线快照 fixture |
| `scheduler.py` | 早期轻量调度演示（保留向后兼容） |

## 用法

在仓库根目录执行：

```powershell
# 成功闭环：全部任务验收通过
python -m workflow_runtime pipeline --scenario success

# 失败 → 有限重试耗尽 → 下游阻塞
python -m workflow_runtime pipeline --scenario retry

# 执行被阻断 → 阻断向下游传播
python -m workflow_runtime pipeline --scenario blocked

# 缺少规定测试证据 → B06 驳回并登记缺陷
python -m workflow_runtime pipeline --scenario evidence

# 契约一致性检查（CI 使用）
python workflow_runtime/verify_contracts.py

# 旧用法（仅跑轻量 DAG 调度器）
python -m workflow_runtime b01-spec-planner/docs/plan.json --run
```

每个场景都会打印：计划版本、运行状态、完成数、DAG 规模、检查点序号、
回放一致性、事件类型清单，以及失败/阻塞任务明细。

## 与各工作块的边界

- 执行由 B04 `b04.gateway.TaskExecutionGateway` 提供（默认 `mock` 模式，
  确定性、可注入失败/阻断/缺证据）。真实 runner 通过 `runner_callable` 注入。
- 验收由 B06 `multiagent_quality.task_gate.review_task` 提供，只返回判定与事件，
  **不写任何状态**；状态推进始终由 B02 完成。
- 状态与事件落盘由 B03 `b03_implementation.task_state.TaskStateStore` 完成。
- 计划由 B01 `b01.dynamic_planner` 生成，`b01.spec_input` 负责通用规格归一化。

## 数据落地

演示状态写入 `work/demo-state/`（已在 `.gitignore` 中忽略）。
每次运行 `pipeline` 会重建该目录，避免旧状态干扰。

## 一份完整链路的示例

```powershell
# 1) B01：任意规格 → 计划
cd b01-spec-planner
python run_checks.py plan --spec docs/fixtures/lab-equipment-loan.spec.md `
  --out-dir docs/fixtures --plan-name lab.plan.json `
  --traceability-name lab.traceability.json

# 2) B02：载入计划并建运行（HTTP）
cd ..\b02-workflow-agent
python -m backend.app --host 127.0.0.1 --port 7100
# 另开终端：
#   POST /api/task-plans/load  {"plan_path": "b01-spec-planner/docs/fixtures/lab.plan.json"}
#   POST /api/task-runs        {"plan_id": "PLAN-lab-equipment-loan-v1.0"}
#   POST /api/task-runs/{id}/run
#   GET  /api/task-runs/{id}/dag

# 3) B05：可视化
cd ..\frontend
npm run dev
# 切换到「真实接口」，填入上面的运行编号
```
