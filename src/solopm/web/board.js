// board.js — the Kanban board: one column per state, cards, and drag-and-drop.
// Moves are optimistic; on a 4xx the card snaps back and a toast shows the error.

import { state, on, refreshTickets } from "./store.js";
import { api } from "./api.js";
import { el, clearChildren } from "./util.js";
import { toastError, assigneeBadge } from "./ui.js";
import { openTicket } from "./ticket.js";

let boardEl = null;
let dragging = null; // { id, from } while a card is being dragged
let movePending = false; // true while an optimistic move POST is in flight

export function initBoard(root) {
  boardEl = el("div", { class: "board", id: "board" });
  root.append(boardEl);
  // Re-render on any data/enum/filter change.
  on("tickets", render);
  on("meta", render);
  on("filter", render);
  on("projects", render);
}

/** True while a card is mid-drag OR an optimistic move is in flight (pauses polling). */
export function isDragging() {
  return !!dragging || movePending;
}

function matchesFilter(t, q) {
  if (!q) return true;
  const needle = q.toLowerCase();
  return t.id.toLowerCase().includes(needle) || (t.title || "").toLowerCase().includes(needle);
}

function render() {
  if (!boardEl) return;
  clearChildren(boardEl);
  if (!state.currentProject) return; // onboarding overlay handles the empty case

  if (state.ticketsError) {
    boardEl.append(
      el("div", { class: "board__error" }, [
        el("p", {}, state.ticketsError.message || "Couldn't load tickets."),
        el("button", { class: "btn btn--ghost", onClick: () => refreshTickets().catch(() => {}) }, "Retry"),
      ]),
    );
    return;
  }

  // Bucket tickets by state.
  const buckets = new Map(state.meta.states.map((s) => [s, []]));
  for (const t of state.tickets) {
    if (!buckets.has(t.state)) buckets.set(t.state, []);
    buckets.get(t.state).push(t);
  }

  for (const s of state.meta.states) {
    boardEl.append(renderColumn(s, buckets.get(s) || []));
  }
}

function renderColumn(stateId, items) {
  const label = state.meta.state_labels[stateId] || stateId;
  const terminal = stateId === "done" || stateId === "cancelled";
  const visible = items.filter((t) => matchesFilter(t, state.filter));
  const count = state.filter ? visible.length : items.length;

  const list = el("div", { class: "col__list" });
  if (!visible.length) {
    list.append(el("div", { class: "col__empty" }, state.filter ? "No matches" : "Drop here"));
  } else {
    for (const t of visible) list.append(renderCard(t));
  }

  const col = el(
    "div",
    { class: `col${terminal ? " col--muted" : ""}`, dataset: { state: stateId } },
    [
      el("div", { class: "col__head" }, [
        el("span", { class: `col__dot col__dot--${stateId}` }),
        el("span", { class: "col__label" }, label),
        el("span", { class: "col__count" }, String(count)),
      ]),
      list,
    ],
  );

  // Drop wiring lives on the whole column so the entire area is a target.
  col.addEventListener("dragover", (e) => onDragOver(e, stateId, col));
  col.addEventListener("dragleave", (e) => onDragLeave(e, col));
  col.addEventListener("drop", (e) => onDrop(e, stateId, col));
  return col;
}

function renderCard(t) {
  const card = el("div", {
    class: "card",
    draggable: true,
    tabindex: "0",
    role: "button",
    dataset: { id: t.id },
  });

  card.append(
    el("div", { class: "card__top" }, [
      el("span", { class: "card__id mono" }, t.id),
      t.session_active ? el("span", { class: "card__live", title: "Active agent session" }) : null,
    ]),
    el("div", { class: "card__title" }, t.title || "(untitled)"),
    el("div", { class: "card__meta" }, [
      assigneeBadge(t.assignee),
      t.pr ? el("span", { class: `card__pr card__pr--${t.pr.state}`, title: `PR #${t.pr.number} · ${t.pr.state}` }, `#${t.pr.number}`) : null,
      t.comment_count ? el("span", { class: "card__comments", title: `${t.comment_count} comment(s)` }, `${t.comment_count}`) : null,
    ]),
  );

  card.addEventListener("click", () => openTicket(t.id));
  card.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      openTicket(t.id);
    }
  });
  card.addEventListener("dragstart", (e) => onDragStart(e, t, card));
  card.addEventListener("dragend", () => onDragEnd(card));
  return card;
}

// --- drag and drop --------------------------------------------------------
function onDragStart(e, t, card) {
  dragging = { id: t.id, from: t.state };
  card.classList.add("card--dragging");
  e.dataTransfer.effectAllowed = "move";
  e.dataTransfer.setData("text/plain", t.id);

  // Light up legal targets, dim illegal ones, mark the source.
  const legal = new Set(state.meta.transitions[t.state] || []);
  boardEl.classList.add("board--dragging");
  boardEl.querySelectorAll(".col").forEach((col) => {
    const s = col.dataset.state;
    if (s === t.state) col.classList.add("col--source");
    else col.classList.add(legal.has(s) ? "col--legal" : "col--illegal");
  });
}

function onDragEnd(card) {
  card.classList.remove("card--dragging");
  boardEl.classList.remove("board--dragging");
  boardEl
    .querySelectorAll(".col")
    .forEach((col) => col.classList.remove("col--legal", "col--illegal", "col--source", "col--over"));
  removeDropLine();
  dragging = null;
}

// A cross-column move (changes state). The source column is NOT a legal cross target —
// dropping there is a reorder, handled separately.
function isLegalCross(stateId) {
  if (!dragging || stateId === dragging.from) return false;
  return (state.meta.transitions[dragging.from] || []).includes(stateId);
}

// --- within-column drop position -----------------------------------------
function cardsIn(col) {
  return [...col.querySelectorAll(".card:not(.card--dragging)")];
}

/** Id of the card the dragged card should sit AFTER, given the cursor Y (null = top). */
function dropAfterId(col, clientY) {
  let afterId = null;
  for (const card of cardsIn(col)) {
    const r = card.getBoundingClientRect();
    if (clientY > r.top + r.height / 2) afterId = card.dataset.id;
    else break;
  }
  return afterId;
}

let dropLine = null;
function showDropLine(col, afterId) {
  if (!dropLine) dropLine = el("div", { class: "drop-line", "aria-hidden": "true" });
  const list = col.querySelector(".col__list");
  if (!list) return;
  if (afterId == null) {
    list.prepend(dropLine);
  } else {
    const card = list.querySelector(`.card[data-id="${CSS.escape(afterId)}"]`);
    if (card) card.after(dropLine);
    else list.append(dropLine);
  }
}

function removeDropLine() {
  dropLine?.remove();
}

// A drop is valid on its own column (reorder) or any legal cross-column target (move).
function isDropTarget(stateId) {
  return stateId === dragging?.from || isLegalCross(stateId);
}

function onDragOver(e, stateId, col) {
  if (!dragging || !isDropTarget(stateId)) return; // illegal: browser disallows drop
  e.preventDefault();
  e.dataTransfer.dropEffect = "move";
  col.classList.add("col--over");
  showDropLine(col, dropAfterId(col, e.clientY)); // line shows the exact landing spot
}

function onDragLeave(e, col) {
  if (!col.contains(e.relatedTarget)) {
    col.classList.remove("col--over");
    removeDropLine();
  }
}

async function onDrop(e, stateId, col) {
  if (!dragging || !isDropTarget(stateId)) return;
  e.preventDefault();
  col.classList.remove("col--over");
  const afterId = dropAfterId(col, e.clientY);
  removeDropLine();
  await doDrop(dragging.id, dragging.from, stateId, afterId);
}

/**
 * Unified optimistic drop: places the card at the exact drop spot (and changes state
 * when crossing columns), then persists — reorder for same-column, move for cross-column.
 * Reverts the whole board order (and the state) on failure.
 */
async function doDrop(id, fromState, toState, afterId) {
  const moving = state.tickets.find((t) => t.id === id);
  if (!moving) {
    dragging = null;
    return;
  }
  const prevOrder = state.tickets.slice();
  const prevState = moving.state;

  // Optimistic: change state (if crossing) and splice into the drop position. The board
  // buckets by state in flat-list order, so the card lands exactly where it was dropped.
  moving.state = toState;
  const without = state.tickets.filter((t) => t.id !== id);
  let idx;
  if (afterId == null) {
    idx = without.findIndex((t) => t.state === toState);
    if (idx === -1) idx = without.length;
  } else {
    idx = without.findIndex((t) => t.id === afterId) + 1;
  }
  without.splice(idx, 0, moving);
  state.tickets = without;
  dragging = null;
  movePending = true; // keep polling paused for the whole in-flight POST
  render();

  try {
    if (toState === fromState) await api.reorder(id, afterId);
    else await api.move(id, toState, afterId);
    refreshTickets({ silent: true }).catch(() => {});
  } catch (err) {
    state.tickets = prevOrder; // revert order
    moving.state = prevState; //  and state
    render();
    toastError(err.message || "Move failed");
  } finally {
    movePending = false;
  }
}
