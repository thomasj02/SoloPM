// @vitest-environment happy-dom
// SOLO-21: tags render as chips on the board card.
import { describe, expect, it } from "vitest";
import { tagChips } from "./board";

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
