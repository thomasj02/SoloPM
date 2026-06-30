// api.ts — thin, typed client over the SoloPM HTTP API (same-origin, under /api).
// Never sends X-SoloPM-Actor: the backend attributes web writes to `human`.

import type {
  Activity,
  Graph,
  GraphQuery,
  LinkType,
  Meta,
  Project,
  ProjectCreate,
  ProjectDelete,
  ProjectPatch,
  ProjectStatus,
  RadarReport,
  ReviewMemoryItem,
  State,
  Ticket,
  TicketCreate,
  TicketSummary,
} from "./types";

const BASE = "/api";

/** Error carrying the backend's machine `code` plus a human message. */
export class ApiError extends Error {
  readonly code: string;
  readonly status: number;

  constructor(code: string | undefined, message: string | undefined, status: number) {
    super(message || code || "Request failed");
    this.name = "ApiError";
    this.code = code || "error";
    this.status = status ?? 0;
  }
}

interface ErrorBody {
  error?: { code?: string; message?: string };
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  let res: Response;
  try {
    res = await fetch(BASE + path, {
      method,
      headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    // Network-level failure (server down, DNS, CORS, offline, ...).
    throw new ApiError("network", "Can't reach backend — is `solopm serve` running?", 0);
  }

  if (res.status === 204) return null as T;

  // Tolerate empty / non-JSON bodies without throwing.
  let data: unknown = null;
  const text = await res.text();
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = null;
    }
  }

  if (!res.ok) {
    const err = (data as ErrorBody | null)?.error ?? {};
    throw new ApiError(err.code, err.message || `HTTP ${res.status}`, res.status);
  }
  return data as T;
}

const enc = encodeURIComponent;

export interface TicketQuery {
  project?: string;
  state?: State;
  assignee?: string;
}

export const api = {
  meta: () => request<Meta>("GET", "/meta"),

  projects: () => request<{ projects: Project[] }>("GET", "/projects"),
  createProject: (body: ProjectCreate) => request<Project>("POST", "/projects", body),
  project: (key: string) => request<Project>("GET", `/projects/${enc(key)}`),
  projectStatus: (key: string) =>
    request<ProjectStatus>("GET", `/projects/${enc(key)}/status`),
  patchProject: (key: string, body: ProjectPatch) =>
    request<Project>("PATCH", `/projects/${enc(key)}`, body),
  // `force` cascade-deletes the project's tickets; without it a non-empty project is
  // refused by the backend (ApiError code "validation").
  deleteProject: (key: string, force = false) =>
    request<ProjectDelete>("DELETE", `/projects/${enc(key)}${force ? "?force=true" : ""}`),

  tickets: (q: TicketQuery = {}) => {
    const params = new URLSearchParams();
    if (q.project) params.set("project", q.project);
    if (q.state) params.set("state", q.state);
    if (q.assignee) params.set("assignee", q.assignee);
    const qs = params.toString();
    return request<{ tickets: TicketSummary[] }>("GET", `/tickets${qs ? "?" + qs : ""}`);
  },
  createTicket: (body: TicketCreate) => request<Ticket>("POST", "/tickets", body),
  ticket: (id: string) => request<Ticket>("GET", `/tickets/${enc(id)}`),
  patchTicket: (id: string, body: { title?: string; description?: string }) =>
    request<Ticket>("PATCH", `/tickets/${enc(id)}`, body),
  comment: (id: string, body: string) =>
    request<Activity>("POST", `/tickets/${enc(id)}/comments`, { body }),
  assign: (id: string, assignee: string) =>
    request<Ticket>("POST", `/tickets/${enc(id)}/assign`, { assignee }),
  reorder: (id: string, after: string | null) =>
    request<Ticket>("POST", `/tickets/${enc(id)}/reorder`, { after }),

  // after: omit -> bottom of target column; null -> top; id -> directly below it.
  move: (id: string, state: State, after?: string | null) =>
    request<Ticket>(
      "POST",
      `/tickets/${enc(id)}/move`,
      after === undefined ? { state } : { state, after },
    ),

  addCriterion: (id: string, text: string) =>
    request<Ticket>("POST", `/tickets/${enc(id)}/criteria`, { text }),
  updateCriterion: (id: string, cid: string, body: { text?: string; done?: boolean }) =>
    request<Ticket>("PATCH", `/tickets/${enc(id)}/criteria/${enc(cid)}`, body),
  removeCriterion: (id: string, cid: string) =>
    request<Ticket>("DELETE", `/tickets/${enc(id)}/criteria/${enc(cid)}`),

  addLink: (id: string, type: LinkType, other: string) =>
    request<Ticket>("POST", `/tickets/${enc(id)}/links`, { type, other }),
  // `direction` ("out"/"in", relative to `id`) pins one orientation so removing a relation
  // row never also deletes its opposite-direction sibling (e.g. A blocks B vs B blocks A).
  removeLink: (id: string, other: string, type?: LinkType, direction?: "out" | "in") => {
    const params = new URLSearchParams();
    if (type) params.set("type", type);
    if (direction) params.set("direction", direction);
    const qs = params.toString();
    return request<Ticket>("DELETE", `/tickets/${enc(id)}/links/${enc(other)}${qs ? "?" + qs : ""}`);
  },

  radar: (project?: string) =>
    request<RadarReport>("GET", `/radar${project ? "?project=" + enc(project) : ""}`),

  graph: (q: GraphQuery = {}) => {
    const p = new URLSearchParams();
    if (q.project) p.set("project", q.project);
    if (q.around) p.set("around", q.around);
    if (q.depth != null) p.set("depth", String(q.depth));
    if (q.active_only) p.set("active_only", "true");
    for (const t of q.types ?? []) p.append("type", t);
    const qs = p.toString();
    return request<Graph>("GET", `/graph${qs ? "?" + qs : ""}`);
  },

  addReviewMemory: (key: string, text: string) =>
    request<ReviewMemoryItem>("POST", `/projects/${enc(key)}/review-memory`, { text }),
  updateReviewMemory: (key: string, id: string, body: { text?: string; status?: string }) =>
    request<ReviewMemoryItem>("PATCH", `/projects/${enc(key)}/review-memory/${enc(id)}`, body),
};
