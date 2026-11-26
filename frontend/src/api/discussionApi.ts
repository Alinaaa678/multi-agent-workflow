import type { DiscussionReplay, DiscussionRun } from '../types/discussion';

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: { Accept: 'application/json', 'Content-Type': 'application/json', ...init?.headers },
  });
  const body = await response.json() as T & { error?: string };
  if (!response.ok) throw new Error(body.error ?? `${response.status} ${response.statusText}`);
  return body;
}

function endpoint(baseUrl: string, runId: string, suffix = ''): string {
  return `${baseUrl.replace(/\/+$/, '')}/api/runs/${encodeURIComponent(runId)}${suffix}`;
}

export async function fetchDiscussion(runId: string, baseUrl: string): Promise<{
  run: DiscussionRun;
  replay: DiscussionReplay;
}> {
  const [runResult, replayResult] = await Promise.all([
    requestJson<{ run: DiscussionRun }>(endpoint(baseUrl, runId)),
    requestJson<{ replay: DiscussionReplay }>(endpoint(baseUrl, runId, '/replay')),
  ]);
  return { run: runResult.run, replay: replayResult.replay };
}

export async function advanceDiscussion(
  runId: string,
  baseUrl: string,
  action: 'step' | 'until-control-point',
): Promise<DiscussionRun> {
  const result = await requestJson<{ run: DiscussionRun }>(endpoint(baseUrl, runId, `/${action}`), {
    method: 'POST', body: JSON.stringify({}),
  });
  return result.run;
}

export interface CreateDiscussionInput {
  idea: string;
  constraints: string;
  scenario: 'normal' | 'value_conflict' | 'budget_exhaustion' | 'agent_failure';
  max_rounds: number;
}

export async function createDiscussion(input: CreateDiscussionInput, baseUrl: string): Promise<DiscussionRun> {
  const result = await requestJson<{ run: DiscussionRun }>(`${baseUrl.replace(/\/+$/, '')}/api/runs`, {
    method: 'POST',
    body: JSON.stringify(input),
  });
  return result.run;
}

export async function confirmDiscussion(runId: string, baseUrl: string, members: string[]): Promise<DiscussionRun> {
  const result = await requestJson<{ run: DiscussionRun }>(endpoint(baseUrl, runId, '/confirm'), {
    method: 'POST',
    body: JSON.stringify({ members }),
  });
  return result.run;
}
