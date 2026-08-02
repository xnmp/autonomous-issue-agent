import { render, screen, waitFor } from "@testing-library/react";
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
  notifications: [],
  warnings: [],
};

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

describe("App navigation", () => {
  it("uses top-level issue-state tabs and keeps verification out of the tab strip", async () => {
    window.location.hash = "#/repo/demo/issues/done";
    let overviewCalls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        if (path === "/api/repos") return json(repos);
        if (path === "/api/usage") return json(usage);
        if (path.endsWith("/issues")) return json(issues);
        if (path === "/api/verification") return json({ rows: [], warnings: [] });
        overviewCalls += 1;
        if (overviewCalls > 1) {
          await new Promise((resolve) => window.setTimeout(resolve, 250));
        }
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
        name: /2026-07-29.*3.*1.*\+2/,
      }),
    ).toBeVisible();
    await userEvent.click(screen.getByRole("tab", { name: /Done/ }));
    expect(screen.getByRole("tab", { name: /Done/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await userEvent.click(screen.getByRole("tab", { name: "Overview" }));
    expect(
      screen.getByRole("row", {
        name: /2026-07-29.*3.*1.*\+2/,
      }),
    ).toBeVisible();
    expect(screen.queryByLabelText("Loading overview")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: /Done/ }));
    expect(screen.getByRole("tab", { name: /Done/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("tab", { name: /In flight/ })).toBeVisible();
    expect(screen.getByRole("tab", { name: /Backlog/ })).toBeVisible();
    expect(screen.queryByText("#/repo/demo/issues/done")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Verification ledger" }));
    expect(screen.queryByRole("tab", { name: "Verification" })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Overview" })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: /In flight/ })).not.toBeInTheDocument();
    expect(await screen.findByText("Verification ledger", { selector: ".section-title span" })).toBeVisible();
  });

  it("collapses the sidebar to a floating quota button and nothing else", async () => {
    window.location.hash = "#/repo/demo/issues/done";
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        if (path === "/api/repos") return json(repos);
        if (path === "/api/usage") return json(usage);
        if (path.endsWith("/issues")) return json(issues);
        return json({ rows: [], warnings: [] });
      }),
    );

    render(<App />);

    await screen.findByRole("button", { name: "Settings" });
    expect(screen.queryByText("Operations")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Collapse sidebar" }));

    // The rail is fully gone: no repo list, no settings, no verification —
    // just the floating quota button and the way back.
    expect(screen.queryByRole("button", { name: "Settings" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Verification ledger" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Quota" }));
    expect(screen.getByRole("region", { name: "Quota" })).toHaveClass("compact");
    expect(screen.queryByText("Quota", { selector: ".panel-title" })).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Expand sidebar" }));
    await userEvent.click(await screen.findByRole("button", { name: "Settings" }));
    await userEvent.selectOptions(screen.getByLabelText("Theme"), "nord");
    const seconds = screen.getByRole("checkbox", { name: "Show seconds in durations" });
    await userEvent.click(seconds);
    expect(document.documentElement).toHaveAttribute("data-theme", "nord");
    expect(seconds).not.toBeChecked();
  });

  it("optimistically removes a promoted backlog issue and updates its tab count", async () => {
    window.location.hash = "#/repo/demo/issues/backlog";
    let finishPromotion: (response: Response) => void = () => {};
    const promotion = new Promise<Response>((resolve) => {
      finishPromotion = resolve;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        if (path === "/api/repos") return json(repos);
        if (path === "/api/usage") return json(usage);
        if (path.endsWith("/promote")) return promotion;
        if (path.endsWith("/issues")) {
          return json({
            ...issues,
            backlog: [{
              issue: 120,
              title: "Toast notifications",
              url: "https://github.com/o/r/issues/120",
              body: "Important state changes",
              labels: ["feature"],
              recurrence: false,
              recurrence_count: 0,
              age_days: 0,
              comments: 0,
              issue_type: "feature",
              difficulty: null,
              impact: null,
            }],
          });
        }
        return json({ rows: [], warnings: [] });
      }),
    );

    render(<App />);

    await screen.findByText("Toast notifications");
    expect(screen.getByRole("tab", { name: /Backlog/ })).toHaveTextContent("1");
    await userEvent.click(screen.getByRole("button", { name: "Promote #120 to ai-ready" }));
    await userEvent.click(screen.getByRole("button", { name: "Promote #120" }));

    await waitFor(() => expect(screen.queryByText("Toast notifications")).not.toBeInTheDocument());
    expect(screen.getByRole("tab", { name: /Backlog/ })).toHaveTextContent("0");
    finishPromotion(json({ ok: true, detail: "promoted" }));
  });
});
