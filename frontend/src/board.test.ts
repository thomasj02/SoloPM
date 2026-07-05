// @vitest-environment happy-dom
// SOLO-21: tags render as chips on the board card.
// SOLO-24: the poll-driven re-render must not reset column scroll positions.
import { beforeEach, describe, expect, it } from "vitest";
import { initBoard, tagChips } from "./board";
import { emit, state } from "./store";
import type { State, TicketSummary } from "./types";

describe("tagChips (card display)", () => {
  it("renders one .tag-chip per tag inside a .card__tags row", () => {
    const node = tagChips(["bug", "frontend"]);
    expect(node).not.toBeNull();
    expect(node!.classList.contains("card__tags")).toBe(true);
    const chips = node!.querySelectorAll(".tag-chip");
    expect(chips.length).toBe(2);
    expect([...chips].map((c) => c.textContent)).toEqual(["bug", "frontend"]);
  });

  it("returns null when there are no tags (so the card shows no empty row)", () => {
    expect(tagChips([])).toBeNull();
    expect(tagChips(undefined)).toBeNull();
  });
});

function summary(id: string, st: State, extra: Partial<TicketSummary> = {}): TicketSummary {
  return {
    id,
    project: "SOLO",
    title: `Ticket ${id}`,
    state: st,
    assignee: "unassigned",
    branch: null,
    session_active: false,
    pr: null,
    acceptance: { done: 0, total: 0 },
    tags: [],
    comment_count: 0,
    blocked: false,
    subtickets: { done: 0, total: 0 },
    state_entered_at: "2026-07-01T00:00:00Z",
    time_in_state_seconds: 300,
    created_at: "2026-07-01T00:00:00Z",
    updated_at: "2026-07-01T00:00:00Z",
    ...extra,
  };
}

describe("board render (SOLO-24: poll re-render must not reset scroll)", () => {
  let root: HTMLElement;

  beforeEach(() => {
    document.body.innerHTML = "";
    root = document.createElement("div");
    document.body.append(root);
    state.currentProject = "SOLO";
    state.filter = "";
    state.ticketsError = null;
    state.tickets = [summary("SOLO-1", "backlog"), summary("SOLO-2", "backlog")];
    initBoard(root);
    emit("tickets");
  });

  const backlogList = () => root.querySelector<HTMLElement>('.col[data-state="backlog"] .col__list')!;

  it("preserves a column's scroll position when a re-render changes the data", () => {
    backlogList().scrollTop = 140;
    state.tickets = [...state.tickets, summary("SOLO-3", "backlog")];
    emit("tickets");
    expect(root.querySelectorAll(".card").length).toBe(3); // the board did rebuild…
    expect(backlogList().scrollTop).toBe(140); //             …but kept the scroll offset
  });

  it("skips the DOM rebuild when nothing rendered has changed", () => {
    const card = root.querySelector(".card");
    expect(card).not.toBeNull();
    emit("tickets"); // a poll tick with identical data
    expect(root.querySelector(".card")).toBe(card); // same node — the board was not torn down
  });

  it("treats sub-badge age drift as unchanged (the server recomputes it every poll)", () => {
    const card = root.querySelector(".card");
    expect(card).not.toBeNull(); // guard: a skipped *initial* render would make this vacuous
    // 300s → 304s: the raw field changed, but the rendered "5m" badge did not.
    state.tickets = state.tickets.map((t) => ({ ...t, time_in_state_seconds: (t.time_in_state_seconds ?? 0) + 4 }));
    emit("tickets");
    expect(root.querySelector(".card")).toBe(card);
  });

  it("still rebuilds when a rendered field changes", () => {
    state.tickets = state.tickets.map((t) => (t.id === "SOLO-1" ? { ...t, title: "Renamed" } : t));
    emit("tickets");
    expect(root.querySelector<HTMLElement>('.card[data-id="SOLO-1"] .card__title')!.textContent).toBe("Renamed");
  });
});
