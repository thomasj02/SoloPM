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
  | "review"
  | "link"
  | "unlink";

export interface Criterion {
  id: string;
  text: string;
  done: boolean;
}

// SOLO-10: ticket relationships. `type` is the canonical link type; `key` is the
// perspective group as seen from the viewing ticket (e.g. an A-blocks-B link reads as
// key "blocks" on A and key "blocked_by" on B).
export type LinkType = "blocks" | "related" | "duplicate" | "parent";

export type RelationKey =
  | "blocks"
  | "blocked_by"
  | "related"
  | "duplicate_of"
  | "duplicated_by"
  | "parent"
  | "sub";

export interface Relation {
  type: LinkType;
  key: RelationKey;
  label: string;
  direction: "out" | "in";
  ticket: { id: string; title: string; state: State };
  created_by: string;
  created_at: string;
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

// SOLO-14: dependency-graph projection of ticket relationships.
export interface GraphNode {
  id: string;
  project: string;
  title: string;
  state: State;
  assignee: string;
  blocked: boolean;
  subtickets: { done: number; total: number };
}

export interface GraphEdge {
  from: string;
  to: string;
  type: LinkType;
}

export interface Graph {
  nodes: GraphNode[];
  edges: GraphEdge[];
  cycles: string[][];
  scope: {
    project: string | null;
    around: string | null;
    depth: number | null;
    active_only: boolean;
    types: string[];
  };
  truncated: boolean;
}

export interface GraphQuery {
  project?: string;
  around?: string;
  depth?: number;
  active_only?: boolean;
  types?: LinkType[];
}

export interface Meta {
  version: string;
  states: State[];
  state_labels: Record<string, string>;
  assignees: Assignee[];
  actors: string[];
  transitions: Record<string, State[]>;
}

// SOLO-12: live git/PR health for the board header.
export interface ProjectStatus {
  open_prs: number;
  unpushed_commits: number;
}

export interface ReviewMemoryItem {
  id: string;
  text: string;
  source: string; // ai_fail | human_miss | manual
  status: "candidate" | "active" | "retired";
  hits: number;
  ticket: string | null;
  created_at: string;
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
  review_memory: ReviewMemoryItem[];
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
  // SOLO-10: derived relationship signals — an open (non-done/cancelled) blocker exists,
  // and the sub-ticket rollup (children done / total) when this ticket is a parent.
  blocked: boolean;
  subtickets: { done: number; total: number };
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
  relations: Relation[];
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
