# B06 质量共识 Agent

B06 负责提案共识评价、任务计划检查、受控测试执行、证据校验和缺陷复测。它只返回带版本绑定的质量结果与事件，不写入任务状态，也不负责派发任务；状态迁移由 B02 编排器负责。

## 功能

- `evaluate_consensus`：检查四类角色立场、六名人类评审、阻断异议、事实验证、条件通过和终止轮次。
- `validate_plan`：检查规格与契约版本、需求覆盖、重复产物、缺失依赖和依赖环，并返回稳定拓扑序。
- `run_case` / `run_suite`：在受限子进程中运行规定测试，支持超时、输出上限、进程树终止和日志哈希。
- `check_evidence`：验证 HMAC、任务与提交上下文、时间窗口、测试编号、退出码、日志和产物 SHA-256。
- `QualityConsensusAgent`：组合上述能力，向 B02 返回验收、拒绝和重验结果。
- `weighted_score`：按固定权重计算方案评分，严格拒绝缺维度、越界值和权重和不为 1 的输入。
- `review_task`（第二阶段 D27—D30）：**开发任务质量门**。对单个任务的 B04 执行结果
  做九项必查，返回 `ACCEPT` / `REJECT` / `REVALIDATION_REQUIRED` 与事件。
- `verify_plan`：计划级只读检查（需求覆盖、依赖环、重复产物、任务字段）。
- `verify_artifacts`：离线核对一组产物的声明哈希。

## 开发任务质量门（D27—D30）

输入输出契约见 [`docs/TASK_ORCHESTRATION.md`](../docs/TASK_ORCHESTRATION.md) §6。

九项必查：

1. 任务字段完整性
2. 需求覆盖（`required_requirements` 全部被覆盖）
3. 依赖环
4. 重复产物（`outputs` 无两个所有者）
5. 版本一致性（`spec_version` / `contract_version` / `plan_version`）
6. 日志哈希
7. 产物哈希
8. 测试编号（`required_test_ids` 全部出现且 `PASS`）
9. 提交引用（合法十六进制哈希）

判定优先级：

```text
任一 blocking finding / 未关闭阻断缺陷  → REJECT
版本或输入快照不一致（无 blocking）      → REVALIDATION_REQUIRED
只有 major / minor                      → ACCEPT（findings 保留）
```

事件类型只允许 `TASK_ACCEPTED`、`TASK_REJECTED`、`TASK_REVALIDATION_REQUIRED`、
`DEFECT_CREATED`、`DEFECT_CLOSED`。**B06 不写 B02 状态**：它只返回事件，
由 B02 作为唯一状态写入者落库与推进。

实现要点：

- 每条阻断 finding 只在 `_open_defects_for_findings` 登记**一次**，
  去重键为 `(code, ref)`：同一违规不会重复成缺陷，不同测试编号的同类违规仍各自登记。
- 缺陷编号形如 `D-<task>-A<attempt>-NNN`，跨尝试唯一，便于事件回放区分新旧缺陷。
- `sha256` 一律校验 `^[0-9a-f]{64}$`，与 B04 `gateway.SHA256_PATTERN` 保持一致。

## 运行

无需第三方测试工具即可运行冒烟测试：

```powershell
python -m tests.smoke
```

完整测试：

```powershell
python -m pip install -e ".[test]"
python -m pytest -q
```

## 证据边界

可信 runner 在与模型 worker 分离的进程中签名证据。签名密钥不能进入任务提示词或模型环境。只有当任务上下文、时间戳、测试编号、退出码、日志哈希、产物哈希和规定证据都与冻结快照一致时，报告才可被接受。

`attempt_id` 标识每次派发，并在重验时递增；`retry_count` 只统计失败重派。上游输入变化使用 `revalidation_id`，只有重验本身失败时才消耗重试预算。

校园失物招领只是外部 fixture，核心模块不写死业务字段。B06 不把模型回复、本地文件存在或多数意见当作测试证据。
