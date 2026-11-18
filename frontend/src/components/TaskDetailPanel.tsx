/**
 * B05-D25 任务详情面板
 *
 * 点击任务后展示：输入、输出、依赖、验收条件、测试编号、日志、提交、缺陷。
 * 只渲染传入的数据，不做业务判断（模拟/真实必须由 dataSource 明确区分）。
 */

import { statusColors, statusLabels } from '../constants/status';
import type { DataSource, Task } from '../types/task';

interface Props {
  task: Task | null;
  dataSource?: DataSource;
  onClose?: () => void;
}

const BLOCK_LABELS: Record<string, string> = {
  B01: 'B01 规格与计划',
  B02: 'B02 任务编排',
  B03: 'B03 状态持久化',
  B04: 'B04 Agent 执行',
  B05: 'B05 交互展示',
  B06: 'B06 质量验收',
};

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section style={{ marginBottom: 14 }}>
      <h4 style={{ margin: '0 0 6px', fontSize: 12, color: '#334155', letterSpacing: 0.3 }}>{title}</h4>
      <div style={{ fontSize: 12, color: '#475569', lineHeight: 1.6 }}>{children}</div>
    </section>
  );
}

function Chips({ items, empty }: { items: string[]; empty: string }) {
  if (!items.length) return <span style={{ color: '#94a3b8' }}>{empty}</span>;
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
      {items.map((item) => (
        <code
          key={item}
          style={{
            background: '#f1f5f9',
            border: '1px solid #e2e8f0',
            borderRadius: 4,
            padding: '1px 6px',
            fontSize: 11,
            fontFamily: 'ui-monospace, monospace',
          }}
        >
          {item}
        </code>
      ))}
    </div>
  );
}

export function TaskDetailPanel({ task, dataSource = 'simulation', onClose }: Props) {
  if (!task) {
    return (
      <aside className="detail-panel detail-panel-empty">
        <p style={{ color: '#94a3b8', fontSize: 13 }}>点击任务节点查看详情</p>
      </aside>
    );
  }

  const color = statusColors[task.status] || '#94a3b8';
  const findings = (task.review?.findings ?? []).filter((item) => item.severity === 'blocking');
  const isReal = dataSource === 'real';

  return (
    <aside className="detail-panel">
      <header style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
        <strong style={{ fontSize: 16 }}>{task.task_id}</strong>
        <span style={{ background: color, color: '#fff', borderRadius: 10, padding: '1px 8px', fontSize: 11 }}>
          {statusLabels[task.status]}
        </span>
        <span
          style={{
            marginLeft: 'auto',
            fontSize: 10,
            padding: '1px 6px',
            borderRadius: 4,
            border: `1px solid ${isReal ? '#0ea5e9' : '#cbd5e1'}`,
            color: isReal ? '#0284c7' : '#64748b',
          }}
          title="数据来源：真实后端或本地模拟"
        >
          {isReal ? '真实接口' : '模拟数据'}
        </span>
        {onClose && (
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭详情"
            style={{ border: 'none', background: 'transparent', cursor: 'pointer', fontSize: 16, color: '#94a3b8' }}
          >
            ×
          </button>
        )}
      </header>

      <p style={{ margin: '0 0 12px', fontSize: 13, color: '#0f172a', lineHeight: 1.5 }}>{task.goal}</p>

      <Section title="负责与追踪">
        <div>{BLOCK_LABELS[task.block_id ?? ''] || task.block_id || '—'}</div>
        <div>
          角色 {task.role_id || task.assignee || '—'} · 能力 {task.capability || '—'} · 优先级 {task.priority || '—'}
        </div>
        <div>
          尝试 #{task.attempt_id} · 重试 {task.retry_count}
          {typeof task.duration_seconds === 'number' ? ` · 耗时 ${task.duration_seconds.toFixed(2)}s` : ''}
        </div>
        <div style={{ fontFamily: 'ui-monospace, monospace', fontSize: 11 }}>
          输入快照 {task.input_snapshot || '—'}
        </div>
      </Section>

      <Section title="依赖（上游任务）">
        <Chips items={task.depends_on} empty="无依赖（根任务）" />
      </Section>

      <Section title="输入">
        <Chips items={task.inputs ?? []} empty="未声明输入" />
      </Section>

      <Section title="输出产物">
        {task.outputs.length ? (
          <ul style={{ margin: 0, paddingLeft: 16 }}>
            {task.outputs.map((artifact) => (
              <li key={`${artifact.path}-${artifact.version}`}>
                <code style={{ fontSize: 11 }}>{artifact.path}</code>
                {artifact.version ? <span style={{ color: '#94a3b8' }}> · {artifact.version}</span> : null}
              </li>
            ))}
          </ul>
        ) : (
          <span style={{ color: '#94a3b8' }}>尚无产物</span>
        )}
      </Section>

      <Section title="验收条件">
        <Chips items={task.acceptance ?? []} empty="未声明验收条件" />
      </Section>

      <Section title="规定测试编号">
        <Chips items={task.required_test_ids ?? []} empty="未声明测试编号" />
      </Section>

      <Section title="证据与测试">
        {task.evidence.length ? (
          <ul style={{ margin: 0, paddingLeft: 16 }}>
            {task.evidence.map((item) => (
              <li key={item.case_id}>
                <strong>{item.case_id}</strong> {item.passed ? '通过' : '未通过'}
                {item.detail ? ` · ${item.detail}` : ''}
              </li>
            ))}
          </ul>
        ) : (
          <span style={{ color: '#94a3b8' }}>尚无证据</span>
        )}
      </Section>

      <Section title="日志">
        {task.logs && task.logs.length ? (
          <ul style={{ margin: 0, paddingLeft: 16 }}>
            {task.logs.map((log) => (
              <li key={log.name}>
                <code style={{ fontSize: 11 }}>{log.name}</code>
                <span style={{ color: '#94a3b8', fontSize: 10 }}> {log.sha256.slice(0, 12)}</span>
              </li>
            ))}
          </ul>
        ) : (
          <span style={{ color: '#94a3b8' }}>尚无日志</span>
        )}
      </Section>

      <Section title="提交">
        {task.candidate_commit ? (
          <code style={{ fontSize: 11 }}>{task.candidate_commit}</code>
        ) : (
          <span style={{ color: '#94a3b8' }}>尚无候选提交</span>
        )}
      </Section>

      <Section title="缺陷 / 异议">
        {task.objections.length ? (
          <ul style={{ margin: 0, paddingLeft: 16 }}>
            {task.objections.map((item) => (
              <li key={item.id} style={{ color: item.status === 'open' ? '#b91c1c' : '#475569' }}>
                <strong>{item.from}</strong> {item.reason}（{item.status === 'open' ? '未关闭' : '已关闭'}）
              </li>
            ))}
          </ul>
        ) : (
          <span style={{ color: '#94a3b8' }}>无缺陷</span>
        )}
        {findings.length > 0 && (
          <ul style={{ margin: '6px 0 0', paddingLeft: 16, color: '#b91c1c' }}>
            {findings.map((item) => (
              <li key={`${item.code}-${item.message}`}>
                {item.code}: {item.message}
              </li>
            ))}
          </ul>
        )}
      </Section>

      {task.blocked_reason && (
        <Section title="阻塞原因">
          <span style={{ color: '#b91c1c' }}>{task.blocked_reason}</span>
        </Section>
      )}

      <Section title="尝试历史">
        {task.attempts.length ? (
          <ul style={{ margin: 0, paddingLeft: 16 }}>
            {task.attempts.map((attempt) => (
              <li key={`${attempt.attempt_id}-${attempt.started_at}`}>
                #{attempt.attempt_id} · {attempt.result} · retry {attempt.retry_count}
                {attempt.note ? ` · ${attempt.note}` : ''}
              </li>
            ))}
          </ul>
        ) : (
          <span style={{ color: '#94a3b8' }}>尚无尝试记录</span>
        )}
      </Section>
    </aside>
  );
}
