// store.ts — central app state, a tiny pub/sub, and the data-loading layer.
// Views subscribe to events and re-render; actions mutate `state` then emit.

import { api, ApiError } from "./api";
import type { Meta, Project, RadarOverlap, TicketSummary } from "./types";

// Fallback enums so the UI stays usable even if GET /api/meta is unreachable.
const FALLBACK_META: Meta = {
  version: "?",
  states: ["backlog", "todo", "in-progress", "in-ai-review", "in-human-review", "done", "cancelled"],
  state_labels: {
    backlog: "Backlog",
    todo: "Todo",
    "in-progress": "In Progress",
    "in-ai-review": "In AI Review",
    "in-human-review": "In Human Review",
    done: "Done",
    cancelled: "Cancelled",
  },
  assignees: ["human", "claude", "codex", "unassigned"],
  actors: ["human", "claude", "codex"],
  transitions: {
    backlog: ["todo", "in-progress", "cancelled"],
    todo: ["backlog", "in-progress", "cancelled"],
    "in-progress": ["backlog", "todo", "in-ai-review", "cancelled"],
    "in-ai-review": ["in-progress", "in-human-review", "cancelled"],
    "in-human-review": ["in-progress", "done", "cancelled"],
    done: [],
    cancelled: [],
  },
};

const LS_PROJECT = "solopm.project";

export interface AppState {
  meta: Meta;
  projects: Project[];
  currentProject: string | null;
  tickets: TicketSummary[];
  filter: string;
  ticketsError: ApiError | null;
  backendDown: boolean;
  radar: RadarOverlap[];
}

export const state: AppState = {
  meta: FALLBACK_META,
  projects: [],
  currentProject: localStorage.getItem(LS_PROJECT) || null,
  tickets: [],
  filter: "",
  ticketsError: null,
  backendDown: false,
  radar: [],
};

// --- tiny event bus -------------------------------------------------------
type Listener = () => void;
const listeners = new Map<string, Set<Listener>>();

export function on(event: string, fn: Listener): () => void {
  let set = listeners.get(event);
  if (!set) {
    set = new Set();
    listeners.set(event, set);
  }
  set.add(fn);
  return () => listeners.get(event)?.delete(fn);
}

export function emit(event: string): void {
  for (const fn of listeners.get(event) ?? []) fn();
}

// --- selectors ------------------------------------------------------------
export function currentProject(): Project | null {
  return state.projects.find((p) => p.key === state.currentProject) ?? null;
}

// --- actions --------------------------------------------------------------
export async function loadMeta(): Promise<void> {
  try {
    const meta = await api.meta();
    if (meta && Array.isArray(meta.states)) state.meta = meta;
  } catch {
    // Keep the fallback enums; backend errors surface via loadProjects/tickets.
  }
  emit("meta");
}

export async function loadProjects(): Promise<void> {
  const data = await api.projects();
  state.backendDown = false;
  state.projects = data?.projects ?? [];

  // Drop a stale persisted selection; default to the first project.
  if (state.currentProject && !state.projects.some((p) => p.key === state.currentProject)) {
    state.currentProject = null;
  }
  if (!state.currentProject && state.projects.length) {
    state.currentProject = state.projects[0].key;
  }
  persistProject();
  emit("projects");
}

export function setProject(key: string): void {
  if (key === state.currentProject) return;
  state.currentProject = key;
  state.tickets = [];
  state.radar = []; // drop the old project's overlaps so the badge can't show/open stale data
  persistProject();
  emit("projects");
  emit("radar");
  void refreshTickets().catch(() => {});
}

function persistProject(): void {
  if (state.currentProject) localStorage.setItem(LS_PROJECT, state.currentProject);
  else localStorage.removeItem(LS_PROJECT);
}

export async function refreshTickets({ silent = false }: { silent?: boolean } = {}): Promise<void> {
  if (!state.currentProject) {
    state.tickets = [];
    state.radar = [];
    emit("tickets");
    emit("radar");
    return;
  }
  try {
    const data = await api.tickets({ project: state.currentProject });
    state.tickets = data?.tickets ?? [];
    state.ticketsError = null;
    emit("tickets");
    void refreshRadar(); // best-effort, non-blocking — keeps the overlap badge fresh
  } catch (err) {
    // A failed *background* poll must not blank an already-populated board — keep the
    // last-known cards on screen. Only initial/explicit loads show the error/Retry view.
    if (silent && state.tickets.length) throw err;
    state.ticketsError = err as ApiError;
    emit("tickets");
    throw err; // let callers (polling/manual) decide how loud to be
  }
}

export async function refreshRadar(): Promise<void> {
  if (!state.currentProject) {
    state.radar = [];
    emit("radar");
    return;
  }
  try {
    const data = await api.radar(state.currentProject);
    state.radar = data?.overlaps ?? [];
  } catch {
    // Radar is informational and best-effort; keep the last-known on error.
  }
  emit("radar");
}

export function setFilter(value: string): void {
  state.filter = value;
  emit("filter");
}
