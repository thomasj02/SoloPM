// types.ts — the domain shapes the web app shares with the backend (see API.md).

export type State =
  | "backlog"
  | "todo"
  | "in-progress"
  | "in-ai-review"
  | "in-human-review"
  | "done"
  | "cancelled";

export type Assignee = "human" | "claude" | "codex" | "unassigned";

export type ActivityKind =
  | "created"
  | "comment"
  | "state_change"
  | "assignment"
  | "edit"
  | "criteria"
  | "review";

export interface Criterion {
  id: string;
  text: string;
  done: boolean;
}

export interface RadarParty {
  ticket: string | null;
  branch: string;
}

export interface RadarOverlap {
  project: string;
  a: RadarParty;
  b: RadarParty;
  files: string[];
}

export interface RadarReport {
  overlaps: RadarOverlap[];
}

export interface Meta {
  version: string;
  states: State[];
  state_labels: Record<string, string>;
  assignees: Assignee[];
  actors: string[];
  transitions: Record<string, State[]>;
}

export interface Project {
  key: string;
  name: string;
  repo: string | null;
  master_branch: string;
  branch_convention: string;
  default_implementer: string;
  default_reviewer: string;
  review_prompt: string;
  ticket_count: number;
  created_at: string;
  updated_at: string;
}

export interface PR {
  number: number;
  url: string | null;
  state: string;
}

export interface Session {
  id: string;
  active: boolean;
}

export interface Activity {
  id: number;
  actor: string;
  kind: ActivityKind | string;
  body: string;
  meta: Record<string, unknown>;
  at: string;
}

export interface Comment {
  author: string;
  body: string;
  at: string;
}

export interface TicketSummary {
  id: string;
  project: string;
  title: string;
  state: State;
  assignee: Assignee;
  branch: string | null;
  session_active: boolean;
  pr: PR | null;
  acceptance: { done: number; total: number };
  comment_count: number;
  // SOLO-13: when the ticket entered its current state, plus the live elapsed seconds.
  state_entered_at: string;
  time_in_state_seconds: number | null;
  created_at: string;
  updated_at: string;
}

export interface Ticket {
  id: string;
  project: string;
  seq: number;
  title: string;
  description: string;
  state: State;
  assignee: Assignee;
  branch: string | null;
  pr: PR | null;
  session: Session | null;
  acceptance_criteria: Criterion[];
  comments: Comment[];
  activity: Activity[];
  state_entered_at: string;
  time_in_state_seconds: number | null;
  created_at: string;
  updated_at: string;
}

// --- request payloads -------------------------------------------------------

export interface ProjectCreate {
  key: string;
  name: string;
  repo?: string | undefined;
  master?: string | undefined;
}

export type ProjectPatch = Partial<{
  name: string;
  repo: string;
  master_branch: string;
  branch_convention: string;
  default_implementer: string;
  default_reviewer: string;
  review_prompt: string;
}>;

export interface TicketCreate {
  project: string;
  title: string;
  description?: string;
  state?: State;
  assignee?: Assignee;
}
