# 第二阶段任务编排闭环契约（B01-B06）

本文是第二阶段（设计规格 → 任务分解 → 编排 → 持久化 → 执行 → 验收 → 可视化 → 发布）
的唯一接口契约。各工作块实现时必须严格匹配本文字段名与取值，不得自行改名或增删必填字段。

总体流程分为两个阶段：

```text
第一阶段：用户想法 → 多智能体讨论 → 建设方案 → R1 需求确认
第二阶段：设计规格 → B01 PM Agent 任务分解 → B02 编排 → B03 持久化
          → B04 执行 → B06 验收 → B05 可视化与控制
```

第一阶段的 R1 是第二阶段的需求来源之一；R1 必须确认后才能通过阶段桥接生成设计规格。
第二阶段也支持直接输入独立的 JSON/Markdown design spec。

第二阶段闭环：

```text
设计规格输入 → B01 任务分解 → B02 任务编排 → B03 状态持久化
→ B04 Agent 执行 → B06 证据验收 → B05 DAG 可视化 → 集成发布
```

## 0. 通用约定

- 全部 JSON 使用 UTF-8、`ensure_ascii=False`。
- 时间统一为带时区的 ISO 8601 字符串（UTC，毫秒或秒均可）。字段名统一 `*_at`。
- 标识：`plan_id`、`run_id`、`task_id`、`attempt_id` 均为非空字符串。
- 除 B02 外，任何工作块**不得**直接改变任务状态；B04/B06 只返回结果与事件。
- 路径使用 POSIX 相对路径（`/` 分隔），禁止 `..`、绝对路径、盘符。

## 1. 任务状态（唯一枚举）

```text
PENDING READY RUNNING WAITING_DEPENDENCY REVIEWING FAILED RETRYING BLOCKED SUCCEEDED
```

| 状态 | 含义 |
| --- | --- |
| `PENDING` | 已登记，尚未评估依赖 |
| `READY` | 依赖全部 `SUCCEEDED`，等待派发 |
| `RUNNING` | 已派发，B04 执行中 |
| `WAITING_DEPENDENCY` | 依赖尚未完成（含依赖处于 RUNNING/REVIEWING/READY） |
| `REVIEWING` | B04 已提交候选结果，等待 B06 验收 |
| `FAILED` | 执行失败且重试预算耗尽 |
| `RETRYING` | 失败但仍在重试预算内，等待重新派发 |
| `BLOCKED` | 上游失败/被阻断或存在阻断缺陷，暂停放行 |
| `SUCCEEDED` | B06 验收通过 |

合法迁移（除此之外一律拒绝，并返回冲突错误）：

```text
PENDING        -> READY | WAITING_DEPENDENCY | BLOCKED
WAITING_DEPENDENCY -> READY | BLOCKED
READY          -> RUNNING | BLOCKED
RUNNING        -> REVIEWING | FAILED | RETRYING | BLOCKED
REVIEWING      -> SUCCEEDED | FAILED | RETRYING | BLOCKED
RETRYING       -> READY | FAILED | BLOCKED
FAILED         -> BLOCKED | READY
BLOCKED        -> READY（仅显式解除阻断时）
SUCCEEDED      -> （终态，不再迁移）
```

## 2. B01 输出：plan 文档

`plan.json` 顶层字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `project_id` | string | 是 | 项目标识 |
| `spec_version` | string | 是 | 规格版本 |
| `contract_version` | string | 是 | 契约版本 |
| `plan_version` | string | 是 | 计划版本，格式 `PLAN-<project>-v<n>.<n>` |
| `generated_by` | string | 是 | 生成者 |
| `generated_at` | string | 是 | 生成时间 |
| `required_requirements` | string[] | 是 | 必须全部被覆盖的需求编号 |
| `tasks` | task[] | 是 | 任务列表，非空 |
| `topological_order` | string[] | 是 | 拓扑序，必须与重算结果一致 |
| `input_snapshot` | object | 是 | 见下 |

`input_snapshot`：

```json
{
  "spec_source": "inline|file",
  "spec_format": "json|markdown",
  "spec_sha256": "<hex>",
  "contract_sha256": "<hex>",
  "spec_version": "...",
  "contract_version": "...",
  "requirement_count": 5,
  "design_entry_count": 14
}
```

task 字段（全部必填，`depends_on` 可为空数组）：

```text
role_id block_id task_id project_id req_refs design_refs
spec_version contract_version goal inputs outputs depends_on
capability write_scope acceptance required_test_ids
priority timeout retry_budget escalation_target
```

约束：

- `task_id` 在计划内唯一；`depends_on` 只能引用已存在任务；不允许自依赖与环。
- `outputs` 不允许同一产物有两个所有者。
- 每个 `required_requirements` 至少被一个任务覆盖（`req_refs`）。
- `timeout` 为正整数秒；`retry_budget` 为非负整数。
- `priority` ∈ {`P0`,`P1`,`P2`}；`capability` 非空字符串。

### 2.1 通用规格输入（B01-D12）

规格输入同时支持 JSON 与 Markdown，归一化为：

```json
{
  "spec_version": "...",
  "contract_version": "...",
  "project_id": "...",
  "title": "...",
  "requirements": [
    {"req_id": "R-001", "title": "...", "description": "...", "priority": "must|should|may",
     "acceptance": ["..."], "constraints": ["..."]}
  ],
  "design_entries": [
    {"design_id": "DS-...", "module": "...", "req_refs": ["R-001"], "contract_refs": ["..."],
     "test_refs": ["..."], "rule": "..."}
  ],
  "contract": {
    "contract_version": "...",
    "operations": [{"name": "...", "method": "...", "path": "...", "authenticated": true,
                    "required": ["..."], "status": 200, "data": "..."}],
    "errors": {"CODE": 400},
    "field_bounds": {}
  }
}
```

校园失物招领仅作为 fixture（`b01-spec-planner/docs/fixtures/campus-lost-found.*`），
不得写死为核心流程的唯一输入。

### 2.2 动态计划（B01-D13/D14）

- 任务数量与编号由规格动态推导，**不固定** T01—T11。
- `plan_version` 随输入变化递增；`input_snapshot` 记录输入哈希。
- 同时输出 `traceability.json`：`design_id → req_refs → contract_refs → test_refs → task_refs`。

## 3. B02 任务编排 API

前缀与现有讨论控制器一致：同一 HTTP 服务，路径以 `/api/` 开头。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/task-plans/load` | 载入并校验 plan |
| POST | `/api/task-runs/from-spec` | 设计规格 → B01 动态计划 → 创建任务运行 |
| POST | `/api/task-runs/from-discussion` | 已确认 R1 → 设计规格 → 创建任务运行 |
| GET | `/api/task-plans` | 已载入计划列表 |
| GET | `/api/task-plans/{plan_id}` | 计划详情（含 DAG） |
| POST | `/api/task-runs` | 由计划创建任务运行 |
| GET | `/api/task-runs` | 运行列表 |
| GET | `/api/task-runs/{run_id}` | 运行快照 |
| POST | `/api/task-runs/{run_id}/step` | 推进一个调度步 |
| POST | `/api/task-runs/{run_id}/run` | 推进直到收敛或步数上限 |
| GET | `/api/task-runs/{run_id}/events` | 事件列表（`after`/`limit`） |
| GET | `/api/task-runs/{run_id}/dag` | DAG 视图（节点+边） |
| POST | `/api/task-runs/{run_id}/replay` | 事件回放并与快照比对 |

`POST /api/task-plans/load` 请求：

```json
{"plan": { ... }}            // 或 {"plan_path": "b01-spec-planner/docs/plan.json"}
```

`POST /api/task-runs/from-spec` 支持 `spec_path`、`spec_text` 或内嵌 `spec` 对象。
`POST /api/task-runs/from-discussion` 接收 `discussion_run_id`，只允许状态为 `COMPLETED`
且 R1 为 `CONFIRMED` 的运行进入第二阶段。`GET /api/runs/{run_id}/design-spec` 可读取
阶段桥接产生的 design spec。

响应：

```json
{"plan": {"plan_id": "PLAN-...", "plan_version": "...", "project_id": "...",
          "task_count": 11, "topological_order": ["..."], "input_snapshot": { ... },
          "spec_version": "...", "contract_version": "..."}}
```

`POST /api/task-runs` 请求：`{"plan_id": "...", "run_id": "可选"}`；响应 `{"run": <snapshot>}`。

`POST /api/task-runs/{run_id}/step` 每次最多派发一个 `READY` 任务并完成一次
执行→验收→状态推进；响应 `{"run": <snapshot>, "transitions": [<event>]}`。
`POST .../run` 请求 `{"max_steps": 50}`，响应同 `step` 但 `transitions` 为全部新事件。

### 3.1 运行快照

```json
{
  "run_id": "TASK-RUN-XXXXXXXX",
  "plan_id": "...", "plan_version": "...", "input_snapshot": "<spec_sha256>",
  "project_id": "...", "spec_version": "...", "contract_version": "...",
  "status": "READY|RUNNING|SUCCEEDED|BLOCKED|FAILED",
  "created_at": "...", "updated_at": "...",
  "counts": {"PENDING": 0, "READY": 1, "...": 0},
  "completed": 3, "total": 11, "events_count": 12,
  "tasks": [
    {
      "task_id": "...", "goal": "...", "block_id": "...", "role_id": "...",
      "capability": "...", "priority": "P0",
      "depends_on": ["..."], "req_refs": ["..."], "outputs": ["..."],
      "acceptance": ["..."], "required_test_ids": ["..."], "write_scope": ["..."],
      "state": "SUCCEEDED",
      "attempt_id": 2, "retry_count": 1,
      "candidate_commit": "abc1234", "evidence_refs": ["..."],
      "known_issues": [{"code": "...", "message": "...", "severity": "blocking|major|minor"}],
      "blocked_reason": null, "error": null,
      "started_at": "...", "finished_at": "...", "duration_seconds": 0.4,
      "artifacts": [{"path": "...", "kind": "...", "sha256": "..."}],
      "attempts": [{"attempt_id": 1, "retry_count": 0, "state": "FAILED",
                    "started_at": "...", "finished_at": "...", "reason": "..."}]
    }
  ]
}
```

### 3.2 任务事件（B02-D16）

事件类型（唯一枚举）：

```text
TASK_READY TASK_DISPATCHED TASK_STARTED TASK_OUTPUT_SUBMITTED TASK_REVIEW_REQUESTED
TASK_ACCEPTED TASK_REJECTED TASK_RETRY_SCHEDULED TASK_BLOCKED TASK_COMPLETED TASK_FAILED
```

事件结构：

```json
{
  "event_id": "TEV-000001",
  "sequence": 1,
  "type": "TASK_DISPATCHED",
  "run_id": "TASK-RUN-XXXXXXXX",
  "task_id": "T01",
  "attempt_id": 1,
  "plan_version": "PLAN-...-v1.0",
  "input_snapshot": "<spec_sha256>",
  "candidate_commit": null,
  "producer": "B02",
  "occurred_at": "...",
  "payload": {}
}
```

绑定要求：涉及派发/提交/验收/重试/阻断/完成的事件必须同时带
`task_id`、`attempt_id`、`plan_version`、`input_snapshot`；`TASK_OUTPUT_SUBMITTED`
与 `TASK_ACCEPTED` 必须带 `candidate_commit`。

### 3.3 DAG 视图

```json
{
  "run_id": "...", "plan_version": "...",
  "nodes": [{"id": "T01", "label": "...", "block_id": "B01", "state": "SUCCEEDED",
             "priority": "P0", "attempt_id": 1, "retry_count": 0,
             "duration_seconds": 0.4, "blocked_reason": null,
             "acceptance": ["..."], "required_test_ids": ["..."]}],
  "edges": [{"from": "T01", "to": "T02", "revalidation": false}],
  "counts": {"...": 0}, "topological_order": ["..."]
}
```

## 4. B03 状态持久化（D18/D19）

新增/扩展的持久化能力，全部落在黑板库（`BLACKBOARD_DB`）：

- `task_runs`：运行登记（`run_id`、`plan_id`、`plan_version`、`input_snapshot`、`状态`、`时间`）。
- `execution_state`：已存在，需支持 9 态（见 §1）。
- `task_attempts`：历史尝试（`task_id`、`attempt_id`、`retry_count`、`state`、`started_at`、`finished_at`、`reason`）。
- `artifacts` / `test_evidence` / `issues_decisions`：已存在，保持只追加。
- 断点恢复：`write_checkpoint`/`load_checkpoint` 保存 `plan_version`、各任务状态、
  已处理事件序号、产物清单；恢复后不得重复执行已完成任务。
- 事件回放：按 `occurred_at, event_id` 稳定排序；重复事件幂等；过期 attempt 事件标记 `stale`。

## 5. B04 统一执行 Gateway（D21/D22/D23）

B02 输入（`TaskExecutionRequest`）：

```json
{
  "task_id": "T01", "attempt_id": 1, "plan_version": "PLAN-...-v1.0",
  "input_snapshot": "<spec_sha256>",
  "run_id": "...",
  "role_id": "...", "block_id": "...", "goal": "...",
  "inputs": [{"ref": "...", "kind": "..."}],
  "outputs": ["..."],
  "acceptance": ["..."],
  "required_test_ids": ["..."],
  "write_scope": ["..."],
  "timeout": 600, "retry_budget": 2,
  "candidate_base_commit": null
}
```

B04 输出（`TaskExecutionResult`）：

```json
{
  "task_id": "T01", "attempt_id": 1,
  "status": "SUCCEEDED|FAILED|BLOCKED",
  "artifacts": [{"path": "docs/x.json", "kind": "document", "sha256": "<hex>"}],
  "candidate_commit": "abc1234",
  "evidence_refs": ["..."],
  "known_issues": [{"code": "TOOL_FAILED", "message": "...", "severity": "blocking|major|minor"}],
  "logs": [{"name": "run.log", "sha256": "<hex>"}],
  "test_results": [{"test_id": "TC-01", "status": "PASS|FAIL", "log_sha256": "<hex>", "detail": "..."}],
  "started_at": "...", "finished_at": "...", "duration_seconds": 0.4
}
```

硬性约束：

- B04 **不得**修改 B02 状态，不写黑板库状态表，只返回上述结构。
- `status=SUCCEEDED` 只代表“执行完成并自检通过”，不代表验收通过。
- 每次调用必须返回与请求相同的 `task_id`/`attempt_id`；不一致时 B02 拒绝该结果。
- 网关需支持 `mock`（确定性模拟，可注入失败/超时）与真实 runner 适配两种模式。

## 6. B06 质量门（D27/D28/D29/D30）

输入：

```json
{
  "plan": { ...plan 文档... },
  "task": { ...plan.tasks 中的一项... },
  "run": {"run_id": "...", "plan_version": "...", "input_snapshot": "..."},
  "result": { ...B04 TaskExecutionResult... },
  "history": [{"task_id": "...", "attempt_id": 1, "status": "FAILED"}]
}
```

输出：

```json
{
  "decision": "ACCEPT|REJECT|REVALIDATION_REQUIRED",
  "findings": [{"code": "MISSING_FIELD", "severity": "blocking|major|minor",
                "message": "...", "ref": "..."}],
  "defects": [{"defect_id": "D-0001", "task_id": "T01", "severity": "blocking",
               "status": "open|closed", "message": "..."}],
  "events": [
    {"type": "TASK_ACCEPTED", "task_id": "T01", "attempt_id": 1,
     "candidate_commit": "abc1234", "payload": {"evidence_refs": ["..."]}}
  ]
}
```

`events[].type` 只能取：

```text
TASK_ACCEPTED TASK_REJECTED TASK_REVALIDATION_REQUIRED DEFECT_CREATED DEFECT_CLOSED
```

判定规则（`decision`）：

- 任一 `blocking` finding → `REJECT`。
- 有未关闭的阻断缺陷 → `REJECT`。
- 证据过期/版本不一致（`input_snapshot`、`plan_version`、`spec_version`、
  `contract_version` 与计划不符）→ `REVALIDATION_REQUIRED`。
- 仅 `major`/`minor` finding → `ACCEPT`，但必须在 `findings` 中保留。

必查项（D27/D28/D29/D30）：

1. 任务字段完整性（§2 全字段）。
2. 需求覆盖：`required_requirements` 全部被覆盖。
3. 依赖环：计划无环。
4. 重复产物：`outputs` 无两个所有者。
5. 版本一致性：`spec_version`/`contract_version`/`plan_version` 全链一致。
6. 日志哈希：`logs[].sha256` 与给定文件内容一致（或与声明的哈希一致）。
7. 产物哈希：`artifacts[].sha256` 校验。
8. 测试编号：`required_test_ids` 全部在 `test_results` 中出现且 `PASS`。
9. 提交引用：`candidate_commit` 非空且为合法十六进制短/长哈希。

**B06 不得直接写 B02 状态**，只返回事件，由 B02 消费后推进状态。

## 7. B05 可视化（D24/D25/D26）

- 展示任务 DAG：节点（任务号、目标、负责 Block、状态、重试、耗时）、边（依赖、重验证边）。
- 阻塞任务必须显式标注 `blocked_reason`。
- 点击任务展示：输入、输出、依赖、验收条件、测试编号、日志、提交、缺陷。
- 数据源切换保持不变：`模拟推演` 与 `真实接口` 必须明确区分；
  真实接口指向 B02 `GET /api/task-runs/{run_id}/dag` 与 `GET /api/task-runs/{run_id}`。
- 无后端时回退到本地快照样例，界面不得崩溃、不得把模拟数据显示为真实。

## 8. 演示脚本（最终验收）

`workflow_runtime/pipeline.py` 提供一键演示：

```text
规格输入 → B01 生成计划 → B02 载入并建运行 → 逐步派发 → B04 模拟执行
→ B06 验收 → B03 持久化 → DAG 快照 → 事件回放一致性检查
```

并覆盖：失败→有限重试→阻断缺陷→后续任务 BLOCKED 的场景。
