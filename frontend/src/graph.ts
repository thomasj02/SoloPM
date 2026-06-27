// graph.ts — SOLO-14 dependency-graph view: a zero-dependency, hand-rolled SVG render of
// ticket relationships. A layered (longest-path) layout for the blocks/parent DAG, with
// related/duplicate drawn as lighter edges; pan/zoom; click a node to open its ticket;
// per-type and active-only filters; blocks-cycle flagging.

import { state } from "./store";
import { api } from "./api";
import { el, clearChildren } from "./util";
import { openTicket } from "./ticket";
import type { Graph, GraphEdge, GraphNode, GraphQuery, LinkType } from "./types";

const SVG_NS = "http://www.w3.org/2000/svg";
const NODE_W = 176;
const NODE_H = 58;
const GAP_X = 80;
const GAP_Y = 26;
const PAD = 60;
const ALL_TYPES: LinkType[] = ["blocks", "parent", "related", "duplicate"];
// Edges that drive the layered layout (the dependency hierarchy).
const DIRECTED: ReadonlySet<LinkType> = new Set<LinkType>(["blocks", "parent"]);
// Edges drawn with an arrowhead (all canonical-directional types). ``duplicate`` is
// directional (duplicate→canonical) so it gets an arrow, but it does NOT shape the layout.
const ARROWED: ReadonlySet<LinkType> = new Set<LinkType>(["blocks", "parent", "duplicate"]);
const TYPE_LABEL: Record<LinkType, string> = {
  blocks: "Blocks",
  parent: "Parent",
  related: "Related",
  duplicate: "Duplicate",
};

let overlay: HTMLElement | null = null;
let svg: SVGSVGElement | null = null;
let viewport: SVGGElement | null = null;
let titleEl: HTMLElement | null = null;
let bodyEl: HTMLElement | null = null;
let currentQuery: GraphQuery = {};
let data: Graph | null = null;
let activeTypes = new Set<LinkType>(ALL_TYPES);
let activeOnly = false;
let depth = 2;
let isOpen = false;
let reloadToken = 0; // bumped per fetch; a stale (superseded) response is dropped
const vb = { x: 0, y: 0, w: 1200, h: 800 };

/** True while the graph overlay is open (used to gate shortcuts / Esc routing). */
export function isGraphOpen(): boolean {
  return isOpen;
}

function svgNode<K extends keyof SVGElementTagNameMap>(
  tag: K,
  attrs: Record<string, string | number> = {},
  children: (Node | string)[] = [],
): SVGElementTagNameMap[K] {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
  for (const c of children) node.append(typeof c === "string" ? document.createTextNode(c) : c);
  return node;
}

// --- open / close ---------------------------------------------------------
export async function openGraph(query: GraphQuery = {}): Promise<void> {
  currentQuery = { ...query };
  activeOnly = !!query.active_only;
  activeTypes = new Set(query.types && query.types.length ? query.types : ALL_TYPES);
  depth = query.depth ?? 2;
  ensureOverlay();
  isOpen = true;
  overlay?.classList.add("graph-overlay--open");
  await reload();
}

export function closeGraph(): void {
  if (!isOpen) return;
  isOpen = false;
  overlay?.classList.remove("graph-overlay--open");
}

function ensureOverlay(): void {
  if (overlay) return;
  titleEl = el("span", { class: "graph__title" });
  bodyEl = el("div", { class: "graph__body" });

  const typeChips = ALL_TYPES.map((t) =>
    el(
      "button",
      {
        class: `chip chip--${t}`,
        type: "button",
        dataset: { type: t },
        onClick: (e: Event) => toggleType(t, e.currentTarget as HTMLElement),
      },
      TYPE_LABEL[t],
    ),
  );
  const activeChip = el(
    "button",
    {
      class: "chip chip--active-only",
      type: "button",
      title: "Hide done/cancelled tickets",
      onClick: (e: Event) => toggleActiveOnly(e.currentTarget as HTMLElement),
    },
    "Active only",
  );
  const fit = el("button", { class: "btn btn--ghost btn--sm", type: "button", onClick: fitView }, "Fit");
  const close = el(
    "button",
    { class: "icon-btn graph__close", title: "Close (Esc)", "aria-label": "Close", onClick: closeGraph },
    "×",
  );

  const header = el("header", { class: "graph__head" }, [
    titleEl,
    el("div", { class: "graph__filters" }, [...typeChips, activeChip]),
    el("div", { class: "graph__spacer" }),
    fit,
    close,
  ]);

  overlay = el("div", { class: "graph-overlay", role: "dialog", "aria-label": "Dependency graph" }, [
    el("div", { class: "graph" }, [header, bodyEl]),
  ]);
  document.body.append(overlay);
  syncChips();

  // Own Esc handling (capture, so the ticket panel underneath doesn't also close).
  document.addEventListener(
    "keydown",
    (e: KeyboardEvent) => {
      if (isOpen && e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        closeGraph();
      }
    },
    true,
  );
}

function syncChips(): void {
  overlay?.querySelectorAll<HTMLElement>(".chip[data-type]").forEach((chip) => {
    const t = chip.dataset.type as LinkType;
    chip.classList.toggle("chip--off", !activeTypes.has(t));
  });
  overlay?.querySelector(".chip--active-only")?.classList.toggle("chip--on", activeOnly);
}

function toggleType(t: LinkType, chip: HTMLElement): void {
  if (activeTypes.has(t)) activeTypes.delete(t);
  else activeTypes.add(t);
  chip.classList.toggle("chip--off", !activeTypes.has(t));
  render(); // client-side edge filter — no refetch
}

function toggleActiveOnly(chip: HTMLElement): void {
  activeOnly = !activeOnly;
  chip.classList.toggle("chip--on", activeOnly);
  void reload(); // changes the node set → refetch
}

// --- data -----------------------------------------------------------------
async function reload(): Promise<void> {
  if (!bodyEl) return;
  clearChildren(bodyEl);
  bodyEl.append(el("div", { class: "graph__center muted" }, "Loading…"));
  // Fetch the full-type graph for the scope; type chips filter client-side for snappiness.
  const q: GraphQuery = { active_only: activeOnly };
  if (currentQuery.around) {
    q.around = currentQuery.around;
    q.depth = depth;
  } else if (currentQuery.project) {
    q.project = currentQuery.project;
  } else if (state.currentProject) {
    q.project = state.currentProject;
  }
  const token = ++reloadToken;
  try {
    const result = await api.graph(q);
    if (token !== reloadToken) return; // a newer open/reload superseded this request
    data = result;
    render();
  } catch (err) {
    if (token !== reloadToken || !bodyEl) return; // don't clobber a newer view with a stale error
    clearChildren(bodyEl);
    bodyEl.append(el("div", { class: "graph__center tp__error" }, (err as Error).message || "Failed to load graph."));
  }
}

// --- layout ---------------------------------------------------------------
interface Placed {
  node: GraphNode;
  x: number;
  y: number;
}

/** Longest-path layering over the *visible* directed (blocks/parent) edges, breaking cycles
 * so a blocks loop can't wedge the layout. Returns node id -> position. */
function layout(nodes: GraphNode[], directedEdges: GraphEdge[]): Map<string, Placed> {
  const ids = new Set(nodes.map((n) => n.id));
  const out = new Map<string, string[]>();
  const indeg = new Map<string, number>();
  for (const n of nodes) {
    out.set(n.id, []);
    indeg.set(n.id, 0);
  }

  // Drop back-edges (DFS) to get a DAG for layering.
  const color = new Map<string, number>(); // 0 unseen, 1 on-stack, 2 done
  const forward: Array<[string, string]> = [];
  const adj = new Map<string, string[]>();
  for (const n of nodes) adj.set(n.id, []);
  for (const e of directedEdges) {
    if (ids.has(e.from) && ids.has(e.to)) adj.get(e.from)!.push(e.to);
  }
  const visit = (u: string): void => {
    color.set(u, 1);
    for (const v of adj.get(u) ?? []) {
      const c = color.get(v) ?? 0;
      if (c === 1) continue; // back-edge → skip (would be a cycle)
      forward.push([u, v]);
      if (c === 0) visit(v);
    }
    color.set(u, 2);
  };
  for (const n of nodes) if ((color.get(n.id) ?? 0) === 0) visit(n.id);

  for (const [u, v] of forward) {
    out.get(u)!.push(v);
    indeg.set(v, (indeg.get(v) ?? 0) + 1);
  }

  // Kahn topological order over forward edges, then longest-path layer assignment.
  const layer = new Map<string, number>(nodes.map((n) => [n.id, 0]));
  const queue = nodes.filter((n) => (indeg.get(n.id) ?? 0) === 0).map((n) => n.id);
  const deg = new Map(indeg);
  while (queue.length) {
    const u = queue.shift() as string;
    for (const v of out.get(u) ?? []) {
      layer.set(v, Math.max(layer.get(v) ?? 0, (layer.get(u) ?? 0) + 1));
      deg.set(v, (deg.get(v) ?? 0) - 1);
      if ((deg.get(v) ?? 0) === 0) queue.push(v);
    }
  }

  // Bucket by layer, order within a layer by id for stability.
  const byLayer = new Map<number, GraphNode[]>();
  for (const n of nodes) {
    const l = layer.get(n.id) ?? 0;
    (byLayer.get(l) ?? byLayer.set(l, []).get(l)!).push(n);
  }
  const placed = new Map<string, Placed>();
  for (const [l, group] of [...byLayer.entries()].sort((a, b) => a[0] - b[0])) {
    group.sort((a, b) => a.id.localeCompare(b.id, undefined, { numeric: true }));
    group.forEach((n, i) => {
      placed.set(n.id, { node: n, x: PAD + l * (NODE_W + GAP_X), y: PAD + i * (NODE_H + GAP_Y) });
    });
  }
  return placed;
}

// --- render ---------------------------------------------------------------
function render(): void {
  if (!bodyEl || !data) return;
  syncChips();
  if (titleEl) titleEl.textContent = scopeLabel(data);
  clearChildren(bodyEl);

  if (!data.nodes.length) {
    bodyEl.append(el("div", { class: "graph__center muted" }, "No relationships to show."));
    return;
  }

  const visibleEdges = data.edges.filter((e) => activeTypes.has(e.type));
  const directed = visibleEdges.filter((e) => DIRECTED.has(e.type));
  const placed = layout(data.nodes, directed);

  let maxX = 0;
  let maxY = 0;
  for (const p of placed.values()) {
    maxX = Math.max(maxX, p.x + NODE_W);
    maxY = Math.max(maxY, p.y + NODE_H);
  }

  svg = svgNode("svg", {
    class: "graph__svg",
    width: "100%",
    height: "100%",
    preserveAspectRatio: "xMidYMid meet",
  });
  svg.append(arrowDefs());
  viewport = svgNode("g", { class: "graph__viewport" });
  svg.append(viewport);

  const cycleNodes = new Set<string>(data.cycles.flat());
  const edgeLayer = svgNode("g", { class: "graph__edges" });
  for (const e of visibleEdges) edgeLayer.append(renderEdge(e, placed, cycleNodes));
  viewport.append(edgeLayer);

  const nodeLayer = svgNode("g", { class: "graph__nodes" });
  for (const p of placed.values()) nodeLayer.append(renderNode(p, cycleNodes.has(p.node.id)));
  viewport.append(nodeLayer);

  bodyEl.append(svg);
  if (data.cycles.length || data.truncated) bodyEl.append(banner(data));

  vb.x = 0;
  vb.y = 0;
  vb.w = Math.max(maxX + PAD, 400);
  vb.h = Math.max(maxY + PAD, 300);
  applyViewBox();
  wirePanZoom();
}

function scopeLabel(g: Graph): string {
  const s = g.scope;
  const what = s.around
    ? `around ${s.around} · depth ${s.depth}`
    : s.project
      ? `project ${s.project}`
      : "all projects";
  return `Dependency graph — ${what} · ${g.nodes.length} nodes · ${g.edges.length} edges`;
}

function arrowDefs(): SVGDefsElement {
  const defs = svgNode("defs");
  for (const t of ["blocks", "parent", "duplicate"]) {
    const marker = svgNode("marker", {
      id: `arrow-${t}`,
      class: `graph__arrow graph__arrow--${t}`,
      viewBox: "0 0 10 10",
      refX: 9,
      refY: 5,
      markerWidth: 7,
      markerHeight: 7,
      orient: "auto-start-reverse",
    });
    marker.append(svgNode("path", { d: "M0,0 L10,5 L0,10 z" }));
    defs.append(marker);
  }
  return defs;
}

function renderEdge(e: GraphEdge, placed: Map<string, Placed>, cycleNodes: Set<string>): SVGElement {
  const a = placed.get(e.from);
  const b = placed.get(e.to);
  if (!a || !b) return svgNode("g");
  const rightward = b.x >= a.x;
  const sx = a.x + (rightward ? NODE_W : 0);
  const sy = a.y + NODE_H / 2;
  const tx = b.x + (rightward ? 0 : NODE_W);
  const ty = b.y + NODE_H / 2;
  const dx = Math.max(30, Math.abs(tx - sx) / 2);
  const c1x = sx + (rightward ? dx : -dx);
  const c2x = tx + (rightward ? -dx : dx);
  const inCycle = e.type === "blocks" && cycleNodes.has(e.from) && cycleNodes.has(e.to);
  const attrs: Record<string, string | number> = {
    class: `graph__edge graph__edge--${e.type}${inCycle ? " graph__edge--cycle" : ""}`,
    d: `M${sx},${sy} C${c1x},${sy} ${c2x},${ty} ${tx},${ty}`,
    fill: "none",
  };
  if (ARROWED.has(e.type)) attrs["marker-end"] = `url(#arrow-${e.type})`;
  const title = svgNode("title", {}, [`${e.from} ${e.type} ${e.to}`]);
  const path = svgNode("path", attrs, [title]);
  return path;
}

function renderNode(p: Placed, inCycle: boolean): SVGGElement {
  const n = p.node;
  const cls =
    `graph__node graph__node--${n.state}` +
    (n.blocked ? " graph__node--blocked" : "") +
    (inCycle ? " graph__node--cycle" : "");
  const g = svgNode("g", { class: cls, transform: `translate(${p.x},${p.y})`, tabindex: 0, role: "button" });
  g.append(svgNode("rect", { class: "graph__nodebox", width: NODE_W, height: NODE_H, rx: 9 }));
  g.append(svgNode("rect", { class: "graph__nodestripe", width: 5, height: NODE_H, rx: 0 }));
  g.append(svgNode("text", { class: "graph__nodeid", x: 14, y: 20 }, [n.id]));
  g.append(svgNode("text", { class: "graph__nodetitle", x: 14, y: 40 }, [truncate(n.title, 22)]));
  // Right-aligned chips: blocked flag and sub-ticket rollup.
  const marks: string[] = [];
  if (n.subtickets.total) marks.push(`▣ ${n.subtickets.done}/${n.subtickets.total}`);
  if (n.blocked) marks.push("⛔");
  if (marks.length) {
    g.append(svgNode("text", { class: "graph__nodemark", x: NODE_W - 12, y: 20, "text-anchor": "end" }, [marks.join("  ")]));
  }
  const open = (): void => {
    closeGraph();
    void openTicket(n.id);
  };
  g.addEventListener("click", open);
  g.addEventListener("keydown", (e: KeyboardEvent) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      open();
    }
  });
  g.append(svgNode("title", {}, [`${n.id} · ${n.title} · ${n.state}${n.blocked ? " · blocked" : ""}`]));
  return g;
}

function banner(g: Graph): HTMLElement {
  const parts: string[] = [];
  for (const cyc of g.cycles) parts.push(`⚠ blocks cycle: ${cyc.join(" → ")} → ${cyc[0]}`);
  if (g.truncated) parts.push("⚠ graph truncated (node cap reached)");
  return el("div", { class: "graph__banner" }, parts.map((p) => el("div", {}, p)));
}

function truncate(s: string, n: number): string {
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

// --- pan / zoom -----------------------------------------------------------
function applyViewBox(): void {
  svg?.setAttribute("viewBox", `${vb.x} ${vb.y} ${vb.w} ${vb.h}`);
}

function fitView(): void {
  if (data) render(); // recompute layout + reset viewBox to content bounds
}

function wirePanZoom(): void {
  if (!svg) return;
  const elSvg = svg;
  elSvg.addEventListener("wheel", (e: WheelEvent) => {
    e.preventDefault();
    const rect = elSvg.getBoundingClientRect();
    const ax = vb.x + ((e.clientX - rect.left) / rect.width) * vb.w;
    const ay = vb.y + ((e.clientY - rect.top) / rect.height) * vb.h;
    const factor = e.deltaY > 0 ? 1.12 : 0.89;
    const nw = Math.min(20000, Math.max(150, vb.w * factor));
    const nh = Math.min(20000, Math.max(100, vb.h * factor));
    vb.x = ax - ((ax - vb.x) * nw) / vb.w;
    vb.y = ay - ((ay - vb.y) * nh) / vb.h;
    vb.w = nw;
    vb.h = nh;
    applyViewBox();
  });

  let panning = false;
  let startX = 0;
  let startY = 0;
  let startVbX = 0;
  let startVbY = 0;
  elSvg.addEventListener("pointerdown", (e: PointerEvent) => {
    // Drag the background to pan; let node clicks through.
    if ((e.target as Element).closest(".graph__node")) return;
    panning = true;
    startX = e.clientX;
    startY = e.clientY;
    startVbX = vb.x;
    startVbY = vb.y;
    elSvg.setPointerCapture(e.pointerId);
    elSvg.classList.add("graph__svg--panning");
  });
  elSvg.addEventListener("pointermove", (e: PointerEvent) => {
    if (!panning) return;
    const rect = elSvg.getBoundingClientRect();
    vb.x = startVbX - ((e.clientX - startX) / rect.width) * vb.w;
    vb.y = startVbY - ((e.clientY - startY) / rect.height) * vb.h;
    applyViewBox();
  });
  const endPan = (e: PointerEvent): void => {
    panning = false;
    elSvg.classList.remove("graph__svg--panning");
    try {
      elSvg.releasePointerCapture(e.pointerId);
    } catch {
      /* pointer already released */
    }
  };
  elSvg.addEventListener("pointerup", endPan);
  elSvg.addEventListener("pointercancel", endPan);
}
