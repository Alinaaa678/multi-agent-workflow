import { useEffect, useMemo, useState } from 'react';
import { statusFromEvents } from '../api/taskRunApi';
import { statusLabels } from '../constants/status';
import type { Task } from '../types/task';
import type { MonitorEvent } from './MonitoringPanel';
import { TaskGraph } from './TaskGraph';

interface ReplayViewProps {
  tasks: Task[];
  events: MonitorEvent[];
  runId: string;
  isReal: boolean;
}

const EVENT_LABELS: Record<string, string> = {
  TASK_READY: '任务就绪', TASK_DISPATCHED: '任务派发', TASK_STARTED: '开始执行',
  TASK_OUTPUT_SUBMITTED: '提交产物', TASK_REVIEW_REQUESTED: '请求验收',
  TASK_ACCEPTED: '验收通过', TASK_REJECTED: '验收驳回', TASK_RETRY_SCHEDULED: '自动重试',
  TASK_RETRY_REQUESTED: '人工重试', TASK_BLOCKED: '任务阻塞', TASK_COMPLETED: '任务完成',
  TASK_FAILED: '任务失败', TASK_WAITING_DEPENDENCY: '等待依赖', DEFECT_CREATED: '登记缺陷',
  DEFECT_CLOSED: '关闭缺陷', RUN_PAUSED: '运行暂停', RUN_RESUMED: '运行继续', RUN_CANCELLED: '运行取消',
};

function eventClass(type: string): string {
  if (/FAILED|BLOCKED|REJECTED/.test(type) || type === 'DEFECT_CREATED') return 'error';
  if (/ACCEPTED|COMPLETED/.test(type) || type === 'DEFECT_CLOSED') return 'success';
  if (type.startsWith('RUN_') || type === 'TASK_RETRY_REQUESTED') return 'control';
  return 'info';
}

function timeLabel(value?: string): string {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleTimeString('zh-CN', { hour12: false });
}

export function ReplayView({ tasks, events, runId, isReal }: ReplayViewProps) {
  const [cursor, setCursor] = useState(events.length);
  const [playing, setPlaying] = useState(false);
  const [taskFilter, setTaskFilter] = useState<string | null>(null);

  useEffect(() => {
    if (!playing || cursor >= events.length) return;
    const timer = window.setTimeout(() => {
      const next = Math.min(cursor + 1, events.length);
      setCursor(next);
      if (next >= events.length) setPlaying(false);
    }, 650);
    return () => window.clearTimeout(timer);
  }, [playing, cursor, events.length]);

  const visibleEvents = useMemo(() => events.slice(0, cursor), [events, cursor]);
  const statusMap = useMemo(() => statusFromEvents(visibleEvents), [visibleEvents]);
  const replayTasks = useMemo(() => tasks.map((task) => ({
    ...task,
    status: statusMap[task.task_id] ?? ('CREATED' as Task['status']),
  })), [tasks, statusMap]);
  const currentEvent = cursor > 0 ? events[cursor - 1] : null;
  const timeline = useMemo(() => visibleEvents
    .filter((event) => !taskFilter || event.task_id === taskFilter)
    .slice(-100)
    .reverse(), [visibleEvents, taskFilter]);
  const sequenceValid = events.every((event, index) => event.sequence === index + 1);
  const counts = {
    done: replayTasks.filter((task) => task.status === 'SUCCEEDED').length,
    active: replayTasks.filter((task) => ['READY', 'RUNNING', 'REVIEWING', 'RETRYING'].includes(task.status)).length,
    attention: replayTasks.filter((task) => ['FAILED', 'BLOCKED'].includes(task.status)).length,
  };

  return (
    <section className="replay-view" aria-label="执行事件回溯">
      <div className="replay-heading">
        <div>
          <span className="section-eyebrow">POST-HOC TRACEABILITY</span>
          <h2>执行事件回溯</h2>
          <p>按有序事件重建 DAG 历史状态；不会重新调用 Agent，也不会修改当前运行。</p>
        </div>
        <div className={`replay-validation ${sequenceValid ? 'valid' : 'invalid'}`}>
          <strong>{sequenceValid ? '事件序列连续' : '事件序列存在缺口'}</strong>
          <span>{runId} · {events.length} 条事件 · {isReal ? '真实接口快照' : '真实编排器历史产物'}</span>
        </div>
      </div>

      <div className="replay-transport">
        <div className="replay-buttons" role="group" aria-label="回放控制">
          <button onClick={() => { setPlaying(false); setCursor(0); }} disabled={cursor === 0}>起点</button>
          <button onClick={() => { setPlaying(false); setCursor((value) => Math.max(0, value - 1)); }} disabled={cursor === 0}>上一事件</button>
          <button className="primary" onClick={() => {
            if (cursor >= events.length) setCursor(0);
            setPlaying((value) => !value);
          }} disabled={events.length === 0}>{playing ? '暂停回放' : '播放回放'}</button>
          <button onClick={() => { setPlaying(false); setCursor((value) => Math.min(events.length, value + 1)); }} disabled={cursor >= events.length}>下一事件</button>
          <button onClick={() => { setPlaying(false); setCursor(events.length); }} disabled={cursor >= events.length}>最新状态</button>
        </div>
        <label className="replay-scrubber">
          <span>事件位置</span>
          <input
            aria-label="回放事件位置"
            type="range"
            min={0}
            max={events.length}
            value={cursor}
            onChange={(event) => { setPlaying(false); setCursor(Number(event.target.value)); }}
          />
          <strong>{cursor} / {events.length}</strong>
        </label>
      </div>

      <div className="replay-current" aria-live="polite">
        <span className={`replay-event-kind ${currentEvent ? eventClass(currentEvent.type) : 'info'}`}>
          {currentEvent ? `#${currentEvent.sequence}` : '#0'}
        </span>
        <div>
          <strong>{currentEvent ? EVENT_LABELS[currentEvent.type] ?? currentEvent.type : '运行尚未开始'}</strong>
          <code>{currentEvent?.type ?? 'INITIAL_STATE'}</code>
        </div>
        <span>{currentEvent?.task_id ?? 'ALL TASKS'}</span>
        <time>{timeLabel(currentEvent?.occurred_at)}</time>
        <div className="replay-counts">
          <span>已通过 <strong>{counts.done}</strong></span>
          <span>活跃 <strong>{counts.active}</strong></span>
          <span>异常 <strong>{counts.attention}</strong></span>
        </div>
      </div>

      <div className="replay-body">
        <div className="replay-canvas">
          {tasks.length > 0 ? (
            <TaskGraph
              tasks={replayTasks}
              selectedId={taskFilter}
              onNodeClick={(task) => setTaskFilter((current) => current === task.task_id ? null : task.task_id)}
            />
          ) : <div className="canvas-empty">加载任务运行后可回溯事件。</div>}
        </div>
        <aside className="replay-events" aria-label="回放事件时间线">
          <div className="replay-events-heading">
            <div><strong>事件时间线</strong><span>{timeline.length} / {visibleEvents.length}</span></div>
            {taskFilter && <button onClick={() => setTaskFilter(null)}>取消筛选 · {taskFilter}</button>}
          </div>
          <p className="replay-hint">点击 DAG 节点可只查看该任务事件，再次点击取消筛选。</p>
          <div className="replay-event-list">
            {timeline.length === 0 ? <div className="event-empty">当前位置暂无匹配事件</div> : timeline.map((event) => (
              <article className="replay-event" key={event.event_id ?? event.sequence}>
                <span className={`event-marker ${eventClass(event.type)}`} aria-hidden="true" />
                <span className="event-sequence">#{event.sequence}</span>
                <div><strong>{EVENT_LABELS[event.type] ?? event.type}</strong><code>{event.type}</code></div>
                <span>{event.task_id ?? 'RUN'}</span>
                <time>{timeLabel(event.occurred_at)}</time>
              </article>
            ))}
          </div>
          <div className="replay-status-legend">
            {Object.entries(statusLabels).map(([status, label]) => <span key={status}>{label}</span>)}
          </div>
        </aside>
      </div>
    </section>
  );
}
