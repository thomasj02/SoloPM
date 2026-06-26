// ticket.js — the ticket detail side panel: full description (markdown), assignee,
// state transitions, activity feed, inline title/description editing, and comments.
// Every write re-fetches the ticket and refreshes the board behind the panel.

import { state, refreshTickets } from "./store.js";
import { api } from "./api.js";
import { el, clearChildren, relativeTime } from "./util.js";
import { renderMarkdown } from "./markdown.js";
import { toastError, toastSuccess, assigneeBadge } from "./ui.js";

let overlayEl = null;
let panelEl = null;
let currentId = null;
let ticket = null;
let editing = false;
let busy = false; // guards against overlapping writes

/** True while the panel is open (used by Esc routing and polling). */
export function isTicketOpen() {
  return !!currentId;
}

export async function openTicket(id) {
  ensurePanel();
  currentId = id;
  editing = false;
  ticket = null;
  openPanel();
  showLoading();
  try {
    ticket = await api.ticket(id);
    if (currentId === id) renderPanel();
  } catch (err) {
    if (currentId === id) showError(err);
  }
}

export function closeTicket() {
  if (!currentId) return;
  currentId = null;
  ticket = null;
  editing = false;
  overlayEl.classList.remove("ticketpanel-overlay--open");
  panelEl.classList.remove("ticketpanel--open");
}

/**
 * Background refresh of the open panel so concurrent CLI/agent writes appear live.
 * No-op while editing or mid-write. Only re-renders when the ticket actually changed,
 * and preserves any in-progress comment draft across the re-render.
 */
export async function pollOpenTicket() {
  if (!currentId || editing || busy) return;
  const id = currentId;
  let fresh;
  try {
    fresh = await api.ticket(id);
  } catch {
    return; // stay quiet; the panel keeps its last-known content
  }
  if (currentId !== id || !fresh) return;

  const changed =
    !ticket ||
    fresh.updated_at !== ticket.updated_at ||
    (fresh.activity?.length || 0) !== (ticket.activity?.length || 0);
  if (!changed) return;

  const draft = panelEl?.querySelector(".composer__input")?.value ?? null;
  ticket = fresh;
  renderPanel();
  if (draft) {
    const composer = panelEl?.querySelector(".composer__input");
    if (composer) composer.value = draft;
  }
}

// --- scaffolding ----------------------------------------------------------
function ensurePanel() {
  if (panelEl) return;
  panelEl = el("aside", { class: "ticketpanel", role: "dialog", "aria-label": "Ticket detail" });
  overlayEl = el("div", { class: "ticketpanel-overlay" }, [panelEl]);
  overlayEl.addEventListener("mousedown", (e) => {
    if (e.target === overlayEl) closeTicket();
  });
  document.body.append(overlayEl);
}

function openPanel() {
  overlayEl.classList.add("ticketpanel-overlay--open");
  panelEl.classList.add("ticketpanel--open");
}

function showLoading() {
  clearChildren(panelEl);
  panelEl.append(el("div", { class: "tp__center muted" }, "Loading…"));
}

function showError(err) {
  clearChildren(panelEl);
  panelEl.append(
    headerBar(currentId),
    el("div", { class: "tp__center tp__error" }, err.message || "Failed to load ticket."),
  );
}

// --- rendering ------------------------------------------------------------
function renderPanel() {
  clearChildren(panelEl);
  panelEl.append(headerBar(ticket.id, ticket));

  const scroll = el("div", { class: "tp__scroll" });
  if (editing) {
    scroll.append(renderEditForm());
  } else {
    scroll.append(
      renderTitle(),
      renderMetaCard(),
      renderMoveActions(),
      renderDescription(),
      renderActivity(),
    );
  }
  panelEl.append(scroll);

  // Composer is pinned to the bottom; hidden while editing the ticket body.
  if (!editing) panelEl.append(renderComposer());
}

function headerBar(id, t) {
  const left = [el("span", { class: "tp__id mono" }, id)];
  if (t) left.push(statePill(t.state));
  return el("header", { class: "tp__head" }, [
    el("div", { class: "tp__headleft" }, left),
    el("button", { class: "icon-btn tp__close", title: "Close (Esc)", "aria-label": "Close", onClick: closeTicket }, "×"),
  ]);
}

function statePill(s) {
  return el("span", { class: `pill pill--${s}` }, state.meta.state_labels[s] || s);
}

function renderTitle() {
  return el("div", { class: "tp__titlewrap" }, [
    el("h1", { class: "tp__title" }, ticket.title || "(untitled)"),
    el("button", {
      class: "btn btn--ghost btn--sm",
      title: "Edit title & description",
      onClick: () => {
        editing = true;
        renderPanel();
      },
    }, "Edit"),
  ]);
}

function renderMetaCard() {
  const assignSelect = el("select", { class: "select", title: "Change assignee", onChange: (e) => doAssign(e.target.value) });
  for (const a of state.meta.assignees) {
    assignSelect.append(el("option", { value: a, selected: a === ticket.assignee }, a));
  }

  const rows = [
    metaRow("Assignee", el("div", { class: "tp__assign" }, [assigneeBadge(ticket.assignee), assignSelect])),
    metaRow("State", statePill(ticket.state)),
  ];
  if (ticket.branch) rows.push(metaRow("Branch", el("code", { class: "mono tp__branch" }, ticket.branch)));
  if (ticket.pr) {
    rows.push(metaRow("PR", el("a", { class: "link", href: ticket.pr.url, target: "_blank", rel: "noopener noreferrer" }, `#${ticket.pr.number} · ${ticket.pr.state}`)));
  }
  if (ticket.session) {
    rows.push(metaRow("Session", el("span", { class: ticket.session.active ? "live-text" : "muted" }, ticket.session.active ? "● active" : "inactive")));
  }
  rows.push(metaRow("Created", el("span", { class: "muted", title: ticket.created_at }, relativeTime(ticket.created_at))));
  rows.push(metaRow("Updated", el("span", { class: "muted", title: ticket.updated_at }, relativeTime(ticket.updated_at))));

  return el("div", { class: "tp__meta surface" }, rows);
}

function metaRow(label, value) {
  return el("div", { class: "tp__metarow" }, [
    el("span", { class: "tp__metalabel" }, label),
    el("span", { class: "tp__metaval" }, value),
  ]);
}

function renderMoveActions() {
  const targets = state.meta.transitions[ticket.state] || [];
  if (!targets.length) {
    return el("div", { class: "tp__moves" }, el("span", { class: "muted" }, "Terminal state — no further transitions."));
  }
  const buttons = targets.map((s) =>
    el("button", { class: `btn btn--move btn--move-${s}`, onClick: () => doMove(s) }, `→ ${state.meta.state_labels[s] || s}`),
  );
  return el("div", { class: "tp__moves" }, [el("span", { class: "tp__moveslabel" }, "Move to"), ...buttons]);
}

function renderDescription() {
  return el("section", { class: "tp__section" }, [
    el("h3", { class: "tp__sectionhead" }, "Description"),
    el("div", { class: "tp__desc md", html: renderMarkdown(ticket.description) }),
  ]);
}

function renderEditForm() {
  const titleInput = el("input", { class: "input", value: ticket.title || "", maxlength: 200, placeholder: "Title" });
  const descInput = el("textarea", { class: "textarea tp__descedit", rows: 14, placeholder: "Description (markdown)…" }, ticket.description || "");

  const save = el("button", { class: "btn btn--primary" }, "Save");
  const cancel = el("button", { class: "btn btn--ghost", onClick: () => { editing = false; renderPanel(); } }, "Cancel");
  save.addEventListener("click", () => {
    const title = titleInput.value.trim();
    if (!title) {
      toastError("Title is required.");
      titleInput.focus();
      return;
    }
    doEdit({ title, description: descInput.value });
  });

  return el("div", { class: "tp__editform" }, [
    el("label", { class: "field" }, [el("span", { class: "field__label" }, "Title"), titleInput]),
    el("label", { class: "field" }, [el("span", { class: "field__label" }, "Description (markdown)"), descInput]),
    el("div", { class: "tp__editactions" }, [save, cancel]),
  ]);
}

function renderActivity() {
  const items = (ticket.activity || []).slice().sort((a, b) => new Date(a.at) - new Date(b.at));
  const feed = el("div", { class: "feed" });
  if (!items.length) feed.append(el("div", { class: "muted" }, "No activity yet."));
  for (const a of items) feed.append(renderActivityItem(a));
  return el("section", { class: "tp__section" }, [
    el("h3", { class: "tp__sectionhead" }, `Activity (${items.length})`),
    feed,
  ]);
}

function renderActivityItem(a) {
  if (a.kind === "comment") {
    return el("div", { class: "feed__comment" }, [
      el("div", { class: "feed__commenthead" }, [
        assigneeBadge(a.actor),
        el("span", { class: "feed__time muted", title: a.at }, relativeTime(a.at)),
      ]),
      el("div", { class: "feed__commentbody md", html: renderMarkdown(a.body) }),
    ]);
  }
  return el("div", { class: "feed__system" }, [
    el("span", { class: `feed__dot feed__dot--${a.kind}` }),
    el("span", { class: "feed__sysactor" }, a.actor),
    el("span", { class: "feed__systext" }, systemText(a)),
    el("span", { class: "feed__time muted", title: a.at }, relativeTime(a.at)),
  ]);
}

function systemText(a) {
  if (a.body) return a.body;
  switch (a.kind) {
    case "created": return "created this ticket";
    case "state_change": return "changed state";
    case "assignment": return "changed assignee";
    case "edit": return "edited the ticket";
    default: return a.kind;
  }
}

function renderComposer() {
  const ta = el("textarea", { class: "textarea composer__input", rows: 2, placeholder: "Write a comment…  (Ctrl/⌘+Enter to send)" });
  const send = el("button", { class: "btn btn--primary" }, "Comment");

  const submit = async () => {
    const body = ta.value.trim();
    if (!body) { ta.focus(); return; }
    send.disabled = true;
    try {
      await api.comment(currentId, body);
      ta.value = "";
      await reload();
      toastSuccess("Comment added");
    } catch (err) {
      toastError(err.message || "Failed to add comment");
    } finally {
      send.disabled = false;
    }
  };

  send.addEventListener("click", submit);
  ta.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      submit();
    }
  });
  return el("div", { class: "composer" }, [ta, el("div", { class: "composer__actions" }, send)]);
}

// --- writes ---------------------------------------------------------------
async function reload() {
  if (!currentId) return;
  try {
    ticket = await api.ticket(currentId);
    renderPanel();
  } catch (err) {
    showError(err);
  }
  refreshTickets({ silent: true }).catch(() => {});
}

async function doAssign(assignee) {
  if (busy) return;
  busy = true;
  try {
    ticket = await api.assign(currentId, assignee);
    renderPanel();
    refreshTickets({ silent: true }).catch(() => {});
  } catch (err) {
    toastError(err.message || "Assign failed");
    renderPanel(); // snap the <select> back to the truth
  } finally {
    busy = false;
  }
}

async function doMove(s) {
  if (busy) return;
  busy = true;
  try {
    ticket = await api.move(currentId, s);
    renderPanel();
    toastSuccess(`Moved to ${state.meta.state_labels[s] || s}`);
    refreshTickets({ silent: true }).catch(() => {});
  } catch (err) {
    toastError(err.message || "Move failed");
  } finally {
    busy = false;
  }
}

async function doEdit({ title, description }) {
  if (busy) return;
  busy = true;
  try {
    ticket = await api.patchTicket(currentId, { title, description });
    editing = false;
    renderPanel();
    toastSuccess("Saved");
    refreshTickets({ silent: true }).catch(() => {});
  } catch (err) {
    toastError(err.message || "Save failed");
  } finally {
    busy = false;
  }
}
