import { describe, expect, it, vi } from "vitest";
import { mockApi } from "../test/utils";
import { ApiError, MESSAGES, configureClient, request } from "./client";

async function failure(promise: Promise<unknown>): Promise<ApiError> {
  try {
    await promise;
  } catch (error) {
    expect(error).toBeInstanceOf(ApiError);
    return error as ApiError;
  }
  throw new Error("expected the request to fail");
}

describe("API client", () => {
  it("sends the bearer token and JSON, and never sends cookies", async () => {
    const { requests, fetchMock } = mockApi({ "POST /search": { status: 200, body: { ok: true } } });
    configureClient(() => "the-token", async () => false);

    await request("/search", { method: "POST", body: { query: "q" } });

    expect(requests[0]?.headers).toMatchObject({ Authorization: "Bearer the-token", "Content-Type": "application/json" });
    expect(fetchMock.mock.calls[0]?.[1]).toMatchObject({ credentials: "omit" });
  });

  it.each([
    [403, "forbidden", MESSAGES.forbidden],
    [404, "not_found", MESSAGES.not_found],
    [409, "conflict", MESSAGES.conflict],
    [503, "unavailable", MESSAGES.unavailable],
    [500, "server", MESSAGES.server],
  ] as const)("maps %i to a fixed message, never the server's text", async (status, kind, message) => {
    mockApi({ "GET /policies": { status, body: { detail: "psycopg.errors.UniqueViolation: duplicate key value" } } });

    const error = await failure(request("/policies"));

    expect([error.status, error.kind, error.message]).toEqual([status, kind, message]);
    expect(error.message).not.toContain("psycopg");
  });

  it("shows the backend's known conflict reasons in plain words", async () => {
    mockApi({ "POST /policies": { status: 409, body: { detail: "A policy with this number already exists" } } });

    const error = await failure(request("/policies", { method: "POST", body: {} }));

    expect(error.message).toBe("A policy with this number already exists in your workspace.");
  });

  it("names invalid fields from a 422 without repeating the server's text or input", async () => {
    mockApi({
      "POST /policies": {
        status: 422,
        body: { detail: [{ loc: ["body", "name"], msg: "String should have at most 300", input: "secret input" }] },
      },
    });

    const error = await failure(request("/policies", { method: "POST", body: {} }));

    expect(error.kind).toBe("validation");
    expect(error.fieldErrors).toEqual({ name: "Enter a valid value." });
    expect(JSON.stringify(error)).not.toContain("secret input");
  });

  it("renews a rejected token once and retries the request with the new one", async () => {
    let token = "old-token";
    const onRejected = vi.fn(async () => {
      token = "new-token";
      return true;
    });
    configureClient(() => token, onRejected);
    const { requests } = mockApi({
      "GET /policies": (r) => (r.headers.Authorization === "Bearer new-token"
        ? { status: 200, body: [] } : { status: 401, body: { detail: "Invalid or expired token" } }),
    });

    await expect(request("/policies")).resolves.toEqual([]);

    expect(onRejected).toHaveBeenCalledExactlyOnceWith("old-token");
    expect(requests.map((r) => r.headers.Authorization)).toEqual(["Bearer old-token", "Bearer new-token"]);
  });

  it("retries only once, and not at all when the token couldn't be renewed", async () => {
    const renewed = vi.fn(async () => true);
    configureClient(() => "the-token", renewed);
    const { requests } = mockApi({ "GET /policies": { status: 401, body: { detail: "Invalid or expired token" } } });

    expect((await failure(request("/policies"))).message).toBe(MESSAGES.unauthenticated);
    expect([renewed.mock.calls.length, requests.length]).toEqual([1, 2]);

    configureClient(() => "the-token", async () => false);
    await failure(request("/policies"));
    expect(requests.length).toBe(3);
  });

  it("opens the demo session without a token, and never retries it", async () => {
    const onRejected = vi.fn(async () => true);
    configureClient(() => "the-token", onRejected);
    const { sessionRequests } = mockApi({ "POST /auth/demo-session": { status: 401, body: { detail: "x" } } });

    await failure(request("/auth/demo-session", { method: "POST", anonymous: true }));

    expect(onRejected).not.toHaveBeenCalled();
    expect(sessionRequests).toHaveLength(1);
    expect(sessionRequests[0]?.headers.Authorization).toBeUndefined();
    expect(sessionRequests[0]?.body).toBeUndefined();
  });

  it("reports a network failure as such", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));

    const error = await failure(request("/policies"));

    expect([error.kind, error.message]).toEqual(["network", MESSAGES.network]);
  });

  it("accepts an empty 204 response", async () => {
    mockApi({ "DELETE /policies/x": { status: 204 } });

    await expect(request("/policies/x", { method: "DELETE" })).resolves.toBeUndefined();
  });
});
