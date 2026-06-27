// main.ts — bootstrap + orchestration: builds the shell and top bar, wires the
// project/ticket creation modals, keyboard shortcuts, polling, and onboarding.

import "./styles.css";

import {
  state,
  on,
  loadMeta,
  loadProjects,
  setProject,
  refreshTickets,
  setFilter,
} from "./store";
import { api, ApiError } from "./api";
import { el, clearChildren } from "./util";
import { openModal, closeTopModal, isOverlayOpen, toast, toastError, toastSuccess } from "./ui";
import { initBoard, isDragging } from "./board";
import { closeTicket, isTicketOpen, openTicket, pollOpenTicket } from "./ticket";
import type { Assignee, Project, ReviewMemoryItem, State } from "./types";

const POLL_MS = 4000;
let searchInput: HTMLInputElement | null = null;
let radarBadge: HTMLButtonElement | null = null;
let statusStrip: HTMLElement | null = null;
let pollTimer: ReturnType<typeof setInterval> | null = null;

async function main(): Promise<void> {
  buildShell();
  const boardRoot = document.getElementById("board-root");
  if (boardRoot) initBoard(boardRoot);
  wireShortcuts();
  on("projects", renderTopbar);
  on("radar", updateRadarBadge); // update the overlap badge in place (no topbar rebuild)
  on("status", updateStatusStrip); // update the git/PR status strip in place

  await loadMeta();
  try {
    await loadProjects();
  } catch (err) {
    state.backendDown = true;
    showBackendDown(err as Error);
  }
  renderTopbar();

  if (state.currentProject) {
    try {
      await refreshTickets();
    } catch (err) {
      toastError((err as Error).message || "Couldn't load tickets.");
    }
  }
  startPolling();
}

// --- shell ----------------------------------------------------------------
function buildShell(): void {
  const app = document.getElementById("app");
  if (!app) return;
  const topbar = el("header", { class: "topbar", id: "topbar" });
  const board = el("div", { id: "board-root", class: "board-root" });
  app.append(topbar, el("main", { class: "main" }, board));
}

function renderTopbar(): void {
  const bar = document.getElementById("topbar");
  if (!bar) return;
  clearChildren(bar);

  const brand = el("div", { class: "brand" }, [
    el("span", { class: "brand__logo", "aria-hidden": "true" }, "◧"),
    el("span", { class: "brand__name" }, "SoloPM"),
  ]);

  // Project selector (the "+ New project" sentinel opens the modal).
  const select = el("select", {
    class: "select project-select",
    title: "Project",
    onChange: (e: Event) => {
      const value = (e.target as HTMLSelectElement).value;
      if (value === "__new__") {
        (e.target as HTMLSelectElement).value = state.currentProject || "";
        openNewProject();
        return;
      }
      setProject(value);
    },
  });
  if (!state.projects.length) {
    select.append(el("option", { value: "" }, "No projects"));
    select.disabled = true;
  } else {
    for (const p of state.projects) {
      select.append(el("option", { value: p.key, selected: p.key === state.currentProject }, `${p.key} · ${p.name}`));
    }
    select.append(el("option", { value: "__new__" }, "＋ New project…"));
  }

  const newProjectBtn = el("button", { class: "btn btn--ghost", title: "New project", onClick: openNewProject }, "+ Project");
  const settingsBtn = el("button", {
    class: "btn btn--ghost icon-btn",
    title: "Project settings",
    "aria-label": "Project settings",
    onClick: () => void openProjectSettings(),
    disabled: !state.currentProject,
  }, "⚙");

  searchInput = el("input", {
    class: "input search",
    type: "search",
    placeholder: "Filter cards…  ( / )",
    value: state.filter,
    "aria-label": "Filter cards",
    onInput: (e: Event) => setFilter((e.target as HTMLInputElement).value),
  });

  const refreshBtn = el("button", { class: "btn btn--ghost icon-btn", title: "Refresh (r)", "aria-label": "Refresh", onClick: () => void manualRefresh() }, "⟳");
  const newTicketBtn = el("button", { class: "btn btn--primary", title: "New ticket (c)", onClick: openCreateTicket, disabled: !state.currentProject }, "+ New ticket");

  radarBadge = el("button", {
    class: "btn radar-badge",
    "aria-label": "Overlap radar",
    onClick: () => {
      const first = state.radar[0];
      const tid = first?.a.ticket || first?.b.ticket;
      if (tid) void openTicket(tid);
    },
  });
  updateRadarBadge();

  // SOLO-12: a small git/PR health strip (open PRs + unpushed commits), refreshed by the
  // existing poll. Built here, content filled (and shown/hidden) by updateStatusStrip.
  statusStrip = el("div", { class: "board-status", "aria-label": "Project git status" });
  updateStatusStrip();

  bar.append(
    el("div", { class: "topbar__left" }, [
      brand,
      el("span", { class: "topbar__sep" }),
      el("div", { class: "selectwrap" }, select),
      settingsBtn,
      newProjectBtn,
      statusStrip,
    ]),
    el("div", { class: "topbar__right" }, [radarBadge, searchInput, refreshBtn, newTicketBtn]),
  );

  renderEmptyState();
}

/** Update the overlap badge from state.radar without rebuilding the whole topbar. */
function updateRadarBadge(): void {
  if (!radarBadge) return;
  const overlaps = state.radar;
  if (!overlaps.length) {
    radarBadge.style.display = "none";
    return;
  }
  radarBadge.style.display = "";
  radarBadge.textContent = `⚠ ${overlaps.length} overlap${overlaps.length === 1 ? "" : "s"}`;
  radarBadge.title =
    "Active worktrees touching the same files (click to open the first):\n" +
    overlaps
      .map((o) => `${o.a.ticket || o.a.branch} ⇄ ${o.b.ticket || o.b.branch}: ${o.files.join(", ")}`)
      .join("\n");
}

/** Fill the git/PR status strip from state.status (no topbar rebuild). Hidden until a
 * project's status has loaded, so it never flashes stale or placeholder counts. */
function updateStatusStrip(): void {
  if (!statusStrip) return;
  const s = state.status;
  if (!state.currentProject || !s) {
    statusStrip.style.display = "none";
    return;
  }
  statusStrip.style.display = "";
  clearChildren(statusStrip);
  const prs = s.open_prs;
  const unpushed = s.unpushed_commits;
  statusStrip.append(
    el(
      "span",
      {
        class: `board-status__item${prs ? "" : " board-status__item--zero"}`,
        title: `${prs} open pull request${prs === 1 ? "" : "s"}`,
      },
      [el("span", { class: "board-status__icon", "aria-hidden": "true" }, "⊙"), `${prs} PR${prs === 1 ? "" : "s"} open`],
    ),
    el(
      "span",
      {
        class: `board-status__item${unpushed ? "" : " board-status__item--zero"}`,
        title: `${unpushed} commit${unpushed === 1 ? "" : "s"} committed locally but not pushed`,
      },
      [el("span", { class: "board-status__icon", "aria-hidden": "true" }, "↑"), `${unpushed} unpushed`],
    ),
  );
}

// Onboarding / backend-down overlay in the board area.
function renderEmptyState(): void {
  const root = document.getElementById("board-root");
  document.getElementById("empty-state")?.remove();
  const board = document.getElementById("board");

  let card: HTMLElement | null = null;
  if (state.backendDown) {
    card = centerCard(
      "Can't reach the backend",
      "The SoloPM server isn't responding. Start it and try again.",
      "Retry",
      async () => {
        try {
          await loadProjects();
          renderTopbar();
          if (state.currentProject) await refreshTickets();
        } catch (err) {
          toastError((err as Error).message || "Still can't reach backend.");
        }
      },
      "solopm serve",
    );
  } else if (!state.projects.length) {
    card = centerCard(
      "Create your first project",
      "Projects map 1:1 to a git repo and own their own ticket sequence (e.g. SOLO-42). Make one to get a board.",
      "+ New project",
      openNewProject,
    );
  }

  if (board) board.style.display = card ? "none" : "";
  if (card && root) {
    root.append(el("div", { id: "empty-state", class: "empty-state" }, card));
  }
}

function centerCard(
  title: string,
  text: string,
  actionLabel: string,
  actionFn: () => void,
  mono?: string,
): HTMLElement {
  return el("div", { class: "empty-card surface" }, [
    el("div", { class: "empty-card__glyph", "aria-hidden": "true" }, "◧"),
    el("h2", { class: "empty-card__title" }, title),
    el("p", { class: "empty-card__text" }, text),
    mono ? el("code", { class: "empty-card__cmd mono" }, mono) : null,
    el("button", { class: "btn btn--primary", onClick: actionFn }, actionLabel),
  ]);
}

// --- modals ---------------------------------------------------------------
function field(label: string, input: HTMLElement, hint?: string): HTMLElement {
  return el("label", { class: "field" }, [
    el("span", { class: "field__label" }, label),
    input,
    hint ? el("span", { class: "field__hint" }, hint) : null,
  ]);
}

function setFormError(node: HTMLElement, msg: string): void {
  node.textContent = msg;
  node.hidden = false;
}

function openNewProject(): void {
  const keyInput = el("input", { class: "input mono", placeholder: "SOLO", autocomplete: "off", maxlength: 16, "aria-label": "Project key" });
  // Enforce the [A-Z][A-Z0-9]* shape as the user types.
  keyInput.addEventListener("input", () => {
    keyInput.value = keyInput.value.toUpperCase().replace(/[^A-Z0-9]/g, "");
  });
  const nameInput = el("input", { class: "input", placeholder: "SoloPM", "aria-label": "Project name" });
  const repoInput = el("input", { class: "input mono", placeholder: "/path/to/repo", "aria-label": "Repository path" });
  const masterInput = el("input", { class: "input mono", value: "main", "aria-label": "Master branch" });
  const errBox = el("div", { class: "form__err", hidden: true });

  const submitBtn = el("button", { class: "btn btn--primary" }, "Create project");
  const submit = async () => {
    errBox.hidden = true;
    const key = keyInput.value.trim();
    const name = nameInput.value.trim();
    if (!/^[A-Z][A-Z0-9]*$/.test(key)) return setFormError(errBox, "Key must be uppercase, start with a letter (e.g. SOLO).");
    if (!name) return setFormError(errBox, "Name is required.");

    submitBtn.disabled = true;
    try {
      const project = await api.createProject({
        key,
        name,
        repo: repoInput.value.trim() || undefined,
        master: masterInput.value.trim() || undefined,
      });
      await loadProjects();
      setProject(project.key);
      renderTopbar();
      modal.close();
      toastSuccess(`Project ${project.key} created`);
    } catch (err) {
      submitBtn.disabled = false;
      const e = err as ApiError;
      setFormError(errBox, e.code === "duplicate" ? `Project key "${key}" already exists.` : e.message || "Failed to create project.");
    }
  };
  submitBtn.addEventListener("click", () => void submit());

  const body = el("form", { class: "form", onSubmit: (e: Event) => { e.preventDefault(); void submit(); } }, [
    field("Key", keyInput, "Uppercase ticket prefix. Letters & digits, starts with a letter."),
    field("Name", nameInput),
    field("Repository path", repoInput, "Optional — local path to the git repo."),
    field("Master branch", masterInput, "Optional — defaults to main."),
    errBox,
  ]);
  const modal = openModal({
    title: "New project",
    body,
    footer: [el("button", { class: "btn btn--ghost", onClick: () => modal.close() }, "Cancel"), submitBtn],
    width: "460px",
  });
}

function openCreateTicket(): void {
  if (!state.currentProject) {
    toastError("Create or select a project first.");
    return;
  }
  const project = state.currentProject;

  const titleInput = el("input", { class: "input", placeholder: "What needs doing?", maxlength: 200, "aria-label": "Title" });
  const descInput = el("textarea", { class: "textarea", rows: 6, placeholder: "Description (markdown, optional)…", "aria-label": "Description" });

  const stateSelect = el("select", { class: "select", "aria-label": "Initial state" });
  for (const s of state.meta.states) {
    stateSelect.append(el("option", { value: s, selected: s === "backlog" }, state.meta.state_labels[s] || s));
  }
  const assignSelect = el("select", { class: "select", "aria-label": "Assignee" });
  for (const a of state.meta.assignees) {
    assignSelect.append(el("option", { value: a, selected: a === "unassigned" }, a));
  }
  const errBox = el("div", { class: "form__err", hidden: true });

  const submitBtn = el("button", { class: "btn btn--primary" }, "Create ticket");
  const submit = async () => {
    errBox.hidden = true;
    const title = titleInput.value.trim();
    if (!title) return setFormError(errBox, "Title is required.");

    submitBtn.disabled = true;
    try {
      const ticket = await api.createTicket({
        project,
        title,
        description: descInput.value,
        state: stateSelect.value as State,
        assignee: assignSelect.value as Assignee,
      });
      modal.close();
      await refreshTickets();
      toastSuccess(`${ticket.id} created`);
    } catch (err) {
      submitBtn.disabled = false;
      setFormError(errBox, (err as ApiError).message || "Failed to create ticket.");
    }
  };
  submitBtn.addEventListener("click", () => void submit());

  const body = el("form", { class: "form", onSubmit: (e: Event) => { e.preventDefault(); void submit(); } }, [
    field("Title", titleInput),
    field("Description", descInput),
    el("div", { class: "form__row" }, [field("State", stateSelect), field("Assignee", assignSelect)]),
    errBox,
  ]);
  const modal = openModal({
    title: `New ticket in ${project}`,
    body,
    footer: [el("button", { class: "btn btn--ghost", onClick: () => modal.close() }, "Cancel"), submitBtn],
    width: "560px",
  });
}

/** A self-contained curation panel for a project's review memory (immediate API calls). */
function reviewMemorySection(key: string, initial: ReviewMemoryItem[]): HTMLElement {
  const container = el("div", { class: "rm" });
  let items = initial;

  const refresh = async (): Promise<void> => {
    try {
      items = (await api.project(key)).review_memory || [];
      render();
    } catch {
      /* keep the current view on a failed refresh */
    }
  };
  const act = async (fn: () => Promise<unknown>): Promise<void> => {
    try {
      await fn();
      await refresh();
    } catch (err) {
      toastError((err as ApiError).message || "Review-memory update failed");
    }
  };

  function render(): void {
    clearChildren(container);
    if (!items.length) {
      container.append(
        el("div", { class: "muted rm__empty" }, "None yet — items accrue from AI-review fails and human kickbacks."),
      );
    }
    for (const i of items) {
      const meta = el(
        "span",
        { class: "rm__meta muted" },
        `${i.status} · ${i.source}${i.hits ? ` · ${i.hits} hit${i.hits === 1 ? "" : "s"}` : ""}`,
      );
      const actions: HTMLElement[] = [];
      if (i.status !== "active")
        actions.push(
          el("button", { type: "button", class: "btn btn--ghost btn--sm", onClick: () => void act(() => api.updateReviewMemory(key, i.id, { status: "active" })) }, "Activate"),
        );
      if (i.status !== "retired")
        actions.push(
          el("button", { type: "button", class: "btn btn--ghost btn--sm", onClick: () => void act(() => api.updateReviewMemory(key, i.id, { status: "retired" })) }, "Retire"),
        );
      container.append(
        el("div", { class: `rm__item rm__item--${i.status}` }, [
          el("div", { class: "rm__text" }, i.text),
          el("div", { class: "rm__row" }, [meta, el("span", { class: "rm__spacer" }), ...actions]),
        ]),
      );
    }
    const input = el("input", { class: "input rm__add", placeholder: "Add an item…", maxlength: 500 });
    const addItem = async (): Promise<void> => {
      const text = input.value.trim();
      if (!text) {
        input.focus();
        return;
      }
      input.value = "";
      await act(() => api.addReviewMemory(key, text));
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        void addItem();
      }
    });
    container.append(
      el("div", { class: "rm__addrow" }, [input, el("button", { type: "button", class: "btn btn--ghost btn--sm", onClick: () => void addItem() }, "Add")]),
    );
  }

  render();
  return container;
}

async function openProjectSettings(): Promise<void> {
  const key = state.currentProject;
  if (!key) {
    toastError("Select or create a project first.");
    return;
  }

  let project: Project;
  try {
    project = await api.project(key);
  } catch (err) {
    toastError((err as Error).message || "Couldn't load project settings.");
    return;
  }

  const nameInput = el("input", { class: "input", value: project.name || "", maxlength: 120, "aria-label": "Name" });
  const repoInput = el("input", { class: "input mono", value: project.repo || "", placeholder: "/path/to/repo", "aria-label": "Repository path" });
  const masterInput = el("input", { class: "input mono", value: project.master_branch || "main", "aria-label": "Master branch" });
  const conventionInput = el("input", { class: "input mono", value: project.branch_convention || "", "aria-label": "Branch convention" });

  const implSelect = el("select", { class: "select", "aria-label": "Default implementer" });
  const revSelect = el("select", { class: "select", "aria-label": "Default reviewer" });
  for (const a of ["claude", "codex"]) {
    implSelect.append(el("option", { value: a, selected: a === project.default_implementer }, a));
    revSelect.append(el("option", { value: a, selected: a === project.default_reviewer }, a));
  }

  const promptInput = el("textarea", { class: "textarea", rows: 6, "aria-label": "Review prompt" }, project.review_prompt || "");
  const errBox = el("div", { class: "form__err", hidden: true });

  const submitBtn = el("button", { class: "btn btn--primary" }, "Save settings");
  const submit = async () => {
    errBox.hidden = true;
    const name = nameInput.value.trim();
    if (!name) return setFormError(errBox, "Name is required.");

    submitBtn.disabled = true;
    try {
      await api.patchProject(key, {
        name,
        repo: repoInput.value.trim(),
        master_branch: masterInput.value.trim() || "main",
        branch_convention: conventionInput.value.trim() || project.branch_convention,
        default_implementer: implSelect.value,
        default_reviewer: revSelect.value,
        review_prompt: promptInput.value,
      });
      await loadProjects();
      renderTopbar();
      modal.close();
      toastSuccess(`Saved ${key} settings`);
    } catch (err) {
      submitBtn.disabled = false;
      setFormError(errBox, (err as ApiError).message || "Failed to save settings.");
    }
  };
  submitBtn.addEventListener("click", () => void submit());

  const body = el("form", { class: "form", onSubmit: (e: Event) => { e.preventDefault(); void submit(); } }, [
    field("Name", nameInput),
    field("Repository path", repoInput, "Local path to the git repo (project ↔ repo is 1:1)."),
    el("div", { class: "form__row" }, [field("Master branch", masterInput), field("Branch convention", conventionInput)]),
    el("div", { class: "form__row" }, [field("Default implementer", implSelect), field("Default reviewer", revSelect)]),
    field("Review prompt", promptInput, "The fresh-context prompt used when an agent reviews this project's work."),
    field(
      "Review memory",
      reviewMemorySection(key, project.review_memory || []),
      "Accumulated review checklist — active items are appended to the review prompt. Candidates are captured from AI-review fails and human kickbacks; curate them here.",
    ),
    errBox,
  ]);
  const modal = openModal({
    title: `${key} settings`,
    body,
    footer: [el("button", { class: "btn btn--ghost", onClick: () => modal.close() }, "Cancel"), submitBtn],
    width: "560px",
  });
}

// --- keyboard -------------------------------------------------------------
function wireShortcuts(): void {
  document.addEventListener("keydown", (e: KeyboardEvent) => {
    // Esc always works — close the topmost overlay, then the panel, then search.
    if (e.key === "Escape") {
      if (closeTopModal()) return;
      if (isTicketOpen()) return closeTicket();
      if (document.activeElement === searchInput) searchInput?.blur();
      return;
    }

    // Other shortcuts don't fire while typing, while a modal is up, or while the
    // ticket detail panel is open.
    if (isTypingTarget(e.target) || isOverlayOpen() || isTicketOpen()) return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;

    switch (e.key) {
      case "c":
        e.preventDefault();
        openCreateTicket();
        break;
      case "/":
        e.preventDefault();
        searchInput?.focus();
        searchInput?.select();
        break;
      case "r":
        e.preventDefault();
        void manualRefresh();
        break;
      default:
        break;
    }
  });
}

function isTypingTarget(t: EventTarget | null): boolean {
  const node = t as HTMLElement | null;
  if (!node) return false;
  return node.tagName === "INPUT" || node.tagName === "TEXTAREA" || node.tagName === "SELECT" || node.isContentEditable;
}

// --- polling --------------------------------------------------------------
function startPolling(): void {
  stopPolling();
  pollTimer = setInterval(async () => {
    // Skip when hidden, mid-drag, or a modal is open, to avoid clobbering.
    if (document.hidden || isDragging() || isOverlayOpen()) return;
    if (!state.currentProject) return;
    try {
      await refreshTickets({ silent: true });
      // Keep an open ticket panel live too, so concurrent CLI/agent writes show up.
      if (isTicketOpen()) await pollOpenTicket();
    } catch {
      // A failed background poll stays quiet; the last-known board remains visible.
    }
  }, POLL_MS);
}

function stopPolling(): void {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
}

async function manualRefresh(): Promise<void> {
  if (!state.currentProject) return;
  try {
    await refreshTickets();
    if (isTicketOpen()) await pollOpenTicket();
    toast("Refreshed", "info", 1200);
  } catch (err) {
    toastError((err as Error).message || "Refresh failed");
  }
}

function showBackendDown(err: Error): void {
  toastError(err.message || "Can't reach backend.");
}

void main();
