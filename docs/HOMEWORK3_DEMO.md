# 课堂作业 3：五分钟演示与提交指南

## 1. 演示目标

用同一条可追踪链路展示：

```text
需求讨论与异议 → 六人确认 R1 → 真实任务 DAG → 执行监控与人工控制 → 事件回溯
```

核心不是“页面会动”，而是说明以下可观测性设计：

- **统一运行编号**：讨论运行 `RUN-…` 与任务运行 `TASK-RUN-…` 均有明确来源。
- **统一事件序列**：任务状态由持久化事件驱动，可按序号重建历史状态。
- **监控与执行分离**：前端只发送控制指令并消费回执、快照和事件，不直接修改任务状态。
- **来源明确**：模拟快照由真实控制器和编排器预先生成；真实接口失败时不回退成模拟数据。

## 2. 演示前准备

### 环境

- Python 3.12+
- Node.js 22+ 和 npm
- Chrome 或 Edge

### 安装与验证

在仓库根目录执行：

```powershell
cd b02-workflow-agent
python -m unittest discover -s tests -v
cd ..\frontend
npm ci
npm run lint
npm run build
cd ..
python _stage3_preview_checks.py
python _stage3_real_checks.py
python workflow_runtime/verify_contracts.py
python workflow_runtime/verify_acceptance.py --skip-ci
```

预期结果：B02 的 117 项测试通过；前端 lint 0 警告、构建成功；两条浏览器回归均为 `PASS`；
跨模块契约 6/6、闭环验收 12/12 通过（`--skip-ci` 表示不核验 GitHub CI）。
真实浏览器回归还会核对测试证据、日志摘要、回溯状态分布和新运行回执隔离。
回归脚本会关闭自己启动的服务，截图保存在 `stage3-shots/`。

### 启动课堂演示

终端一——B02 后端：

```powershell
cd b02-workflow-agent
python -m backend.app --host 127.0.0.1 --port 7100
```

终端二——React 可观测性前端：

```powershell
cd frontend
npm run dev -- --host 127.0.0.1 --port 5173
```

浏览器打开 `http://127.0.0.1:5173/`。`7100` 是后端 API 地址，不要把 B02 自带的旧原型页面当作本次演示前端。

建议提前把浏览器缩放设为 100%，窗口宽度保持在 1440 px 以上。演示时使用“真实接口”，后端地址保持 `http://127.0.0.1:7100`。

## 3. 五分钟操作流程

| 时间 | 操作 | 要讲的重点 |
| --- | --- | --- |
| 0:00—0:25 | 指向顶部三个视图和“真实接口”来源标签 | “框架覆盖作业 1 的需求共识、作业 2 的任务 DAG，以及作业 3 的监控、交互与事件回溯；模拟和真实来源始终分开。” |
| 0:25—1:25 | 进入“需求共识”，输入校园失物招领想法，创建讨论；点击“运行至人工介入” | 指向 A—H 阶段、四角色独立观点、异议及处理状态、B06 共识评价。强调未满足人工门槛时不会发布 R1。 |
| 1:25—1:50 | 勾选六名成员并确认，打开“Proposal 与 R1” | “六人确认是显式控制点。确认后才形成 R1 需求基线，讨论、异议和确认记录都可追踪。” |
| 1:50—2:20 | 点击“进入第二阶段任务编排” | 页面生成新的 `TASK-RUN-…` 并显示真实 DAG。强调 R1 经设计规格桥接到 B01/B02，不是前端伪造任务。 |
| 2:20—3:10 | 点击“推进一步”；暂停派发，再继续，然后再推进一步 | 展示任务九态、依赖放行、完成计数、控制回执和人工控制事件。说明暂停与取消只影响后续派发，不伪造正在执行任务的结果。 |
| 3:10—3:40 | 点击“运行至完成”，再点击一个任务节点 | 展示任务输入、输出产物、验收条件、测试编号、日志和提交。说明 B04 执行、B06 验收、B02 写状态的职责边界。 |
| 3:40—4:30 | 进入“执行事件回溯”，点击“起点”，逐事件推进，再拖动进度；点击一个 DAG 节点 | “回放只消费已有事件，按事件序号重建任一历史时刻，不重新调用 Agent，也不改变当前运行。节点筛选用于定位单个任务的因果链。” |
| 4:30—5:00 | 回到 DAG，指向事件流和导出按钮 | “最终交付包括状态、指标、事件、人工控制、历史回放和证据导出，形成从需求到执行再到验收的闭环。” |

### 30 秒收尾讲解词

“这套框架把可观测性分成三层：第一层是当前状态，包括 DAG、进度和异常；第二层是过程证据，包括事件、尝试、验收和人工控制；第三层是历史重建，通过统一事件序列回放任意时刻。前端不直接改状态，所有控制都经过 B02，并留下审计事件，因此它不仅能看，还能安全地监控和干预多智能体工作流。”

## 4. 时间不够时的压缩路线

若现场只剩两分钟：

1. 用默认“模拟推演”展示作业 1 的完整共识快照，约 25 秒。
2. 切换到 DAG，展示九态、任务详情和事件流，约 35 秒。
3. 进入“执行事件回溯”，拖动进度并点击节点筛选，约 40 秒。
4. 用“统一状态、统一事件、统一追溯”收尾，约 20 秒。

模拟模式不能代替真实接口联调；它只是由真实控制器和编排器事先生成的只读历史快照。

## 5. 故障备用方案

- **后端无法启动**：切换“模拟推演”，继续展示三个视图，并明确说明当前为真实控制器生成的历史快照。
- **真实创建失败**：不要反复点击；打开已生成的 `stage3-shots/real-phase-bridge.png` 和 `real-controls.png`，说明已完成的联调及失败原因。
- **浏览器或端口异常**：使用 `stage3-shots/discussion-consensus.png`、`dag-success.png`、`execution-replay.png` 完成静态演示。
- **DAG 节点被缩放得过小**：点击 React Flow 左下角的适应视图按钮；回溯中点击“最新状态”可恢复当前事件位置。

## 6. Git 分支与提交方案

本地功能分支已经创建：

```powershell
git switch feat/homework3-observability
```

以下命令只分组提交本次作业，不要直接使用 `git add .`。`.qa/`、`_stage3_*.log` 和 `stage3-shots/` 是本地验证产物，不纳入这三组提交。

### 提交 1：B02 执行控制与审计事件

```powershell
git add b02-workflow-agent/backend/app.py b02-workflow-agent/backend/task_api.py b02-workflow-agent/backend/task_orchestrator.py b02-workflow-agent/tests/test_task_api.py b02-workflow-agent/tests/test_task_controls.py b02-workflow-agent/tests/test_task_orchestrator.py b02-workflow-agent/tests/test_task_recovery.py b02-workflow-agent/README.md docs/TASK_ORCHESTRATION.md docs/B02_EXCEPTION_RECOVERY.md
git commit -m "feat(b02): add observable task execution controls"
```

提交说明：增加暂停、继续、取消、人工重试、执行超时与崩溃恢复，保持任务状态机约束，并持久化对应审计事件。
验证：`python -m unittest discover -s tests -v`，117 项通过。

### 提交 2：B05 三视图与真实阶段桥接

```powershell
git add frontend
git commit -m "feat(b05): add consensus monitoring and event replay views"
```

提交说明：增加需求共识、任务 DAG 和执行回溯三个联动视图，接入真实讨论与任务接口，并区分模拟快照和实时数据。
验证：`npm run lint`、`npm run build`、`python _stage3_preview_checks.py`。

### 提交 3：端到端回归、快照生成与演示文档

```powershell
git add README.md docs/HOMEWORK3_DEMO.md workflow_runtime/make_discussion_fixture.py _stage3_preview_checks.py _stage3_real_checks.py
git commit -m "test: verify homework3 workflow end to end"
```

提交说明：补充可重复的真实接口浏览器回归、讨论快照生成脚本和五分钟课堂演示指南。
验证：`python _stage3_real_checks.py`，真实讨论、六人确认、R1 桥接、执行控制和节点可见性全部通过。

### 推送与 PR

先检查每组提交确实只包含预期文件：

```powershell
git status --short
git diff --check
git log --oneline -3
```

确认后再推送：

```powershell
git push -u origin feat/homework3-observability
```

PR 目标分支选 `integration`，标题建议：

```text
feat: add observable SE workflow for homework 3
```

PR 描述模板：

```markdown
## 范围
- B02：任务暂停、继续、取消、人工重试、执行超时、崩溃恢复及审计事件
- B05：需求共识、任务 DAG、执行事件回溯三视图
- 集成：R1 → 设计规格 → 真实任务运行，浏览器端到端回归

## 验证
- B02：117 项单元与接口测试通过
- 前端：lint 0 错误、生产构建通过
- 模拟与真实浏览器回归通过，控制台错误 0，HTTP 4xx/5xx 为 0

## 边界
- 模拟模式为真实控制器/编排器生成的只读历史快照
- 真实模式需要 B02 后端，连接失败不会回退成模拟数据
- 暂停与取消影响后续派发，不强行终止已开始的同步任务
- 执行超时只停止 B02 等待并触发重试，不保证强杀外部进程；崩溃恢复需显式调用 `/recover`，不会自动重跑
```
