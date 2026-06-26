// app.js — bootstrap + orchestration: builds the shell and top bar, wires the
// project/ticket creation modals, keyboard shortcuts, polling, and onboarding.

import {
  state, on, loadMeta, loadProjects, setProject, refreshTickets, setFilter,
} from "./store.js";
import { api } from "./api.js";
import { el, clearChildren } from "./util.js";
import { openModal, closeTopModal, isOverlayOpen, toast, toastError, toastSuccess } from "./ui.js";
import { initBoard, isDragging } from "./board.js";
import { closeTicket, isTicketOpen, pollOpenTicket } from "./ticket.js";

const POLL_MS = 4000;
let searchInput = null;
let pollTimer = null;

async function main() {
  buildShell();
  initBoard(document.getElementById("board-root"));
  wireShortcuts();
  on("projects", renderTopbar);

  await loadMeta();
  try {
    await loadProjects();
  } catch (err) {
    state.backendDown = true;
    showBackendDown(err);
  }
  renderTopbar();

  if (state.currentProject) {
    try {
      await refreshTickets();
    } catch (err) {
      toastError(err.message || "Couldn't load tickets.");
    }
  }
  startPolling();
}

// --- shell ----------------------------------------------------------------
function buildShell() {
  const app = document.getElementById("app");
  const topbar = el("header", { class: "topbar", id: "topbar" });
  const board = el("div", { id: "board-root", class: "board-root" });
  app.append(topbar, el("main", { class: "main" }, board));
}

function renderTopbar() {
  const bar = document.getElementById("topbar");
  clearChildren(bar);

  const brand = el("div", { class: "brand" }, [
    el("span", { class: "brand__logo", "aria-hidden": "true" }, "◧"),
    el("span", { class: "brand__name" }, "SoloPM"),
  ]);

  // Project selector (the "+ New project" sentinel opens the modal).
  const select = el("select", {
    class: "select project-select",
    title: "Project",
    onChange: (e) => {
      if (e.target.value === "__new__") {
        e.target.value = state.currentProject || "";
        openNewProject();
        return;
      }
      setProject(e.target.value);
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
    onClick: openProjectSettings,
    disabled: !state.currentProject,
  }, "⚙");

  searchInput = el("input", {
    class: "input search",
    type: "search",
    placeholder: "Filter cards…  ( / )",
    value: state.filter,
    "aria-label": "Filter cards",
    onInput: (e) => setFilter(e.target.value),
  });

  const refreshBtn = el("button", { class: "btn btn--ghost icon-btn", title: "Refresh (r)", "aria-label": "Refresh", onClick: manualRefresh }, "⟳");
  const newTicketBtn = el("button", { class: "btn btn--primary", title: "New ticket (c)", onClick: openCreateTicket, disabled: !state.currentProject }, "+ New ticket");

  bar.append(
    el("div", { class: "topbar__left" }, [
      brand,
      el("span", { class: "topbar__sep" }),
      el("div", { class: "selectwrap" }, select),
      settingsBtn,
      newProjectBtn,
    ]),
    el("div", { class: "topbar__right" }, [searchInput, refreshBtn, newTicketBtn]),
  );

  renderEmptyState();
}

// Onboarding / backend-down overlay in the board area.
function renderEmptyState() {
  const root = document.getElementById("board-root");
  document.getElementById("empty-state")?.remove();
  const board = document.getElementById("board");

  let card = null;
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
          toastError(err.message || "Still can't reach backend.");
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
  if (card) {
    root.append(el("div", { id: "empty-state", class: "empty-state" }, card));
  }
}

function centerCard(title, text, actionLabel, actionFn, mono) {
  return el("div", { class: "empty-card surface" }, [
    el("div", { class: "empty-card__glyph", "aria-hidden": "true" }, "◧"),
    el("h2", { class: "empty-card__title" }, title),
    el("p", { class: "empty-card__text" }, text),
    mono ? el("code", { class: "empty-card__cmd mono" }, mono) : null,
    el("button", { class: "btn btn--primary", onClick: actionFn }, actionLabel),
  ]);
}

// --- modals ---------------------------------------------------------------
function field(label, input, hint) {
  return el("label", { class: "field" }, [
    el("span", { class: "field__label" }, label),
    input,
    hint ? el("span", { class: "field__hint" }, hint) : null,
  ]);
}

function setFormError(node, msg) {
  node.textContent = msg;
  node.hidden = false;
}

function openNewProject() {
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
      setFormError(errBox, err.code === "duplicate" ? `Project key "${key}" already exists.` : err.message || "Failed to create project.");
    }
  };
  submitBtn.addEventListener("click", submit);

  const body = el("form", { class: "form", onSubmit: (e) => { e.preventDefault(); submit(); } }, [
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

function openCreateTicket() {
  if (!state.currentProject) {
    toastError("Create or select a project first.");
    return;
  }

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
        project: state.currentProject,
        title,
        description: descInput.value,
        state: stateSelect.value,
        assignee: assignSelect.value,
      });
      modal.close();
      await refreshTickets();
      toastSuccess(`${ticket.id} created`);
    } catch (err) {
      submitBtn.disabled = false;
      setFormError(errBox, err.message || "Failed to create ticket.");
    }
  };
  submitBtn.addEventListener("click", submit);

  const body = el("form", { class: "form", onSubmit: (e) => { e.preventDefault(); submit(); } }, [
    field("Title", titleInput),
    field("Description", descInput),
    el("div", { class: "form__row" }, [field("State", stateSelect), field("Assignee", assignSelect)]),
    errBox,
  ]);
  const modal = openModal({
    title: `New ticket in ${state.currentProject}`,
    body,
    footer: [el("button", { class: "btn btn--ghost", onClick: () => modal.close() }, "Cancel"), submitBtn],
    width: "560px",
  });
}

async function openProjectSettings() {
  const key = state.currentProject;
  if (!key) {
    toastError("Select or create a project first.");
    return;
  }

  let project;
  try {
    project = await api.project(key);
  } catch (err) {
    toastError(err.message || "Couldn't load project settings.");
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
      setFormError(errBox, err.message || "Failed to save settings.");
    }
  };
  submitBtn.addEventListener("click", submit);

  const body = el("form", { class: "form", onSubmit: (e) => { e.preventDefault(); submit(); } }, [
    field("Name", nameInput),
    field("Repository path", repoInput, "Local path to the git repo (project ↔ repo is 1:1)."),
    el("div", { class: "form__row" }, [field("Master branch", masterInput), field("Branch convention", conventionInput)]),
    el("div", { class: "form__row" }, [field("Default implementer", implSelect), field("Default reviewer", revSelect)]),
    field("Review prompt", promptInput, "The fresh-context prompt used when an agent reviews this project's work."),
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
function wireShortcuts() {
  document.addEventListener("keydown", (e) => {
    // Esc always works — close the topmost overlay, then the panel, then search.
    if (e.key === "Escape") {
      if (closeTopModal()) return;
      if (isTicketOpen()) return closeTicket();
      if (document.activeElement === searchInput) searchInput.blur();
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
        manualRefresh();
        break;
      default:
        break;
    }
  });
}

function isTypingTarget(t) {
  if (!t) return false;
  return t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable;
}

// --- polling --------------------------------------------------------------
function startPolling() {
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

function stopPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
}

async function manualRefresh() {
  if (!state.currentProject) return;
  try {
    await refreshTickets();
    if (isTicketOpen()) await pollOpenTicket();
    toast("Refreshed", "info", 1200);
  } catch (err) {
    toastError(err.message || "Refresh failed");
  }
}

function showBackendDown(err) {
  toastError(err.message || "Can't reach backend.");
}

main();
