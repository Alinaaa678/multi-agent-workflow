import { useState } from 'react';
import rawFixture from '../data/discussionRunFixture.json';
import { advanceDiscussion, confirmDiscussion, createDiscussion, fetchDiscussion, type CreateDiscussionInput } from '../api/discussionApi';
import type { DiscussionFixture, DiscussionRun } from '../types/discussion';

const fixture = rawFixture as DiscussionFixture;
const TEAM_MEMBERS = ['成员 A', '成员 B', '成员 C', '成员 D', '成员 E', '成员 F'];

type DiscussionTab = 'board' | 'requirements' | 'events';

interface DiscussionViewProps {
  apiBase: string;
  isReal: boolean;
  phase2Loading: boolean;
  onEnterPhase2: (discussionRunId: string) => void;
}

const STATUS_LABELS: Record<string, string> = {
  READY: '可推进', RUNNING: '执行中', WAITING_HUMAN: '等待人工', PAUSED: '已暂停',
  STOPPED: '预算停止', BLOCKED: '已阻断', COMPLETED: '已完成',
};

const EVENT_LABELS: Record<string, string> = {
  RUN_CREATED: '创建讨论运行', STAGE_STARTED: '阶段开始', IDEA_FRAMED: 'Idea 框定完成',
  INDEPENDENT_DIVERGENCE_COMPLETED: '独立发散完成', CLAIMS_CLUSTERED: '观点聚类完成',
  CRITIQUE_COMPLETED: '结构化批判完成', CONSENSUS_EVALUATED: '共识评价完成',
  PROPOSAL_REVIEWED: 'Proposal 审阅完成', R1_PUBLISHED: 'R1 正式发布',
  RUN_PAUSED: '运行暂停', RUN_RESUMED: '运行继续', RUN_BLOCKED: '运行阻断',
  HUMAN_DECISION_RECORDED: '记录人工裁决',
};

function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleTimeString('zh-CN', { hour12: false });
}

export function DiscussionView({ apiBase, isReal, phase2Loading, onEnterPhase2 }: DiscussionViewProps) {
  const [realRun, setRealRun] = useState<DiscussionRun | null>(null);
  const [realReplay, setRealReplay] = useState<DiscussionFixture['replay'] | null>(null);
  const [runIdInput, setRunIdInput] = useState('');
  const [idea, setIdea] = useState('设计一个校园失物招领系统，支持发布、检索和认领');
  const [constraints, setConstraints] = useState('未经六人确认不得发布 R1，保留完整讨论与执行证据。');
  const [scenario, setScenario] = useState<CreateDiscussionInput['scenario']>('normal');
  const [maxRounds, setMaxRounds] = useState(2);
  const [confirmedMembers, setConfirmedMembers] = useState<string[]>([]);
  const [activeTab, setActiveTab] = useState<DiscussionTab>('board');
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');

  const run = isReal ? realRun : fixture.run;
  const replay = isReal ? realReplay : fixture.replay;
  const stages = fixture.workflow.stages;
  const stageIndex = run ? stages.findIndex((stage) => stage.id === run.stage) : -1;
  const openObjections = run?.blackboard.objections.filter((item) => item.status === 'open').length ?? 0;
  const awaitingConfirmation = run?.stage === 'HUMAN_CONFIRMATION' && run.status === 'WAITING_HUMAN';

  const loadRun = async () => {
    if (!runIdInput.trim()) return;
    setBusy('load');
    setError('');
    try {
      const result = await fetchDiscussion(runIdInput.trim(), apiBase);
      setRealRun(result.run);
      setRealReplay(result.replay);
      setConfirmedMembers(result.run.blackboard.r1?.confirmed_by ?? []);
      setActiveTab('board');
    } catch (caught) {
      setRealRun(null);
      setRealReplay(null);
      setConfirmedMembers([]);
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy('');
    }
  };

  const createRun = async () => {
    setBusy('create');
    setError('');
    try {
      const created = await createDiscussion({ idea, constraints, scenario, max_rounds: maxRounds }, apiBase);
      const refreshed = await fetchDiscussion(created.run_id, apiBase);
      setRealRun(created);
      setRealReplay(refreshed.replay);
      setRunIdInput(created.run_id);
      setConfirmedMembers([]);
      setActiveTab('board');
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy('');
    }
  };

  const advance = async (action: 'step' | 'until-control-point') => {
    if (!realRun) return;
    setBusy(action);
    setError('');
    try {
      const next = await advanceDiscussion(realRun.run_id, apiBase, action);
      setRealRun(next);
      const refreshed = await fetchDiscussion(next.run_id, apiBase);
      setRealReplay(refreshed.replay);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy('');
    }
  };

  const confirm = async () => {
    if (!realRun || confirmedMembers.length !== TEAM_MEMBERS.length) return;
    setBusy('confirm');
    setError('');
    try {
      const next = await confirmDiscussion(realRun.run_id, apiBase, confirmedMembers);
      setRealRun(next);
      const refreshed = await fetchDiscussion(next.run_id, apiBase);
      setRealReplay(refreshed.replay);
      setActiveTab('requirements');
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy('');
    }
  };

  return (
    <section className="discussion-view" aria-label="作业一需求讨论与共识可视化">
      <div className="discussion-toolbar">
        <div>
          <span className="section-eyebrow">HOMEWORK 01 · DISCUSSION CONTROLLER</span>
          <h2>需求讨论与共识收敛</h2>
          <p>观察独立观点、结构化异议、共识评价和人工确认，不以“聊完”代替需求发布。</p>
        </div>
        {isReal ? (
          <form className="discussion-load" onSubmit={(event) => { event.preventDefault(); void loadRun(); }}>
            <input
              aria-label="讨论运行编号"
              value={runIdInput}
              onChange={(event) => setRunIdInput(event.target.value)}
              placeholder="输入 RUN-XXXXXXXXXX"
            />
            <button type="submit" disabled={Boolean(busy) || !runIdInput.trim()}>{busy === 'load' ? '加载中…' : '加载讨论'}</button>
          </form>
        ) : (
          <div className="snapshot-note"><strong>历史快照 · 只读</strong><span>{fixture.note}</span></div>
        )}
      </div>

      {isReal && !run && (
        <form className="discussion-create" onSubmit={(event) => { event.preventDefault(); void createRun(); }}>
          <div className="discussion-create-heading">
            <strong>创建真实讨论</strong>
            <span>由 B02 创建 RUN 运行；默认场景可在两轮批判后收敛到六人确认。</span>
          </div>
          <div className="discussion-create-fields">
            <label className="idea-field"><span>项目想法</span><textarea aria-label="项目想法" rows={2} minLength={3} required disabled={Boolean(busy)} value={idea} onChange={(event) => setIdea(event.target.value)} /></label>
            <label className="constraints-field"><span>讨论约束</span><textarea aria-label="讨论约束" rows={2} disabled={Boolean(busy)} value={constraints} onChange={(event) => setConstraints(event.target.value)} /></label>
            <label><span>演示场景</span><select aria-label="讨论场景" disabled={Boolean(busy)} value={scenario} onChange={(event) => setScenario(event.target.value as CreateDiscussionInput['scenario'])}><option value="normal">正常收敛</option><option value="value_conflict">价值分歧</option><option value="budget_exhaustion">预算耗尽</option><option value="agent_failure">Agent 失败</option></select></label>
            <label><span>最大批判轮数</span><input aria-label="最大批判轮数" type="number" min={1} max={5} required disabled={Boolean(busy)} value={maxRounds} onChange={(event) => setMaxRounds(Number(event.target.value))} /></label>
            <button className="primary" type="submit" disabled={Boolean(busy) || idea.trim().length < 3}>{busy === 'create' ? '创建中…' : '创建真实讨论'}</button>
          </div>
        </form>
      )}

      {error && <div className="alert error" role="alert">讨论操作失败：{error}</div>}
      {!run ? (
        <div className="discussion-empty">
          <strong>创建或加载一个真实讨论运行</strong>
          <p>创建后可直接推进讨论；也可输入已有 RUN 编号加载历史现场。</p>
        </div>
      ) : (
        <>
          <div className="discussion-run-summary">
            <div className="discussion-idea">
              <span>{run.run_id} · {isReal ? '真实接口' : '模拟数据 · 真实控制器产物'}</span>
              <h3>{run.idea}</h3>
              <p>{run.constraints}</p>
            </div>
            <dl className="discussion-metrics">
              <div><dt>当前阶段</dt><dd>{stages[stageIndex]?.name ?? run.stage}</dd></div>
              <div><dt>控制状态</dt><dd>{STATUS_LABELS[run.status] ?? run.status}</dd></div>
              <div><dt>批判轮次</dt><dd>{run.round} / {run.max_rounds}</dd></div>
              <div><dt>开放异议</dt><dd className={openObjections ? 'attention' : ''}>{openObjections}</dd></div>
              <div><dt>角色观点</dt><dd>{run.blackboard.claims.length}</dd></div>
              <div><dt>控制事件</dt><dd>{run.events.length}</dd></div>
            </dl>
          </div>

          <ol className="discussion-stages" aria-label="A 到 H 讨论阶段">
            {stages.map((stage, index) => {
              const state = run.status === 'COMPLETED' || index < stageIndex ? 'done' : index === stageIndex ? 'current' : 'pending';
              return (
                <li key={stage.id} className={state} title={`${stage.owner}：${stage.action}`}>
                  <span>{stage.code}</span>
                  <div><strong>{stage.name}</strong><small>{stage.owner}</small></div>
                </li>
              );
            })}
          </ol>

          {isReal && awaitingConfirmation && (
            <fieldset className="member-confirmation" disabled={Boolean(busy)}>
              <legend>六人确认门槛</legend>
              <div className="member-confirmation-list">
                {TEAM_MEMBERS.map((member) => (
                  <label key={member}><input type="checkbox" name="member-confirmation" value={member} checked={confirmedMembers.includes(member)} onChange={() => setConfirmedMembers((current) => current.includes(member) ? current.filter((item) => item !== member) : [...current, member])} /><span>{member}</span></label>
                ))}
                <button type="button" onClick={() => setConfirmedMembers(TEAM_MEMBERS)}>全选六人</button>
                <strong aria-live="polite">已确认 {confirmedMembers.length} / 6</strong>
              </div>
              <p>六名成员分别确认后才能发布 R1；沉默或未勾选不计为赞同。</p>
            </fieldset>
          )}

          {isReal && (
            <div className="discussion-controls" aria-label="讨论执行控制">
              <span>控制指令由 B02 执行，前端只读取快照与事件。</span>
              <button onClick={() => void advance('step')} disabled={Boolean(busy) || run.status !== 'READY'}>{busy === 'step' ? '推进中…' : '推进一阶段'}</button>
              <button className="primary" onClick={() => void advance('until-control-point')} disabled={Boolean(busy) || run.status !== 'READY'}>{busy === 'until-control-point' ? '执行中…' : '运行至人工介入'}</button>
              <button onClick={() => void confirm()} disabled={Boolean(busy) || !awaitingConfirmation || confirmedMembers.length !== TEAM_MEMBERS.length}>{busy === 'confirm' ? '发布中…' : '六人确认并发布 R1'}</button>
              <button className="phase-bridge" onClick={() => onEnterPhase2(run.run_id)} disabled={Boolean(busy) || phase2Loading || !run.blackboard.r1}>{phase2Loading ? '正在生成任务…' : '进入第二阶段任务编排 →'}</button>
            </div>
          )}

          <div className="discussion-content">
            <div className="discussion-main">
              <div className="discussion-tabs" role="tablist" aria-label="讨论详情视图">
                <button role="tab" aria-selected={activeTab === 'board'} className={activeTab === 'board' ? 'active' : ''} onClick={() => setActiveTab('board')}>观点与分歧</button>
                <button role="tab" aria-selected={activeTab === 'requirements'} className={activeTab === 'requirements' ? 'active' : ''} onClick={() => setActiveTab('requirements')}>Proposal 与 R1</button>
                <button role="tab" aria-selected={activeTab === 'events'} className={activeTab === 'events' ? 'active' : ''} onClick={() => setActiveTab('events')}>讨论事件 · {run.events.length}</button>
              </div>

              {activeTab === 'board' && (
                <div className="discussion-board" role="tabpanel">
                  <section>
                    <div className="content-section-heading"><div><h3>四角色独立观点</h3><p>保留来源、理由和证据，不以多数票抹掉少数意见。</p></div><span>{run.blackboard.claims.length} CLAIMS</span></div>
                    <div className="claim-grid">
                      {run.blackboard.claims.map((claim) => (
                        <article className="claim-item" key={claim.claim_id}>
                          <header><strong>{claim.role}</strong><span>ROUND {claim.round}</span></header>
                          <p>{claim.claim}</p>
                          <dl><div><dt>理由</dt><dd>{claim.reason}</dd></div><div><dt>证据</dt><dd>{claim.evidence}</dd></div></dl>
                          <code>{claim.claim_id}</code>
                        </article>
                      ))}
                    </div>
                  </section>
                  <section>
                    <div className="content-section-heading"><div><h3>异议及处理状态</h3><p>开放、回应和解决均可追踪。</p></div><span>{openObjections} OPEN</span></div>
                    <div className="objection-list">
                      {run.blackboard.objections.map((objection) => (
                        <article className={`discussion-objection ${objection.status}`} key={objection.objection_id}>
                          <header><strong>{objection.raised_by}</strong><span>{objection.category} · {objection.severity}</span><b>{objection.status}</b></header>
                          <p>{objection.content}</p>
                          <small>{objection.objection_id} → {objection.target_id} · 第 {objection.round} 轮</small>
                          {objection.response && <p className="objection-response">处理：{objection.response}</p>}
                        </article>
                      ))}
                    </div>
                  </section>
                </div>
              )}

              {activeTab === 'requirements' && (
                <div className="requirements-view" role="tabpanel">
                  <section>
                    <div className="content-section-heading"><div><h3>建设方案 Proposal</h3><p>经四角色审阅后提交人工确认。</p></div><span>{run.blackboard.signoffs.length} SIGN-OFFS</span></div>
                    <pre className="proposal-document">{run.blackboard.proposal_draft || '尚未生成 Proposal。'}</pre>
                  </section>
                  <section>
                    <div className="content-section-heading"><div><h3>需求基线 {run.blackboard.r1?.version ?? 'R-draft'}</h3><p>只有六人确认后才发布 R1，未确认前不会进入第二阶段。</p></div><span>{run.blackboard.r1?.status ?? run.blackboard.requirements_draft?.status ?? 'NOT READY'}</span></div>
                    <div className="requirement-list">
                      {(run.blackboard.r1 ?? run.blackboard.requirements_draft)?.requirements.map((requirement) => (
                        <article key={requirement.req_id}>
                          <header><code>{requirement.req_id}</code><span>{requirement.priority}</span><b>{requirement.status}</b></header>
                          <p>{requirement.description}</p>
                          <small>验收：{requirement.acceptance}</small>
                        </article>
                      ))}
                    </div>
                    {run.blackboard.r1 && <p className="confirmed-members">确认成员：{run.blackboard.r1.confirmed_by?.join('、')}</p>}
                  </section>
                </div>
              )}

              {activeTab === 'events' && (
                <div className="discussion-events" role="tabpanel">
                  <div className={`replay-validation ${replay?.passed ? 'valid' : 'invalid'}`}>
                    <strong>{replay?.passed ? '回放校验通过' : '等待回放校验'}</strong>
                    <span>{replay ? `${replay.event_count} 个连续事件；控制状态与 checkpoint 一致。` : '加载真实运行后校验。'}</span>
                  </div>
                  <div className="discussion-event-list">
                    {run.events.map((event) => (
                      <article key={event.event_id}>
                        <span>#{event.sequence}</span>
                        <div><strong>{EVENT_LABELS[event.type] ?? event.type}</strong><code>{event.type}</code></div>
                        <span>{event.after.stage}</span>
                        <span>{STATUS_LABELS[event.after.status] ?? event.after.status} · R{event.after.round}</span>
                        <time>{formatTime(event.time)}</time>
                      </article>
                    ))}
                  </div>
                </div>
              )}
            </div>

            <aside className="consensus-panel">
              <section>
                <span className="section-eyebrow">B06 QUALITY EVALUATION</span>
                <h3>最近共识评价</h3>
                <p>{run.latest_consensus?.reason ?? '尚未执行共识评价。'}</p>
                <dl>
                  <div><dt>可以继续</dt><dd>{String(run.latest_consensus?.can_proceed ?? false)}</dd></div>
                  <div><dt>需要人工</dt><dd>{String(run.latest_consensus?.needs_human ?? false)}</dd></div>
                  <div><dt>角色签署</dt><dd>{run.latest_consensus?.signoff_count ?? 0} / 4</dd></div>
                </dl>
              </section>
              <section>
                <h3>角色 sign-off</h3>
                <div className="signoff-list">
                  {run.blackboard.signoffs.map((signoff) => (
                    <div key={signoff.role}><span className={signoff.stance === 'Agree' ? 'agree' : 'pending'}>{signoff.stance}</span><strong>{signoff.role}</strong><p>{signoff.reason}</p></div>
                  ))}
                  {run.blackboard.signoffs.length === 0 && <p>Proposal 审阅后显示角色签署。</p>}
                </div>
              </section>
              <section>
                <h3>保留问题</h3>
                <ul>{run.blackboard.open_questions.map((question) => <li key={question}>{question}</li>)}</ul>
              </section>
            </aside>
          </div>
        </>
      )}
    </section>
  );
}
