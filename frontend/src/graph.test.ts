// @vitest-environment happy-dom
// SOLO-14: the dependency-graph render pipeline (layout → SVG) is exercised headlessly,
// since it has no other unit coverage. We mock the API + the (circular) ticket import.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Graph } from "./types";

const { graphMock } = vi.hoisted(() => ({ graphMock: vi.fn() }));
vi.mock("./api", () => ({ api: { graph: graphMock } }));
vi.mock("./ticket", () => ({ openTicket: vi.fn() }));
vi.mock("./store", () => ({ state: { currentProject: "DEMO" } }));

import { openGraph, closeGraph, isGraphOpen } from "./graph";

const FIXTURE: Graph = {
  // 1→2→3→1 is a blocks cycle; 2—5 is a related edge.
  nodes: [
    { id: "DEMO-1", project: "DEMO", title: "Auth", state: "in-progress", assignee: "claude", blocked: true, subtickets: { done: 0, total: 0 } },
    { id: "DEMO-2", project: "DEMO", title: "Login", state: "backlog", assignee: "claude", blocked: true, subtickets: { done: 0, total: 0 } },
    { id: "DEMO-3", project: "DEMO", title: "Session", state: "in-progress", assignee: "unassigned", blocked: true, subtickets: { done: 0, total: 0 } },
    { id: "DEMO-5", project: "DEMO", title: "Docs", state: "done", assignee: "unassigned", blocked: false, subtickets: { done: 0, total: 0 } },
  ],
  edges: [
    { from: "DEMO-1", to: "DEMO-2", type: "blocks" },
    { from: "DEMO-2", to: "DEMO-3", type: "blocks" },
    { from: "DEMO-3", to: "DEMO-1", type: "blocks" },
    { from: "DEMO-2", to: "DEMO-5", type: "related" },
  ],
  cycles: [["DEMO-1", "DEMO-2", "DEMO-3"]],
  scope: { project: "DEMO", around: null, depth: null, active_only: false, types: ["blocks", "related", "duplicate", "parent"] },
  truncated: false,
};

beforeEach(() => {
  graphMock.mockReset();
  graphMock.mockResolvedValue(FIXTURE);
});

afterEach(() => {
  // The overlay is a module-level singleton (like the toast host) — close it but leave it
  // attached, so the next openGraph re-renders into the live node rather than a detached one.
  closeGraph();
});

describe("dependency graph render", () => {
  it("renders a node per ticket and a path per edge", async () => {
    await openGraph({ project: "DEMO" });
    expect(isGraphOpen()).toBe(true);
    expect(document.querySelectorAll(".graph__node").length).toBe(4);
    expect(document.querySelectorAll(".graph__edge").length).toBe(4);
  });

  it("flags blocks-cycle nodes and shows a warning banner", async () => {
    await openGraph({ project: "DEMO" });
    expect(document.querySelectorAll(".graph__node--cycle").length).toBe(3);
    const banner = document.querySelector(".graph__banner");
    expect(banner?.textContent).toContain("cycle");
  });

  it("dims done nodes", async () => {
    await openGraph({ project: "DEMO" });
    // DEMO-5 is done → carries the done state class (CSS dims it).
    expect(document.querySelectorAll(".graph__node--done").length).toBe(1);
  });

  it("hides edges of a type when its chip is toggled off (no refetch)", async () => {
    await openGraph({ project: "DEMO" });
    expect(document.querySelectorAll(".graph__edge--related").length).toBe(1);
    const chip = document.querySelector<HTMLButtonElement>('.chip[data-type="related"]');
    chip?.click();
    expect(document.querySelectorAll(".graph__edge--related").length).toBe(0);
    expect(document.querySelectorAll(".graph__edge--blocks").length).toBe(3); // others kept
    expect(graphMock).toHaveBeenCalledTimes(1); // type toggle is client-side only
  });

  it("shows an empty-state message for a graph with no nodes", async () => {
    graphMock.mockResolvedValue({ ...FIXTURE, nodes: [], edges: [], cycles: [] });
    await openGraph({ project: "DEMO" });
    expect(document.querySelector(".graph__center")?.textContent).toContain("No relationships");
  });

  it("draws arrowheads on directional edges (incl. duplicate) but not on related", async () => {
    graphMock.mockResolvedValue({
      ...FIXTURE,
      nodes: [FIXTURE.nodes[0], FIXTURE.nodes[1], FIXTURE.nodes[3]],
      edges: [
        { from: "DEMO-1", to: "DEMO-2", type: "blocks" },
        { from: "DEMO-1", to: "DEMO-5", type: "duplicate" },
        { from: "DEMO-2", to: "DEMO-5", type: "related" },
      ],
      cycles: [],
    });
    await openGraph({ project: "DEMO" });
    expect(document.querySelector(".graph__edge--blocks")?.getAttribute("marker-end")).toContain("arrow-blocks");
    expect(document.querySelector(".graph__edge--duplicate")?.getAttribute("marker-end")).toContain("arrow-duplicate");
    expect(document.querySelector(".graph__edge--related")?.getAttribute("marker-end")).toBeNull();
  });

  it("requests an ego-graph with depth when opened around a ticket", async () => {
    await openGraph({ around: "DEMO-1", depth: 2 });
    expect(graphMock).toHaveBeenCalledWith(expect.objectContaining({ around: "DEMO-1", depth: 2 }));
  });

  it("drops a stale response when a newer request supersedes it", async () => {
    let resolveStale!: (g: Graph) => void;
    const stale = new Promise<Graph>((r) => {
      resolveStale = r;
    });
    graphMock.mockReturnValueOnce(stale); // first request hangs
    graphMock.mockResolvedValueOnce({ ...FIXTURE, nodes: [FIXTURE.nodes[0]], edges: [], cycles: [] });
    const p1 = openGraph({ around: "DEMO-1" }); // in-flight (pending)
    const p2 = openGraph({ around: "DEMO-2" }); // supersedes, resolves now
    await p2;
    resolveStale(FIXTURE); // the older response lands last…
    await p1;
    expect(document.querySelectorAll(".graph__node").length).toBe(1); // …but is ignored
  });
});
