import { describe, expect, it, vi } from "vitest";
import { api, ApiError } from "./client";

describe("validated API client", () => {
  it("rejects a successful response that violates the generated contract", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ repos: "not-an-array", warnings: [] }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    await expect(api.repos()).rejects.toEqual(
      expect.objectContaining<ApiError>({
        name: "ApiError",
        message: expect.stringContaining("invalid response"),
        status: 200,
      }),
    );
  });

  it("never reports a success body's detail as the error of a contract violation", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ repos: "not-an-array", detail: "updated" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      })),
    );

    await expect(api.repos()).rejects.toThrow(/invalid response/);
  });

  it.each([[200], [503]])("reads an empty HTTP %i body as a restarting backend, not a JSON error", async (status) => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("", { status })));

    await expect(api.repos()).rejects.toEqual(
      expect.objectContaining({ name: "ApiError", message: expect.stringMatching(/restarting/), status }),
    );
  });

  it("reports a request that timed out distinctly from an unreachable backend", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => {
      throw new DOMException("request timed out", "TimeoutError");
    }));

    await expect(api.repos()).rejects.toThrow("the dashboard backend did not respond in time");
  });

  it("rejects an issues payload whose board status is not one the contract names", async () => {
    // The board is the server's derived state; a status this client cannot
    // place must fail loudly, never land in a guessed group.
    const card = {
      issue: 7, title: "t", url: null, labels: [], pr: null, pr_url: null,
      board: { status: "blocked", headline: "?", reason: "", since_ts: null },
      rail: { current: "resolve", furthest: "resolve", resolve_attempts: 0, review_rounds: 0, ci_runs: 0, steps: 0, live: false },
      cost_usd: 0, estimated: false, legs: 0, started_ts: null, last_ts: null, leg_running: false,
    };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({ in_flight: [card], done: [], backlog: [], gh_ok: true, warnings: [] }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );

    await expect(api.issues("demo")).rejects.toThrow("invalid response");
  });

});
