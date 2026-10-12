import { expect, test, type Page, type Route } from "@playwright/test";

const role = (name: string) => ({
  role: name,
  alive: true,
  stale: false,
  last_event_ts: "2026-07-29T08:00:00Z",
  last_event: "tick",
  events: 4,
  log_path: `/logs/${name}.log`,
});

const repos = {
  repos: [
    {
      name: "self",
      repo: "openai/autonomous-issue-agent",
      target_dir: "/tmp/self",
      is_self: true,
      active: true,
      roles: [role("fix"), role("review"), role("steward")],
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
    global_daily_cap_usd: 50,
    today_all_repos_usd: 2,
    per_repo: [
      { name: "self", repo: "openai/autonomous-issue-agent", daily_cap_usd: 10, today_usd: 2 },
    ],
  },
  warnings: [],
};

const issueCard = {
  issue: 50,
  title: "Explain completed work",
  url: "https://github.com/openai/autonomous-issue-agent/issues/50",
  pr: 75,
  pr_url: "https://github.com/openai/autonomous-issue-agent/pull/75",
  pr_state: "merged",
  stage: "done",
  done: true,
  cost_usd: 2,
  estimated: false,
  elapsed_s: 4700,
  working_s: 4638,
  idle_s: 62,
  idle_active: false,
  started_ts: "2026-07-29T07:00:00Z",
  last_ts: "2026-07-29T08:00:00Z",
  merged_ts: "2026-07-29T08:00:00Z",
  projected_cost_usd: 2,
  projected_model: "gpt-5.6-terra",
  projected_eta_s: 60,
  rounds: 1,
  models: ["gpt-5.6-terra"],
  model_badges: [{ model: "gpt-5.6-terra", effort: "medium" }],
  issue_verification: "VERIFIED",
  pr_verification: "pending",
  steps: [{ kind: "RESOLVE", state: "ok", status: "done", cost_usd: 2 }],
};

const issues = {
  in_flight: [{
    ...issueCard,
    issue: 49,
    title: "Exercise model wrapping",
    url: "https://github.com/openai/autonomous-issue-agent/issues/49",
    pr: 74,
    pr_url: "https://github.com/openai/autonomous-issue-agent/pull/74",
    pr_state: "open",
    stage: "reviewing",
    done: false,
    merged_ts: null,
    leg_running: true,
    board: {
      status: "working", activity: "agent", headline: "Reviewing",
      reason: "The reviewer is reading the patch.", since_ts: "2026-07-29T08:00:00Z",
    },
    rail: {
      current: "review", furthest: "review", resolve_attempts: 1, review_rounds: 1,
      ci_runs: 0, steps: 2, live: true,
    },
    models: ["gpt-5.6-terra", "gpt-5.6-fable", "gpt-5.6-luna"],
    model_badges: [
      { model: "gpt-5.6-terra", effort: "medium" },
      { model: "gpt-5.6-fable", effort: "high" },
      { model: "gpt-5.6-luna", effort: "medium" },
    ],
  }],
  pending: [],
  done: [
    issueCard,
    ...Array.from({ length: 18 }, (_, index) => ({
      ...issueCard,
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
    recurrence: false,
    recurrence_count: 0,
    steward_state: "raw",
    age_days: 0,
    age_s: 1800,
    comments: 2,
    issue_type: "feature",
    difficulty: "medium",
    impact: "high",
  }],
  gh_ok: true,
  notifications: [],
  warnings: [],
};

const overview = {
  metrics: null,
  cost_mix: null,
  caps: { issue_cap_usd: 5, daily_cap_usd: 10, today_usd: 2 },
  health: [],
  unattributed_count: 30,
  needs_attention: [],
  interventions: [],
  spend_per_hour: [],
  escalation_reasons: [],
  health_trend: [{
    day: "2026-07-29",
    opened: 3,
    closed: 1,
    net_open: 2,
    recurrences: 0,
    first_observation: true,
  }],
  warnings: [],
};

const round3Issues = {
  ...issues,
  in_flight: [{
    ...issues.in_flight[0],
    issue: 457,
    title: "Required-test gate rejects diagnostic output",
    url: "https://github.com/openai/autonomous-issue-agent/issues/457",
    pr: 482,
    pr_url: "https://github.com/openai/autonomous-issue-agent/pull/482",
    stage: "reviewing",
    escalation_reason: "Harness issue: required-test gate rejects diagnostic output after the full acceptance plan was supplied; operator decision is required before another review round",
    leg_running: true,
    board: {
      status: "needs_you", ask: "escalated", headline: "Answer the agent",
      reason: "Harness issue: required-test gate rejects diagnostic output after the full acceptance plan was supplied; operator decision is required before another review round",
      since_ts: "2026-07-29T07:30:00Z",
    },
    rail: {
      current: "review", furthest: "ci", resolve_attempts: 1, review_rounds: 4,
      ci_runs: 1, steps: 10, live: true,
    },
    rounds: 3,
    steps: [
      { kind: "RESOLVE", state: "ok", status: "done", cost_usd: 1.22, count: 1 },
      { kind: "REVIEW", state: "ok", status: "REQUEST_CHANGES", cost_usd: 0.48, count: 2 },
      { kind: "ADDRESS_REVIEW", state: "ok", status: "done", cost_usd: 0.31, count: 1 },
      { kind: "RE_REVIEW", state: "ok", status: "REQUEST_CHANGES", cost_usd: 0.36, count: 1 },
      { kind: "ADDRESS_REVIEW", state: "ok", status: "done", cost_usd: 0.28, count: 1 },
      { kind: "RE_REVIEW", state: "ok", status: "REQUEST_CHANGES", cost_usd: 0.34, count: 1 },
      { kind: "ADDRESS_REVIEW", state: "ok", status: "done", cost_usd: 0.27, count: 1 },
      { kind: "RE_REVIEW", state: "ok", status: "REQUEST_CHANGES", cost_usd: 0.33, count: 1 },
      { kind: "FIX_CI", state: "ok", status: "done", cost_usd: 0.22, count: 1 },
      { kind: "ADDRESS_REVIEW", state: "running", status: "running", cost_usd: 0.31, count: 1 },
    ],
  }],
  backlog: [
    {
      ...issues.backlog[0],
      issue: 119,
      title: "Add OAuth token refresh diagnostics",
      url: "https://github.com/openai/autonomous-issue-agent/issues/119",
      labels: ["ai-triage", "feature", "effort:medium", "impact:high"],
      steward_state: "processed",
    },
    issues.backlog[0],
  ],
};

const round3Overview = {
  ...overview,
  first_observed_day: "2026-07-22",
  metrics: {
    by_kind: [],
    cost_per_done_usd: 1.84,
    done_issues: 19,
    escalations: 2,
    first_pass_approvals: 8,
    first_pass_changes: 3,
    first_pass_rate: 8 / 11,
    in_flight_issues: 2,
    interventions_by_class: [
      { name: "rearm", count: 1 },
      { name: "policy", count: 1 },
      { name: "other", count: 0 },
    ],
    interventions_per_merged_pr: 0.11,
    interventions_total: 2,
    recurrences: 0,
    rounds_per_pr: 1.32,
    spend_blocks: 1,
    today_usd: 6.42,
    total_cost_usd: 34.96,
  },
  caps: { issue_cap_usd: 10, daily_cap_usd: 25, today_usd: 6.42 },
  needs_attention: [
    {
      issue: 457,
      kind: "escalated",
      title: "Required-test gate rejects diagnostic output",
      detail: "operator decision required before another review round",
    },
    {
      issue: 462,
      kind: "blocked",
      title: "Choose the event retention boundary",
      detail: "waiting for the retention window",
    },
  ],
  health: ["fix", "review", "steward"].map((name, index) => ({
    ...role(name),
    last_event_ts: `2026-08-09T07:${55 + index}:00Z`,
  })),
  interventions: [
    {
      ts: "2026-08-09T07:42:00Z",
      action_class: "guidance",
      issue: 457,
      pr: 482,
      kind: "rearm review",
      detail: "supplied the required-test acceptance plan",
    },
    {
      ts: "2026-08-09T06:18:00Z",
      action_class: "policy",
      issue: 462,
      pr: null,
      kind: "promote backlog issue",
      detail: "acceptance criteria and test plan approved",
    },
  ],
  spend_per_hour: Array.from({ length: 24 }, (_, index) => ({
    hour_start_iso: `2026-08-${index < 16 ? "08" : "09"}T${String((index + 8) % 24).padStart(2, "0")}:00:00Z`,
    usd: [0, 0.12, 0.35, 0.08, 0, 0.61][index % 6] ?? 0,
  })),
  health_trend: Array.from({ length: 7 }, (_, index) => ({
    day: `2026-08-0${index + 3}`,
    opened: 2 + (index % 3),
    closed: 1 + (index % 2),
    net_open: 1,
    recurrences: 0,
    first_observation: index === 0,
  })),
  unattributed_count: 3,
};

const issue = {
  issue: 50,
  title: "Explain completed work",
  url: "https://github.com/openai/autonomous-issue-agent/issues/50",
  stage: "done",
  done: true,
  prs: [75],
  pull_requests: [
    {
      number: 75,
      url: "https://github.com/openai/autonomous-issue-agent/pull/75",
      state: "merged",
    },
  ],
  cost_usd: 2,
  elapsed_s: 4700,
  working_s: 4638,
  idle_s: 62,
  idle_active: false,
  steps: [
    {
      idx: 0,
      kind: "RESOLVE",
      role: "fix",
      state: "ok",
      model: "claude-sonnet-5",
      effort: "medium",
      runtime: "codex",
      status: "done",
      exit_reason: "ok",
      duration_s: 4638,
      cost_usd: 2,
      estimated: false,
      pricing_basis: "configured_rate_card",
      budget_cap_usd: 10,
      session_id: "session-50",
      has_transcript: false,
      turns: 3,
      usage: null,
      ts: "2026-07-29T08:00:00Z",
      change: {
        pr: 75,
        branch: "feat/50-explain-work",
        head_sha: "a123456",
        commit_subjects: ["red: reproduce missing task narrative"],
        files_changed_count: 6,
      },
    },
    {
      idx: 1,
      kind: "REVIEW",
      role: "review",
      state: "ok",
      model: "gpt-5.6-terra",
      effort: "medium",
      runtime: "codex",
      status: "REQUEST_CHANGES",
      exit_reason: "ok",
      duration_s: 92,
      cost_usd: 0.42,
      estimated: true,
      pricing_basis: "learned_estimate",
      budget_cap_usd: 8,
      session_id: "review-50-r2",
      has_transcript: false,
      turns: 7,
      usage: null,
      ts: "2026-07-29T08:02:00Z",
      note: "REQUEST_CHANGES round 2",
      review: {
        verdict_excerpt: "The implementation still hides the actual review findings. Surface the requested-change body as untrusted text and preserve the rejection and spend context so an operator can decide whether another round is productive.",
        rejection: 2,
        unproductive_rounds: 0,
        unproductive_cap: 5,
      },
    },
    {
      idx: 2,
      kind: "ADDRESS_REVIEW",
      role: "fix",
      state: "ok",
      model: "gpt-5.6-terra",
      effort: "high",
      runtime: "codex",
      status: "done",
      exit_reason: "ok",
      duration_s: 186,
      cost_usd: 0.88,
      estimated: false,
      pricing_basis: "configured_rate_card",
      budget_cap_usd: 10,
      session_id: "session-50-r2",
      has_transcript: false,
      turns: 11,
      usage: null,
      ts: "2026-07-29T08:06:00Z",
      escalation: {
        notches: 1,
        from_effort: "medium",
        to_effort: "high",
        after_review_round: 2,
      },
      change: {
        pr: 75,
        branch: "feat/50-explain-work",
        head_sha: "b123456",
        commit_subjects: ["green: expose verdict and spend context"],
        files_changed_count: 9,
      },
    },
    {
      idx: 3,
      kind: "RE_REVIEW",
      role: "review",
      state: "ok",
      model: "gpt-5.6-terra",
      effort: "medium",
      runtime: "codex",
      status: "APPROVE",
      exit_reason: "ok",
      duration_s: 74,
      cost_usd: 0.31,
      estimated: false,
      pricing_basis: "configured_rate_card",
      budget_cap_usd: 8,
      session_id: "review-50-r3",
      has_transcript: false,
      turns: 5,
      usage: null,
      ts: "2026-07-29T08:08:00Z",
      note: "APPROVE round 3",
      review: {
        verdict_excerpt: null,
        rejection: null,
        unproductive_rounds: 0,
        unproductive_cap: 5,
      },
    },
  ],
  events: [
    {
      ts: "2026-07-29T07:59:00Z",
      event: "action",
      role: "steward",
      desc: "comment issue #50: verification recorded",
    },
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
          id: "merge-50",
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
    if (path === "/api/repos/self/issues/49")
      return json(route, {
        ...issue,
        issue: 49,
        title: "Exercise model wrapping",
        stage: "reviewing",
        done: false,
        prs: [74],
        pull_requests: [{
          number: 74,
          url: "https://github.com/openai/autonomous-issue-agent/pull/74",
          state: "open",
        }],
      });
    const detail = /^\/api\/repos\/self\/issues\/(\d+)$/.exec(path);
    if (detail) return json(route, { ...issue, issue: Number(detail[1]), done: false, prs: [], pull_requests: [] });
    if (path.startsWith("/api/repos/self/issues/50/steps/") && path.endsWith("/events"))
      return json(route, {
        events: [{
          ts: "2026-07-29T07:59:30Z",
          kind: "RESOLVE",
          role: "fix",
          status: "done",
          model: "claude-sonnet-5",
          issue: 50,
        }],
        warnings: [],
      });
    if (path === "/api/verification")
      return json(route, {
        rows: [{ id: "V-1", change: "Dashboard", status: "verified", evidence: "E2E", shipped: "yes", raw: "" }],
        warnings: [],
      });
    if (path === "/api/repos/self/overview") return json(route, overviewPayload);
    return json(route, { ok: true, detail: "started" });
  });
}

test("operator workflow preserves semantics and navigation", async ({ page }) => {
  await mockApi(page);
  await page.setViewportSize({ width: 1280, height: 560 });
  await page.goto("/#/repo/self/issues/done");

  await expect(page.locator(".chip strong", { hasText: "issue-agent" })).toBeVisible();
  await page.locator(".rail-past > summary").click();
  await expect(page.getByRole("button", { name: "Start daemons for sleeping" })).toBeVisible();
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
  await page.getByRole("button", { name: "Quota", exact: true }).click();
  await expectVerticallyContained(".quota-card");
  await page.getByRole("button", { name: "Quota", exact: true }).click();
  await page.getByRole("button", { name: "Expand sidebar" }).click();
  await page.setViewportSize({ width: 1440, height: 900 });
  await expect(page.getByRole("tab", { name: /In flight/ })).toBeVisible();
  await expect(page.getByRole("tab", { name: /Backlog/ })).toBeVisible();
  await expect(page.getByText("#/repo/self/issues/done")).toHaveCount(0);
  await expect(page.getByRole("columnheader", { name: /Since done/ })).toBeVisible();
  await expect(page.getByRole("columnheader", { name: /^Date/ })).toHaveCount(0);
  await expect(page.locator(".duration-breakdown").first()).toContainText("1:17:18");

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
  await expect(page.getByRole("button", { name: "Quota" })).toBeVisible();
  await page.getByRole("button", { name: "Quota", exact: true }).click();
  await expect(page.getByRole("region", { name: "Quota" })).toHaveClass(/compact/);
  const expandBounds = await page.getByRole("button", { name: "Expand sidebar" }).boundingBox();
  const repoTitleBounds = await page.locator(".repo-context-name").boundingBox();
  expect(expandBounds).not.toBeNull();
  expect(repoTitleBounds).not.toBeNull();
  expect(expandBounds!.x + expandBounds!.width).toBeLessThanOrEqual(repoTitleBounds!.x);
  const collapsedQuotaBounds = await page.getByRole("button", { name: "Quota", exact: true }).boundingBox();
  expect(collapsedQuotaBounds?.width).toBeGreaterThanOrEqual(40);
  expect(collapsedQuotaBounds?.height).toBeGreaterThanOrEqual(40);
  const compactTrack = page.locator(".compact-quota-track").first();
  const compactTrackBounds = await compactTrack.boundingBox();
  expect(compactTrackBounds!.width).toBeGreaterThan(compactTrackBounds!.height);
  await page.screenshot({
    path: "../evidence/dashboard-sidebar-collapsed.png",
    fullPage: true,
  });
  await page.getByRole("button", { name: "Quota", exact: true }).click();
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
  await expect(page.locator(".duration-breakdown").first()).toContainText("1:17");
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

  const durationHeader = page.locator('th[data-column-key="duration"]');
  const durationBefore = await durationHeader.boundingBox();
  const durationHandle = page.getByRole("separator", { name: "Resize Duration column" });
  const handleBox = await durationHandle.boundingBox();
  expect(durationBefore).not.toBeNull();
  expect(handleBox).not.toBeNull();
  const handleX = handleBox!.x + 1;
  const handleY = handleBox!.y + handleBox!.height / 2;
  await page.mouse.move(handleX, handleY);
  await page.mouse.down();
  await page.mouse.move(handleX - 36, handleY);
  await page.mouse.up();
  const durationAfter = await durationHeader.boundingBox();
  expect(durationAfter!.width).toBeLessThan(durationBefore!.width);
  expect(durationAfter!.width).toBeGreaterThanOrEqual(112);

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
  await expect(page.getByRole("region", { name: "Blocked" }).locator(".board-row", { hasText: "#120" }))
    .toContainText("Queued");
  await page.locator(".board-row", { hasText: "Exercise model wrapping" }).click();
  await expect(page.locator("#board-drawer-49").getByText("gpt-5.6-terra, gpt-5.6-fable, gpt-5.6-luna"))
    .toBeVisible();
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
  await expect(page.getByText("Luna", { exact: true })).toHaveCount(0);
  await expect(page.getByText("1:17", { exact: true })).toHaveCount(2);

  await page.locator('th[data-column-key="operation"]').click({ button: "right" });
  await page.getByRole("checkbox", { name: "Model" }).check();
  await page.keyboard.press("Escape");
  for (const [name, minimum] of [
    ["Model", 100],
    ["Status", 96],
    ["Duration", 84],
    ["Cost", 76],
  ] as const) {
    const separator = page.getByRole("separator", { name: `Resize ${name} column` });
    await separator.focus();
    for (let index = 0; index < 12; index += 1) await page.keyboard.press("ArrowLeft");
    await expect(separator).toHaveAttribute("aria-valuenow", String(minimum));
    const header = page.locator(`th[data-column-key="${name.toLowerCase()}"]`);
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
  await expect(taskDetail.getByText("claude-sonnet-5")).toBeVisible();

  await page.getByRole("tab", { name: /^Events ·/ }).click();
  await expect(page.getByText("by steward")).toBeVisible();
  await expect(page.getByText(/comment issue #50/)).toBeVisible();

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
  await expect(activeIssueRow.locator(".board-headline")).toHaveText("Answer the agent");
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
  await expect(page.getByText("AC + test plan written")).toBeVisible();
  await expect(page.getByText("awaiting steward")).toBeVisible();
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
  await page.getByRole("tab", { name: /^Events ·/ }).click();
  await expect(page.getByText("by steward")).toBeVisible();
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
