import { afterEach, describe, expect, it, vi } from "vitest";

import { api, ApiError, isAbort } from "./client";
import type { ResolveCommand } from "./types";


const available = {
  status: "available",
  expected: {
    repository: "o/r",
    issue: 7,
    family: "resolve",
    epoch: 1,
    generation: 2,
    version: 3,
    episode_id: "episode-7",
  },
  workflow_held: true,
  gates: [],
  guidance: "keep the existing API contract",
  failures: 3,
  current_intent: null,
  requested_id: null,
  requested_intent: null,
  observation: null,
} as const;

const inactive = {
  status: "inactive",
  repository: "o/r",
  issue: 7,
} as const;

const absent = {
  status: "absent",
  repository: "o/r",
  issue: 7,
  epoch: 1,
} as const;

const unavailable = {
  status: "unavailable",
  reason: "control authority unavailable",
} as const;

const requestCommand = Object.freeze({
  operation: "request_rearm",
  expected: Object.freeze({ ...available.expected }),
  request_id: "request/7?retry=true",
  gate_id: "resolve-gate-7",
  guidance: Object.freeze({ kind: "keep" }),
}) satisfies ResolveCommand;


function jsonResponse(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}


function mockResponse(body: unknown, status: number) {
  const mocked = vi.fn().mockResolvedValue(jsonResponse(body, status));
  vi.stubGlobal("fetch", mocked);
  return mocked;
}


afterEach(() => {
  vi.unstubAllGlobals();
});


describe("resolve control reads", () => {
  it.each([
    [200, available],
    [200, inactive],
    [404, absent],
    [503, unavailable],
  ] as const)("parses the typed control returned with HTTP %i", async (status, body) => {
    mockResponse(body, status);

    await expect(api.resolveControl("demo", 7)).resolves.toEqual(body);
  });

  it("encodes deployment and request identities without changing their meaning", async () => {
    const mocked = mockResponse(available, 200);

    await api.resolveControl("team/repo ?", 7, "request/? &=");

    expect(mocked).toHaveBeenCalledOnce();
    expect(mocked.mock.calls[0]![0]).toBe(
      "/api/repos/team%2Frepo%20%3F/issues/7/resolve-control" +
      "?request_id=request%2F%3F%20%26%3D",
    );
    expect(mocked.mock.calls[0]![1]).toEqual({
      method: "GET",
      signal: undefined,
      headers: { Accept: "application/json" },
    });
  });

  it.each([403, 500])(
    "does not turn an HTTP %i error with a typed-looking body into inactive control",
    async (status) => {
      mockResponse(inactive, status);

      await expect(api.resolveControl("demo", 7)).rejects.toEqual(
        expect.objectContaining<ApiError>({
          name: "ApiError", message: expect.any(String), status,
        }),
      );
    },
  );

  it("does not turn framework validation errors into inactive control", async () => {
    mockResponse({ detail: [{ loc: ["query", "request_id"], msg: "invalid" }] }, 422);

    await expect(api.resolveControl("demo", 7)).rejects.toEqual(
      expect.objectContaining<ApiError>({
        name: "ApiError", message: expect.any(String), status: 422,
      }),
    );
  });

  it.each([
    [200, { status: "invented" }],
    [404, { ...absent, issue: "seven" }],
    [503, { status: "unavailable" }],
  ] as const)("rejects malformed typed outcomes at HTTP %i", async (status, body) => {
    mockResponse(body, status);

    await expect(api.resolveControl("demo", 7)).rejects.toEqual(
      expect.objectContaining<ApiError>({
        name: "ApiError",
        message: expect.stringContaining("invalid response"),
        status,
      }),
    );
  });

  it.each([
    [404, available],
    [503, absent],
    [200, unavailable],
  ] as const)("rejects a typed control paired with the wrong HTTP %i status", async (status, body) => {
    mockResponse(body, status);

    await expect(api.resolveControl("demo", 7)).rejects.toEqual(
      expect.objectContaining<ApiError>({
        name: "ApiError", message: expect.any(String), status,
      }),
    );
  });
});


describe("resolve commands", () => {
  it.each([
    [202, "accepted", available, "re-arm accepted"],
    [200, "replayed", available, "already accepted"],
    [409, "conflict", available, "workflow changed"],
    [422, "rejected", inactive, "family is inactive"],
    [503, "unavailable", unavailable, "authority unavailable"],
  ] as const)(
    "parses the typed command result returned with HTTP %i",
    async (status, outcome, control, reason) => {
      const body = { outcome, reason, control };
      mockResponse(body, status);

      await expect(api.resolveCommand("demo", 7, requestCommand)).resolves.toEqual(body);
    },
  );

  it("sends release with JSON and CSRF headers while retaining the frozen journal command", async () => {
    const result = { outcome: "accepted", reason: "", control: available } as const;
    const mocked = mockResponse(result, 202);

    await api.resolveCommand("team/repo ?", 7, requestCommand);

    expect(mocked).toHaveBeenCalledOnce();
    const [path, init] = mocked.mock.calls[0]!;
    expect(path).toBe(
      "/api/repos/team%2Frepo%20%3F/issues/7/resolve-control/commands",
    );
    expect(init).toEqual({
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "X-Issue-Agent-Dashboard": "1",
      },
      body: JSON.stringify({ operation: "release", expected: available.expected, request_id: "request/7?retry=true",
        selection: { kind: "gate", gate_id: "resolve-gate-7", intent: { kind: "resume", guidance: { kind: "keep" } } } }),
      signal: undefined,
    });
    expect(requestCommand).toEqual({
      operation: "request_rearm",
      expected: available.expected,
      request_id: "request/7?retry=true",
      gate_id: "resolve-gate-7",
      guidance: { kind: "keep" },
    });
  });

  it.each([403, 500])(
    "does not turn an HTTP %i error with a typed-looking body into success",
    async (status) => {
      mockResponse({ outcome: "accepted", reason: "", control: available }, status);

      await expect(api.resolveCommand("demo", 7, requestCommand)).rejects.toEqual(
        expect.objectContaining<ApiError>({
          name: "ApiError", message: expect.any(String), status,
        }),
      );
    },
  );

  it("does not turn a framework validation response into a command result", async () => {
    mockResponse({ detail: [{ loc: ["body"], msg: "invalid command" }] }, 422);

    await expect(api.resolveCommand("demo", 7, requestCommand)).rejects.toEqual(
      expect.objectContaining<ApiError>({
        name: "ApiError",
        message: expect.stringContaining("invalid command"),
        status: 422,
      }),
    );
  });

  it.each([
    [202, { outcome: "accepted", reason: "", control: { status: "available" } }],
    [409, { outcome: "conflict", reason: 7, control: available }],
    [503, { outcome: "invented", reason: "bad", control: unavailable }],
  ] as const)("rejects malformed command results at HTTP %i", async (status, body) => {
    mockResponse(body, status);

    await expect(api.resolveCommand("demo", 7, requestCommand)).rejects.toEqual(
      expect.objectContaining<ApiError>({
        name: "ApiError",
        message: expect.stringContaining("invalid response"),
        status,
      }),
    );
  });

  it.each([
    [202, "conflict"],
    [200, "accepted"],
    [409, "accepted"],
    [422, "replayed"],
    [503, "rejected"],
  ] as const)("rejects the mismatched command outcome at HTTP %i", async (status, outcome) => {
    mockResponse({ outcome, reason: "mismatched", control: available }, status);

    await expect(api.resolveCommand("demo", 7, requestCommand)).rejects.toEqual(
      expect.objectContaining<ApiError>({
        name: "ApiError", message: expect.any(String), status,
      }),
    );
  });
});


describe("resolve request cancellation", () => {
  it("forwards the abort signal and preserves AbortError", async () => {
    const controller = new AbortController();
    const mocked = vi.fn((_path: string, init: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        init.signal?.addEventListener(
          "abort",
          () => reject(new DOMException("aborted", "AbortError")),
          { once: true },
        );
      }),
    );
    vi.stubGlobal("fetch", mocked);

    const pending = api.resolveControl("demo", 7, undefined, controller.signal);
    controller.abort();

    await expect(pending).rejects.toSatisfy(isAbort);
    expect(mocked.mock.calls[0]![1].signal).toBe(controller.signal);
  });
});
