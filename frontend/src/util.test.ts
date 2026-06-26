// SOLO-13: compactDuration powers the board's time-in-state badge.
import { describe, expect, it } from "vitest";
import { compactDuration } from "./util";

describe("compactDuration", () => {
  it("returns empty string for null/undefined/negative", () => {
    expect(compactDuration(null)).toBe("");
    expect(compactDuration(undefined)).toBe("");
    expect(compactDuration(-1)).toBe("");
  });

  it("renders sub-minute as 'now'", () => {
    expect(compactDuration(0)).toBe("now");
    expect(compactDuration(59)).toBe("now");
  });

  it("truncates to the largest whole unit (m/h/d)", () => {
    expect(compactDuration(60)).toBe("1m");
    expect(compactDuration(59 * 60)).toBe("59m");
    expect(compactDuration(60 * 60)).toBe("1h");
    expect(compactDuration(23 * 3600 + 59 * 60)).toBe("23h");
    expect(compactDuration(24 * 3600)).toBe("1d");
    expect(compactDuration(50 * 3600)).toBe("2d");
  });
});
