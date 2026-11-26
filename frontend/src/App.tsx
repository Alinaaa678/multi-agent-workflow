import { useCallback, useMemo, useState } from 'react';
import { loadSimulationRun, listSimulationRuns, type SimulationRun } from './data/simulationRuns';
import { TaskDetailPanel } from './components/TaskDetailPanel';
import { TaskGraph } from './components/TaskGraph';
import { MonitoringPanel } from './components/MonitoringPanel';
import { DiscussionView } from './components/DiscussionView';
import { ReplayView } from './components/ReplayView';
import { ALL_STATUSES, statusColors, statusLabels } from './constants/status';
import { createTaskRunFromSpec, type RunEvent } from './api/taskRunApi';
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
import './observability.css';
import type { AckEntry, LogEntry } from './hooks/useTaskWebSocket';

/** 真实接口默认地址：B02 任务编排服务。 */
const DEFAULT_API_BASE = 'http://127.0.0.1:7100';

type ReportEvent = Pick<RunEvent, 'sequence' | 'type' | 'task_id' | 'occurred_at'> & { event_id?: string };

function eventLogs(events: ReportEvent[]): LogEntry[] {
  return events.map((event) => ({
    id: event.event_id || `event-${event.sequence}`,
    level: event.type.includes('FAILED') || event.type.includes('BLOCKED')
      ? 'error'
      : event.type.includes('COMPLETED') || event.type.includes('ACCEPTED')
        ? 'success'
        : 'info',
    message: `${event.type}${event.task_id ? ` · ${event.task_id}` : ''}`,
    task_id: event.task_id ?? undefined,
    timestamp: event.occurred_at,
  }));
}

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
  const [activeView, setActiveView] = useState<'discussion' | 'execution' | 'replay'>('execution');
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [panelOpen, setPanelOpen] = useState(true);
  const [sideView, setSideView] = useState<'monitor' | 'detail'>('monitor');
  const [autoRefresh, setAutoRefresh] = useState(true);

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
  const [phase2Loading, setPhase2Loading] = useState(false);
  const isReal = source === 'real';
  const real = useTaskRun({
    baseUrl: apiBase,
    runId: submittedRunId,
    enabled: isReal,
    pollIntervalMs: isReal && autoRefresh ? 1500 : 0,
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
  const currentEvents = isReal ? real.events : simulation?.events ?? [];
  const reportLogs = eventLogs(currentEvents);
  const reportAcks: AckEntry[] = real.lastControl ? [{
    id: real.lastControl.request_id,
    command: real.lastControl.action,
    accepted: real.lastControl.accepted,
    message: real.lastControl.message,
    timestamp: real.lastControl.timestamp,
  }] : [];
  const dispatchDisabled = !isReal || !submittedRunId || real.loading || ['PAUSED', 'CANCELLED'].includes(meta?.status ?? '');

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

  const showTaskDetails = useCallback((taskId: string) => {
    setSelectedId(taskId);
    setSideView('detail');
    setPanelOpen(true);
  }, []);

  const handleLoadRun = useCallback(() => {
    setSubmittedRunId(runIdInput.trim());
    setSource('real');
    setSelectedId(null);
    setSideView('monitor');
  }, [runIdInput]);

  const handleCreateFromSpec = useCallback(async () => {
    setLaunching(true);
    try {
      const created = await createTaskRunFromSpec({ baseUrl: apiBase, specPath });
      setRunIdInput(created.run.run_id);
      setSubmittedRunId(created.run.run_id);
      setSource('real');
      setActiveView('execution');
      setSelectedId(null);
    } catch (caught) {
      window.alert(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setLaunching(false);
    }
  }, [apiBase, specPath]);

  const handleEnterPhase2 = useCallback(async (discussionRunId: string) => {
    setPhase2Loading(true);
    try {
      const created = await createTaskRunFromSpec({ baseUrl: apiBase, discussionRunId });
      setRunIdInput(created.run.run_id);
      setSubmittedRunId(created.run.run_id);
      setSource('real');
      setSelectedId(null);
      setSideView('monitor');
      setActiveView('execution');
    } catch (caught) {
      window.alert(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setPhase2Loading(false);
    }
  }, [apiBase]);

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
          <h1>多智能体软件工程工作流 · 可观测性框架</h1>
          <span className="subtitle">
            课堂作业三 ｜ 需求讨论 → 方案确认 → 任务 DAG → Agent 执行 → 质量验收 → 事件回溯
            {activeView !== 'discussion' && meta ? ` ｜ ${meta.plan_version} ｜ ${meta.project_id}` : ''}
          </span>
        </div>

        <div className="header-right">
          {activeView !== 'discussion' && (isReal ? (
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
                <option key={run.key} value={run.key}>{run.label}</option>
              ))}
            </select>
          ))}
          {activeView === 'discussion' && isReal && (
            <div className="header-inputs">
              <input
                aria-label="后端地址"
                value={apiBase}
                onChange={(event) => setApiBase(event.target.value)}
                placeholder="http://127.0.0.1:7100"
                size={22}
              />
            </div>
          )}

          <div className="source-switch" role="group" aria-label="数据源切换">
            <button className={!isReal ? 'active' : ''} onClick={() => switchSource('simulation')}>模拟推演</button>
            <button className={isReal ? 'active real' : ''} onClick={() => switchSource('real')}>真实接口</button>
          </div>

          <span className={`source-badge ${isReal ? 'real' : 'sim'}`}>
            {isReal
              ? activeView === 'discussion'
                ? '真实接口 · B02 讨论控制器'
                : real.error
                  ? '真实接口 · 连接失败'
                  : real.loading
                    ? '真实接口 · 加载中…'
                    : '真实接口 · 已同步'
              : '模拟数据 · 真实控制器产物'}
          </span>
        </div>
      </header>

      <nav className="workflow-nav" aria-label="课堂作业三视图">
        <button className={activeView === 'discussion' ? 'active' : ''} aria-current={activeView === 'discussion' ? 'page' : undefined} onClick={() => setActiveView('discussion')}><span>1</span>需求共识 · 作业 1</button>
        <button className={activeView === 'execution' ? 'active' : ''} aria-current={activeView === 'execution' ? 'page' : undefined} onClick={() => setActiveView('execution')}><span>2</span>开发任务 DAG · 作业 2</button>
        <button className={activeView === 'replay' ? 'active' : ''} aria-current={activeView === 'replay' ? 'page' : undefined} onClick={() => setActiveView('replay')}><span>3</span>执行事件回溯</button>
        <span className="nav-flow-note">统一运行编号 · 统一事件序列 · 真实／模拟来源明确区分</span>
      </nav>

      {activeView === 'discussion' ? (
        <div className="app-main">
          <DiscussionView apiBase={apiBase} isReal={isReal} phase2Loading={phase2Loading} onEnterPhase2={(runId) => void handleEnterPhase2(runId)} />
        </div>
      ) : activeView === 'replay' ? (
        <div className="app-main">
          <ReplayView key={meta?.run_id ?? 'NO-RUN'} tasks={tasks} events={currentEvents} runId={meta?.run_id ?? 'NO-RUN'} isReal={isReal} />
        </div>
      ) : (
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
              <button onClick={() => void real.step()} disabled={dispatchDisabled}>推进一步</button>
              <button onClick={() => void real.runToCompletion()} disabled={dispatchDisabled}>运行至完成</button>
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
                <button onClick={() => exportRunReport(tasks, reportLogs, reportAcks, buildExportContext())}>运行报告</button>
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
                    {isReal ? '尚未加载真实任务运行：请输入运行编号后点击「加载运行」。' : '正在等待任务数据…'}
                  </div>
                ) : (
                  <TaskGraph tasks={tasks} selectedId={selectedId} onNodeClick={(task) => showTaskDetails(task.task_id)} />
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
            <aside className="side-panel" aria-label="监控与任务详情">
              <div className="side-tabs" role="tablist" aria-label="右侧面板切换">
                <button role="tab" aria-selected={sideView === 'monitor'} className={sideView === 'monitor' ? 'active' : ''} onClick={() => setSideView('monitor')}>监控与控制</button>
                <button role="tab" aria-selected={sideView === 'detail'} className={sideView === 'detail' ? 'active' : ''} onClick={() => setSideView('detail')}>任务详情{selectedTask ? ` · ${selectedTask.task_id}` : ''}</button>
              </div>
              {sideView === 'monitor' ? (
                <MonitoringPanel
                  events={currentEvents}
                  tasks={tasks}
                  status={meta?.status ?? ''}
                  completed={meta?.completed ?? 0}
                  total={meta?.total ?? 0}
                  isReal={isReal}
                  loading={real.loading}
                  autoRefresh={autoRefresh}
                  lastUpdated={real.lastUpdated}
                  lastControl={real.lastControl}
                  selectedTask={selectedTask}
                  onAutoRefreshChange={setAutoRefresh}
                  onPause={() => void real.pause()}
                  onResume={() => void real.resume()}
                  onCancel={() => void real.cancel()}
                  onRetry={(taskId) => void real.retry(taskId)}
                  onSelectTask={showTaskDetails}
                />
              ) : (
                <TaskDetailPanel task={selectedTask} dataSource={source} onClose={() => setPanelOpen(false)} />
              )}
            </aside>
          )}
        </div>
      )}
    </div>
  );
}

export default App;
