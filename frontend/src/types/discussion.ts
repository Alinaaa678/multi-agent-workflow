export interface DiscussionStage {
  id: string;
  code: string;
  name: string;
  owner: string;
  action: string;
}

export interface DiscussionClaim {
  claim_id: string;
  role: string;
  claim: string;
  reason: string;
  evidence: string;
  impact: string;
  round: number;
}

export interface DiscussionObjection {
  objection_id: string;
  raised_by: string;
  target_id: string;
  category: string;
  content: string;
  severity: string;
  evidence: string;
  status: string;
  round: number;
  response?: string;
}

export interface DiscussionStance {
  role: string;
  stance: string;
  reason: string;
  round?: number;
}

export interface RequirementItem {
  req_id: string;
  description: string;
  priority: string;
  acceptance: string;
  status: string;
}

export interface RequirementsVersion {
  version: string;
  status: string;
  requirements: RequirementItem[];
  confirmed_by?: string[];
  open_questions?: string[];
  decision_refs?: string[];
}

export interface DiscussionEvent {
  event_id: string;
  sequence: number;
  type: string;
  time: string;
  payload: Record<string, unknown>;
  after: { stage: string; status: string; round: number };
}

export interface DiscussionRun {
  run_id: string;
  idea: string;
  constraints: string;
  scenario: string;
  max_rounds: number;
  round: number;
  stage: string;
  status: string;
  created_at: string;
  updated_at: string;
  events: DiscussionEvent[];
  blackboard: {
    problem: string;
    assumptions: string[];
    claims: DiscussionClaim[];
    objections: DiscussionObjection[];
    stances: DiscussionStance[];
    decisions: Record<string, unknown>[];
    open_questions: string[];
    proposal_draft: string;
    signoffs: DiscussionStance[];
    requirements_draft: RequirementsVersion | null;
    r1: RequirementsVersion | null;
  };
  latest_consensus: {
    can_proceed?: boolean;
    needs_human?: boolean;
    reason?: string;
    open_count?: number;
    high_count?: number;
    signoff_count?: number;
  } | null;
  failure: { type: string; message: string; stage: string } | null;
}

export interface DiscussionReplay {
  passed: boolean;
  event_count: number;
  replayed_state: { stage: string; status: string; round: number };
  checkpoint_state: { stage: string; status: string; round: number };
  timeline: {
    sequence: number;
    event_id: string;
    type: string;
    stage: string;
    status: string;
    round: number;
  }[];
}

export interface DiscussionFixture {
  generated_at: string;
  note: string;
  workflow: {
    stages: DiscussionStage[];
    transitions: { from: string; event: string; guard: string; to: string }[];
  };
  run: DiscussionRun;
  replay: DiscussionReplay;
}
