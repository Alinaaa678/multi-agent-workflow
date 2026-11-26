import { useMemo, useState } from 'react';
import type { ControlAck } from '../api/taskRunApi';
import type { Task } from '../types/task';

export interface MonitorEvent {
  event_id?: string;
  sequence: number;
  type: string;
  task_id?: string | null;
  attempt_id?: number | null;
  occurred_at: string;
  producer?: string;
  payload?: Record<string, unknown>;
}

interface Props {
  events: MonitorEvent[];
  tasks: Task[];
  status: string;
  completed: number;
  total: number;
  isReal: boolean;
  loading: boolean;
  autoRefresh: boolean;
  lastUpdated: string | null;
  lastControl: ControlAck | null;
  selectedTask: Task | null;
  onAutoRefreshChange: (enabled: boolean) => void;
  onPause: () => void;
  onResume: () => void;
  onCancel: () => void;
  onRetry: (taskId: string) => void;
  onSelectTask: (taskId: string) => void;
}

type EventFilter = 'all' | 'attention' | 'control' | 'gate';

const EVENT_LABELS: Record<string, string> = {
  TASK_READY: '任务就绪',
  TASK_DISPATCHED: '任务派发',
  TASK_STARTED: '开始执行',
  TASK_OUTPUT_SUBMITTED: '提交产物',
  TASK_REVIEW_REQUESTED: '请求验收',
  TASK_ACCEPTED: '验收通过',
  TASK_REJECTED: '验收驳回',
  TASK_RETRY_SCHEDULED: '自动重试',
  TASK_RETRY_REQUESTED: '人工重试',
  TASK_BLOCKED: '任务阻塞',
  TASK_COMPLETED: '任务完成',
  TASK_FAILED: '任务失败',
  TASK_WAITING_DEPENDENCY: '等待依赖',
  DEFECT_CREATED: '登记缺陷',
  DEFECT_CLOSED: '关闭缺陷',
  RUN_PAUSED: '运行暂停',
  RUN_RESUMED: '运行继续',
  RUN_CANCELLED: '运行取消',
};

const RUN_LABELS: Record<string, string> = {
  READY: '待执行',
  RUNNING: '运行中',
  PAUSED: '已暂停',
  CANCELLED: '已取消',
  SUCCEEDED: '已完成',
  FAILED: '执行失败',
  BLOCKED: '需要介入',
};

function eventKind(type: string): 'info' | 'success' | 'error' | 'control' {
  if (type.includes('FAILED') || type.includes('BLOCKED') || type.includes('REJECTED') || type === 'DEFECT_CREATED') {
    return 'error';
  }
  if (type.includes('ACCEPTED') || type.includes('COMPLETED') || type === 'DEFECT_CLOSED') return 'success';
  if (type.startsWith('RUN_') || type === 'TASK_RETRY_REQUESTED') return 'control';
  return 'info';
}

function formatTime(value: string | null): string {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleTimeString('zh-CN', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

export function MonitoringPanel({
  events,
  tasks,
  status,
  completed,
  total,
  isReal,
  loading,
  autoRefresh,
  lastUpdated,
  lastControl,
  selectedTask,
  onAutoRefreshChange,
  onPause,
  onResume,
  onCancel,
  onRetry,
  onSelectTask,
}: Props) {
  const [filter, setFilter] = useState<EventFilter>('all');
  const progress = total > 0 ? Math.round((completed / total) * 100) : 0;
  const duration = tasks.reduce((sum, task) => sum + (task.duration_seconds ?? 0), 0);
  const attempts = tasks.reduce((sum, task) => sum + task.attempts.length, 0);
  const canControl = isReal && Boolean(status) && !loading;
  const retryable = selectedTask && ['FAILED', 'BLOCKED'].includes(selectedTask.status);

  const visibleEvents = useMemo(() => {
    const filtered = events.filter((event) => {
      if (filter === 'attention') return eventKind(event.type) === 'error';
      if (filter === 'control') return event.type.startsWith('RUN_') || event.type === 'TASK_RETRY_REQUESTED';
      if (filter === 'gate') {
        return ['TASK_ACCEPTED', 'TASK_REJECTED', 'TASK_REVIEW_REQUESTED', 'DEFECT_CREATED', 'DEFECT_CLOSED'].includes(event.type);
      }
      return true;
    });
    return filtered.slice(-120).reverse();
  }, [events, filter]);

  const confirmCancel = () => {
    if (window.confirm('确定取消当前运行吗？后续任务将停止派发，已有状态和事件会保留。')) onCancel();
  };

  return (
    <div className="monitor-panel">
      <section className="run-monitor" aria-label="运行监控">
        <div className="monitor-heading">
          <div>
            <span className="monitor-caption">当前运行</span>
            <strong>{RUN_LABELS[status] ?? status ?? '未加载'}</strong>
          </div>
          <span className={`run-status-dot ${status.toLowerCase()}`} aria-hidden="true" />
        </div>
        <div className="progress-label">
          <span>验收进度</span>
          <strong>{completed}/{total} · {progress}%</strong>
        </div>
        <div
          className="progress-track"
          role="progressbar"
          aria-label="任务验收进度"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={progress}
        >
          <span style={{ width: `${progress}%` }} />
        </div>
        <div className="monitor-metrics">
          <span><strong>{events.length}</strong> 条事件</span>
          <span><strong>{attempts}</strong> 次尝试</span>
          <span><strong>{duration.toFixed(2)}s</strong> 累计耗时</span>
        </div>
      </section>

      <section className="execution-controls" aria-label="执行控制">
        <div className="panel-section-title">
          <strong>执行控制</strong>
          <span>{isReal ? '指令 → B02 → 事件 → 界面' : '历史快照只读'}</span>
        </div>
        <div className="control-grid">
          {status === 'PAUSED' ? (
            <button className="primary" onClick={onResume} disabled={!canControl}>继续运行</button>
          ) : (
            <button onClick={onPause} disabled={!canControl || !['READY', 'RUNNING'].includes(status)}>暂停派发</button>
          )}
          <button className="danger" onClick={confirmCancel} disabled={!canControl || ['SUCCEEDED', 'FAILED', 'BLOCKED', 'CANCELLED'].includes(status)}>取消运行</button>
          <button
            className="wide"
            onClick={() => selectedTask && onRetry(selectedTask.task_id)}
            disabled={!canControl || !retryable || ['PAUSED', 'CANCELLED'].includes(status)}
            title={selectedTask ? `重试 ${selectedTask.task_id}` : '先在 DAG 中选择失败或阻塞任务'}
          >
            {selectedTask ? `重试选中任务 · ${selectedTask.task_id}` : '选择失败任务后重试'}
          </button>
        </div>
        <p className="control-note">暂停和取消只影响后续派发，不打断当前任务；重试仍须通过依赖检查与 B06 质量门。</p>
        {lastControl && isReal && (
          <div className="control-ack" role="status">
            <strong>控制回执</strong>
            <span>{lastControl.message}</span>
            <code>{lastControl.request_id}</code>
          </div>
        )}
      </section>

      <section className="event-monitor" aria-label="事件监控">
        <div className="event-toolbar">
          <div>
            <strong>实时事件流</strong>
            <span className="event-count">{visibleEvents.length} / {events.length}</span>
          </div>
          <select aria-label="事件筛选" value={filter} onChange={(event) => setFilter(event.target.value as EventFilter)}>
            <option value="all">全部事件</option>
            <option value="attention">失败与阻塞</option>
            <option value="control">人工控制</option>
            <option value="gate">质量验收</option>
          </select>
        </div>
        <div className="refresh-line">
          <label>
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={(event) => onAutoRefreshChange(event.target.checked)}
              disabled={!isReal}
            />
            自动刷新（1.5 秒）
          </label>
          <span title={lastUpdated ?? '历史运行快照'}>{isReal ? `更新于 ${formatTime(lastUpdated)}` : '模拟历史事件'}</span>
        </div>
        <div className="event-list" role="log" aria-label="任务运行事件">
          {visibleEvents.length === 0 ? (
            <div className="event-empty">当前筛选条件下暂无事件</div>
          ) : visibleEvents.map((event) => {
            const content = (
              <>
                <span className={`event-marker ${eventKind(event.type)}`} aria-hidden="true" />
                <span className="event-sequence">#{event.sequence}</span>
                <span className="event-copy">
                  <strong>{EVENT_LABELS[event.type] ?? event.type}</strong>
                  <code>{event.type}</code>
                </span>
                <span className="event-task">{event.task_id ?? 'RUN'}</span>
                <time>{formatTime(event.occurred_at)}</time>
              </>
            );
            return event.task_id ? (
              <button
                className="event-item"
                key={event.event_id ?? event.sequence}
                onClick={() => onSelectTask(event.task_id!)}
                title={`查看 ${event.task_id} 任务详情`}
              >
                {content}
              </button>
            ) : (
              <div className="event-item" key={event.event_id ?? event.sequence}>{content}</div>
            );
          })}
        </div>
      </section>
    </div>
  );
}
