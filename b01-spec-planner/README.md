# B01 规格规划 Agent 参考实现

本目录是第 10 组 B01（SpecPlannerAgent）的参考交付包，依据桌面《项目实践》中两份以“第10组”开头的团队文档生成：

- `docs/sources/第10组_第一部分_多智能体讨论与需求确认(1).docx`
- `docs/sources/第10组_第二部分_设计规格与开发任务编排(1).docx`

## 重要边界

团队文档明确说明案例推演不构成真实六人确认记录。因此本包默认生成 **REFERENCE_FROZEN** 参考基线，只用于设计、联调和评审，不得作为真实任务派发或“已确认 R1”的证据。真实使用前必须由六人填写确认状态，并重新运行基线冻结。

B01 不负责：流程调度状态写入（B02）、黑板持久化（B03）、Agent/模型执行（B04）、界面控制（B05）、测试结果宣布（B06）。

## 目录

```text
b01-spec-planner/
├── README.md
├── pyproject.toml
├── run_checks.py
├── B01_设计说明.md
├── B01_交付清单.md
├── B01_实验验收说明.md
├── docs/
│   ├── r1.json                       # 案例需求基线参考草案
│   ├── s1.json                       # 设计规格
│   ├── c1.json                       # 接口契约
│   ├── design_entries.json           # 设计条目
│   ├── plan.json                     # 校园案例的固定参考任务包（T01—T11）
│   ├── traceability.json             # 需求—设计—契约—测试—任务映射
│   ├── baseline_manifest.json        # T01 冻结登记清单
│   ├── fixtures/                     # 通用规格输入样例（不是唯一输入）
│   │   ├── campus-lost-found.spec.json       # 校园失物招领（JSON）
│   │   ├── campus-lost-found.plan.json       # 上者动态生成的计划
│   │   ├── campus-lost-found.traceability.json
│   │   └── lab-equipment-loan.spec.md        # 设备借用系统（Markdown）
│   ├── change_request_template.md
│   ├── B03-B01_对齐回执摘要.md
│   └── sources/                      # 两份原始 DOCX 来源快照
├── src/b01/
│   ├── design.py                     # 文档转录的常量数据
│   ├── spec_input.py                 # 通用规格输入（JSON/Markdown 归一化）
│   ├── dynamic_planner.py            # 按规格动态生成任务、DAG 与追溯表
│   ├── task_planner.py               # 校园案例的固定参考任务包 + 拓扑排序
│   ├── plan_validator.py             # 计划校验（通用 + 参考 profile）
│   ├── baseline.py                   # T01 基线登记
│   ├── cli.py                        # 命令行入口
│   └── __main__.py
└── tests/
```

## 通用规格输入与动态计划

除固定的校园参考包外，B01 支持**任意系统**的设计规格输入，并据此动态生成任务：

```powershell
# JSON 规格
python run_checks.py plan --spec docs/fixtures/campus-lost-found.spec.json

# Markdown 规格（含中英文两种键名与全角/半角冒号）
python run_checks.py plan --spec docs/fixtures/lab-equipment-loan.spec.md `
  --out-dir docs/fixtures --plan-name lab.plan.json --traceability-name lab.traceability.json
```

生成的任务数量与编号**由规格推导**，不固定为 T01—T11；同一规格重复生成结果稳定，
内容变化时 `plan_version` 递增且 `input_snapshot.spec_sha256` 随之改变。
字段定义见仓库根目录 [`docs/TASK_ORCHESTRATION.md`](../docs/TASK_ORCHESTRATION.md) §2。

校验也分两层：

- `validate_plan(plan)`：通用结构/覆盖/DAG/版本一致性，适用于任意规格生成的计划。
- `validate_reference_profile(plan)`：只适用于校园参考包的额外约束
  （关键链、T04 的 TC-P01/02/03、T09 的 TC-P02、T10 依赖集合）；基线冻结时使用。


## 快速开始

无需安装依赖，Python 3.10+：

```powershell
cd b01-spec-planner
python run_checks.py all --allow-reference
python -m unittest discover -s tests -v
```

预期：

- `all` 生成 6 个 JSON 资产、校验计划通过、冻结 `baseline_manifest.json`。
- 单元测试全部通过。
- `baseline_manifest.json` 的 `status` 为 `REFERENCE_FROZEN`，并带未确认警告。

如果想验证真实冻结门槛，去掉 `--allow-reference`，命令应以“六人确认未完成”失败退出。

安装为包（可选）：

```powershell
python -m pip install -e .
b01 all --allow-reference
```

## 输出接口

| 消费方 | 输入文件 | 说明 |
| --- | --- | --- |
| B02 | `docs/plan.json` | `spec_version`、`contract_version`、`tasks[]`、`topological_order` |
| B03 | `docs/c1.json`、`docs/design_entries.json`、`docs/plan.json` | 字段边界、事件/任务 schema 来源与 T01 冻结引用 |
| B04 | `docs/c1.json`、`docs/plan.json` | 11 个操作、错误映射、T03—T05 任务包 |
| B05 | `docs/c1.json`、`docs/traceability.json` | 展示字段与导出清单来源 |
| B06 | `docs/plan.json`、`docs/traceability.json` | 兼容只读计划校验；T09/T10 复核证据来源 |

## 任务包字段

每个任务包含文档 4.3.1 要求的字段：

`task_id`、`project_id`、`req_refs`、`design_refs`、`spec_version`、`contract_version`、`goal`、`inputs`、`outputs`、`depends_on`、`capability`、`write_scope`、`acceptance`、`required_test_ids`、`priority`、`timeout`、`retry_budget`，以及统一指令字段 `role_id`、`block_id`、`escalation_target`。

派发记录字段 `attempt_id`、`retry_count`、`revalidation_id` 和提交引用由 B02/B03 在执行阶段追加，B01 任务包不预填。
