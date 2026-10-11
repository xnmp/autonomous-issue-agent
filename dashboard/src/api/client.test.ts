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

  it("keeps the issues payload usable when a card names an unknown review state", async () => {
    // `pr_review_state` is a closed enum in the contract. A backend that grows a
    // third value must cost one badge, not the whole Issues tab.
    const card = {
      issue: 7, title: "t", url: null, labels: [], pr: 9, pr_url: null,
      pr_state: "open", pr_review_state: "conflicting", stage: "review",
      done: false, steps: [], cost_usd: 1, estimated: false,
      projected_cost_usd: null, started_ts: null, last_ts: null,
      elapsed_s: null, working_s: null, idle_s: null, idle_active: false,
      projected_eta_s: null, models: [], rounds: null, flags: [],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            in_flight: [card], pending: [], done: [], backlog: [],
            gh_ok: true, notifications: [], warnings: [],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );

    const payload = await api.issues("demo");

    expect(payload.in_flight).toHaveLength(1);
    expect(payload.in_flight[0]!.issue).toBe(7);
    expect(payload.in_flight[0]!.pr_review_state).toBeNull();
  });
});
