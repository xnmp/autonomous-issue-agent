import { render, screen, waitFor, within } from "@testing-library/react";
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
  slots: { capacity: 3, heavy: 0, light: 0 },
  done: [],
  backlog: [],
  gh_ok: true, merged_partial: false,
  observed_at: "2026-10-12T12:00:00Z",
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
    localStorage.setItem(
      "issue-agent.dashboard.options",
      JSON.stringify({ showOverviewIssueHealthTrend: true }),
    );
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
          caps: null,
          health: [],
          health_trend: [{
            day: "2026-07-29",
            opened: 3,
            closed: 1,
            net_open: 2,
          }],
          observed_at: "2026-10-12T12:00:00Z",
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

  it("collapses the sidebar to a floating settings button and nothing else", async () => {
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

    // The rail is fully gone: no repo list, no quota, no verification —
    // just the floating settings button and the way back.
    expect(screen.queryByRole("button", { name: "Quota" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Verification ledger" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Settings" }));
    expect(screen.getByRole("group", { name: "Dashboard options" })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Expand sidebar" }));
    await userEvent.click(await screen.findByRole("button", { name: "Settings" }));
    await userEvent.selectOptions(screen.getByLabelText("Theme"), "nord");
    const seconds = screen.getByRole("checkbox", { name: "Show seconds in durations" });
    await userEvent.click(seconds);
    expect(document.documentElement).toHaveAttribute("data-theme", "nord");
    expect(seconds).not.toBeChecked();
  });

  it("always shows daemon health, defaults the issue health trend off and persists its toggle", async () => {
    window.location.hash = "#/repo/demo/overview";
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        if (path === "/api/repos") return json(repos);
        if (path === "/api/usage") return json(usage);
        if (path.endsWith("/issues")) return json(issues);
        return json({
          metrics: null,
          caps: null,
          health: [{
            role: "fix",
            alive: true,
            stale: false,
            heartbeat_ts: "2026-08-09T01:00:00Z",
            in_flight: [],
          }],
          health_trend: [],
          needs_attention: [],
          spend_per_hour: [],
          warnings: [],
        });
      }),
    );

    const first = render(<App />);

    expect(await screen.findByText("Merged", { selector: ".tile-label" })).toBeVisible();
    expect(screen.getByText("Daemon health", { selector: ".panel-title" })).toBeVisible();
    expect(screen.queryByText("Issue health trend", { selector: ".panel-title" }))
      .not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Settings" }));
    expect(screen.queryByRole("checkbox", { name: "Daemon health" })).not.toBeInTheDocument();
    const issueHealthTrend = screen.getByRole("checkbox", { name: "Issue health trend" });
    expect(issueHealthTrend).not.toBeChecked();

    await userEvent.click(issueHealthTrend);
    expect(await screen.findByText("Issue health trend", { selector: ".panel-title" })).toBeVisible();
    expect(JSON.parse(localStorage.getItem("issue-agent.dashboard.options") ?? "{}"))
      .toMatchObject({ showOverviewIssueHealthTrend: true });

    first.unmount();
    render(<App />);

    expect(await screen.findByText("Daemon health", { selector: ".panel-title" })).toBeVisible();
    expect(screen.getByText("Issue health trend", { selector: ".panel-title" })).toBeVisible();
  });

  it("does not treat corrupt stored option strings as enabled booleans", async () => {
    window.location.hash = "#/repo/demo/overview";
    localStorage.setItem(
      "issue-agent.dashboard.options",
      JSON.stringify({
        showOverviewIssueHealthTrend: "true",
      }),
    );
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        if (path === "/api/repos") return json(repos);
        if (path === "/api/usage") return json(usage);
        if (path.endsWith("/issues")) return json(issues);
        return json({
          metrics: null,
          caps: null,
          health: [],
          health_trend: [],
          needs_attention: [],
          spend_per_hour: [],
          warnings: [],
        });
      }),
    );

    render(<App />);

    expect(await screen.findByText("Merged", { selector: ".tile-label" })).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Settings" }));
    expect(screen.getByRole("checkbox", { name: "Issue health trend" })).not.toBeChecked();
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

  describe("settings dialog", () => {
    const settings = {
      settings: [{
        key: "ISSUE_PICKUP", label: "Issue pickup", help: "", kind: "choice", group: "dispatch",
        value: "ready", source: "default", default: "ready", choices: ["ready", "open"],
        runtime: null, effective: null,
      }],
      runtimes: ["claude"], efforts: { claude: ["low"] }, models: { claude: ["sonnet"] },
      files: { repo: "/h/repos/demo.env", shared: "/h/shared.env" }, warnings: [],
    };
    const stubBackend = () => vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        if (path === "/api/repos") return json(repos);
        if (path === "/api/usage") return json(usage);
        if (path.endsWith("/issues")) return json(issues);
        if (path.includes("/settings")) return json(settings);
        return json({ rows: [], warnings: [] });
      }),
    );

    it("holds dashboard options and the selected repo's deployment settings, not a tab", async () => {
      window.location.hash = "#/repo/demo/issues/done";
      stubBackend();
      render(<App />);

      const button = await screen.findByRole("button", { name: "Settings" });
      expect(screen.queryByRole("tab", { name: "Settings" })).not.toBeInTheDocument();
      await userEvent.click(button);

      const dialog = screen.getByRole("dialog", { name: "Settings" });
      expect(dialog).toHaveAttribute("aria-modal", "true");
      expect(within(dialog).getByLabelText("Theme")).toBeInTheDocument();
      expect(await within(dialog).findByLabelText("Issue pickup")).toHaveValue("ready");

      await userEvent.keyboard("{Escape}");
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      expect(button).toHaveFocus();
    });

    it("closes from the close button and the backdrop", async () => {
      window.location.hash = "#/repo/demo/issues/done";
      stubBackend();
      render(<App />);

      await userEvent.click(await screen.findByRole("button", { name: "Settings" }));
      await userEvent.click(screen.getByRole("button", { name: "Close settings" }));
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

      await userEvent.click(screen.getByRole("button", { name: "Settings" }));
      await userEvent.click(screen.getByRole("dialog").parentElement!);
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });

    it("keeps the legacy /settings URL on the repo overview", async () => {
      window.location.hash = "#/repo/demo/settings";
      stubBackend();
      render(<App />);

      expect(await screen.findByText("Merged", { selector: ".tile-label" })).toBeVisible();
      expect(screen.queryByRole("tab", { name: "Settings" })).not.toBeInTheDocument();
    });

    it("shows an empty state when no deployment is selected", async () => {
      window.location.hash = "#/";
      vi.stubGlobal(
        "fetch",
        vi.fn(async (input: RequestInfo | URL) => {
          const path = String(input);
          if (path === "/api/repos") return json({ ...repos, repos: [] });
          if (path === "/api/usage") return json(usage);
          return json({ rows: [], warnings: [] });
        }),
      );
      render(<App />);

      await userEvent.click(await screen.findByRole("button", { name: "Settings" }));
      expect(screen.getByText(/Select a deployment/)).toBeInTheDocument();
      expect(screen.getByLabelText("Theme")).toBeInTheDocument();
    });
  });
});
