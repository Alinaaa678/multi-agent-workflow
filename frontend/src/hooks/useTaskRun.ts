/**
 * B05-D24/D26：从 B02 真实接口加载任务运行（快照 + DAG + 事件）。
 *
 * 设计约束：
 * - 真实接口失败时**不得**把模拟数据当作真实数据展示；
 *   因此错误单独暴露，界面据此显示「连接失败」而不是静默回退。
 * - 轮询间隔可配置，卸载时清理定时器与在途请求。
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { fetchTaskRun, type RunEvent, type TaskApiResult } from '../api/taskRunApi';
import type { Task } from '../types/task';

export interface UseTaskRunOptions {
  baseUrl: string;
  runId: string;
  /** 轮询间隔（毫秒）；0 表示只加载一次。 */
  pollIntervalMs?: number;
  enabled?: boolean;
}

export interface TaskRunState {
  tasks: Task[];
  meta: TaskApiResult['meta'] | null;
  events: RunEvent[];
  dag: TaskApiResult['dag'] | null;
  loading: boolean;
  error: string | null;
  lastUpdated: string | null;
  reload: () => void;
}

export function useTaskRun(options: UseTaskRunOptions): TaskRunState {
  const { baseUrl, runId, pollIntervalMs = 0, enabled = true } = options;
  const [tasks, setTasks] = useState<Task[]>([]);
  const [meta, setMeta] = useState<TaskApiResult['meta'] | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [dag, setDag] = useState<TaskApiResult['dag'] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<string | null>(null);
  const controllerRef = useRef<AbortController | null>(null);
  const [tick, setTick] = useState(0);

  const reload = useCallback(() => setTick((value) => value + 1), []);

  useEffect(() => {
    if (!enabled || !baseUrl || !runId) return undefined;

    let cancelled = false;
    const load = async () => {
      controllerRef.current?.abort();
      const controller = new AbortController();
      controllerRef.current = controller;
      setLoading(true);
      try {
        const result = await fetchTaskRun({ baseUrl, runId }, controller.signal);
        if (cancelled) return;
        setTasks(result.tasks);
        setMeta(result.meta);
        setEvents(result.events);
        setDag(result.dag);
        setError(null);
        setLastUpdated(new Date().toISOString());
      } catch (caught) {
        if (cancelled || controller.signal.aborted) return;
        setError(caught instanceof Error ? caught.message : String(caught));
        // 失败时清空真实数据，避免把旧的“真实”数据误认为当前状态。
        setTasks([]);
        setMeta(null);
        setEvents([]);
        setDag(null);
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    void load();
    if (pollIntervalMs > 0) {
      const timer = setInterval(() => void load(), pollIntervalMs);
      return () => {
        cancelled = true;
        clearInterval(timer);
        controllerRef.current?.abort();
      };
    }
    return () => {
      cancelled = true;
      controllerRef.current?.abort();
    };
  }, [baseUrl, runId, pollIntervalMs, enabled, tick]);

  return { tasks, meta, events, dag, loading, error, lastUpdated, reload };
}
