// markdown.ts — a tiny, XSS-safe Markdown -> HTML renderer.
//
// Design for safety: every piece of user text is HTML-escaped BEFORE any
// markdown tags are emitted, and we only ever generate a fixed, known-safe set
// of tags. Links are restricted to safe URL schemes. There is no path by which
// attacker-controlled HTML reaches innerHTML, so no external sanitizer is needed
// (and no CDN dependency — this works fully offline).

import { escapeHtml } from "./util";

// Allowed link targets: http(s), mailto, in-page anchors, and relative paths.
const SAFE_URL = /^(https?:\/\/|mailto:|\/|#|\.{1,2}\/)/i;

// Private-use sentinels for protecting code spans (kept out of the source as
// raw bytes; they cannot appear in user-typed markdown).
const C_OPEN = String.fromCharCode(0xe000);
const C_CLOSE = String.fromCharCode(0xe001);
const CODE_RESTORE = new RegExp(`${C_OPEN}(\\d+)${C_CLOSE}`, "g");

function safeHref(rawUrl: string): string | null {
  const trimmed = rawUrl.trim();
  return SAFE_URL.test(trimmed) ? escapeHtml(trimmed) : null;
}

// Inline formatting: code spans, links, bold, italic. `text` is raw (unescaped).
function inline(text: string): string {
  // Protect inline code first so its contents aren't treated as markdown.
  const codeSpans: string[] = [];
  let s = text.replace(/`([^`]+)`/g, (_m, code: string) => {
    codeSpans.push(code);
    return `${C_OPEN}${codeSpans.length - 1}${C_CLOSE}`;
  });

  // Escape everything else (this neutralizes any raw HTML).
  s = escapeHtml(s);

  // Links [label](url) — operate on the now-escaped text.
  s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (whole, label: string, url: string) => {
    // The url was escaped (e.g. & -> &amp;); restore for scheme validation.
    const href = safeHref(url.replace(/&amp;/g, "&"));
    if (!href) return whole; // unsafe scheme -> render literally
    return `<a href="${href}" target="_blank" rel="noopener noreferrer">${label}</a>`;
  });

  // Bold then italic. Bold first so ** isn't eaten by the * rule.
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^*\w])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  s = s.replace(/(^|[^_\w])_([^_\n]+)_/g, "$1<em>$2</em>");

  // Restore (escaped) code spans.
  s = s.replace(CODE_RESTORE, (_m, i: string) => `<code>${escapeHtml(codeSpans[+i])}</code>`);
  return s;
}

const isHeading = (l: string) => /^(#{1,6})\s+/.test(l);
const isQuote = (l: string) => /^>\s?/.test(l);
const isUl = (l: string) => /^\s*[-*+]\s+/.test(l);
const isOl = (l: string) => /^\s*\d+\.\s+/.test(l);
const isHr = (l: string) => /^\s*([-*_])(\s*\1){2,}\s*$/.test(l);
const isFence = (l: string) => /^```/.test(l);

/** Render markdown source to a safe HTML string. */
export function renderMarkdown(src: string | null | undefined): string {
  if (!src || !String(src).trim()) {
    return '<p class="md-empty">No description.</p>';
  }
  const lines = String(src).replace(/\r\n/g, "\n").split("\n");
  const out: string[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    // Fenced code block.
    if (isFence(line)) {
      const buf: string[] = [];
      i++;
      while (i < lines.length && !isFence(lines[i])) buf.push(lines[i++]);
      i++; // consume closing fence (if present)
      out.push(`<pre class="md-pre"><code>${escapeHtml(buf.join("\n"))}</code></pre>`);
      continue;
    }

    if (!line.trim()) {
      i++;
      continue;
    }

    if (isHr(line)) {
      out.push('<hr class="md-hr">');
      i++;
      continue;
    }

    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      const level = h[1].length;
      out.push(`<h${level} class="md-h">${inline(h[2])}</h${level}>`);
      i++;
      continue;
    }

    if (isQuote(line)) {
      const buf: string[] = [];
      while (i < lines.length && isQuote(lines[i])) buf.push(lines[i++].replace(/^>\s?/, ""));
      out.push(`<blockquote class="md-quote">${inline(buf.join(" "))}</blockquote>`);
      continue;
    }

    if (isUl(line)) {
      const items: string[] = [];
      while (i < lines.length && isUl(lines[i])) {
        items.push(`<li>${inline(lines[i++].replace(/^\s*[-*+]\s+/, ""))}</li>`);
      }
      out.push(`<ul class="md-ul">${items.join("")}</ul>`);
      continue;
    }

    if (isOl(line)) {
      const items: string[] = [];
      while (i < lines.length && isOl(lines[i])) {
        items.push(`<li>${inline(lines[i++].replace(/^\s*\d+\.\s+/, ""))}</li>`);
      }
      out.push(`<ol class="md-ol">${items.join("")}</ol>`);
      continue;
    }

    // Paragraph: gather consecutive "plain" lines.
    const buf: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !isFence(lines[i]) &&
      !isHeading(lines[i]) &&
      !isQuote(lines[i]) &&
      !isUl(lines[i]) &&
      !isOl(lines[i]) &&
      !isHr(lines[i])
    ) {
      buf.push(lines[i++]);
    }
    out.push(`<p class="md-p">${inline(buf.join("\n")).replace(/\n/g, "<br>")}</p>`);
  }

  return out.join("\n");
}
