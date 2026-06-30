// SOLO-20: the web client's deleteProject must hit the right URL, thread `force`
// through as a query param, encode the key, and surface backend errors as ApiError.
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "./api";

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => (body === undefined ? "" : JSON.stringify(body)),
  };
}

/** A fetch stub typed with the (input, init) params so `.mock.calls` indexes type-check. */
function stubFetch(body: unknown, status = 200) {
  const fetchMock = vi.fn(async (_input: string, _init?: RequestInit) =>
    jsonResponse(body, status),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("api.deleteProject", () => {
  it("DELETEs the project without force by default", async () => {
    const fetchMock = stubFetch({ key: "SOLO", deleted: true, tickets_deleted: 0 });

    const res = await api.deleteProject("SOLO");
    expect(res).toEqual({ key: "SOLO", deleted: true, tickets_deleted: 0 });
    expect(fetchMock.mock.calls[0][0]).toBe("/api/projects/SOLO");
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ method: "DELETE" });
  });

  it("adds ?force=true when force is set", async () => {
    const fetchMock = stubFetch({ key: "SOLO", deleted: true, tickets_deleted: 3 });

    const res = await api.deleteProject("SOLO", true);
    expect(res.tickets_deleted).toBe(3);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/projects/SOLO?force=true");
  });

  it("encodes the project key", async () => {
    const fetchMock = stubFetch({ key: "A B", deleted: true, tickets_deleted: 0 });

    await api.deleteProject("A B");
    expect(fetchMock.mock.calls[0][0]).toBe("/api/projects/A%20B");
  });

  it("surfaces a backend refusal as an ApiError with its code", async () => {
    stubFetch({ error: { code: "validation", message: "has tickets" } }, 400);

    await expect(api.deleteProject("SOLO")).rejects.toBeInstanceOf(ApiError);
    await expect(api.deleteProject("SOLO")).rejects.toMatchObject({ code: "validation" });
  });
});

describe("api tags (SOLO-21)", () => {
  it("addTags POSTs the tag list to the ticket's /tags", async () => {
    const fetchMock = stubFetch({ id: "SOLO-1", tags: ["bug", "frontend"] });

    const res = await api.addTags("SOLO-1", ["bug", "frontend"]);
    expect(res.tags).toEqual(["bug", "frontend"]);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/tickets/SOLO-1/tags");
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ tags: ["bug", "frontend"] });
  });

  it("removeTag DELETEs the encoded tag path segment", async () => {
    const fetchMock = stubFetch({ id: "SOLO-1", tags: [] });

    await api.removeTag("SOLO-1", "front end");
    expect(fetchMock.mock.calls[0][0]).toBe("/api/tickets/SOLO-1/tags/front%20end");
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ method: "DELETE" });
  });

  it("tickets() appends repeated tag query params", async () => {
    const fetchMock = stubFetch({ tickets: [] });

    await api.tickets({ project: "SOLO", tags: ["bug", "frontend"] });
    expect(fetchMock.mock.calls[0][0]).toBe("/api/tickets?project=SOLO&tag=bug&tag=frontend");
  });
});

describe("api.prune (SOLO-23)", () => {
  it("POSTs a dry-run by default", async () => {
    const fetchMock = stubFetch({ project: "SOLO", applied: false, pruned: [], skipped: [] });

    const res = await api.prune("SOLO");
    expect(res.applied).toBe(false);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/projects/SOLO/prune");
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ apply: false });
  });

  it("passes apply=true", async () => {
    const fetchMock = stubFetch({ project: "SOLO", applied: true, pruned: [], skipped: [] });

    await api.prune("SOLO", true);
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(JSON.parse(init.body as string)).toEqual({ apply: true });
  });
});
