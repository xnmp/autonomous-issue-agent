import { expect, test, type Page, type Route } from "@playwright/test";

const repos = {
  repos: [{
    name: "self",
    repo: "openai/autonomous-issue-agent",
    target_dir: "/tmp/self",
    is_self: true,
    active: true,
    roles: [],
    frecency: 1,
    last_activity_ts: "2026-07-30T08:00:00Z",
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

const backlogIssue = {
  issue: 120,
  title: "Toast notifications for state transitions",
  url: "https://github.com/openai/autonomous-issue-agent/issues/120",
  body: "Important state changes arrive silently.",
  labels: ["feature"],
  age_days: 0,
  comments: 0,
  issue_type: "feature",
  difficulty: "medium",
  impact: "high",
};

const queuedIssue = {
  issue: 120,
  title: "Toast notifications for state transitions",
  url: "https://github.com/openai/autonomous-issue-agent/issues/120",
  labels: ["feature", "ai-ready"],
  pr: null,
  pr_url: null,
  board: {
    status: "working", activity: "queued", act: "RESOLVE", headline: "Queued",
    reason: "Dispatches when a slot is free.", since_ts: null,
  },
  rail: { current: "resolve", furthest: "resolve", resolve_attempts: 0, review_rounds: 0, ci_runs: 0, steps: 0, live: false },
  cost_usd: 0,
  estimated: false,
  legs: 0,
  started_ts: null,
  last_ts: null,
  leg_running: false,
};

async function json(route: Route, body: unknown) {
  await route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function mockApi(page: Page) {
  let promoted = false;
  let issuePolls = 0;
  await page.route("**/*", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (!path.startsWith("/api/")) return route.continue();
    if (path === "/api/repos") return json(route, repos);
    if (path === "/api/usage") return json(route, usage);
    if (path === "/api/repos/self/issues/120/promote") {
      await new Promise((resolve) => setTimeout(resolve, 2_000));
      promoted = true;
      return json(route, { ok: true, detail: "promoted" });
    }
    if (path === "/api/repos/self/issues") {
      issuePolls += 1;
      return json(route, {
        in_flight: promoted ? [queuedIssue] : [],
        done: [],
        backlog: promoted ? [] : [backlogIssue],
        gh_ok: true,
        observed_at: "2026-07-30T08:00:00Z",
        notifications: issuePolls <= 2 ? [] : [{
          id: "leg:L120:start",
          ts: "2026-07-30T08:01:00Z",
          severity: "info",
          title: "Issue #120 dispatched",
          detail: "RESOLVE started",
          issue: 120,
          pr: null,
        }],
        warnings: [],
      });
    }
    return json(route, { rows: [], warnings: [] });
  });
}

test("promotion updates both tabs and a new dispatch raises a toast", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/repo/self/issues/backlog");

  await expect(page.getByText("Toast notifications for state transitions")).toBeVisible();
  await page.getByRole("button", { name: "Promote #120 to ai-ready" }).click();
  await page.getByRole("button", { name: "Promote #120", exact: true }).click();

  await expect(page.getByRole("tab", { name: /Backlog/ })).toContainText("0");
  await expect(page.getByRole("tab", { name: /In flight/ })).toContainText("1");
  await page.getByRole("tab", { name: /In flight/ }).click();
  const queued = page.getByRole("region", { name: "Working" }).locator(".board-row", { hasText: "#120" });
  await expect(queued.locator(".board-headline")).toHaveText("Queued");

  await expect(page.getByRole("status").filter({ hasText: "Issue #120 dispatched" }))
    .toBeVisible({ timeout: 7_000 });
});
