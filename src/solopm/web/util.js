// util.js — small DOM + formatting helpers shared across modules.
// Zero dependencies; safe-by-default (all user text goes through escapeHtml or
// text nodes, never raw innerHTML except the explicit `html` escape hatch).

/** Escape a string for safe insertion into HTML text/attribute context. */
export function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

/**
 * Terse hyperscript-style element factory.
 *   el("div", { class: "x", onClick: fn, dataset: { id: "1" } }, [child, "text"])
 *
 * Props rules:
 *   - "class"            -> className
 *   - "style"            -> setAttribute (string cssText)
 *   - "dataset"          -> Object.assign(node.dataset, val)
 *   - "html"             -> innerHTML (caller MUST pass already-sanitized HTML)
 *   - onX (function)     -> addEventListener("x", fn)
 *   - val === false/null -> skipped (lets you do `disabled: cond`)
 *   - a known property   -> assigned directly, else setAttribute
 * Children may be a node, a string, an array, or nullish (skipped).
 */
export function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, val] of Object.entries(props)) {
    if (val == null || val === false) continue;
    if (key === "class") node.className = val;
    else if (key === "style") node.setAttribute("style", val);
    else if (key === "dataset") Object.assign(node.dataset, val);
    else if (key === "html") node.innerHTML = val; // sanitized markdown only
    else if (key.startsWith("on") && typeof val === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), val);
    } else if (key in node && key !== "list") {
      node[key] = val;
    } else {
      node.setAttribute(key, val);
    }
  }
  for (const child of [].concat(children)) {
    if (child == null || child === false) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

/** Remove all children from a node. */
export function clearChildren(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

/** Human-friendly relative time, e.g. "3 minutes ago". */
export function relativeTime(iso) {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const secs = Math.round((Date.now() - then) / 1000);
  const abs = Math.abs(secs);
  const suffix = secs >= 0 ? "ago" : "from now";
  const unit = (n, name) => `${n} ${name}${n === 1 ? "" : "s"} ${suffix}`;
  if (abs < 45) return "just now";
  const mins = Math.round(abs / 60);
  if (abs < 90) return unit(1, "minute");
  if (mins < 60) return unit(mins, "minute");
  const hours = Math.round(mins / 60);
  if (hours < 24) return unit(hours, "hour");
  const days = Math.round(hours / 24);
  if (days < 30) return unit(days, "day");
  const months = Math.round(days / 30);
  if (months < 12) return unit(months, "month");
  return unit(Math.round(months / 12), "year");
}
