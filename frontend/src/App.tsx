import { useCallback, useMemo, useState } from 'react';
import { loadSimulationRun, listSimulationRuns, type SimulationRun } from './data/simulationRuns';
import { TaskDetailPanel } from './components/TaskDetailPanel';
import { TaskGraph } from './components/TaskGraph';
import { ALL_STATUSES, statusColors, statusLabels } from './constants/status';
import { createTaskRunFromSpec } from './api/taskRunApi';
import { useTaskRun } from './hooks/useTaskRun';
import type { DataSource, Task } from './types/task';
import {
  exportDefects,
  exportRunReport,
  exportSnapshot,
  exportTraceability,
  type ExportContext,
} from './utils/export';
import './App.css';

/** 真实接口默认地址：B02 任务编排服务。 */
const DEFAULT_API_BASE = 'http://127.0.0.1:7100';

function StatCard({ label, value, color, bg }: { label: string; value: number | string; color: string; bg: string }) {
  return (
    <div style={{ background: '#fff', border: '1px solid #e2e8f0', borderRadius: 10, padding: '10px 16px', minWidth: 92 }}>
      <div style={{ fontSize: 11, color: '#64748b' }}>{label}</div>
      <div style={{ fontSize: 22, fontWeight: 700, color, background: bg, borderRadius: 6, paddingLeft: 2 }}>{value}</div>
    </div>
  );
}

function App() {
  const [source, setSource] = useState<DataSource>('simulation');
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [panelOpen, setPanelOpen] = useState(true);

  // 模拟数据源：选择由真实编排器跑出的历史运行
  const runs = useMemo(() => listSimulationRuns(), []);
  const [simRunKey, setSimRunKey] = useState<string>(() => runs[0]?.key ?? '');
  const simulation = useMemo<SimulationRun | null>(() => loadSimulationRun(simRunKey), [simRunKey]);

  // 真实数据源：连接 B02 的任务运行
  const [apiBase, setApiBase] = useState(DEFAULT_API_BASE);
  const [runIdInput, setRunIdInput] = useState('TASK-RUN-DEMO');
  const [submittedRunId, setSubmittedRunId] = useState('');
  const [specPath, setSpecPath] = useState('b01-spec-planner/docs/fixtures/campus-lost-found.spec.json');
  const [launching, setLaunching] = useState(false);
  const isReal = source === 'real';
  const real = useTaskRun({
    baseUrl: apiBase,
    runId: submittedRunId,
    enabled: isReal,
    pollIntervalMs: isReal ? 3000 : 0,
  });

  const tasks = useMemo<Task[]>(
    () => (isReal ? real.tasks : simulation?.tasks ?? []),
    [isReal, real.tasks, simulation],
  );
  const meta = useMemo(
    () => (isReal ? real.meta : simulation?.meta ?? null),
    [isReal, real.meta, simulation],
  );
  const selectedTask = tasks.find((task) => task.task_id === selectedId) ?? null;

  const stats = useMemo(() => {
    const done = tasks.filter((task) => task.status === 'SUCCEEDED').length;
    const active = tasks.filter((task) =>
      ['READY', 'RUNNING', 'WAITING_DEPENDENCY', 'REVIEWING', 'RETRYING'].includes(task.status),
    ).length;
    const problems = tasks.filter((task) => task.status === 'FAILED' || task.status === 'BLOCKED').length;
    const retries = tasks.reduce((sum, task) => sum + task.retry_count, 0);
    const defects = tasks.flatMap((task) => task.objections).filter((item) => item.status === 'open').length;
    return { done, active, problems, retries, defects };
  }, [tasks]);

  const selectRun = useCallback((key: string) => {
    setSimRunKey(key);
    setSelectedId(null);
  }, []);

  const switchSource = useCallback((next: DataSource) => {
    setSource(next);
    setSelectedId(null);
  }, []);

  const handleLoadRun = useCallback(() => {
    setSubmittedRunId(runIdInput.trim());
    setSource('real');
    setSelectedId(null);
  }, [runIdInput]);

  const handleCreateFromSpec = useCallback(async () => {
    setLaunching(true);
    try {
      const created = await createTaskRunFromSpec({ baseUrl: apiBase, specPath });
      setRunIdInput(created.run.run_id);
      setSubmittedRunId(created.run.run_id);
      setSource('real');
      setSelectedId(null);
    } catch (caught) {
      window.alert(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setLaunching(false);
    }
  }, [apiBase, specPath]);

  // 导出上下文在“点击导出时”构造：时间戳必须反映真实导出时刻，
  // 不能在渲染期求值（否则每次重渲染都会变，且违反 React 纯函数约束）。
  const buildExportContext = useCallback(
    (): ExportContext => ({
      source,
      runId: meta?.run_id ?? 'N/A',
      specVersion: meta?.spec_version ?? 'S1',
      contractVersion: meta?.contract_version ?? 'C1',
      exportedAt: new Date().toISOString(),
    }),
    [source, meta],
  );

  return (
    <div className="app-shell">
      <header className="app-header">
        <div>
          <h1>多智能体协作开发平台 · 开发任务 DAG</h1>
          <span className="subtitle">
            规格 → 计划 → 编排 → 执行 → 验收 → 展示
            {meta ? ` ｜ ${meta.plan_version} ｜ ${meta.project_id}` : ''}
          </span>
        </div>

        <div className="header-right">
          {/* 双数据源对照区：真实运行 ID 输入 + 模拟运行选择 */}
          {isReal ? (
            <div className="header-inputs">
              <input
                aria-label="后端地址"
                value={apiBase}
                onChange={(event) => setApiBase(event.target.value)}
                placeholder="http://127.0.0.1:7100"
                size={22}
              />
              <input
                aria-label="任务运行编号"
                value={runIdInput}
                onChange={(event) => setRunIdInput(event.target.value)}
                placeholder="TASK-RUN-XXXXXXXX"
                size={18}
              />
              <button onClick={handleLoadRun}>加载运行</button>
            </div>
          ) : (
            <select
              aria-label="模拟运行"
              value={simRunKey}
              onChange={(event) => selectRun(event.target.value)}
              style={{ padding: '6px 8px', borderRadius: 6, border: '1px solid #cbd5e1' }}
            >
              {runs.map((run) => (
                <option key={run.key} value={run.key}>
                  {run.label}
                </option>
              ))}
            </select>
          )}

          <div className="source-switch" role="group" aria-label="数据源切换">
            <button className={!isReal ? 'active' : ''} onClick={() => switchSource('simulation')}>
              模拟推演
            </button>
            <button className={isReal ? 'active real' : ''} onClick={() => switchSource('real')}>
              真实接口
            </button>
          </div>

          <span className={`source-badge ${isReal ? 'real' : 'sim'}`}>
            {isReal
              ? real.error
                ? `真实接口 · 连接失败`
                : real.loading
                  ? '真实接口 · 加载中…'
                  : '真实接口 · 已同步'
              : '模拟数据 · 真实编排器产物'}
          </span>
        </div>
      </header>

      <div className="app-main">
        <div className="app-content">
          <div className="spec-launch-bar">
            <div>
              <strong>第二阶段入口：设计规格 → PM Agent → 编排器</strong>
              <span>输入规格路径后创建真实任务运行，可逐步执行或运行至完成。</span>
            </div>
            <input
              aria-label="设计规格路径"
              value={specPath}
              onChange={(event) => setSpecPath(event.target.value)}
              placeholder="b01-spec-planner/docs/fixtures/your.spec.json"
            />
            <button onClick={handleCreateFromSpec} disabled={launching || !specPath.trim()}>
              {launching ? '正在生成计划…' : '从设计规格启动'}
            </button>
            <button onClick={() => void real.step()} disabled={!isReal || !submittedRunId || real.loading}>
              推进一步
            </button>
            <button onClick={() => void real.runToCompletion()} disabled={!isReal || !submittedRunId || real.loading}>
              运行至完成
            </button>
          </div>
          <div className="toolbar">
            <div className="stats">
              <StatCard label="已通过" value={stats.done} color="#15803d" bg="#f0fdf4" />
              <StatCard label="进行中" value={stats.active} color="#b45309" bg="#fffbeb" />
              <StatCard label="失败/阻塞" value={stats.problems} color="#b91c1c" bg="#fef2f2" />
              <StatCard label="累计重试" value={stats.retries} color="#6d28d9" bg="#f5f3ff" />
              <StatCard label="未关闭缺陷" value={stats.defects} color="#b91c1c" bg="#fef2f2" />
            </div>
            {meta && (
              <div className="meta-line">
                <span>{meta.run_id}</span>
                <span>{meta.spec_version} / {meta.contract_version}</span>
                <span className="mono" title="输入快照">snap {meta.input_snapshot.slice(0, 12)}</span>
                <span>完成 {meta.completed}/{meta.total}</span>
              </div>
            )}
            <div className="exports">
              <span className="exports-label">成果导出</span>
              <button onClick={() => exportSnapshot(tasks, buildExportContext())}>快照 JSON</button>
              <button onClick={() => exportTraceability(tasks, buildExportContext())}>追溯表 CSV</button>
              <button onClick={() => exportDefects(tasks, buildExportContext())}>缺陷清单</button>
              <button onClick={() => exportRunReport(tasks, [], [], buildExportContext())}>运行报告</button>
            </div>
          </div>

          {isReal && real.error && (
            <div className="alert error" role="alert">
              无法读取真实任务运行：{real.error}
              <div className="muted" style={{ marginTop: 4 }}>
                请确认 B02 服务已启动（python -m backend.app），并已通过
                <code> POST /api/task-runs</code> 创建该运行编号。
              </div>
            </div>
          )}

          <div className="graph-area">
            <div className="legend">
              <strong>状态图例：</strong>
              {ALL_STATUSES.map((status) => (
                <span key={status} className="legend-item">
                  <i style={{ background: statusColors[status] }} />
                  {statusLabels[status]}
                </span>
              ))}
            </div>

            <div className="canvas">
              {tasks.length === 0 ? (
                <div className="canvas-empty">
                  {isReal
                    ? '尚未加载真实任务运行：请输入运行编号后点击「加载运行」。'
                    : '正在等待任务数据…'}
                </div>
              ) : (
                <TaskGraph
                  tasks={tasks}
                  selectedId={selectedId}
                  onNodeClick={(task) => {
                    setSelectedId(task.task_id);
                    setPanelOpen(true);
                  }}
                />
              )}
            </div>
          </div>

          <div className="agent-bar">
            <strong>Block 状态：</strong>
            {['B01', 'B02', 'B03', 'B04', 'B05', 'B06'].map((block) => {
              const owned = tasks.filter((task) => task.block_id === block);
              const running = owned.filter((task) => task.status === 'RUNNING' || task.status === 'REVIEWING').length;
              return (
                <span key={block} className={`agent-chip ${running > 0 ? 'busy' : ''}`}>
                  {block}: {owned.length === 0 ? '无任务' : running > 0 ? `进行中 ${running}` : `${owned.length} 个任务`}
                </span>
              );
            })}
          </div>
        </div>

        {panelOpen && (
          <TaskDetailPanel
            task={selectedTask}
            dataSource={source}
            onClose={() => setPanelOpen(false)}
          />
        )}
      </div>
    </div>
  );
}

export default App;
