// Preserves the markdown renderer's XSS guarantee in the TypeScript toolchain.
import { describe, expect, it } from "vitest";
import { renderMarkdown } from "./markdown";

const SAFE_TAGS = new Set([
  "p", "a", "strong", "em", "code", "pre", "ul", "ol", "li",
  "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "br",
]);

/** A real leak = an unsafe opening tag, or an <a href> with a non-allowlisted scheme. */
function hasXssLeak(html: string): boolean {
  const tags = [...html.matchAll(/<([a-z][a-z0-9]*)/gi)].map((m) => m[1].toLowerCase());
  if (tags.some((t) => !SAFE_TAGS.has(t))) return true;
  return /href="(?!https?:|mailto:|\/|#|\.{1,2}\/)/i.test(html);
}

describe("renderMarkdown", () => {
  it.each([
    "<script>alert(1)</script>",
    "[click](javascript:alert(1))",
    "[x](JaVaScRiPt:alert(1))",
    "<img src=x onerror=alert(1)>",
    "- item <svg/onload=alert(1)>",
    "[data](data:text/html,<script>alert(1)</script>)",
    "> quote with <b>tags</b>",
  ])("never emits an XSS sink for %j", (input) => {
    expect(hasXssLeak(renderMarkdown(input))).toBe(false);
  });

  it("renders safe links and basic formatting", () => {
    const html = renderMarkdown("**bold** _it_ `code` [ok](https://example.com)");
    expect(html).toContain("<strong>bold</strong>");
    expect(html).toContain("<em>it</em>");
    expect(html).toContain("<code>code</code>");
    expect(html).toContain('href="https://example.com"');
  });

  it("shows a placeholder for empty input", () => {
    expect(renderMarkdown("")).toContain("No description.");
  });
});
