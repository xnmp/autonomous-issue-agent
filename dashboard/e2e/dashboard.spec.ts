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
  flags: [],
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
      status: "done",
      exit_reason: "ok",
      duration_s: 4638,
      cost_usd: 2,
      estimated: false,
      session_id: "session-50",
      has_transcript: false,
      turns: 3,
      usage: null,
      ts: "2026-07-29T08:00:00Z",
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

async function mockApi(page: Page, reposDelayMs = 0) {
  let issuePolls = 0;
  await page.route("**/*", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (!path.startsWith("/api/")) return route.continue();
    if (path === "/api/repos") return json(route, repos, reposDelayMs);
    if (path === "/api/usage") return json(route, usage);
    if (path === "/api/repos/self/issues") {
      issuePolls += 1;
      return json(route, {
        ...issues,
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
    if (path === "/api/repos/self/issues/50/steps/0/events")
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
    if (path === "/api/repos/self/overview")
      return json(route, {
        metrics: null,
        cost_mix: null,
        caps: { issue_cap_usd: 5, daily_cap_usd: 10, today_usd: 2 },
        health: [],
        unattributed: Array.from({ length: 30 }, (_, index) => ({
          ts: `2026-07-29T07:${String(index).padStart(2, "0")}:00Z`,
          event: "tick",
          kind: `background-${index}`,
          role: index % 2 === 0 ? "fix" : "review",
        })),
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
  await page.getByRole("button", { name: "Settings", exact: true }).click();

  await page.getByRole("button", { name: "Collapse sidebar" }).click();
  await page.getByRole("button", { name: "Quota", exact: true }).click();
  await expectVerticallyContained(".quota-card");
  await page.getByRole("button", { name: "Quota", exact: true }).click();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expectVerticallyContained(".options-popover");
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByRole("button", { name: "Expand sidebar" }).click();
  await page.setViewportSize({ width: 1440, height: 900 });
  await expect(page.getByRole("tab", { name: /In flight/ })).toBeVisible();
  await expect(page.getByRole("tab", { name: /Backlog/ })).toBeVisible();
  await expect(page.getByText("#/repo/self/issues/done")).toHaveCount(0);
  await expect(page.getByRole("columnheader", { name: /Date/ })).toBeVisible();
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

  const sidebar = page.locator(".sidebar");
  await expect(page.locator(".sidebar-scroll")).toHaveCSS("overflow-y", "auto");
  await expect(page.getByRole("separator", { name: "Resize sidebar" })).toBeVisible();
  await page.getByRole("button", { name: "Collapse sidebar" }).click();
  await expect(sidebar).toHaveClass(/collapsed/);
  await expect(page.getByRole("button", { name: "Quota" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Settings" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Quota" })).toHaveClass(/compact/);
  for (const name of ["Quota", "Settings", "Verification ledger"]) {
    const bounds = await page.getByRole("button", { name, exact: true }).boundingBox();
    expect(bounds?.width).toBeGreaterThanOrEqual(40);
    expect(bounds?.height).toBeGreaterThanOrEqual(40);
  }
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
  const modelList = page.locator(".model-list").first();
  await expect(modelList.locator(".chip")).toHaveCount(3);
  await expect(modelList).toHaveCSS("flex-wrap", "wrap");
  const modelListBounds = await modelList.boundingBox();
  const modelChipBounds = await modelList.locator(".chip").first().boundingBox();
  expect(modelListBounds!.height).toBeGreaterThan(modelChipBounds!.height);
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
  await expect(page.getByText(/issue on GitHub/i)).toHaveCount(0);
  await expect(page.getByText("Resolve", { exact: true })).toBeVisible();
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
    expect((await header.boundingBox())!.width).toBeLessThanOrEqual(minimum + 1);
  }
  await page.screenshot({
    path: "../evidence/dashboard-issue-columns.png",
    fullPage: true,
  });

  await page.locator(".step-disclosure").click();
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
  expect(await page.evaluate(() => document.documentElement.scrollHeight)).toBe(900);
  await expect(page.locator(".sidebar-foot")).toBeInViewport();
  await page.getByRole("tab", { name: /Done/ }).click();
  await expect(page.getByRole("tab", { name: /Done/ })).toHaveAttribute("aria-selected", "true");

  await page.getByRole("button", { name: "Verification ledger" }).click();
  await expect(page.getByText("Verification ledger", { selector: ".section-title span" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Verification" })).toHaveCount(0);
  await expect(page.getByRole("tab", { name: "Overview" })).toHaveCount(0);
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
