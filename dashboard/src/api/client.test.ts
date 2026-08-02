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
