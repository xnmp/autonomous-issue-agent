import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Polled } from "../api/hooks";
import type { IssueCard, IssuesPayload, PendingCard } from "../api/types";
import type { BoardStatus, StageRail, StageVisit } from "../features/issues/board";
import { IssuesTab } from "./IssuesTab";

const NOW = "2026-10-12T12:00:00Z";

const rail = (over: Partial<StageRail> = {}): StageRail => ({
  current: "review", furthest: "review", resolve_attempts: 1, review_rounds: 2, ci_runs: 1,
  steps: 4, live: false, ...over,
});

const needs = (ask: "rearm" | "escalated" | "budget" | "awaiting_reply" | "merge" | "stalled", since: string | null, headline = "Answer the agent"): BoardStatus => ({
  status: "needs_you", ask, headline, reason: `reason for ${headline}`, since_ts: since,
});

function card(issue: number, board: BoardStatus | null, over: Partial<IssueCard> = {}): IssueCard {
  return {
    issue,
    title: `Issue ${issue} title`,
    url: `https://github.com/o/r/issues/${issue}`,
    labels: [],
    pr: null,
    pr_url: null,
    pr_state: null,
    stage: null,
    done: false,
    steps: [],
    cost_usd: 1,
    estimated: false,
    projected_cost_usd: 4,
    started_ts: "2026-10-12T10:00:00Z",
    last_ts: "2026-10-12T11:50:00Z",
    elapsed_s: 600,
    working_s: 100,
    idle_s: 500,
    idle_active: false,
    leg_running: false,
    models: ["gpt-6.1-sol"],
    model_badges: [{ model: "gpt-6.1-sol", effort: null }],
    rounds: 1,
    board,
    rail: rail(),
    tier: null,
    overspend_ratio: null,
    ...over,
  } as IssueCard;
}

function polled(payload: Partial<IssuesPayload>, updatedAt: number | null = Date.parse(NOW)): Polled<IssuesPayload> {
  return {
    data: { in_flight: [], pending: [], done: [], backlog: [], gh_ok: true, notifications: [], warnings: [], ...payload } as IssuesPayload,
    error: null,
    loading: false,
    updatedAt,
    refresh: vi.fn(),
  };
}

function renderBoard(payload: Partial<IssuesPayload>, updatedAt?: number | null) {
  return render(<IssuesTab repo="demo" sub="inflight" issues={polled(payload, updatedAt)} onOpenIssue={vi.fn()} />);
}

function section(label: string): HTMLElement {
  return screen.getByRole("region", { name: label });
}

function row(title: RegExp | string): HTMLElement {
  const node = screen.getByText(title).closest(".board-row");
  if (!(node instanceof HTMLElement)) throw new Error(`no row for ${String(title)}`);
  return node;
}

const history: StageVisit[] = [
  { stage: "resolve", steps: [{ kind: "ROUTE", outcome: "done", count: 1 }, { kind: "RESOLVE", outcome: "done", count: 1 }] },
  { stage: "review", steps: [{ kind: "REVIEW", outcome: "passed", count: 1 }] },
  { stage: "ci", steps: [{ kind: "CI", outcome: "failed", count: 1 }, { kind: "FIX_CI", outcome: "done", count: 2 }] },
  { stage: "review", steps: [{ kind: "RE_REVIEW", outcome: "changes_requested", count: 3 }, { kind: "ADDRESS_REVIEW", outcome: "running", count: 1 }] },
];

function detail(issue: number, over: Record<string, unknown> = {}) {
  return {
    issue, title: `Issue ${issue} title`, url: null, stage: null, done: false, prs: [], pull_requests: [],
    cost_usd: 1, elapsed_s: 600, working_s: 100, idle_s: 500, idle_active: false, leg_running: false,
    steps: [], events: [], warnings: [], history, rail: rail(), harness_message: null, ...over,
  };
}

/** Answers the issue-detail read and records every request. */
function stubFetch(details: Record<number, Record<string, unknown>> = {}) {
  const calls: { url: string; method: string; body: string | null }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, method: init?.method ?? "GET", body: typeof init?.body === "string" ? init.body : null });
    const match = /\/issues\/(\d+)$/.exec(url);
    const body = match && (init?.method ?? "GET") === "GET"
      ? details[Number(match[1])] ?? detail(Number(match[1]))
      : { ok: true, detail: "ok" };
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
  return calls;
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(new Date(NOW));
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("issues board groups", () => {
  it("puts each row in its server-decided group, in order, longest Needs-you wait first", () => {
    stubFetch();
    renderBoard({
      in_flight: [
        card(101, { status: "working", activity: "agent", headline: "Resolving", reason: "An agent is writing the fix.", since_ts: "2026-10-12T11:48:00Z" }, { leg_running: true }),
        card(102, needs("escalated", "2026-10-11T12:00:00Z", "Answer the agent")),
        card(103, needs("rearm", "2026-09-21T12:00:00Z", "Re-arm or close")),
      ],
      pending: [{
        issue: 104, title: "Waits for another", url: null, labels: ["ai-ready"], held: true,
        hold_reason: "waiting on #102", overlap: null,
        board: { status: "blocked", on: { kind: "issue", issue: 102 }, headline: "Waiting for #102", reason: "Starts when #102 merges.", since_ts: "2026-10-12T09:00:00Z" },
      } as PendingCard],
    });

    const groups = screen.getAllByRole("region").map((node) => node.getAttribute("aria-labelledby"));
    expect(groups).toEqual(["board-group-needs_you", "board-group-working", "board-group-blocked"]);
    const needsRows = within(section("Needs you")).getAllByText(/title$/).map((node) => node.textContent);
    expect(needsRows).toEqual(["Issue 103 title", "Issue 102 title"]);
    expect(within(section("Working")).getByText("Resolving")).toBeVisible();
    expect(within(section("Blocked")).getByText("Waiting for #102")).toBeVisible();
    expect(within(section("Blocked")).getByText("Starts when #102 merges.")).toBeVisible();
  });

  it("keeps a row the server holds for a person in Needs you while a leg still runs", () => {
    stubFetch();
    renderBoard({ in_flight: [card(110, needs("escalated", "2026-10-12T10:00:00Z"), { leg_running: true })] });

    expect(within(section("Needs you")).getByText("Issue 110 title")).toBeVisible();
    expect(within(section("Working")).getByText("Nothing here right now.")).toBeVisible();
  });

  it("names the second and fourth columns for each group", () => {
    stubFetch();
    renderBoard({
      in_flight: [
        card(120, needs("stalled", "2026-10-12T10:00:00Z")),
        card(121, { status: "working", activity: "ci", headline: "CI running", reason: "Approved on review round 5.", since_ts: "2026-10-12T11:48:00Z" }),
        card(122, { status: "blocked", on: { kind: "pr", pr: 9 }, headline: "Waiting for PR #9", reason: "", since_ts: "2026-10-12T10:00:00Z" }),
      ],
    });

    const headings = (label: string) => Array.from(section(label).querySelectorAll(".board-cols span")).map((node) => node.textContent);
    expect(headings("Needs you")).toEqual(["Issue", "What to do", "Stage", "Waiting", ""]);
    expect(headings("Working")).toEqual(["Issue", "Now", "Stage", "Last activity", ""]);
    expect(headings("Blocked")).toEqual(["Issue", "Waiting for", "Stage", "Waiting", ""]);
    expect(within(row("Issue 121 title")).getByText("12m ago")).toBeVisible();
    expect(within(row("Issue 120 title")).getByText("2h")).toBeVisible();
  });

  it("filters to one group from the summary strip and clears on a second click", async () => {
    stubFetch();
    renderBoard({ in_flight: [card(130, needs("escalated", null)), card(131, needs("budget", null))] });

    const chip = (name: RegExp) => screen.getByRole("button", { name });
    expect(chip(/^2 All$/)).toHaveAttribute("aria-pressed", "true");
    expect(chip(/0 Working/)).toHaveClass("board-chip-zero");
    expect(chip(/2 Needs you/)).not.toHaveClass("board-chip-zero");

    await userEvent.click(chip(/0 Working/));
    expect(screen.queryByRole("region", { name: "Working" })).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Needs you" })).not.toBeInTheDocument();
    expect(screen.queryByText("Nothing here right now.")).not.toBeInTheDocument();

    await userEvent.click(chip(/2 Needs you/));
    expect(section("Needs you")).toBeVisible();
    expect(screen.queryByRole("region", { name: "Blocked" })).not.toBeInTheDocument();

    await userEvent.click(chip(/2 Needs you/));
    expect(screen.getAllByRole("region")).toHaveLength(3);
    expect(screen.getAllByText("Nothing here right now.")).toHaveLength(2);
  });
});

describe("issues board row", () => {
  it("makes the title the disclosure button, described by the headline, reason, stage and wait", async () => {
    stubFetch();
    renderBoard({
      in_flight: [card(145, needs("budget", "2026-10-12T11:00:00Z", "Approve spend or close"), {
        pr: 61, pr_url: "https://github.com/o/r/pull/61",
        rail: rail({ current: "review", furthest: "ci", review_rounds: 5, ci_runs: 3 }),
      })],
    });

    const toggle = screen.getByRole("button", { name: "Issue 145 title" });
    // Browsers read the rail by its label and jsdom by its text; either way
    // the description carries the headline, reason, counts and wait.
    expect(toggle).toHaveAccessibleDescription(
      /^Approve spend or close reason for Approve spend or close .*Review,? round 5.*CI,? 3 runs.* Waiting: 1h$/,
    );
    // The issue and PR links sit beside the button, never inside it.
    expect(toggle.querySelector("a, button")).toBeNull();
    expect(toggle).toHaveAttribute("aria-expanded", "false");

    toggle.focus();
    await userEvent.keyboard("{Enter}");
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(toggle).toHaveAttribute("aria-controls", "board-drawer-145");
    await userEvent.keyboard(" ");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
  });

  it("flags overspend from twice the projection, with the exact figures in its tooltip", () => {
    stubFetch();
    renderBoard({
      in_flight: [
        card(150, needs("budget", null), { overspend_ratio: 2, cost_usd: 8, projected_cost_usd: 4 }),
        card(151, needs("budget", null), { overspend_ratio: 1.99, cost_usd: 7.96, projected_cost_usd: 4 }),
        card(152, needs("budget", null), { overspend_ratio: null }),
      ],
    });

    const tag = within(row("Issue 150 title")).getByText("2.0× budget");
    expect(tag).toHaveAttribute("data-tip", "$8.00 spent of a $4.00 projection");
    expect(within(row("Issue 151 title")).queryByText(/× budget/)).not.toBeInTheDocument();
    expect(within(row("Issue 152 title")).queryByText(/× budget/)).not.toBeInTheDocument();
  });

  it("links the issue and PR numbers and clamps the title with its full text as a tooltip", async () => {
    stubFetch();
    const long = "A".repeat(120);
    renderBoard({ in_flight: [card(160, needs("merge", null), { title: long, pr: 61, pr_url: "https://github.com/o/r/pull/61" })] });

    expect(screen.getByText(long)).toHaveAttribute("title", long);
    expect(screen.getByRole("link", { name: "#160" })).toHaveAttribute("href", "https://github.com/o/r/issues/160");
    await userEvent.click(screen.getByRole("link", { name: "PR #61" }));
    expect(screen.getByRole("button", { name: long })).toHaveAttribute("aria-expanded", "false");
  });

  it("draws the rail counts, amber thresholds and a rewound stage", () => {
    stubFetch();
    renderBoard({
      in_flight: [
        card(170, needs("rearm", null), { rail: rail({ current: "review", furthest: "ci", review_rounds: 4, ci_runs: 3, resolve_attempts: 2 }) }),
        card(171, needs("rearm", null), { rail: rail({ current: "review", furthest: "review", review_rounds: 3, ci_runs: 2 }) }),
      ],
    });

    const hot = within(row("Issue 170 title"));
    expect(hot.getByText("round 4")).toHaveAttribute("data-tip", "4 or more review rounds: above typical");
    expect(hot.getByText("3 runs")).toHaveAttribute("data-tip", "3 or more CI runs: above typical");
    expect(hot.getByText("2 attempts")).not.toHaveClass("rail-hot");
    expect(hot.getByText("CI").closest("li")).toHaveAttribute("data-tip", "Reached earlier; now back at Review");
    const calm = within(row("Issue 171 title"));
    expect(calm.getByText("round 3")).not.toHaveClass("rail-hot");
    expect(calm.getByText("2 runs")).not.toHaveClass("rail-hot");
    expect(calm.getByText("CI").closest("li")).toHaveClass("rail-future");
  });
});

describe("issues board drawer", () => {
  it("shows the full harness message, the stage-visit history and the facts", async () => {
    stubFetch({ 180: detail(180, { harness_message: "**Circuit breaker:** resolve failed 3× without opening a PR.\n\nRe-arm to continue." }) });
    renderBoard({
      in_flight: [card(180, needs("rearm", "2026-10-12T10:00:00Z", "Re-arm or close"), {
        tier: "complex", cost_usd: 10.63, projected_cost_usd: 13.33, labels: ["ai-working"],
      })],
    });

    await userEvent.click(row("Issue 180 title"));

    const drawer = document.getElementById("board-drawer-180") as HTMLElement;
    expect(await within(drawer).findByText(/Circuit breaker: resolve failed 3× without opening a PR\./)).toBeVisible();
    expect(within(drawer).queryByText(/\*\*/)).not.toBeInTheDocument();
    expect(within(drawer).getByText("↩ back from CI")).toBeVisible();
    expect(within(drawer).getByText("Show 1 earlier step")).toBeVisible();
    expect(within(drawer).getByText("complex")).toBeVisible();
    expect(within(drawer).getByText("$10.63 of $13.33")).toBeVisible();
    expect(within(drawer).getByText("ai-working")).toBeVisible();
    expect(within(drawer).getByText("gpt-6.1-sol")).toBeVisible();
  });

  it("opens the dashboard's own issue page from the drawer", async () => {
    stubFetch();
    const onOpenIssue = vi.fn();
    render(
      <IssuesTab
        repo="demo"
        sub="inflight"
        issues={polled({ in_flight: [card(185, needs("escalated", null))] })}
        onOpenIssue={onOpenIssue}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Issue 185 title" }));
    await userEvent.click(within(document.getElementById("board-drawer-185") as HTMLElement)
      .getByRole("button", { name: "Open full issue" }));

    expect(onOpenIssue).toHaveBeenCalledWith(185);
  });

  it("expands the history in place to every step and collapses it again", async () => {
    stubFetch();
    renderBoard({ in_flight: [card(181, needs("escalated", null))] });
    await userEvent.click(row("Issue 181 title"));
    const drawer = document.getElementById("board-drawer-181") as HTMLElement;

    await userEvent.click(await within(drawer).findByRole("button", { name: "Show 1 earlier step" }));
    expect(within(drawer).getByText("Route")).toBeVisible();
    expect(within(drawer).getAllByRole("listitem").filter((node) => node.classList.contains("board-visit"))).toHaveLength(4);

    await userEvent.click(within(drawer).getByRole("button", { name: "Hide earlier steps" }));
    expect(within(drawer).queryByText("Route")).not.toBeInTheDocument();
  });

  it("says when there is no agent work yet and marks an untriaged issue", async () => {
    const calls = stubFetch();
    renderBoard({ in_flight: [card(182, needs("escalated", null), { rail: rail({ steps: 0 }), tier: null })] });

    await userEvent.click(row("Issue 182 title"));

    expect(screen.getByText("No agent work yet.")).toBeVisible();
    expect(screen.getByText("untriaged")).toHaveClass("board-tier-none");
    expect(calls.some(({ url }) => url.endsWith("/issues/182"))).toBe(false);
  });

  it("ticks the agent clock while a leg runs and freezes both clocks on a quiescent row", async () => {
    stubFetch();
    renderBoard({
      in_flight: [
        card(183, { status: "working", activity: "agent", headline: "Resolving", reason: "", since_ts: NOW }, { leg_running: true }),
        card(184, needs("escalated", NOW), { leg_running: false, idle_active: false }),
      ],
    });
    await userEvent.click(row("Issue 183 title"));
    await userEvent.click(row("Issue 184 title"));
    const fact = (issue: number, name: string) =>
      within(document.getElementById(`board-drawer-${issue}`) as HTMLElement).getByText(name).nextElementSibling;
    expect(fact(183, "Agent time")).toHaveTextContent("1:40");

    act(() => {
      vi.advanceTimersByTime(30_000);
    });

    expect(fact(183, "Agent time")).toHaveTextContent("2:10");
    expect(fact(184, "Agent time")).toHaveTextContent("1:40");
    expect(fact(184, "Time idle")).toHaveTextContent("8:20");
  });
});

describe("issues board actions", () => {
  // "Open full issue" is navigation to the dashboard's issue page, not an
  // action on the issue, so it is not counted here.
  const secondary = /^(Open PR|Close PR|Close issue|Park|Review holds)$/;

  it.each([
    ["escalated", "Open issue"],
    ["awaiting_reply", "Open issue"],
    ["stalled", "Open issue"],
    ["merge", "Open PR"],
    ["rearm", "Re-arm"],
    ["budget", "Approve spend"],
  ] as const)("offers one primary action for %s: %s", async (ask, label) => {
    stubFetch();
    const issue = 200 + ["escalated", "awaiting_reply", "stalled", "merge", "rearm", "budget"].indexOf(ask);
    renderBoard({ in_flight: [card(issue, needs(ask, null), { pr: 7, pr_url: "https://github.com/o/r/pull/7" })] });

    await userEvent.click(row(`Issue ${issue} title`));
    const actions = document.querySelector(`#board-drawer-${issue} .board-actions`) as HTMLElement;

    const controls = within(actions).queryAllByRole("button").concat(within(actions).queryAllByRole("link"));
    expect(controls.map((node) => node.textContent)).toEqual([label]);
    const drawer = document.getElementById(`board-drawer-${issue}`) as HTMLElement;
    expect(within(drawer).queryAllByText(secondary).filter((node) => node.textContent !== label)).toEqual([]);
  });

  it("offers no action on a Working row or one blocked on another issue", async () => {
    stubFetch();
    renderBoard({
      in_flight: [card(210, { status: "working", activity: "agent", headline: "Resolving", reason: "", since_ts: null })],
      pending: [{
        issue: 211, title: "Waits", url: null, labels: [], held: true, hold_reason: null, overlap: null,
        board: { status: "blocked", on: { kind: "issue", issue: 210 }, headline: "Waiting for #210", reason: "", since_ts: null },
      } as PendingCard],
    });

    await userEvent.click(row("Issue 210 title"));
    await userEvent.click(row("Waits"));

    expect(document.querySelectorAll(".board-actions")).toHaveLength(0);
    expect(screen.queryAllByRole("button", { name: secondary })).toEqual([]);
  });

  it("unblocks a needs-human issue through one confirmed label move", async () => {
    const calls = stubFetch();
    const refresh = vi.fn();
    const issues = polled({
      pending: [{
        issue: 220, title: "Parked work", url: null, labels: ["needs-human"], held: true, hold_reason: "parked", overlap: null,
        board: { status: "blocked", on: { kind: "triage", why: "parked" }, headline: "Parked", reason: "A human owns its PR.", since_ts: null },
      } as PendingCard],
    });
    render(<IssuesTab repo="demo" sub="inflight" issues={{ ...issues, refresh }} onOpenIssue={vi.fn()} />);

    await userEvent.click(row("Parked work"));
    await userEvent.click(screen.getByRole("button", { name: "Unblock" }));
    expect(calls.filter(({ method }) => method !== "GET")).toEqual([]);
    expect(screen.getByText(/No other label changes/)).toBeVisible();

    await userEvent.click(screen.getByRole("button", { name: "Unblock #220" }));
    expect(calls.filter(({ method }) => method === "POST").map(({ url }) => url)).toEqual(["/api/repos/demo/issues/220/unblock"]);
    expect(refresh).toHaveBeenCalled();
  });

  it("re-arms through a confirmed unblock, never a direct write", async () => {
    const calls = stubFetch();
    renderBoard({ in_flight: [card(240, needs("rearm", null))] });

    await userEvent.click(row("Issue 240 title"));
    await userEvent.click(screen.getByRole("button", { name: "Re-arm" }));
    await userEvent.click(screen.getByRole("button", { name: "Unblock" }));
    expect(calls.filter(({ method }) => method !== "GET")).toEqual([]);

    await userEvent.click(screen.getByRole("button", { name: "Unblock #240" }));
    expect(calls.filter(({ method }) => method === "POST").map(({ url }) => url)).toEqual(["/api/repos/demo/issues/240/unblock"]);
  });
});
