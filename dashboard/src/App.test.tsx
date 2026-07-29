import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import App from "./App";

const repos = {
  repos: [
    {
      name: "demo",
      repo: "o/r",
      target_dir: "/tmp/demo",
      is_self: true,
      active: true,
      roles: [],
      frecency: 1,
      last_activity_ts: null,
      in_flight: 0,
      done: 0,
    },
  ],
  warnings: [],
};

const usage = {
  claude: null,
  codex: null,
  harness: {
    global_daily_cap_usd: null,
    today_all_repos_usd: 0,
    per_repo: [],
  },
  warnings: [],
};

const issues = {
  in_flight: [],
  pending: [],
  done: [],
  backlog: [],
  gh_ok: true,
  warnings: [],
};

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

describe("App navigation", () => {
  it("remembers each repository's issue subtab and keeps verification out of the tab strip", async () => {
    window.location.hash = "#/repo/demo/issues/done";
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        if (path === "/api/repos") return json(repos);
        if (path === "/api/usage") return json(usage);
        if (path.endsWith("/issues")) return json(issues);
        if (path === "/api/verification") return json({ rows: [], warnings: [] });
        return json({
          metrics: null,
          cost_mix: null,
          caps: null,
          health: [],
          unattributed: [],
          health_trend: [{
            day: "2026-07-29",
            opened: 3,
            closed: 1,
            net_open: 2,
            recurrences: 0,
            first_observation: true,
          }],
          warnings: [],
        });
      }),
    );

    render(<App />);

    expect(await screen.findByRole("tab", { name: /Done/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await userEvent.click(screen.getByRole("tab", { name: "Overview" }));
    expect(
      await screen.findByRole("row", {
        name: /2026-07-29.*first observed.*3.*1.*\+2/,
      }),
    ).toBeVisible();
    await userEvent.click(screen.getByRole("tab", { name: /Issues/ }));
    expect(screen.getByRole("tab", { name: /Done/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );

    await userEvent.click(screen.getByRole("button", { name: "Verification ledger" }));
    expect(screen.queryByRole("tab", { name: "Verification" })).not.toBeInTheDocument();
    expect(await screen.findByText("Verification ledger", { selector: ".section-title span" })).toBeVisible();
  });
});
