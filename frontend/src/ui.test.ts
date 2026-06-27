// @vitest-environment happy-dom
// SOLO-19: toasts must stay visible until the user explicitly dismisses them.
import { afterEach, describe, expect, it, vi } from "vitest";
import { toast, toastError, toastSuccess } from "./ui";

afterEach(() => {
  // The toast host is a module-level singleton; clear only the toasts so it
  // stays attached to document.body between tests.
  document.querySelectorAll(".toast").forEach((n) => n.remove());
  vi.useRealTimers();
});

const toastNodes = () => document.querySelectorAll(".toast");

describe("toast persistence", () => {
  it("does not auto-dismiss — stays visible after a long delay", () => {
    vi.useFakeTimers();
    toast("hello");
    expect(toastNodes().length).toBe(1);
    vi.advanceTimersByTime(60_000);
    expect(toastNodes().length).toBe(1);
  });

  it("error toasts also persist", () => {
    vi.useFakeTimers();
    toastError("boom");
    vi.advanceTimersByTime(60_000);
    expect(toastNodes().length).toBe(1);
  });

  it("success toasts also persist", () => {
    vi.useFakeTimers();
    toastSuccess("yay");
    vi.advanceTimersByTime(60_000);
    expect(toastNodes().length).toBe(1);
  });

  it("removes the toast when the close button is clicked", () => {
    vi.useFakeTimers();
    toast("dismiss me");
    (document.querySelector(".toast__close") as HTMLButtonElement).click();
    vi.advanceTimersByTime(300); // allow the out-animation removal timeout
    expect(toastNodes().length).toBe(0);
  });

  it("the returned dismiss fn removes the toast", () => {
    vi.useFakeTimers();
    const dismiss = toast("bye");
    dismiss();
    vi.advanceTimersByTime(300);
    expect(toastNodes().length).toBe(0);
  });
});
