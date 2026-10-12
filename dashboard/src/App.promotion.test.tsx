import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import App from "./App";

const repos = {
  repos: [{
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
  }],
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

const backlogIssue = {
  issue: 120,
  title: "Toast notifications",
  url: "https://github.com/o/r/issues/120",
  body: "Important state changes",
  labels: ["feature"],
  recurrence: false,
  recurrence_count: 0,
  steward_state: "raw",
  age_days: 0,
  comments: 0,
  issue_type: "feature",
  difficulty: null,
  impact: null,
};

const historicalDoneIssue = {
  issue: 120,
  title: "Previous completion",
  url: "https://github.com/o/r/issues/120",
  pr: null,
  pr_url: null,
  pr_state: null,
  stage: "done",
  done: true,
  steps: [],
  cost_usd: null,
  estimated: false,
  projected_cost_usd: null,
  projected_model: null,
  started_ts: null,
  last_ts: null,
  elapsed_s: null,
  working_s: null,
  idle_s: null,
  idle_active: false,
  models: [],
  model_badges: [],
  rounds: null,
  issue_verification: null,
  pr_verification: null,
  merged_ts: null,
};

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

describe("backlog promotion", () => {
  it("optimistically moves a promoted issue into the in-flight tab", async () => {
    window.location.hash = "#/repo/demo/issues/backlog";
    let finishPromotion: (response: Response) => void = () => {};
    let serverPromoted = false;
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
          if (serverPromoted) {
            return json({
              ...issues,
              pending: [{
                issue: 120,
                title: "Toast notifications",
                url: "https://github.com/o/r/issues/120",
                priority: "P1",
                labels: ["feature", "ai-ready"],
                held: false,
                hold_reason: null,
                overlap: null,
              }],
            });
          }
          return json({
            ...issues,
            done: [historicalDoneIssue],
            backlog: [backlogIssue],
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

    await waitFor(() => {
      expect(screen.queryByText("Toast notifications")).not.toBeInTheDocument();
    });
    expect(screen.getByRole("tab", { name: /Backlog/ })).toHaveTextContent("0");
    expect(screen.getByRole("tab", { name: /In flight/ })).toHaveTextContent("1");

    await userEvent.click(screen.getByRole("tab", { name: /In flight/ }));
    expect(await screen.findByText("Toast notifications")).toBeVisible();
    expect(screen.getByText("Queued")).toBeVisible();

    serverPromoted = true;
    finishPromotion(json({ ok: true, detail: "promoted" }));
    expect(await screen.findByText(/picks it up \(P1\)/)).toBeVisible();
    expect(screen.getAllByText("Toast notifications")).toHaveLength(1);
  });

  it("rolls both tabs back when promotion fails", async () => {
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
          return json({ ...issues, backlog: [backlogIssue] });
        }
        return json({ rows: [], warnings: [] });
      }),
    );

    render(<App />);

    await screen.findByText("Toast notifications");
    await userEvent.click(screen.getByRole("button", { name: "Promote #120 to ai-ready" }));
    await userEvent.click(screen.getByRole("button", { name: "Promote #120" }));
    expect(screen.getByRole("tab", { name: /In flight/ })).toHaveTextContent("1");

    finishPromotion(new Response("promotion failed", { status: 500 }));

    expect(await screen.findByText("Toast notifications")).toBeVisible();
    expect(screen.getByRole("tab", { name: /Backlog/ })).toHaveTextContent("1");
    expect(screen.getByRole("tab", { name: /In flight/ })).toHaveTextContent("0");
  });

  it("clears optimistic state when promotion advances directly to done", async () => {
    window.location.hash = "#/repo/demo/issues/backlog";
    let finishPromotion: (response: Response) => void = () => {};
    let serverDone = false;
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
          return serverDone
            ? json({ ...issues, done: [historicalDoneIssue] })
            : json({ ...issues, backlog: [backlogIssue] });
        }
        return json({ rows: [], warnings: [] });
      }),
    );

    render(<App />);

    await screen.findByText("Toast notifications");
    await userEvent.click(screen.getByRole("button", { name: "Promote #120 to ai-ready" }));
    await userEvent.click(screen.getByRole("button", { name: "Promote #120" }));
    expect(screen.getByRole("tab", { name: /In flight/ })).toHaveTextContent("1");

    serverDone = true;
    finishPromotion(json({ ok: true, detail: "promoted" }));

    await waitFor(() => {
      expect(screen.getByRole("tab", { name: /In flight/ })).toHaveTextContent("0");
    });
    expect(screen.getByRole("tab", { name: /Done/ })).toHaveTextContent("1");
    expect(screen.getByRole("tab", { name: /Backlog/ })).toHaveTextContent("0");
  });

  it("keeps optimistic state while GitHub data is unavailable", async () => {
    window.location.hash = "#/repo/demo/issues/backlog";
    let finishPromotion: (response: Response) => void = () => {};
    let githubUnavailable = false;
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
          return githubUnavailable
            ? json({ ...issues, gh_ok: false })
            : json({ ...issues, backlog: [backlogIssue] });
        }
        return json({ rows: [], warnings: [] });
      }),
    );

    render(<App />);

    await screen.findByText("Toast notifications");
    await userEvent.click(screen.getByRole("button", { name: "Promote #120 to ai-ready" }));
    await userEvent.click(screen.getByRole("button", { name: "Promote #120" }));

    githubUnavailable = true;
    finishPromotion(json({ ok: true, detail: "promoted" }));

    await waitFor(() => {
      expect(screen.getByRole("tab", { name: /In flight/ })).toHaveTextContent("1");
    });
    await userEvent.click(screen.getByRole("tab", { name: /In flight/ }));
    expect(screen.getByText("Toast notifications")).toBeVisible();
  });
});
