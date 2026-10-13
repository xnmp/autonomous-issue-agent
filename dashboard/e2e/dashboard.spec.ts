import { expect, test, type Page, type Route } from "@playwright/test";

const role = (name: string) => ({ role: name, alive: true, stale: false, heartbeat_ts: "2026-07-29T08:00:00Z" });

const repos = {
  repos: [
    {
      name: "self",
      repo: "openai/autonomous-issue-agent",
      target_dir: "/tmp/self",
      is_self: true,
      active: true,
      roles: [role("fix"), role("review")],
      frecency: 1,
      last_activity_ts: "2026-07-29T08:00:00Z",
      in_flight: 0,
      done: 1,
    },
    {
      name: "sleeping",
      repo: "openai/sleeping",
      target_dir: "/tmp/sleeping",
      is_self: false,
      active: false,
      roles: [],
      frecency: 0,
      last_activity_ts: null,
      in_flight: null,
      done: null,
    },
  ],
  warnings: [],
};

const usage = {
  claude: null,
  codex: null,
  harness: {
    global_daily_cap_usd: 50,
    today_all_repos_usd: 2,
    per_repo: [
      { name: "self", repo: "openai/autonomous-issue-agent", daily_cap_usd: 10, today_usd: 2 },
    ],
  },
  warnings: [],
};

const doneCard = {
  issue: 50,
  title: "Explain completed work",
  url: "https://github.com/openai/autonomous-issue-agent/issues/50",
  pr: 75,
  pr_url: "https://github.com/openai/autonomous-issue-agent/pull/75",
  merged_ts: "2026-07-29T08:00:00Z",
  cost_usd: 2,
  legs: 4,
  review_rounds: 2,
  labels: [],
};

const rail = (over: Record<string, unknown> = {}) => ({
  current: "review", furthest: "review", resolve_attempts: 1, review_rounds: 1, ci_runs: 0, steps: 2, live: false, ...over,
});

const inFlightCard = {
  issue: 49,
  title: "Exercise model wrapping",
  url: "https://github.com/openai/autonomous-issue-agent/issues/49",
  labels: ["ai-ready"],
  pr: 74,
  pr_url: "https://github.com/openai/autonomous-issue-agent/pull/74",
  pr_ci_state: "passing",
  board: {
    status: "working", activity: "agent", act: "REVIEW", headline: "Review running",
    reason: "An agent leg holds this item.", since_ts: "2026-07-29T08:00:00Z",
  },
  action: null,
  rail: rail({ live: true }),
  history: [
    { stage: "resolve", steps: [{ kind: "RESOLVE", outcome: "done", count: 1 }] },
    { stage: "review", steps: [{ kind: "REVIEW", outcome: "running", count: 1 }] },
  ],
  cost_usd: 1.8,
  estimated: true,
  legs: 2,
  started_ts: "2026-07-29T07:00:00Z",
  last_ts: "2026-07-29T08:00:00Z",
  leg_running: true,
};

const issues = {
  in_flight: [inFlightCard],
  done: [
    doneCard,
    ...Array.from({ length: 18 }, (_, index) => ({
      ...doneCard,
      issue: 51 + index,
      title: `Completed issue ${51 + index}`,
      url: `https://github.com/openai/autonomous-issue-agent/issues/${51 + index}`,
      pr: 76 + index,
      pr_url: `https://github.com/openai/autonomous-issue-agent/pull/${76 + index}`,
    })),
  ],
  backlog: [{
    issue: 120,
    title: "Toast notifications for state transitions",
    url: "https://github.com/openai/autonomous-issue-agent/issues/120",
    body: "## Problem\n\nImportant state changes arrive silently.",
    labels: ["feature", "effort:medium", "impact:high"],
    age_days: 0,
    age_s: 1800,
    comments: 2,
    issue_type: "feature",
    difficulty: "medium",
    impact: "high",
  }],
  gh_ok: true,
  observed_at: "2026-07-29T08:00:00Z",
  notifications: [],
  warnings: [],
};

const overview = {
  metrics: null,
  caps: { issue_cap_usd: 5, daily_cap_usd: 10, global_daily_cap_usd: 50, today_usd: 2 },
  health: [],
  needs_attention: [],
  spend_per_hour: [],
  health_trend: [{ day: "2026-07-29", opened: 3, closed: 1, net_open: 2 }],
  warnings: [],
};

const question = "Harness issue: the required-test gate rejects diagnostic output after the full acceptance plan was supplied. Should the plan's diagnostics count as test output?";

const round3Issues = {
  ...issues,
  in_flight: [{
    ...inFlightCard,
    issue: 457,
    title: "Required-test gate rejects diagnostic output",
    url: "https://github.com/openai/autonomous-issue-agent/issues/457",
    pr: 482,
    pr_url: "https://github.com/openai/autonomous-issue-agent/pull/482",
    leg_running: false,
    action: "open",
    board: {
      status: "needs_you", reason: "question", headline: "Answer the agent's question",
      hint: "Reply on GitHub with the answer; the agent resumes with it.",
      detail: question, target: "pr", since_ts: "2026-07-29T07:30:00Z",
    },
    rail: rail({ current: "review", furthest: "review", review_rounds: 4, ci_runs: 1, steps: 10 }),
    history: [
      { stage: "resolve", steps: [{ kind: "RESOLVE", outcome: "done", count: 1 }] },
      { stage: "ci", steps: [{ kind: "FIX_CI", outcome: "done", count: 1 }] },
      {
        stage: "review",
        steps: [
          { kind: "REVIEW", outcome: "changes_requested", count: 2 },
          { kind: "ADDRESS_REVIEW", outcome: "done", count: 1 },
          { kind: "REVIEW", outcome: "changes_requested", count: 1 },
          { kind: "ADDRESS_REVIEW", outcome: "done", count: 1 },
          { kind: "REVIEW", outcome: "changes_requested", count: 1 },
          { kind: "ADDRESS_REVIEW", outcome: "asked", count: 1 },
        ],
      },
    ],
  }],
  backlog: [
    {
      ...issues.backlog[0],
      issue: 119,
      title: "Add OAuth token refresh diagnostics",
      url: "https://github.com/openai/autonomous-issue-agent/issues/119",
      labels: ["feature", "effort:medium", "impact:high"],
    },
    issues.backlog[0],
  ],
};

const round3Overview = {
  ...overview,
  metrics: {
    merged_prs: 19,
    open_items: 2,
    needs_human: 2,
    total_cost_usd: 34.96,
    cost_per_merged_pr_usd: 1.84,
    by_basis: [{ basis: "provider_reported", usd: 30.1 }, { basis: "estimate", usd: 4.86 }],
    by_kind: [
      { kind: "RESOLVE", usd: 20.4, runs: 22 },
      { kind: "REVIEW", usd: 9.2, runs: 31 },
      { kind: "ADDRESS_REVIEW", usd: 4.1, runs: 9 },
      { kind: "FIX_CI", usd: 1.26, runs: 4 },
    ],
    first_pass_approvals: 8,
    first_pass_changes: 3,
    first_pass_rate: 8 / 11,
    rounds_per_pr: 1.32,
    legs: 66,
    machine_legs: 1,
    today_usd: 6.42,
  },
  caps: { issue_cap_usd: 10, daily_cap_usd: 25, global_daily_cap_usd: 60, today_usd: 6.42 },
  needs_attention: [
    {
      issue: 457,
      title: "Required-test gate rejects diagnostic output",
      reason: "question",
      headline: "Answer the agent's question",
      detail: question,
    },
    {
      issue: 462,
      title: "Choose the event retention boundary",
      reason: "labelled",
      headline: "Parked",
      detail: null,
    },
  ],
  health: ["fix", "review"].map((name, index) => ({
    ...role(name),
    heartbeat_ts: `2026-08-09T07:${55 + index}:00Z`,
  })),
  spend_per_hour: Array.from({ length: 24 }, (_, index) => ({
    hour_start_iso: `2026-08-${index < 16 ? "08" : "09"}T${String((index + 8) % 24).padStart(2, "0")}:00:00Z`,
    usd: [0, 0.12, 0.35, 0.08, 0, 0.61][index % 6] ?? 0,
  })),
  health_trend: Array.from({ length: 7 }, (_, index) => ({
    day: `2026-08-0${index + 3}`,
    opened: 2 + (index % 3),
    closed: 1 + (index % 2),
    net_open: 1,
  })),
};

const leg = (idx: number, over: Record<string, unknown>) => ({
  idx,
  leg_id: `leg-50-${idx}`,
  kind: "RESOLVE",
  role: "fix",
  runtime: "codex",
  state: "ended",
  outcome: "done",
  started_ts: `2026-07-29T08:0${idx * 2}:00Z`,
  ended_ts: null,
  duration_s: 60,
  cost_usd: 0.5,
  estimated: false,
  pricing_basis: "provider_reported",
  end_reason: "normal_return",
  session_id: `session-50-${idx}`,
  usage: null,
  head: null,
  pr: null,
  ...over,
});

const issue = {
  issue: 50,
  title: "Explain completed work",
  url: "https://github.com/openai/autonomous-issue-agent/issues/50",
  labels: [],
  board: null,
  action: null,
  done: true,
  held_for_human: false,
  pull_requests: [
    {
      number: 75,
      url: "https://github.com/openai/autonomous-issue-agent/pull/75",
      state: "merged",
    },
  ],
  cost_usd: 2,
  estimated: true,
  rail: rail({ current: "merge", furthest: "merge", review_rounds: 2, steps: 4 }),
  history: [],
  steps: [
    leg(0, { duration_s: 4638, cost_usd: 2, summary: "Added the task narrative to the issue page." }),
    leg(1, {
      kind: "REVIEW", role: "review", runtime: "claude", outcome: "changes_requested", duration_s: 92,
      cost_usd: 0.42, estimated: true, pricing_basis: "estimate", pr: 75, head: "a1234567890",
      verdict: "REQUEST_CHANGES",
      verdict_excerpt: "The implementation still hides the actual review findings. Surface the requested-change body as untrusted text so an operator can decide whether another round is productive.",
    }),
    leg(2, { kind: "ADDRESS_REVIEW", duration_s: 186, cost_usd: 0.88, pr: 75, head: "b1234567890" }),
    leg(3, { kind: "REVIEW", role: "review", runtime: "claude", outcome: "passed", duration_s: 74, cost_usd: 0.31, pr: 75, verdict: "APPROVE" }),
  ],
  warnings: [],
};

async function json(route: Route, body: unknown, delayMs = 0) {
  if (delayMs) await new Promise((resolve) => setTimeout(resolve, delayMs));
  await route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function mockApi(
  page: Page,
  reposDelayMs = 0,
  fixtures: {
    issuesPayload?: Record<string, unknown>;
    overviewPayload?: Record<string, unknown>;
  } = {},
) {
  const issuesPayload = fixtures.issuesPayload ?? issues;
  const overviewPayload = fixtures.overviewPayload ?? overview;
  let issuePolls = 0;
  await page.route("**/*", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (!path.startsWith("/api/")) return route.continue();
    if (path === "/api/repos") return json(route, repos, reposDelayMs);
    if (path === "/api/usage") return json(route, usage);
    if (path === "/api/repos/self/issues") {
      issuePolls += 1;
      return json(route, {
        ...issuesPayload,
        notifications: issuePolls <= 2 ? [] : [{
          id: "leg:leg-50-3:end",
          ts: "2026-07-29T08:00:00Z",
          severity: "info",
          title: "Issue #50 completed",
          detail: "PR #75 merged",
          issue: 50,
          pr: 75,
        }],
      });
    }
    if (path === "/api/repos/self/issues/50") return json(route, issue);
    const detail = /^\/api\/repos\/self\/issues\/(\d+)$/.exec(path);
    if (detail) return json(route, { ...issue, issue: Number(detail[1]), done: false, pull_requests: [] });
    if (path === "/api/verification")
      return json(route, {
        rows: [{ id: "V-1", change: "Dashboard", status: "verified", evidence: "E2E", shipped: "yes", raw: "" }],
        warnings: [],
      });
    if (path === "/api/repos/self/overview") return json(route, overviewPayload);
    return json(route, { ok: true, detail: "ok" });
  });
}

test("operator workflow preserves semantics and navigation", async ({ page }) => {
  await mockApi(page);
  await page.setViewportSize({ width: 1280, height: 560 });
  await page.goto("/#/repo/self/issues/done");

  await expect(page.locator(".chip strong", { hasText: "issue-agent" })).toBeVisible();
  await page.locator(".rail-past > summary").click();
  await expect(page.locator(".rail-past .repo-name", { hasText: "sleeping" })).toBeVisible();
  // Starting daemons is not the dashboard's job: the only writes are label changes.
  await expect(page.getByRole("button", { name: /Start daemons/ })).toHaveCount(0);
  const sidebarBounds = await page.locator(".sidebar").boundingBox();
  const footerBounds = await page.locator(".sidebar-foot").boundingBox();
  expect(sidebarBounds).not.toBeNull();
  expect(footerBounds).not.toBeNull();
  expect(footerBounds!.y + footerBounds!.height).toBeLessThanOrEqual(
    sidebarBounds!.y + sidebarBounds!.height + 1,
  );
  await expect(page.getByRole("button", { name: "Quota", exact: true })).toBeInViewport();
  await expect(page.getByRole("button", { name: "Settings", exact: true })).toBeInViewport();
  await expect(page.getByRole("button", { name: "Verification ledger", exact: true })).toBeInViewport();
  expect(await page.evaluate(() => document.documentElement.scrollHeight)).toBe(560);

  const expectVerticallyContained = async (selector: string) => {
    const bounds = await page.locator(selector).boundingBox();
    expect(bounds).not.toBeNull();
    expect(bounds!.y).toBeGreaterThanOrEqual(0);
    expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(560);
  };
  await page.getByRole("button", { name: "Quota", exact: true }).click();
  await expectVerticallyContained(".quota-card");
  await page.screenshot({
    path: "../evidence/dashboard-sidebar-footer.png",
    fullPage: true,
  });
  await page.getByRole("button", { name: "Quota", exact: true }).click();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expectVerticallyContained(".options-popover");
  const issueHealthTrend = page.getByRole("checkbox", { name: "Issue health trend" });
  await expect(issueHealthTrend).not.toBeChecked();
  await issueHealthTrend.check();
  await page.getByRole("button", { name: "Settings", exact: true }).click();

  await page.getByRole("button", { name: "Collapse sidebar" }).click();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expectVerticallyContained(".options-popover");
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByRole("button", { name: "Expand sidebar" }).click();
  await page.setViewportSize({ width: 1440, height: 900 });
  await expect(page.getByRole("tab", { name: /In flight/ })).toBeVisible();
  await expect(page.getByRole("tab", { name: /Backlog/ })).toBeVisible();
  await expect(page.getByText("#/repo/self/issues/done")).toHaveCount(0);
  await expect(page.getByRole("columnheader", { name: /Since done/ })).toBeVisible();
  await expect(page.getByRole("columnheader", { name: /^Date/ })).toHaveCount(0);
  await expect(page.getByRole("columnheader", { name: /Legs/ })).toBeVisible();

  const mainbody = page.locator(".mainbody");
  await mainbody.evaluate((element) => {
    element.scrollTop = 420;
  });
  const mainbodyBounds = await mainbody.boundingBox();
  await expect(page.getByRole("tab", { name: /Done/ })).toBeInViewport();
  const doneTabBounds = await page.getByRole("tab", { name: /Done/ }).boundingBox();
  expect(doneTabBounds!.y + doneTabBounds!.height).toBeLessThanOrEqual(mainbodyBounds!.y);
  await mainbody.evaluate((element) => {
    element.scrollTop = 0;
  });

  await expect(page.getByRole("status").filter({ hasText: "Issue #50 completed" }))
    .toBeVisible({ timeout: 7_000 });
  await page.screenshot({
    path: "../evidence/dashboard-toasts.png",
    fullPage: true,
  });
  await page.getByRole("button", { name: "Dismiss Issue #50 completed" }).click();

  const quotaButton = page.getByRole("button", { name: "Quota", exact: true });
  await expect(quotaButton).toHaveAttribute("aria-expanded", "false");
  await quotaButton.click();
  await expect(quotaButton).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByRole("region", { name: "Quota" })).toBeVisible();
  await expect(page.getByText("Quota", { exact: true })).toHaveCount(1);

  await expect(page.locator(".sidebar-scroll")).toHaveCSS("overflow-y", "auto");
  await expect(page.getByRole("separator", { name: "Resize sidebar" })).toBeVisible();
  await page.getByRole("button", { name: "Collapse sidebar" }).click();
  await expect(page.getByRole("button", { name: "Expand sidebar" })).toBeVisible();
  // Collapsed, the rail is gone: the bottom-left button opens settings, not quota.
  await expect(page.getByRole("button", { name: "Quota", exact: true })).toHaveCount(0);
  const expandBounds = await page.getByRole("button", { name: "Expand sidebar" }).boundingBox();
  const repoTitleBounds = await page.locator(".repo-context-name").boundingBox();
  expect(expandBounds).not.toBeNull();
  expect(repoTitleBounds).not.toBeNull();
  expect(expandBounds!.x + expandBounds!.width).toBeLessThanOrEqual(repoTitleBounds!.x);
  const collapsedSettings = page.getByRole("button", { name: "Settings", exact: true });
  const collapsedSettingsBounds = await collapsedSettings.boundingBox();
  expect(collapsedSettingsBounds?.width).toBeGreaterThanOrEqual(40);
  expect(collapsedSettingsBounds?.height).toBeGreaterThanOrEqual(40);
  expect(collapsedSettingsBounds!.x).toBeLessThan(100);
  await collapsedSettings.click();
  await expect(page.getByRole("group", { name: "Dashboard options" })).toBeInViewport();
  await page.screenshot({
    path: "../evidence/dashboard-sidebar-collapsed.png",
    fullPage: true,
  });
  await collapsedSettings.click();
  await page.getByRole("button", { name: "Expand sidebar" }).click();
  await page.getByRole("button", { name: "Settings" }).click();
  await page.getByLabel("Theme").selectOption("nord");
  await page.getByRole("checkbox", { name: "Show seconds in durations" }).uncheck();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "nord");
  await page.screenshot({
    path: "../evidence/dashboard-theme-settings.png",
    fullPage: true,
  });
  await page.getByLabel("Theme").selectOption("catppuccin-latte");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "catppuccin-latte");
  // The only light theme: it must actually render light, and every state
  // tone used as text — and ink on every filled shape — must stay readable
  // (WCAG AA 4.5:1). Tokens are resolved by the browser, color-mix included.
  const latte = await page.evaluate(() => {
    const probe = document.createElement("span");
    document.body.append(probe);
    const resolve = (token: string) => {
      probe.style.color = `var(${token})`;
      const value = getComputedStyle(probe).color;
      const parts = (value.match(/-?\d*\.?\d+/g) ?? []).map(Number);
      return value.startsWith("color(") ? parts.slice(0, 3).map((c) => c * 255) : parts.slice(0, 3);
    };
    const luminance = ([r, g, b]: number[]) => {
      const [lr, lg, lb] = [r, g, b].map((c) => {
        const s = c / 255;
        return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
      });
      return 0.2126 * lr + 0.7152 * lg + 0.0722 * lb;
    };
    const contrast = (a: string, b: string) => {
      const [la, lb] = [luminance(resolve(a)), luminance(resolve(b))];
      return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
    };
    const onSurface = ["--ink", "--ink-muted", "--ok", "--running", "--fail", "--info", "--meta",
      "--link", "--accent", "--tier-luna", "--tier-terra", "--tier-sol", "--tier-high"];
    const pairs: Record<string, number> = {};
    for (const token of onSurface) pairs[`${token} on --bg`] = contrast(token, "--bg");
    for (const fill of ["--accent", "--ok"]) pairs[`--ink-on-accent on ${fill}`] = contrast("--ink-on-accent", fill);
    for (const fill of ["--running", "--series-1", "--series-2", "--series-3", "--series-4"]) {
      pairs[`--ink-on-fill on ${fill}`] = contrast("--ink-on-fill", fill);
    }
    const bg = luminance(resolve("--bg"));
    probe.remove();
    return { bg, pairs, scheme: getComputedStyle(document.documentElement).colorScheme };
  });
  expect(latte.scheme).toBe("light");
  expect(latte.bg).toBeGreaterThan(0.75);
  const illegible = Object.entries(latte.pairs).filter(([, ratio]) => ratio < 4.5);
  expect(illegible).toEqual([]);
  await page.screenshot({
    path: "../evidence/dashboard-theme-catppuccin-latte.png",
    fullPage: true,
  });
  await page.getByLabel("Theme").selectOption("cosmic-dusk");
  await page.getByRole("button", { name: "Settings" }).click();
  await page.getByRole("button", { name: "Quota", exact: true }).click();
  await page.screenshot({
    path: "../evidence/dashboard-issues-table.png",
    fullPage: true,
  });

  const costHeader = page.locator('th[data-column-key="cost"]');
  await costHeader.focus();
  await page.keyboard.press("Alt+ArrowLeft");
  await expect(page.locator(".issue-list-table th").first()).toHaveAttribute("data-column-key", "cost");
  await expect(page.locator(".issue-list-table tbody tr").first().locator("td").first())
    .toHaveText("$2.00");
  expect((await costHeader.boundingBox())!.width).toBeLessThan(320);
  expect((await page.locator('th[data-column-key="issue"]').boundingBox())!.width)
    .toBeGreaterThanOrEqual(320);
  await page.keyboard.press("Alt+ArrowRight");
  await expect(page.locator(".issue-list-table th").first()).toHaveAttribute("data-column-key", "issue");

  const issueColumn = page.locator('th[data-column-key="issue"]');
  const issueBefore = await issueColumn.boundingBox();
  const issueHandle = page.getByRole("separator", { name: "Resize Issue / PR column" });
  const handleBox = await issueHandle.boundingBox();
  expect(issueBefore).not.toBeNull();
  expect(handleBox).not.toBeNull();
  const handleX = handleBox!.x + 1;
  const handleY = handleBox!.y + handleBox!.height / 2;
  await page.mouse.move(handleX, handleY);
  await page.mouse.down();
  await page.mouse.move(handleX - 36, handleY);
  await page.mouse.up();
  const issueAfter = await issueColumn.boundingBox();
  expect(issueAfter!.width).toBeLessThan(issueBefore!.width);
  expect(issueAfter!.width).toBeGreaterThanOrEqual(112);

  const issueHeader = page.locator('th[data-column-key="issue"]');
  await expect(issueHeader).toHaveAttribute("aria-sort", "none");
  await page.getByRole("button", { name: "Sort by Issue" }).click();
  await expect(issueHeader).toHaveAttribute("aria-sort", "ascending");
  await page.getByRole("button", { name: "Sort by Issue" }).click();
  await expect(issueHeader).toHaveAttribute("aria-sort", "descending");

  await page.getByRole("tab", { name: /Backlog/ }).click();
  await expect(page.getByRole("columnheader", { name: /Type/ })).toBeVisible();
  await expect(page.getByRole("columnheader", { name: /Difficulty/ })).toBeVisible();
  await expect(page.getByRole("columnheader", { name: /Impact/ })).toBeVisible();
  await expect(page.getByRole("row", { name: /Toast notifications.*feature.*medium.*high/ }))
    .toBeVisible();
  await page.screenshot({
    path: "../evidence/dashboard-backlog-columns.png",
    fullPage: true,
  });
  await page.getByRole("button", { name: "Promote #120 to ai-ready" }).click();
  await page.getByRole("button", { name: "Promote #120", exact: true }).click();
  await expect(page.getByText("Toast notifications for state transitions")).toHaveCount(0);
  await expect(page.getByRole("tab", { name: /Backlog/ })).toContainText("0");

  await page.getByRole("tab", { name: /In flight/ }).click();
  await expect(page.getByRole("region", { name: "Working" }).locator(".board-row", { hasText: "#120" }))
    .toContainText("Queued");
  await page.locator(".board-row", { hasText: "Exercise model wrapping" }).click();
  await expect(page.locator("#board-drawer-49").getByText("$1.80 est.")).toBeVisible();
  await expect(page.locator("#board-drawer-49 .board-visit")).toHaveCount(2);
  await page.screenshot({
    path: "../evidence/dashboard-inflight-models.png",
    fullPage: true,
  });
  await page.getByRole("tab", { name: /Done/ }).click();

  await page.getByRole("row", { name: /Explain completed work.*#50/ }).click();
  await page.getByRole("button", { name: "Open full issue" }).click();
  await expect(page).toHaveURL(/#\/repo\/self\/issue\/50$/);
  await expect(page.locator(".issue-hd .chip", { hasText: "done" })).toHaveCount(1);
  await expect(page.locator(".issue-hd .chip", { hasText: "resolving" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: /PR #75/ })).toHaveAttribute(
    "href",
    "https://github.com/openai/autonomous-issue-agent/pull/75",
  );
  await expect(page.getByRole("link", { name: "#50" })).toHaveAttribute(
    "href",
    "https://github.com/openai/autonomous-issue-agent/issues/50",
  );
  await expect(page.getByRole("link", { name: "#50" })).toHaveAttribute("target", "_blank");
  await expect(page.getByText(/issue on GitHub/i)).toHaveCount(0);
  await expect(page.locator(".step-disclosure").first()).toContainText("Resolve");
  await expect(page.getByText("1:17", { exact: true })).toHaveCount(1);

  await page.locator('th[data-column-key="operation"]').click({ button: "right" });
  await page.getByRole("checkbox", { name: "Role" }).check();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("columnheader", { name: /Role/ })).toBeVisible();
  for (const [name, key, minimum] of [
    ["Runtime", "runtime", 90],
    ["Outcome", "status", 96],
    ["Duration", "duration", 84],
    ["Cost", "cost", 76],
  ] as const) {
    const separator = page.getByRole("separator", { name: `Resize ${name} column` });
    await separator.focus();
    for (let index = 0; index < 12; index += 1) await page.keyboard.press("ArrowLeft");
    await expect(separator).toHaveAttribute("aria-valuenow", String(minimum));
    const header = page.locator(`th[data-column-key="${key}"]`);
    const renderedWidth = (await header.boundingBox())!.width;
    expect(renderedWidth).toBeGreaterThanOrEqual(minimum);
    expect(renderedWidth).toBeLessThanOrEqual(minimum + 24);
  }
  await page.screenshot({
    path: "../evidence/dashboard-issue-columns.png",
    fullPage: true,
  });

  await page.locator(".step-disclosure").first().click();
  const taskDetail = page.locator(".stepdetail");
  await expect(taskDetail).toBeVisible();
  const taskDetailBox = await taskDetail.boundingBox();
  expect(taskDetailBox?.width).toBeGreaterThan(700);
  await expect(taskDetail.getByText("session-50-0")).toBeVisible();
  await expect(taskDetail.getByText("Added the task narrative to the issue page.")).toBeVisible();

  await taskDetail.getByRole("tab", { name: "Raw" }).click();
  await expect(taskDetail.getByText(/leg-50-0/)).toBeVisible();

  await page.getByRole("tab", { name: "Overview" }).click();
  await expect(page.getByRole("row", { name: /2026-07-29.*3.*1.*\+2/ })).toBeVisible();
  await mainbody.evaluate((element) => {
    element.scrollTop = element.scrollHeight;
  });
  const overviewScroll = await mainbody.evaluate((element) => ({
    top: element.scrollTop,
    max: element.scrollHeight - element.clientHeight,
  }));
  expect(overviewScroll.top).toBe(overviewScroll.max);
  await expect(page.locator(".sidebar-foot")).toBeInViewport();
  await page.getByRole("tab", { name: /Done/ }).click();
  await expect(page.getByRole("tab", { name: /Done/ })).toHaveAttribute("aria-selected", "true");

  await page.getByRole("button", { name: "Verification ledger" }).click();
  // getByText takes no `selector` option; scope to the view's own title (the sidebar and mainbar repeat the name).
  await expect(page.locator(".section-title span", { hasText: "Verification ledger" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Verification" })).toHaveCount(0);
  await expect(page.getByRole("tab", { name: "Overview" })).toHaveCount(0);
});

test("captures the round-three operator states at 1440px", async ({ page }) => {
  await mockApi(page, 0, {
    issuesPayload: round3Issues,
    overviewPayload: round3Overview,
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/#/repo/self/overview");

  await expect(page.getByText("Needs attention")).toHaveCount(0);
  await expect(page.getByRole("region", { name: "Spending caps" })).toContainText("$6.42");
  const daemonHealthPanel = page.locator(".panel-title", { hasText: "Daemon health" });
  await expect(daemonHealthPanel).toHaveCount(0);
  await page.getByRole("button", { name: "Settings" }).click();
  const daemonHealth = page.getByRole("checkbox", { name: "Daemon health" });
  await expect(daemonHealth).not.toBeChecked();
  await daemonHealth.check();
  await expect(daemonHealthPanel).toBeVisible();
  await page.getByRole("button", { name: "Settings" }).click();
  await expect(page.getByText("last", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("searchbox", { name: "Filter operator actions" })).toHaveCount(0);
  await expect(page.getByText("Active lifecycle")).toHaveCount(0);
  await page.screenshot({
    path: "../evidence-facelift/after-overview.png",
    fullPage: true,
    animations: "disabled",
  });
  const spendBar = page.locator('.hist-col[data-tip*="$0.12 spent"]').first();
  await spendBar.scrollIntoViewIfNeeded();
  await spendBar.hover();
  await expect(page.locator(".tip.tip-on")).toContainText("$0.12 spent");
  await page.locator(".mainbody").evaluate((element) => {
    element.scrollTop = 0;
  });

  await page.getByRole("tab", { name: /In flight/ }).click();
  const needsYou = page.getByRole("region", { name: "Needs you" });
  const activeIssueRow = needsYou.locator(".board-row", { hasText: "Required-test gate rejects diagnostic output" });
  await expect(activeIssueRow.locator(".board-headline")).toHaveText("Answer the agent's question");
  await expect(activeIssueRow.getByText("round 4")).toHaveAttribute("data-tip", "4 or more review rounds: above typical");
  await activeIssueRow.click();
  await expect(activeIssueRow.locator(".board-title-btn")).toHaveAttribute("aria-expanded", "true");
  await expect(page.locator("body")).not.toContainText("↗");
  const mainbodyDimensions = await page.locator(".mainbody").evaluate((element) => ({
    clientWidth: element.clientWidth,
    scrollWidth: element.scrollWidth,
  }));
  expect(mainbodyDimensions.scrollWidth).toBe(mainbodyDimensions.clientWidth);
  await page.screenshot({
    path: "../evidence-facelift/after-issues-inflight.png",
    fullPage: true,
    animations: "disabled",
  });

  await page.getByRole("tab", { name: /Done/ }).click();
  await expect(page.locator(".lifecycle")).toHaveCount(0);
  await page.screenshot({
    path: "../evidence-facelift/after-issues-done.png",
    fullPage: true,
    animations: "disabled",
  });

  await page.getByRole("tab", { name: /Backlog/ }).click();
  await expect(page.getByText("Add OAuth token refresh diagnostics")).toBeVisible();
  await page.screenshot({
    path: "../evidence-facelift/after-issues-backlog.png",
    fullPage: true,
    animations: "disabled",
  });

  await page.setViewportSize({ width: 320, height: 700 });
  await page.getByRole("tab", { name: "Overview" }).click();
  await expect(page.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
  await page.screenshot({
    path: "../evidence-facelift/after-overview-mobile.png",
    fullPage: true,
    animations: "disabled",
  });
  await page.getByRole("tab", { name: /In flight/ }).click();
  await expect(page.getByRole("tab", { name: /In flight/ })).toHaveAttribute("aria-selected", "true");
  await page.screenshot({
    path: "../evidence-facelift/after-issues-inflight-mobile.png",
    fullPage: true,
    animations: "disabled",
  });
});

test("shows a skeleton before the deployment request resolves", async ({ page }) => {
  await mockApi(page, 500);
  await page.goto("/");
  await expect(page.getByLabel("Loading deployments")).toBeVisible();
  await expect(page.locator(".repo-name", { hasText: "self" })).toBeVisible();
});

test("serves a validated operator workflow through FastAPI and the production bundle", async ({
  page,
}) => {
  await page.goto("http://127.0.0.1:4174/#/repo/self/issues/done");
  await page.getByRole("tab", { name: "Overview" }).click();
  await page.getByRole("button", { name: "Settings" }).click();
  const issueHealthTrend = page.getByRole("checkbox", { name: "Issue health trend" });
  await expect(issueHealthTrend).not.toBeChecked();
  await issueHealthTrend.check();
  await page.getByRole("button", { name: "Settings" }).click();
  await expect(
    page.getByRole("row", { name: /2026-07-29.*3.*1.*\+2/ }),
  ).toBeVisible();
  await page.getByRole("tab", { name: /Done/ }).click();
  await page.getByRole("row", { name: /Explain completed work.*#50/ }).click();
  await page.getByRole("button", { name: "Open full issue" }).click();
  await expect(page.getByRole("link", { name: /PR #75/ })).toHaveAttribute(
    "href",
    "https://github.com/openai/autonomous-issue-agent/pull/75",
  );
  await page.locator(".step-disclosure").click();
  await expect(page.locator(".stepdetail")).toBeVisible();
  expect((await page.locator(".stepdetail").boundingBox())?.width).toBeGreaterThan(700);
  await expect(page.locator(".stepdetail").getByText("Explained the completed work.")).toBeVisible();
  await page.screenshot({
    path: "../evidence/dashboard-issue-tasks.png",
    fullPage: true,
  });
});

/**
 * Browser zoom shrinks the viewport in CSS pixels at an unchanged window size,
 * so an operator at 150–200% lands in the narrow-viewport rules without ever
 * resizing the window. The rail's width is React state and its grid track is
 * `auto`; anything that narrows the rail has to narrow the element, or the
 * remembered width overhangs the track and covers the left edge of the board.
 */
test("the rail never covers the board at the viewport widths browser zoom produces", async ({
  page,
}) => {
  await mockApi(page);
  await page.goto("/#/repo/self/issues/in-flight");
  await expect(page.locator(".repo-name", { hasText: "self" })).toBeVisible();

  // 1366 CSS px at 100%, then the same laptop at 125 / 175 / 200%.
  for (const width of [1366, 1093, 781, 683]) {
    await page.setViewportSize({ width, height: 700 });
    const rail = await page.locator(".sidebar").boundingBox();
    const board = await page.locator(".main").boundingBox();
    expect(rail, `no rail at ${width}px`).not.toBeNull();
    expect(board, `no board at ${width}px`).not.toBeNull();
    expect(
      rail!.x + rail!.width,
      `the rail overhangs the board at a ${width}px viewport`,
    ).toBeLessThanOrEqual(board!.x + 0.5);
    expect(
      await page.evaluate(() => {
        const de = document.documentElement;
        return de.scrollWidth - de.clientWidth;
      }),
      `the page scrolls sideways at a ${width}px viewport`,
    ).toBeLessThanOrEqual(0);
  }
});
