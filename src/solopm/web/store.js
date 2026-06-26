// store.js — central app state, a tiny pub/sub, and the data-loading layer.
// Views subscribe to events and re-render; actions mutate `state` then emit.

import { api } from "./api.js";

// Fallback enums so the UI stays usable even if GET /api/meta is unreachable.
const FALLBACK_META = {
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

export const state = {
  meta: FALLBACK_META,
  projects: [],
  currentProject: localStorage.getItem(LS_PROJECT) || null,
  tickets: [],
  filter: "",
  ticketsError: null,
  backendDown: false,
};

// --- tiny event bus -------------------------------------------------------
const listeners = new Map();

export function on(event, fn) {
  if (!listeners.has(event)) listeners.set(event, new Set());
  listeners.get(event).add(fn);
  return () => listeners.get(event)?.delete(fn);
}

export function emit(event, payload) {
  for (const fn of listeners.get(event) || []) fn(payload);
}

// --- selectors ------------------------------------------------------------
export function currentProject() {
  return state.projects.find((p) => p.key === state.currentProject) || null;
}

// --- actions --------------------------------------------------------------
export async function loadMeta() {
  try {
    const meta = await api.meta();
    if (meta && Array.isArray(meta.states)) state.meta = meta;
  } catch {
    // Keep the fallback enums; backend errors surface via loadProjects/tickets.
  }
  emit("meta");
}

export async function loadProjects() {
  const data = await api.projects();
  state.backendDown = false;
  state.projects = (data && data.projects) || [];

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

export function setProject(key) {
  if (key === state.currentProject) return;
  state.currentProject = key;
  state.tickets = [];
  persistProject();
  emit("projects");
  refreshTickets().catch(() => {});
}

function persistProject() {
  if (state.currentProject) localStorage.setItem(LS_PROJECT, state.currentProject);
  else localStorage.removeItem(LS_PROJECT);
}

export async function refreshTickets({ silent = false } = {}) {
  if (!state.currentProject) {
    state.tickets = [];
    emit("tickets");
    return;
  }
  try {
    const data = await api.tickets({ project: state.currentProject });
    state.tickets = (data && data.tickets) || [];
    state.ticketsError = null;
    emit("tickets");
  } catch (err) {
    // A failed *background* poll must not blank an already-populated board — keep the
    // last-known cards on screen. Only initial/explicit loads show the error/Retry view.
    if (silent && state.tickets.length) throw err;
    state.ticketsError = err;
    emit("tickets");
    throw err; // let callers (polling/manual) decide how loud to be
  }
}

export function setFilter(value) {
  state.filter = value;
  emit("filter");
}
