// ui.ts — shared UI primitives: toasts, a modal system (focus-trapped, Esc/backdrop
// to close), and the assignee badge component used by both the board and detail panel.

import { el, type ElChild } from "./util";

// --- toasts ---------------------------------------------------------------
let toastHost: HTMLElement | undefined;
function host(): HTMLElement {
  if (!toastHost) {
    toastHost = el("div", { class: "toast-host", id: "toast-host", "aria-live": "polite" });
    document.body.append(toastHost);
  }
  return toastHost;
}

export type ToastKind = "info" | "success" | "error";

/**
 * Show a toast. It stays visible until the user dismisses it (clicks ×) or the
 * returned dismiss fn is called — toasts never auto-dismiss, so the user always
 * has time to read them.
 */
export function toast(message: string, kind: ToastKind = "info"): () => void {
  const node = el("div", { class: `toast toast--${kind}`, role: "status" });
  const dismiss = () => {
    node.classList.add("toast--out");
    setTimeout(() => node.remove(), 180);
  };
  node.append(
    el("span", { class: "toast__msg" }, message),
    el("button", { class: "toast__close", title: "Dismiss", "aria-label": "Dismiss", onClick: dismiss }, "×"),
  );
  host().append(node);
  return dismiss;
}

export const toastError = (m: string): (() => void) => toast(m, "error");
export const toastSuccess = (m: string): (() => void) => toast(m, "success");

// --- modal ----------------------------------------------------------------
export interface ModalHandle {
  close: () => void;
  panel: HTMLElement;
}

export interface ModalOptions {
  title: string;
  body: ElChild | ElChild[];
  footer?: ElChild | ElChild[];
  onClose?: () => void;
  width?: string;
}

const openModals: ModalHandle[] = [];

/** True while any modal is open (used to pause polling / gate shortcuts). */
export function isOverlayOpen(): boolean {
  return openModals.length > 0;
}

/** Open a modal dialog. `body`/`footer` accept nodes/arrays (see el()'s children). */
export function openModal({ title, body, footer, onClose, width }: ModalOptions): ModalHandle {
  const previouslyFocused = document.activeElement as HTMLElement | null;

  const closeBtn = el("button", { class: "modal__close", title: "Close (Esc)", "aria-label": "Close" }, "×");
  const panel = el(
    "div",
    { class: "modal", role: "dialog", "aria-modal": "true", style: width ? `width:${width}` : "" },
    [
      el("header", { class: "modal__head" }, [el("h2", { class: "modal__title" }, title || ""), closeBtn]),
      el("div", { class: "modal__body" }, body),
      footer ? el("footer", { class: "modal__foot" }, footer) : null,
    ],
  );
  const backdrop = el("div", { class: "modal-backdrop" }, [panel]);

  const handle: ModalHandle = { close, panel };

  function close(): void {
    const idx = openModals.indexOf(handle);
    if (idx === -1) return; // already closed
    openModals.splice(idx, 1);
    backdrop.classList.add("modal-backdrop--out");
    setTimeout(() => backdrop.remove(), 150);
    document.removeEventListener("keydown", onKey, true);
    previouslyFocused?.focus?.();
    onClose?.();
  }

  function onKey(e: KeyboardEvent): void {
    if (openModals[openModals.length - 1] !== handle) return; // only the topmost reacts
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      close();
    } else if (e.key === "Tab") {
      trapFocus(panel, e);
    }
  }

  backdrop.addEventListener("mousedown", (e) => {
    if (e.target === backdrop) close();
  });
  closeBtn.addEventListener("click", close);
  document.addEventListener("keydown", onKey, true);

  openModals.push(handle);
  document.body.append(backdrop);

  // Focus the first interactive field (skip the close button when possible).
  const focusable = panel.querySelector<HTMLElement>(
    "input, textarea, select, button:not(.modal__close)",
  );
  (focusable || closeBtn).focus();

  return handle;
}

/** Close the topmost modal if any. Returns true if one was closed. */
export function closeTopModal(): boolean {
  if (openModals.length) {
    openModals[openModals.length - 1].close();
    return true;
  }
  return false;
}

function trapFocus(container: HTMLElement, e: KeyboardEvent): void {
  const items = Array.from(
    container.querySelectorAll<HTMLElement>(
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ),
  ).filter((n) => n.offsetParent !== null);
  if (!items.length) return;
  const first = items[0];
  const last = items[items.length - 1];
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault();
    first.focus();
  }
}

// --- shared components -----------------------------------------------------
/** Color-coded assignee chip with an initial avatar. */
export function assigneeBadge(assignee: string): HTMLElement {
  const a = assignee || "unassigned";
  const initial = a === "unassigned" ? "?" : a[0].toUpperCase();
  return el("span", { class: `badge badge--${a}`, title: `Assignee: ${a}` }, [
    el("span", { class: "badge__avatar" }, initial),
    el("span", { class: "badge__name" }, a),
  ]);
}
