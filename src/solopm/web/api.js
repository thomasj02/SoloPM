// api.js — thin client over the SoloPM HTTP API (same-origin, under /api).
// Never sends X-SoloPM-Actor: the backend attributes web writes to `human`.

const BASE = "/api";

/** Error carrying the backend's machine `code` plus a human message. */
export class ApiError extends Error {
  constructor(code, message, status) {
    super(message || code || "Request failed");
    this.name = "ApiError";
    this.code = code || "error";
    this.status = status ?? 0;
  }
}

async function request(method, path, body) {
  let res;
  try {
    res = await fetch(BASE + path, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    // Network-level failure (server down, DNS, CORS, offline, ...).
    throw new ApiError("network", "Can't reach backend — is `solopm serve` running?", 0);
  }

  if (res.status === 204) return null;

  // Tolerate empty / non-JSON bodies without throwing.
  let data = null;
  const text = await res.text();
  if (text) {
    try { data = JSON.parse(text); } catch { data = null; }
  }

  if (!res.ok) {
    const err = (data && data.error) || {};
    throw new ApiError(err.code, err.message || `HTTP ${res.status}`, res.status);
  }
  return data;
}

const enc = encodeURIComponent;

export const api = {
  meta: () => request("GET", "/meta"),

  projects: () => request("GET", "/projects"),
  createProject: (body) => request("POST", "/projects", body),
  project: (key) => request("GET", `/projects/${enc(key)}`),
  patchProject: (key, body) => request("PATCH", `/projects/${enc(key)}`, body),

  tickets: (q = {}) => {
    const params = new URLSearchParams();
    if (q.project) params.set("project", q.project);
    if (q.state) params.set("state", q.state);
    if (q.assignee) params.set("assignee", q.assignee);
    const qs = params.toString();
    return request("GET", `/tickets${qs ? "?" + qs : ""}`);
  },
  createTicket: (body) => request("POST", "/tickets", body),
  ticket: (id) => request("GET", `/tickets/${enc(id)}`),
  patchTicket: (id, body) => request("PATCH", `/tickets/${enc(id)}`, body),
  comment: (id, body) => request("POST", `/tickets/${enc(id)}/comments`, { body }),
  // after: omit → bottom of target column; null → top; id → directly below it.
  move: (id, state, after) =>
    request("POST", `/tickets/${enc(id)}/move`, after === undefined ? { state } : { state, after }),
  assign: (id, assignee) => request("POST", `/tickets/${enc(id)}/assign`, { assignee }),
  // after = id of the ticket to sit below, or null for the top of the column.
  reorder: (id, after) => request("POST", `/tickets/${enc(id)}/reorder`, { after }),
};
