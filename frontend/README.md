# B05 交互展示 Agent · 多智能体工作流可观测性

第 10 组 B05（InterfaceAgent）参考实现。基于 React + TypeScript + Vite + React Flow + dagre，
把作业 1 的需求讨论、作业 2 的开发任务 DAG 和作业 3 的执行事件回溯组织成三个联动视图。

数据源严格区分「模拟推演」与「真实接口」，界面会明确标注当前来源，
连接失败时不会把模拟数据当作真实数据展示。

## 三个联动视图

1. **需求共识 · 作业 1**：展示 A—H 讨论阶段、四角色独立观点、结构化异议、B06 共识评价、Proposal、R1、六人确认和讨论事件。真实模式下可创建讨论、运行到人工介入并完成六人确认；R1 确认后可直接进入第二阶段任务编排。
2. **开发任务 DAG · 作业 2**：展示九态任务节点、依赖、重试、阻塞和任务详情；真实运行支持暂停、继续、单步、取消、人工重试及成果导出。
3. **执行事件回溯**：从同一条有序事件流重建任一历史时刻的任务状态，支持起点、逐事件、播放/暂停、拖动进度、回到最新和点击 DAG 节点筛选事件。回放不会重新调用 Agent，也不会修改当前运行。

## 运行

```bash
npm ci
npm run dev
```

浏览器打开 Vite 提示的地址（默认 http://localhost:5173）。默认“模拟推演”无需后端即可查看三个视图。

如果要用真实接口，先启动 B02 后端：

```bash
cd ../b02-workflow-agent
python -m backend.app --host 127.0.0.1 --port 7100
```

然后在界面顶部切到「真实接口」，填入 B02 地址与运行编号（`TASK-RUN-…`），
点击「加载运行」。运行编号需要先在 B02 中创建（见 `b02-workflow-agent/README.md`）。

也可以直接在页面的“第二阶段入口：设计规格 → PM Agent → 编排器”输入设计规格路径，
点击“从设计规格启动”。页面会调用 B02 的 `/api/task-runs/from-spec`，由 B01 动态生成
计划并创建真实运行；“推进一步”和“运行至完成”分别对应 B02 的调度控制接口。

右侧“监控与控制”面板提供验收进度、事件/尝试/耗时指标、可筛选事件流和控制回执。
真实运行可暂停后续派发、继续、取消，或在选中失败/阻塞任务后人工重试；模拟快照始终只读。
暂停与取消不会伪造任务状态，也不会强行终止已经开始的同步任务。

## 数据源

| 模式 | 数据来源 | 说明 |
| --- | --- | --- |
| 模拟推演 · 任务运行 | `src/data/taskRunFixture.json` | 由**真实编排器**（B02+B03+B04+B06）跑出的历史运行快照，不是手工编造。可在下拉框切换「全部通过」「失败/阻断」两个场景 |
| 模拟推演 · 需求讨论 | `src/data/discussionRunFixture.json` | 由真实 `discussion_controller` 生成的正常收敛快照，包含 23 条可回放事件 |
| 真实接口 | `GET /api/task-runs/{run_id}`、`/dag`、`/events`、`/api/runs/{run_id}` | 任务运行默认每 1.5 秒轮询，可关闭；连接失败显示错误并清空任务，不回退成模拟数据 |

在仓库根目录可重新生成两份快照：

```bash
python workflow_runtime/make_frontend_fixture.py
python workflow_runtime/make_discussion_fixture.py
```

## 作业 1—3 能力

| 要求 | 实现位置 |
| --- | --- |
| A—H 阶段、四角色观点、异议、Proposal、R1 与六人确认 | `src/components/DiscussionView.tsx`、`src/api/discussionApi.ts` |
| 任务节点、依赖、负责 Block、状态、重试、耗时、阻塞 | `src/components/TaskNode.tsx`、`src/layout/dagreLayout.ts` |
| 九态图例（含 `WAITING_DEPENDENCY`、`RETRYING`） | `src/constants/status.ts` |
| 点击任务展示输入、输出、依赖、验收、测试、日志、提交、缺陷 | `src/components/TaskDetailPanel.tsx` |
| 按事件重建历史 DAG、播放/暂停、进度拖动、节点筛选 | `src/components/ReplayView.tsx` |
| 真实接口接入与来源区分 | `src/api/taskRunApi.ts`、`src/hooks/useTaskRun.ts` |
| 进度/指标监控、事件筛选、控制回执 | `src/components/MonitoringPanel.tsx` |
| 暂停、继续、取消、人工重试 | `src/hooks/useTaskRun.ts` + B02 `/pause` `/resume` `/cancel` `/retry` |
| R1 确认后生成真实第二阶段任务运行 | `src/App.tsx` + B02 阶段桥接接口 |
| 模拟/真实数据源切换 | `src/App.tsx` |

任务类型定义（含 `block_id`、`acceptance`、`required_test_ids`、`candidate_commit`、
`logs`、`duration_seconds`、`review` 等字段）见 `src/types/task.ts`，
与 [`docs/TASK_ORCHESTRATION.md`](../docs/TASK_ORCHESTRATION.md) §3 一致。

## 验证

```bash
npm run build     # tsc -b && vite build
npm run lint      # oxlint
```

仓库根目录提供两条一键浏览器回归命令，需要本机 Chrome 或 Edge。脚本会自行启动临时服务，
结束后自动关闭，并把截图写入 `stage3-shots/`：

```bash
python _stage3_preview_checks.py  # 三个模拟视图、失败场景、详情、回放与截图
python _stage3_real_checks.py     # 真实讨论、R1 桥接、执行控制与可见性检查
```

真实回归使用独立的临时数据库，实际点击暂停、继续、单步和取消，创建讨论并完成六人确认，
再从 R1 生成新的真实任务 DAG。两条回归均要求浏览器控制台无错误、无 HTTP 4xx/5xx 资源请求；
桥接测试还会检查所有任务节点确实位于画布可视区域，而不只存在于 DOM 中。

也可以在自行启动前端预览后单独运行：

```bash
node tests/ui_smoke.mjs http://127.0.0.1:4321
node tests/screenshots.mjs http://127.0.0.1:4321 shots
```

真实控制回归还需启动 B02，并通过 `B02_BASE` 指定后端地址：

```bash
node tests/ui_controls.mjs http://127.0.0.1:4321 shots/real-controls.png
```

## 目录

```text
frontend/
├── src/
│   ├── App.tsx                     # 三视图、双数据源与阶段桥接
│   ├── api/
│   │   ├── controlApi.ts           # 讨论流程控制指令
│   │   ├── discussionApi.ts        # 讨论快照与六人确认适配
│   │   └── taskRunApi.ts           # B02 任务运行 / DAG / 事件 / 控制适配
│   ├── components/
│   │   ├── DiscussionView.tsx      # 作业 1：讨论、共识与 R1
│   │   ├── ReplayView.tsx          # 作业 3：执行事件回溯
│   │   ├── TaskGraph.tsx           # React Flow 画布
│   │   ├── TaskNode.tsx            # 任务节点（Block/重试/耗时/阻塞）
│   │   ├── TaskDetailPanel.tsx     # 任务详情（D25）
│   │   └── MonitoringPanel.tsx     # 指标、事件流与执行控制
│   ├── constants/status.ts         # 九态配色与文案
│   ├── data/
│   │   ├── simulationRuns.ts       # 任务快照 fixture 加载
│   │   ├── taskRunFixture.json     # 真实编排器产物
│   │   └── discussionRunFixture.json # 真实讨论控制器产物
│   ├── hooks/
│   │   ├── useTaskRun.ts           # 真实接口轮询与控制
│   │   └── useTaskWebSocket.ts     # 讨论流程 WebSocket
│   ├── layout/dagreLayout.ts       # DAG 布局
│   └── types/
│       ├── task.ts                 # 任务数据模型
│       └── discussion.ts           # 讨论与共识数据模型
└── tests/
    ├── ui_smoke.mjs                # 三视图与回溯浏览器冒烟
    ├── ui_controls.mjs             # 真实控制与 R1 桥接回归
    └── screenshots.mjs             # 生成交付截图
```

## 验收条件对照

| 验收条件 | 实现位置 |
| --- | --- |
| 界面不直接改状态 | 控制动作经 `src/api/controlApi.ts` 发给 B02，仅按回执/事件流更新视图 |
| 可追踪当前尝试与输入版本 | 节点与详情面板展示 `attempt_id`、`retry_count`、`input_snapshot` |
| 模拟与真实明确区分 | 顶部「模拟推演 / 真实接口」切换 + 来源徽标 + 详情面板来源标签 |
| 任务 DAG 与运行面板 | `TaskGraph` + `dagreLayout`（状态图例、MiniMap、Block 状态栏） |
| 阻塞及异议详情 | 详情面板含阻塞原因、缺陷/异议、验收证据、尝试历史 |
| 成果导出 | `src/utils/export.ts`：快照 JSON、追溯表 CSV、缺陷清单、运行报告 |
