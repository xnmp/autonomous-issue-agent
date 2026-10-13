import { expect, test, type Locator, type Page, type Route } from "@playwright/test";

/**
 * The regrouped issues board (#330) against a mocked API: every status in
 * these fixtures is what the server projection sends, so the assertions are
 * on what the operator sees — the group a row lands in, its headline, its
 * rail counts, and a drawer that opens without moving the row.
 */

const NOW = "2026-10-12T12:00:00Z";
const ago = (minutes: number) => new Date(Date.parse(NOW) - minutes * 60_000).toISOString();

const repos = {
  repos: [{
    name: "self",
    repo: "openai/autonomous-issue-agent",
    target_dir: "/tmp/self",
    is_self: true,
    active: true,
    roles: ["fix", "review"].map((name) => ({ role: name, alive: true, stale: false, heartbeat_ts: NOW })),
    frecency: 1,
    last_activity_ts: NOW,
    in_flight: 6,
    done: 0,
  }],
  warnings: [],
};

const usage = { claude: null, codex: null, harness: null, warnings: [] };

const rail = (over: Record<string, unknown> = {}) => ({
  current: "review", furthest: "review", resolve_attempts: 1, review_rounds: 1, ci_runs: 0, steps: 4, live: false, ...over,
});

// Each history is the fold the row's rail summarises: its visit stages, step
// total, review decisions and CI runs agree with that rail.
const step = (kind: string, outcome: string, count = 1) => ({ kind, outcome, count });
const resolved = { stage: "resolve", steps: [step("RESOLVE", "done")] };
const histories: Record<number, unknown[]> = {
  710: [{ stage: "resolve", steps: [step("RESOLVE", "failed", 3)] }],
  1082: [
    resolved,
    { stage: "ci", steps: [step("FIX_CI", "done"), step("FIX_CI", "done")] },
    { stage: "review", steps: [step("REVIEW", "changes_requested"), step("ADDRESS_REVIEW", "done"), step("REVIEW", "passed")] },
    { stage: "ci", steps: [step("FIX_CI", "done")] },
    { stage: "review", steps: [step("REVIEW", "changes_requested", 3), step("ADDRESS_REVIEW", "done"), step("REVIEW", "changes_requested")] },
  ],
  993: [
    resolved,
    { stage: "ci", steps: [step("FIX_CI", "done")] },
    { stage: "review", steps: [step("REVIEW", "changes_requested", 4), step("ADDRESS_REVIEW", "done"), step("REVIEW", "passed")] },
    { stage: "ci", steps: [step("FIX_CI", "done"), step("REBASE", "done")] },
  ],
  1083: [resolved, { stage: "review", steps: [step("REVIEW", "changes_requested"), step("ADDRESS_REVIEW", "running")] }],
};

function card(issue: number, title: string, over: Record<string, unknown>) {
  return {
    issue,
    title,
    url: `https://github.com/openai/autonomous-issue-agent/issues/${issue}`,
    labels: ["ai-ready"],
    pr: null,
    pr_url: null,
    pr_ci_state: null,
    action: null,
    history: histories[issue] ?? [],
    cost_usd: 1.8,
    estimated: false,
    legs: 4,
    started_ts: ago(600),
    last_ts: ago(12),
    leg_running: false,
    ...over,
  };
}

function backlogIssue(issue: number, title: string, labels: string[], ageDays: number) {
  return {
    issue, title, url: `https://github.com/openai/autonomous-issue-agent/issues/${issue}`, body: null, labels,
    age_days: ageDays, age_s: ageDays * 86_400, comments: 0, issue_type: null, difficulty: null, impact: null,
  };
}

const pr = (n: number) => ({ pr: n, pr_url: `https://github.com/openai/autonomous-issue-agent/pull/${n}` });

const boardIssues = {
  in_flight: [
    card(710, "test: window-transfer-lifetime native smoke flake on windows (moved=false / listingHas)", {
      cost_usd: 10.63,
      action: "unblock",
      board: {
        status: "needs_you", reason: "attempts", headline: "Attempts exhausted",
        hint: "The same act ran without progress; reply or unblock to let it try again.",
        detail: "**needs-human**: RESOLVE ran 3 times without opening a PR. Reply with guidance or unblock to let it try again.",
        target: "issue", since_ts: ago(30 * 24 * 60),
      },
      rail: rail({ current: "resolve", furthest: "resolve", resolve_attempts: 3, review_rounds: 0, steps: 3 }),
    }),
    card(1082, "feat(git-graph): create/merge/approve PR actions from the graph", {
      ...pr(1088),
      action: "open",
      board: {
        status: "needs_you", reason: "question", headline: "Answer the agent's question",
        hint: "Reply on GitHub with the answer; the agent resumes with it.",
        detail: "Agent needs input: the merge action needs a confirmation dialog. Should it reuse the existing modal, or a new inline confirm?",
        target: "issue", since_ts: ago(72),
      },
      rail: rail({ current: "review", furthest: "review", review_rounds: 5, ci_runs: 3, steps: 12 }),
    }),
    card(993, "choosing which details, the popup menu is in the wrong place, when zoomed", {
      ...pr(1084),
      pr_ci_state: "pending",
      board: {
        status: "working", activity: "ci", act: null, headline: "CI running",
        reason: "Checks are running on the PR head.", since_ts: ago(41),
      },
      rail: rail({ current: "ci", furthest: "review", review_rounds: 5, ci_runs: 3, steps: 10 }),
    }),
    card(1083, "feat(git-graph): jump to merge-base/fork point of two branches", {
      ...pr(1090),
      leg_running: true,
      board: {
        status: "working", activity: "agent", act: "ADDRESS_REVIEW", headline: "Address review running",
        reason: "An agent leg holds this item.", since_ts: ago(4),
      },
      rail: rail({ current: "review", furthest: "review", review_rounds: 1, steps: 3, live: true }),
    }),
    card(1055, "add command palette command to open git issue list in browser", {
      ...pr(1095),
      board: {
        status: "waiting", on: "branch-protection", headline: "Waiting on branch protection",
        reason: "GitHub blocks the merge until its rules pass.", since_ts: ago(26 * 60),
      },
      rail: rail({ current: "merge", furthest: "merge", review_rounds: 1, steps: 3 }),
    }),
    card(1010, "make it so that in fullscreen for pdf previews, left/right arrows goes to next/previous", {
      labels: ["needs-human"],
      action: "unblock",
      board: {
        status: "needs_you", reason: "labelled", headline: "Parked",
        hint: "Labelled needs-human with no stop comment; unblock to hand it back.",
        detail: null, target: "issue", since_ts: null,
      },
      rail: rail({ current: "resolve", furthest: "resolve", resolve_attempts: 0, review_rounds: 0, steps: 0 }),
      history: [],
    }),
  ],
  done: [],
  backlog: [
    backlogIssue(1101, "settings: remember the last opened folder per window", ["enhancement"], 3),
    backlogIssue(1102, "crash when dragging a tab onto a closed window on macOS", ["bug"], 0),
  ],
  gh_ok: true,
  observed_at: NOW,
  notifications: [],
  warnings: [],
};

async function json(route: Route, body: unknown) {
  await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
}

async function mockApi(page: Page) {
  await page.clock.setFixedTime(new Date(NOW));
  await page.route("**/*", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (!path.startsWith("/api/")) return route.continue();
    if (path === "/api/repos") return json(route, repos);
    if (path === "/api/usage") return json(route, usage);
    if (path === "/api/repos/self/issues") return json(route, boardIssues);
    return json(route, { ok: true, detail: "ok" });
  });
}

async function openBoard(page: Page, theme: "cosmic-dusk" | "catppuccin-latte" = "cosmic-dusk") {
  await page.addInitScript((value) => {
    localStorage.setItem("issue-agent.dashboard.options", JSON.stringify({ theme: value }));
  }, theme);
  await mockApi(page);
  await page.goto("/#/repo/self/issues/inflight");
  await expect(page.getByRole("region", { name: "Needs you" })).toBeVisible();
}

const group = (page: Page, name: string) => page.getByRole("region", { name });
const boardRow = (page: Page, title: string): Locator =>
  page.locator(".board-row").filter({ hasText: title });

async function noSidewaysScroll(page: Page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth))
    .toBeLessThanOrEqual(0);
  const main = page.locator(".mainbody");
  if (await main.count()) {
    expect(await main.evaluate((element) => element.scrollWidth - element.clientWidth)).toBeLessThanOrEqual(0);
  }
}

for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
  test.describe(`issues board at ${viewport.width}px`, () => {
    test.use({ viewport });

    test("lands each row in its group with its headline and rail counts", async ({ page }) => {
      await openBoard(page);
      if (viewport.width < 600) {
        const collapse = page.getByRole("button", { name: "Collapse sidebar" });
        if (await collapse.isVisible()) await collapse.click();
      }

      const needs = group(page, "Needs you");
      await expect(needs.locator(".board-title")).toHaveText([
        /window-transfer-lifetime/, // waited 30 days: longest first
        /create\/merge\/approve PR actions/,
        /fullscreen for pdf previews/, // no wait start: last
      ]);
      await expect(boardRow(page, "window-transfer-lifetime").locator(".board-headline")).toHaveText("Attempts exhausted");
      await expect(boardRow(page, "window-transfer-lifetime").getByText("3 attempts")).toHaveCount(0);
      await expect(boardRow(page, "window-transfer-lifetime").getByText("attempt 3")).toBeVisible();
      await expect(boardRow(page, "window-transfer-lifetime").locator(".board-wait")).toHaveText("30d");

      const asked = boardRow(page, "create/merge/approve PR actions");
      await expect(asked.getByText("round 5")).toHaveClass(/rail-hot/);
      await expect(asked.getByText("3 runs")).toHaveClass(/rail-hot/);
      await expect(asked.locator(".board-reason")).toHaveText("Reply on GitHub with the answer; the agent resumes with it.");

      const working = group(page, "Working");
      await expect(working.locator(".board-headline")).toHaveText(["CI running", "Address review running"]);
      await expect(boardRow(page, "popup menu is in the wrong place").locator(".rail-stage", { hasText: "Review" }))
        .toHaveClass(/rail-rewound/);
      await expect(boardRow(page, "popup menu is in the wrong place").locator(".board-wait")).toHaveText("41m ago");
      await expect(boardRow(page, "jump to merge-base").locator(".rail-stage.rail-current")).toHaveClass(/rail-live/);

      const waiting = group(page, "Waiting");
      await expect(waiting.locator(".board-headline")).toHaveText(["Waiting on branch protection"]);
      await expect(boardRow(page, "fullscreen for pdf previews").locator(".board-say")).toHaveClass(/board-say-owned/);
      await expect(boardRow(page, "branch protection").locator(".board-say")).not.toHaveClass(/board-say-owned/);
      await noSidewaysScroll(page);
    });

    test("opens the drawer without moving the row's text", async ({ page }) => {
      await openBoard(page);
      const row = boardRow(page, "create/merge/approve PR actions");
      const title = row.locator(".board-title");
      const headline = row.locator(".board-headline");
      const before = [await title.boundingBox(), await headline.boundingBox()];

      await row.click();
      const drawer = page.locator("#board-drawer-1082");
      await expect(drawer).toBeVisible();
      await expect(drawer.getByText(/Agent needs input: the merge action needs a confirmation dialog/)).toBeVisible();
      await expect(drawer.getByText("↩ back from Review")).toBeVisible();
      const after = [await title.boundingBox(), await headline.boundingBox()];

      for (const [index, box] of after.entries()) {
        expect(box).not.toBeNull();
        expect(Math.abs(box!.x - before[index]!.x)).toBeLessThanOrEqual(0.5);
        expect(Math.abs(box!.y - before[index]!.y)).toBeLessThanOrEqual(0.5);
      }
      await expect(drawer.getByRole("button", { name: "Show 4 earlier steps" })).toBeVisible();
      await drawer.getByRole("button", { name: "Show 4 earlier steps" }).click();
      await expect(drawer.locator(".board-visit")).toHaveCount(5);
      await expect(drawer.locator(".board-actions").getByRole("link")).toHaveText(["Open issue"]);
      await noSidewaysScroll(page);
    });
  });
}

test("the current node pulses only while a leg runs, and not under reduced motion", async ({ page }) => {
  await openBoard(page);
  const live = boardRow(page, "jump to merge-base").locator(".rail-stage.rail-current .rail-node");
  const idle = boardRow(page, "popup menu is in the wrong place").locator(".rail-stage.rail-current .rail-node");

  await expect(live).toHaveCSS("animation-name", "rail-pulse");
  await expect(idle).toHaveCSS("animation-name", "none");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await expect(live).toHaveCSS("animation-name", "none");
  await expect(live).not.toHaveCSS("box-shadow", "none");
});

for (const theme of ["cosmic-dusk", "catppuccin-latte"] as const) {
  const shade = theme === "catppuccin-latte" ? "light" : "dark";

  test(`captures the board in the ${shade} theme`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 1700 });
    await openBoard(page, theme);
    await page.emulateMedia({ reducedMotion: "reduce" });

    await boardRow(page, "window-transfer-lifetime").click();
    await expect(page.locator("#board-drawer-710").getByText(/RESOLVE ran 3 times without opening a PR/)).toBeVisible();
    await page.screenshot({ path: `../evidence/dashboard-issues-board-${shade}.png`, fullPage: true, animations: "disabled" });

    await page.getByRole("button", { name: /Needs you/ }).first().click();
    await expect(page.getByRole("region", { name: "Working" })).toHaveCount(0);
    await page.screenshot({ path: `../evidence/dashboard-issues-board-${shade}-filter.png`, fullPage: true, animations: "disabled" });
  });
}

test("captures the board at phone width with a drawer open", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await openBoard(page);
  await page.emulateMedia({ reducedMotion: "reduce" });
  const collapse = page.getByRole("button", { name: "Collapse sidebar" });
  if (await collapse.isVisible()) await collapse.click();
  await boardRow(page, "create/merge/approve PR actions").click();
  await expect(page.locator("#board-drawer-1082")).toBeVisible();
  await noSidewaysScroll(page);
  await page.screenshot({ path: "../evidence/dashboard-issues-board-phone.png", fullPage: true, animations: "disabled" });
});

test("opens a row from the keyboard through its title button", async ({ page }) => {
  await openBoard(page);
  const toggle = boardRow(page, "window-transfer-lifetime").getByRole("button", { name: /window-transfer-lifetime/ });

  await toggle.focus();
  await page.keyboard.press("Enter");

  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await expect(page.locator("#board-drawer-710")).toBeVisible();
  await expect(toggle).toHaveAccessibleDescription(
    /^Attempts exhausted The same act ran without progress; reply or unblock to let it try again\. .*Resolve,? attempt 3.* Waiting: 30d$/,
  );
});

test("lists backlog issues on their own tab, off the board, each promotable", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await openBoard(page);
  await expect(page.getByRole("tab", { name: /Backlog/ })).toContainText("2");
  await expect(page.locator(".board-row", { hasText: "remember the last opened folder" })).toHaveCount(0);

  await page.getByRole("tab", { name: /Backlog/ }).click();
  await expect(page.getByText("remember the last opened folder per window")).toBeVisible();
  await expect(page.getByText("crash when dragging a tab")).toBeVisible();
  await noSidewaysScroll(page);
  await page.screenshot({ path: "../evidence/dashboard-issues-backlog.png", fullPage: true, animations: "disabled" });

  await page.getByRole("button", { name: "Promote #1101 to ai-ready" }).click();
  const promoted = page.waitForRequest((request) => request.method() === "POST" && request.url().endsWith("/issues/1101/promote"));
  await page.getByRole("button", { name: "Promote #1101", exact: true }).click();
  await promoted;
});
