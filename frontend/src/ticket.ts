// ticket.ts — the ticket detail side panel: full description (markdown), assignee,
// state transitions, activity feed, inline title/description editing, and comments.
// Every write re-fetches the ticket and refreshes the board behind the panel.

import { state, refreshTickets } from "./store";
import { api } from "./api";
import { el, clearChildren, relativeTime } from "./util";
import { renderMarkdown } from "./markdown";
import { toastError, toastSuccess, assigneeBadge } from "./ui";
import { openGraph } from "./graph";
import type { Activity, LinkType, Relation, RelationKey, State, Ticket } from "./types";

// Stable display order of the relation perspective groups (mirrors the backend).
const RELATION_GROUP_ORDER: RelationKey[] = [
  "parent",
  "sub",
  "blocks",
  "blocked_by",
  "related",
  "duplicate_of",
  "duplicated_by",
];

// The relation types a user can create, read as "<this ticket> <label> <other>".
const LINK_TYPE_OPTIONS: { value: LinkType; label: string }[] = [
  { value: "blocks", label: "Blocks" },
  { value: "related", label: "Related to" },
  { value: "duplicate", label: "Duplicate of" },
  { value: "parent", label: "Parent" },
];

let overlayEl: HTMLElement | null = null;
let panelEl: HTMLElement | null = null;
let currentId: string | null = null;
let ticket: Ticket | null = null;
let editing = false;
let busy = false; // guards against overlapping writes

/** True while the panel is open (used by Esc routing and polling). */
export function isTicketOpen(): boolean {
  return !!currentId;
}

/** A cheap signature of the rendered relation rows (linked id/state/title + perspective),
 * so polling can tell when a *linked* ticket changed even though this ticket's own
 * updated_at did not. */
function relationsSignature(t: Ticket | null): string {
  return (t?.relations ?? [])
    .map((r) => `${r.key}:${r.ticket.id}:${r.ticket.state}:${r.ticket.title}`)
    .join("|");
}

export async function openTicket(id: string): Promise<void> {
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
    if (currentId === id) showError(err as Error);
  }
}

export function closeTicket(): void {
  if (!currentId) return;
  currentId = null;
  ticket = null;
  editing = false;
  overlayEl?.classList.remove("ticketpanel-overlay--open");
  panelEl?.classList.remove("ticketpanel--open");
}

/**
 * Background refresh of the open panel so concurrent CLI/agent writes appear live.
 * No-op while editing or mid-write. Only re-renders when the ticket actually changed,
 * and preserves any in-progress comment draft across the re-render.
 */
export async function pollOpenTicket(): Promise<void> {
  if (!currentId || editing || busy) return;
  const id = currentId;
  let fresh: Ticket;
  try {
    fresh = await api.ticket(id);
  } catch {
    return; // stay quiet; the panel keeps its last-known content
  }
  if (currentId !== id || !fresh) return;

  const changed =
    !ticket ||
    fresh.updated_at !== ticket.updated_at ||
    (fresh.activity?.length ?? 0) !== (ticket.activity?.length ?? 0) ||
    // Relation rows render the *linked* ticket's title/state, which change independently of
    // this ticket's updated_at — re-render when any of them shift (e.g. a sub-ticket reaches
    // Done). GET /tickets/{id} re-resolves these on every poll.
    relationsSignature(fresh) !== relationsSignature(ticket);
  if (!changed) return;

  const draft = panelEl?.querySelector<HTMLTextAreaElement>(".composer__input")?.value ?? null;
  ticket = fresh;
  renderPanel();
  if (draft) {
    const composer = panelEl?.querySelector<HTMLTextAreaElement>(".composer__input");
    if (composer) composer.value = draft;
  }
}

// --- scaffolding ----------------------------------------------------------
function ensurePanel(): void {
  if (panelEl) return;
  panelEl = el("aside", { class: "ticketpanel", role: "dialog", "aria-label": "Ticket detail" });
  overlayEl = el("div", { class: "ticketpanel-overlay" }, [panelEl]);
  overlayEl.addEventListener("mousedown", (e) => {
    if (e.target === overlayEl) closeTicket();
  });
  document.body.append(overlayEl);
}

function openPanel(): void {
  overlayEl?.classList.add("ticketpanel-overlay--open");
  panelEl?.classList.add("ticketpanel--open");
}

function showLoading(): void {
  if (!panelEl) return;
  clearChildren(panelEl);
  panelEl.append(el("div", { class: "tp__center muted" }, "Loading…"));
}

function showError(err: Error): void {
  if (!panelEl) return;
  clearChildren(panelEl);
  panelEl.append(
    headerBar(currentId ?? ""),
    el("div", { class: "tp__center tp__error" }, err.message || "Failed to load ticket."),
  );
}

// --- rendering ------------------------------------------------------------
function renderPanel(): void {
  const t = ticket;
  if (!panelEl || !t) return;
  clearChildren(panelEl);
  panelEl.append(headerBar(t.id, t));

  const scroll = el("div", { class: "tp__scroll" });
  if (editing) {
    scroll.append(renderEditForm(t));
  } else {
    scroll.append(
      renderTitle(t),
      renderMetaCard(t),
      renderMoveActions(t),
      renderCriteria(t),
      renderRelations(t),
      renderDescription(t),
      renderActivity(t),
    );
  }
  panelEl.append(scroll);

  // Composer is pinned to the bottom; hidden while editing the ticket body.
  if (!editing) panelEl.append(renderComposer());
}

function headerBar(id: string, t?: Ticket): HTMLElement {
  const left = [el("span", { class: "tp__id mono" }, id)];
  if (t) left.push(statePill(t.state));
  return el("header", { class: "tp__head" }, [
    el("div", { class: "tp__headleft" }, left),
    el("button", { class: "icon-btn tp__close", title: "Close (Esc)", "aria-label": "Close", onClick: closeTicket }, "×"),
  ]);
}

function statePill(s: State): HTMLElement {
  return el("span", { class: `pill pill--${s}` }, state.meta.state_labels[s] || s);
}

function renderTitle(t: Ticket): HTMLElement {
  return el("div", { class: "tp__titlewrap" }, [
    el("h1", { class: "tp__title" }, t.title || "(untitled)"),
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

function renderMetaCard(t: Ticket): HTMLElement {
  const assignSelect = el("select", {
    class: "select",
    title: "Change assignee",
    onChange: (e: Event) => doAssign((e.target as HTMLSelectElement).value),
  });
  for (const a of state.meta.assignees) {
    assignSelect.append(el("option", { value: a, selected: a === t.assignee }, a));
  }

  const rows: HTMLElement[] = [
    metaRow("Assignee", el("div", { class: "tp__assign" }, [assigneeBadge(t.assignee), assignSelect])),
    metaRow("State", statePill(t.state)),
  ];
  if (t.branch) rows.push(metaRow("Branch", el("code", { class: "mono tp__branch" }, t.branch)));
  if (t.pr) {
    rows.push(
      metaRow(
        "PR",
        el("a", { class: "link", href: t.pr.url ?? "#", target: "_blank", rel: "noopener noreferrer" }, `#${t.pr.number} · ${t.pr.state}`),
      ),
    );
  }
  if (t.session) {
    rows.push(metaRow("Session", el("span", { class: t.session.active ? "live-text" : "muted" }, t.session.active ? "● active" : "inactive")));
  }
  rows.push(metaRow("Created", el("span", { class: "muted", title: t.created_at }, relativeTime(t.created_at))));
  rows.push(metaRow("Updated", el("span", { class: "muted", title: t.updated_at }, relativeTime(t.updated_at))));

  return el("div", { class: "tp__meta surface" }, rows);
}

function metaRow(label: string, value: HTMLElement): HTMLElement {
  return el("div", { class: "tp__metarow" }, [
    el("span", { class: "tp__metalabel" }, label),
    el("span", { class: "tp__metaval" }, value),
  ]);
}

function renderMoveActions(t: Ticket): HTMLElement {
  const targets = state.meta.transitions[t.state] || [];
  if (!targets.length) {
    return el("div", { class: "tp__moves" }, el("span", { class: "muted" }, "Terminal state — no further transitions."));
  }
  const buttons = targets.map((s) =>
    el("button", { class: `btn btn--move btn--move-${s}`, onClick: () => doMove(s) }, `→ ${state.meta.state_labels[s] || s}`),
  );
  return el("div", { class: "tp__moves" }, [el("span", { class: "tp__moveslabel" }, "Move to"), ...buttons]);
}

function renderCriteria(t: Ticket): HTMLElement {
  const crit = t.acceptance_criteria || [];
  const doneN = crit.filter((c) => c.done).length;

  const list = el("ul", { class: "tp__criteria" });
  if (!crit.length) list.append(el("li", { class: "muted tp__critempty" }, "No acceptance criteria yet."));
  for (const c of crit) {
    const box = el("input", {
      type: "checkbox",
      class: "tp__critbox",
      checked: c.done,
      title: c.done ? "Mark not done" : "Mark done",
      onChange: () => void doCheckCriterion(c.id, !c.done),
    });
    const text = el("span", { class: c.done ? "tp__crittext tp__crittext--done" : "tp__crittext" }, c.text);
    const remove = el("button", {
      class: "icon-btn tp__critdel",
      title: "Remove criterion",
      "aria-label": "Remove criterion",
      onClick: () => void doRemoveCriterion(c.id),
    }, "×");
    list.append(el("li", { class: "tp__crit" }, [box, text, remove]));
  }

  const input = el("input", { class: "input tp__critadd", placeholder: "Add a criterion…", maxlength: 500 });
  const add = async () => {
    const text = input.value.trim();
    if (!text) {
      input.focus();
      return;
    }
    await doAddCriterion(text);
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      void add();
    }
  });
  const addBtn = el("button", { class: "btn btn--ghost btn--sm", onClick: () => void add() }, "Add");

  return el("section", { class: "tp__section" }, [
    el("h3", { class: "tp__sectionhead" }, `Acceptance Criteria (${doneN}/${crit.length})`),
    list,
    el("div", { class: "tp__critaddrow" }, [input, addBtn]),
  ]);
}

function renderRelations(t: Ticket): HTMLElement {
  const rels = t.relations || [];
  const groups = new Map<RelationKey, Relation[]>();
  for (const r of rels) {
    const arr = groups.get(r.key) ?? [];
    arr.push(r);
    groups.set(r.key, arr);
  }

  const section = el("section", { class: "tp__section" }, [
    el("div", { class: "tp__sectionrow" }, [
      el("h3", { class: "tp__sectionhead" }, `Relations (${rels.length})`),
      el(
        "button",
        {
          class: "btn btn--ghost btn--sm",
          title: "Open the dependency graph around this ticket",
          onClick: () => void openGraph({ around: t.id, depth: 2 }),
        },
        "⛓ Graph",
      ),
    ]),
  ]);
  if (!rels.length) {
    section.append(el("div", { class: "muted tp__critempty" }, "No linked tickets yet."));
  }
  for (const key of RELATION_GROUP_ORDER) {
    const items = groups.get(key);
    if (items && items.length) section.append(renderRelationGroup(key, items));
  }
  section.append(renderAddRelation(t));
  return section;
}

function renderRelationGroup(key: RelationKey, items: Relation[]): HTMLElement {
  let heading = items[0].label;
  if (key === "sub") {
    const done = items.filter((r) => r.ticket.state === "done").length;
    heading = `Sub-tickets (${done}/${items.length})`;
  }
  const list = el("ul", { class: "tp__rellist" });
  for (const r of items) list.append(renderRelationRow(r, key === "sub"));
  return el("div", { class: "tp__relgroup" }, [
    el("div", { class: "tp__rellabel" }, heading),
    list,
  ]);
}

function renderRelationRow(r: Relation, isSub: boolean): HTMLElement {
  const tk = r.ticket;
  const main: HTMLElement[] = [];
  if (isSub) {
    // Sub-tickets render as a (read-only) checklist with state — a sub-ticket is "done"
    // when it reaches the Done column, not by ticking it here.
    main.push(
      el("input", {
        type: "checkbox",
        class: "tp__critbox",
        checked: tk.state === "done",
        disabled: true,
        title: "A sub-ticket completes when it reaches the Done column",
      }),
    );
  }
  main.push(
    el("span", { class: "tp__relid mono" }, tk.id),
    el("span", { class: "tp__reltitle" }, tk.title || "(untitled)"),
    statePill(tk.state),
  );

  const remove = el("button", {
    class: "icon-btn tp__reldel",
    title: "Remove link",
    "aria-label": "Remove link",
    onClick: (e: Event) => {
      e.stopPropagation();
      void doRemoveRelation(r);
    },
  }, "×");

  return el("li", { class: "tp__rel" }, [
    el("div", { class: "tp__relmain", title: `Open ${tk.id}`, onClick: () => void openTicket(tk.id) }, main),
    remove,
  ]);
}

function renderAddRelation(t: Ticket): HTMLElement {
  const typeSelect = el("select", { class: "select tp__reltype", "aria-label": "Relation type" });
  for (const o of LINK_TYPE_OPTIONS) typeSelect.append(el("option", { value: o.value }, o.label));

  const listId = "rel-ticket-options";
  const datalist = el("datalist", { id: listId });
  for (const s of state.tickets) {
    if (s.id !== t.id) datalist.append(el("option", { value: s.id }, `${s.id} — ${s.title}`));
  }
  const idInput = el("input", {
    class: "input tp__relinput",
    placeholder: "Ticket id (e.g. SOLO-2)…",
    list: listId,
    autocomplete: "off",
    maxlength: 64,
  });

  const add = async (): Promise<void> => {
    // Tolerate a pasted "SOLO-2 — title" by taking the leading id token.
    const other = idInput.value.trim().split(/[\s—]/)[0].trim();
    if (!other) {
      idInput.focus();
      return;
    }
    await doAddRelation(typeSelect.value as LinkType, other);
    idInput.value = "";
  };
  idInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      void add();
    }
  });
  const addBtn = el("button", { class: "btn btn--ghost btn--sm", onClick: () => void add() }, "Link");

  return el("div", { class: "tp__reladd" }, [typeSelect, datalist, idInput, addBtn]);
}

function renderDescription(t: Ticket): HTMLElement {
  return el("section", { class: "tp__section" }, [
    el("h3", { class: "tp__sectionhead" }, "Description"),
    el("div", { class: "tp__desc md", html: renderMarkdown(t.description) }),
  ]);
}

function renderEditForm(t: Ticket): HTMLElement {
  const titleInput = el("input", { class: "input", value: t.title || "", maxlength: 200, placeholder: "Title" });
  const descInput = el("textarea", { class: "textarea tp__descedit", rows: 14, placeholder: "Description (markdown)…" }, t.description || "");

  const save = el("button", { class: "btn btn--primary" }, "Save");
  const cancel = el("button", { class: "btn btn--ghost", onClick: () => { editing = false; renderPanel(); } }, "Cancel");
  save.addEventListener("click", () => {
    const title = titleInput.value.trim();
    if (!title) {
      toastError("Title is required.");
      titleInput.focus();
      return;
    }
    void doEdit({ title, description: descInput.value });
  });

  return el("div", { class: "tp__editform" }, [
    el("label", { class: "field" }, [el("span", { class: "field__label" }, "Title"), titleInput]),
    el("label", { class: "field" }, [el("span", { class: "field__label" }, "Description (markdown)"), descInput]),
    el("div", { class: "tp__editactions" }, [save, cancel]),
  ]);
}

function renderActivity(t: Ticket): HTMLElement {
  const items = (t.activity || []).slice().sort((a, b) => new Date(a.at).getTime() - new Date(b.at).getTime());
  const feed = el("div", { class: "feed" });
  if (!items.length) feed.append(el("div", { class: "muted" }, "No activity yet."));
  for (const a of items) feed.append(renderActivityItem(a));
  return el("section", { class: "tp__section" }, [
    el("h3", { class: "tp__sectionhead" }, `Activity (${items.length})`),
    feed,
  ]);
}

function renderActivityItem(a: Activity): HTMLElement {
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

function systemText(a: Activity): string {
  if (a.body) return a.body;
  switch (a.kind) {
    case "created": return "created this ticket";
    case "state_change": return "changed state";
    case "assignment": return "changed assignee";
    case "edit": return "edited the ticket";
    default: return a.kind;
  }
}

function renderComposer(): HTMLElement {
  const ta = el("textarea", { class: "textarea composer__input", rows: 2, placeholder: "Write a comment…  (Ctrl/⌘+Enter to send)" });
  const send = el("button", { class: "btn btn--primary" }, "Comment");

  const submit = async () => {
    const id = currentId;
    const body = ta.value.trim();
    if (!id || !body) {
      ta.focus();
      return;
    }
    send.disabled = true;
    try {
      await api.comment(id, body);
      ta.value = "";
      await reload();
      toastSuccess("Comment added");
    } catch (err) {
      toastError((err as Error).message || "Failed to add comment");
    } finally {
      send.disabled = false;
    }
  };

  send.addEventListener("click", () => void submit());
  ta.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      void submit();
    }
  });
  return el("div", { class: "composer" }, [ta, el("div", { class: "composer__actions" }, send)]);
}

// --- writes ---------------------------------------------------------------
async function reload(): Promise<void> {
  if (!currentId) return;
  try {
    ticket = await api.ticket(currentId);
    renderPanel();
  } catch (err) {
    showError(err as Error);
  }
  refreshTickets({ silent: true }).catch(() => {});
}

async function doAssign(assignee: string): Promise<void> {
  const id = currentId;
  if (!id || busy) return;
  busy = true;
  try {
    ticket = await api.assign(id, assignee);
    renderPanel();
    refreshTickets({ silent: true }).catch(() => {});
  } catch (err) {
    toastError((err as Error).message || "Assign failed");
    renderPanel(); // snap the <select> back to the truth
  } finally {
    busy = false;
  }
}

async function doMove(s: State): Promise<void> {
  const id = currentId;
  if (!id || busy) return;
  busy = true;
  try {
    ticket = await api.move(id, s);
    renderPanel();
    toastSuccess(`Moved to ${state.meta.state_labels[s] || s}`);
    refreshTickets({ silent: true }).catch(() => {});
  } catch (err) {
    toastError((err as Error).message || "Move failed");
  } finally {
    busy = false;
  }
}

async function doAddCriterion(text: string): Promise<void> {
  const id = currentId;
  if (!id || busy) return;
  busy = true;
  try {
    ticket = await api.addCriterion(id, text);
    renderPanel();
    refreshTickets({ silent: true }).catch(() => {});
  } catch (err) {
    toastError((err as Error).message || "Add failed");
  } finally {
    busy = false;
  }
}

async function doCheckCriterion(cid: string, done: boolean): Promise<void> {
  const id = currentId;
  if (!id || busy) return;
  busy = true;
  try {
    ticket = await api.updateCriterion(id, cid, { done });
    renderPanel();
    refreshTickets({ silent: true }).catch(() => {});
  } catch (err) {
    toastError((err as Error).message || "Update failed");
    renderPanel(); // snap the checkbox back to the truth
  } finally {
    busy = false;
  }
}

async function doRemoveCriterion(cid: string): Promise<void> {
  const id = currentId;
  if (!id || busy) return;
  busy = true;
  try {
    ticket = await api.removeCriterion(id, cid);
    renderPanel();
    refreshTickets({ silent: true }).catch(() => {});
  } catch (err) {
    toastError((err as Error).message || "Remove failed");
  } finally {
    busy = false;
  }
}

async function doAddRelation(type: LinkType, other: string): Promise<void> {
  const id = currentId;
  if (!id || busy) return;
  busy = true;
  try {
    ticket = await api.addLink(id, type, other);
    renderPanel();
    toastSuccess("Linked");
    refreshTickets({ silent: true }).catch(() => {});
  } catch (err) {
    toastError((err as Error).message || "Link failed");
  } finally {
    busy = false;
  }
}

async function doRemoveRelation(r: Relation): Promise<void> {
  const id = currentId;
  if (!id || busy) return;
  busy = true;
  try {
    // Pass the row's direction so removing one relation never deletes its mirror
    // (e.g. an "A blocks B" row must not also drop a separate "B blocks A").
    ticket = await api.removeLink(id, r.ticket.id, r.type, r.direction);
    renderPanel();
    refreshTickets({ silent: true }).catch(() => {});
  } catch (err) {
    toastError((err as Error).message || "Unlink failed");
  } finally {
    busy = false;
  }
}

async function doEdit({ title, description }: { title: string; description: string }): Promise<void> {
  const id = currentId;
  if (!id || busy) return;
  busy = true;
  try {
    ticket = await api.patchTicket(id, { title, description });
    editing = false;
    renderPanel();
    toastSuccess("Saved");
    refreshTickets({ silent: true }).catch(() => {});
  } catch (err) {
    toastError((err as Error).message || "Save failed");
  } finally {
    busy = false;
  }
}
