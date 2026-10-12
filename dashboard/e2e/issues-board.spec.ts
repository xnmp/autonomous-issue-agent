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
    roles: ["fix", "review", "steward"].map((name) => ({
      role: name, alive: true, stale: false, last_event_ts: NOW, last_event: "tick", events: 4, log_path: `/logs/${name}.log`,
    })),
    frecency: 1,
    last_activity_ts: NOW,
    in_flight: 4,
    done: 0,
  }],
  warnings: [],
};

const usage = { claude: null, codex: null, harness: null, warnings: [] };

const rail = (over: Record<string, unknown> = {}) => ({
  current: "review", furthest: "review", resolve_attempts: 1, review_rounds: 1, ci_runs: 0, steps: 4, live: false, ...over,
});

function card(issue: number, title: string, over: Record<string, unknown>) {
  return {
    issue,
    title,
    url: `https://github.com/openai/autonomous-issue-agent/issues/${issue}`,
    labels: [],
    pr: null,
    pr_url: null,
    pr_state: null,
    stage: null,
    done: false,
    steps: [],
    cost_usd: 1.8,
    estimated: false,
    projected_cost_usd: 5.01,
    projected_model: "gpt-6.1-sol",
    started_ts: ago(600),
    last_ts: ago(12),
    elapsed_s: 6_000,
    working_s: 1_320,
    idle_s: 4_680,
    idle_active: false,
    leg_running: false,
    projected_eta_s: null,
    models: ["gpt-6.1-sol"],
    model_badges: [{ model: "gpt-6.1-sol", effort: "high" }],
    rounds: 1,
    tier: "standard",
    overspend_ratio: null,
    ...over,
  };
}

const pr = (n: number) => ({ pr: n, pr_url: `https://github.com/openai/autonomous-issue-agent/pull/${n}`, pr_state: "open" });

const boardIssues = {
  in_flight: [
    card(710, "test: window-transfer-lifetime native smoke flake on windows (moved=false / listingHas)", {
      tier: "complex",
      cost_usd: 10.63,
      projected_cost_usd: 4.2,
      overspend_ratio: 2.53,
      board: {
        status: "needs_you", ask: "rearm", headline: "Re-arm or close",
        reason: "Resolve failed 3× without opening a PR.", since_ts: ago(30 * 24 * 60),
      },
      rail: rail({ current: "resolve", furthest: "resolve", resolve_attempts: 3, review_rounds: 0, steps: 6 }),
    }),
    card(1082, "feat(git-graph): create/merge/approve PR actions from the graph", {
      ...pr(1088),
      tier: "exceptional",
      board: {
        status: "needs_you", ask: "escalated", headline: "Answer the agent",
        reason: "Escalated with a question in the issue comments.", since_ts: ago(72),
      },
      rail: rail({ current: "review", furthest: "ci", review_rounds: 5, ci_runs: 3, steps: 14 }),
    }),
    card(993, "choosing which details, the popup menu is in the wrong place, when zoomed", {
      ...pr(1084),
      leg_running: false,
      board: {
        status: "working", activity: "ci", headline: "CI running",
        reason: "Approved on review round 5.", since_ts: ago(41),
      },
      rail: rail({ current: "ci", furthest: "ci", review_rounds: 5, ci_runs: 3, steps: 16 }),
    }),
    card(1083, "feat(git-graph): jump to merge-base/fork point of two branches", {
      ...pr(1090),
      leg_running: true,
      board: {
        status: "working", activity: "agent", headline: "Addressing review",
        reason: "Review round 1 asked for changes.", since_ts: ago(4),
      },
      rail: rail({ current: "review", furthest: "review", review_rounds: 1, steps: 5, live: true }),
    }),
  ],
  pending: [
    {
      issue: 1055, title: "add command palette command to open git issue list in browser", url: null,
      labels: ["ai-ready"], priority: null, held: true, hold_reason: "waiting on #1082", overlap: null,
      board: {
        status: "blocked", on: { kind: "issue", issue: 1082 }, headline: "Waiting for #1082",
        reason: "Starts when #1082 merges.", since_ts: ago(26 * 60),
      },
      rail: rail({ current: "triage", furthest: "triage", review_rounds: 0, steps: 0 }),
    },
    {
      issue: 1010, title: "make it so that in fullscreen for pdf previews, left/right arrows goes to next/previous", url: null,
      labels: ["ai-parked"], priority: null, held: true, hold_reason: "parked", overlap: null,
      board: {
        status: "blocked", on: { kind: "triage", why: "parked" }, headline: "Waiting on triage",
        reason: "Parked for human review. Unpark it to queue it again.", since_ts: ago(3 * 24 * 60),
      },
      rail: rail({ current: "triage", furthest: "triage", review_rounds: 0, steps: 0 }),
    },
  ],
  done: [],
  backlog: [],
  gh_ok: true,
  notifications: [],
  warnings: [],
};

const visits = [
  { stage: "resolve", steps: [{ kind: "ROUTE", outcome: "done", count: 1 }, { kind: "RESOLVE", outcome: "done", count: 1 }] },
  { stage: "review", steps: [{ kind: "REVIEW", outcome: "changes_requested", count: 1 }, { kind: "ADDRESS_REVIEW", outcome: "done", count: 1 }, { kind: "RE_REVIEW", outcome: "passed", count: 1 }] },
  { stage: "ci", steps: [{ kind: "CI", outcome: "failed", count: 1 }, { kind: "FIX_CI", outcome: "done", count: 2 }] },
  { stage: "review", steps: [{ kind: "RE_REVIEW", outcome: "changes_requested", count: 3 }, { kind: "ADDRESS_REVIEW", outcome: "done", count: 1 }, { kind: "REBASE", outcome: "running", count: 1 }] },
];

function detail(issue: number) {
  const row = boardIssues.in_flight.find((item) => item.issue === issue);
  return {
    issue, title: row?.title ?? null, url: null, stage: null, done: false, prs: [], pull_requests: [],
    cost_usd: row?.cost_usd ?? null, elapsed_s: 6_000, working_s: 1_320, idle_s: 4_680, idle_active: false,
    leg_running: false, steps: [], events: [], warnings: [],
    rail: row?.rail ?? null,
    history: visits,
    harness_message: issue === 1082
      ? "**Agent needs input:** the merge action needs a confirmation dialog. Should it reuse the existing modal, or a new inline confirm?\n\nTwo other issues are blocked on this one."
      : "**Circuit breaker:** resolve failed 3× without opening a PR. This episode is held; an explicit harness re-arm is required to grant another allowance.",
  };
}

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
    const issue = /^\/api\/repos\/self\/issues\/(\d+)$/.exec(path);
    if (issue) return json(route, detail(Number(issue[1])));
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
      ]);
      await expect(boardRow(page, "window-transfer-lifetime").locator(".board-headline")).toHaveText("Re-arm or close");
      await expect(boardRow(page, "window-transfer-lifetime").getByText("2.5× budget")).toBeVisible();
      await expect(boardRow(page, "window-transfer-lifetime").getByText("3 attempts")).toHaveCount(0);
      await expect(boardRow(page, "window-transfer-lifetime").getByText("attempt 3")).toBeVisible();
      await expect(boardRow(page, "window-transfer-lifetime").locator(".board-wait")).toHaveText("30d");

      const escalated = boardRow(page, "create/merge/approve PR actions");
      await expect(escalated.getByText("round 5")).toHaveClass(/rail-hot/);
      await expect(escalated.getByText("3 runs")).toHaveClass(/rail-hot/);
      await expect(escalated.locator(".rail-stage", { hasText: "CI" })).toHaveClass(/rail-rewound/);

      const working = group(page, "Working");
      await expect(working.locator(".board-headline")).toHaveText(["CI running", "Addressing review"]);
      await expect(boardRow(page, "popup menu is in the wrong place").locator(".board-wait")).toHaveText("41m ago");
      await expect(boardRow(page, "jump to merge-base").locator(".rail-stage.rail-current")).toHaveClass(/rail-live/);

      const blocked = group(page, "Blocked");
      await expect(blocked.locator(".board-headline")).toHaveText(["Waiting on triage", "Waiting for #1082"]);
      await expect(boardRow(page, "fullscreen for pdf previews").locator(".board-say")).toHaveClass(/board-say-owned/);
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
      await expect(drawer.getByText("↩ back from CI")).toBeVisible();
      const after = [await title.boundingBox(), await headline.boundingBox()];

      for (const [index, box] of after.entries()) {
        expect(box).not.toBeNull();
        expect(Math.abs(box!.x - before[index]!.x)).toBeLessThanOrEqual(0.5);
        expect(Math.abs(box!.y - before[index]!.y)).toBeLessThanOrEqual(0.5);
      }
      await expect(drawer.getByRole("button", { name: "Show 4 earlier steps" })).toBeVisible();
      await drawer.getByRole("button", { name: "Show 4 earlier steps" }).click();
      await expect(drawer.locator(".board-visit")).toHaveCount(4);
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
    await expect(page.locator("#board-drawer-710").getByText(/Circuit breaker: resolve failed/)).toBeVisible();
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
