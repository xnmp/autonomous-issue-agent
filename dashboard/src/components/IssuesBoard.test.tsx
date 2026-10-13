import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Polled } from "../api/hooks";
import type { BoardStatus, IssueCard, IssuesPayload, StageRail, StageVisit } from "../api/types";
import { IssuesTab } from "./IssuesTab";

const NOW = "2026-10-12T12:00:00Z";

const rail = (over: Partial<StageRail> = {}): StageRail => ({
  current: "review", furthest: "review", resolve_attempts: 1, review_rounds: 2, ci_runs: 1,
  steps: 4, live: false, ...over,
});

const needs = (
  reason: "question" | "attempts" | "review-rounds" | "lifetime-cap" | "labelled",
  since: string | null,
  headline = "Attempts exhausted",
  detail: string | null = null,
  target: "issue" | "pr" = "issue",
): BoardStatus => ({
  status: "needs_you", reason, headline, hint: `hint for ${headline}`, detail, target, since_ts: since,
});

const history: StageVisit[] = [
  { stage: "resolve", steps: [{ kind: "RESOLVE", outcome: "failed", count: 1 }, { kind: "RESOLVE", outcome: "done", count: 1 }] },
  { stage: "review", steps: [{ kind: "REVIEW", outcome: "passed", count: 1 }] },
  { stage: "ci", steps: [{ kind: "REBASE", outcome: "failed", count: 1 }, { kind: "FIX_CI", outcome: "done", count: 2 }] },
  { stage: "review", steps: [{ kind: "REVIEW", outcome: "changes_requested", count: 3 }, { kind: "ADDRESS_REVIEW", outcome: "running", count: 1 }] },
];

function card(issue: number, board: BoardStatus, over: Partial<IssueCard> = {}): IssueCard {
  return {
    issue,
    title: `Issue ${issue} title`,
    url: `https://github.com/o/r/issues/${issue}`,
    labels: [],
    pr: null,
    pr_url: null,
    pr_ci_state: null,
    board,
    action: board.status === "needs_you" ? (board.reason === "question" ? "open" : "unblock") : null,
    rail: rail(),
    history,
    cost_usd: 1,
    estimated: false,
    legs: 4,
    started_ts: "2026-10-12T10:00:00Z",
    last_ts: "2026-10-12T11:50:00Z",
    leg_running: false,
    ...over,
  };
}

function polled(payload: Partial<IssuesPayload>): Polled<IssuesPayload> {
  return {
    data: { in_flight: [], done: [], backlog: [], gh_ok: true, merged_partial: false, observed_at: NOW, notifications: [], warnings: [], ...payload },
    error: null,
    loading: false,
    updatedAt: Date.parse(NOW),
    refresh: vi.fn(),
  };
}

function renderBoard(payload: Partial<IssuesPayload>) {
  return render(<IssuesTab repo="demo" sub="inflight" issues={polled(payload)} onOpenIssue={vi.fn()} />);
}

function section(label: string): HTMLElement {
  return screen.getByRole("region", { name: label });
}

function row(title: RegExp | string): HTMLElement {
  const node = screen.getByText(title).closest(".board-row");
  if (!(node instanceof HTMLElement)) throw new Error(`no row for ${String(title)}`);
  return node;
}

/** Records every request; answers writes with success. */
function stubFetch() {
  const calls: { url: string; method: string }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: String(input), method: init?.method ?? "GET" });
    return new Response(JSON.stringify({ ok: true, detail: "ok" }), { status: 200, headers: { "Content-Type": "application/json" } });
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
  it("lists an item whose resolve has not started under Waiting, not Working", () => {
    stubFetch();
    renderBoard({
      in_flight: [
        card(131, { status: "waiting", on: "slot", headline: "Resolve next", reason: "Waiting for a free agent slot.", since_ts: null }),
        card(132, { status: "working", activity: "queued", act: "FIX_CI", headline: "Fix CI next", reason: "Dispatches when a slot is free.", since_ts: null }),
      ],
    });

    expect(within(section("Waiting")).getByText("Resolve next")).toBeVisible();
    expect(within(section("Working")).queryByText("Resolve next")).not.toBeInTheDocument();
    expect(within(section("Working")).getByText("Fix CI next")).toBeVisible();
  });

  it("puts each row in its server-derived group, in order, longest Needs-you wait first", () => {
    stubFetch();
    renderBoard({
      in_flight: [
        card(101, { status: "working", activity: "agent", act: "RESOLVE", headline: "Resolve running", reason: "An agent leg holds this item.", since_ts: "2026-10-12T11:48:00Z" }, { leg_running: true }),
        card(102, needs("question", "2026-10-11T12:00:00Z", "Answer the agent's question")),
        card(103, needs("attempts", "2026-09-21T12:00:00Z", "Attempts exhausted")),
        card(104, { status: "waiting", on: "branch-protection", headline: "Waiting on branch protection", reason: "GitHub blocks the merge until its rules pass.", since_ts: "2026-10-12T09:00:00Z" }),
      ],
    });

    const groups = screen.getAllByRole("region").map((node) => node.getAttribute("aria-labelledby"));
    expect(groups).toEqual(["board-group-needs_you", "board-group-working", "board-group-waiting"]);
    const needsRows = within(section("Needs you")).getAllByText(/title$/).map((node) => node.textContent);
    expect(needsRows).toEqual(["Issue 103 title", "Issue 102 title"]);
    expect(within(section("Working")).getByText("Resolve running")).toBeVisible();
    expect(within(section("Waiting")).getByText("Waiting on branch protection")).toBeVisible();
    expect(within(section("Waiting")).getByText("GitHub blocks the merge until its rules pass.")).toBeVisible();
  });

  it("says on a Needs-you row what releases it, not the stop's raw comment", () => {
    stubFetch();
    renderBoard({ in_flight: [card(110, needs("attempts", "2026-10-12T10:00:00Z", "Attempts exhausted", "**needs-human** long stop text"))] });

    expect(within(row("Issue 110 title")).getByText("hint for Attempts exhausted")).toBeVisible();
    expect(within(row("Issue 110 title")).queryByText(/long stop text/)).not.toBeInTheDocument();
  });

  it("names the second and fourth columns for each group", () => {
    stubFetch();
    renderBoard({
      in_flight: [
        card(120, needs("labelled", "2026-10-12T10:00:00Z", "Parked")),
        card(121, { status: "working", activity: "ci", act: null, headline: "CI running", reason: "Checks are running on the PR head.", since_ts: "2026-10-12T11:48:00Z" }),
        card(122, { status: "waiting", on: "human-review", headline: "Waiting on a human review", reason: "", since_ts: "2026-10-12T10:00:00Z" }),
      ],
    });

    const headings = (label: string) => Array.from(section(label).querySelectorAll(".board-cols span")).map((node) => node.textContent);
    expect(headings("Needs you")).toEqual(["Issue", "What to do", "Stage", "Waiting", ""]);
    expect(headings("Working")).toEqual(["Issue", "Now", "Stage", "In step", ""]);
    expect(headings("Waiting")).toEqual(["Issue", "Waiting for", "Stage", "Waiting", ""]);
    expect(within(row("Issue 121 title")).getByText("12m")).toBeVisible();
    expect(within(row("Issue 120 title")).getByText("2h")).toBeVisible();
  });

  it("filters to one group from the summary strip and clears on a second click", async () => {
    stubFetch();
    renderBoard({ in_flight: [card(130, needs("attempts", null)), card(131, needs("lifetime-cap", null))] });

    const chip = (name: RegExp) => screen.getByRole("button", { name });
    expect(chip(/^2 All$/)).toHaveAttribute("aria-pressed", "true");
    expect(chip(/0 Working/)).toHaveClass("board-chip-zero");
    expect(chip(/2 Needs you/)).not.toHaveClass("board-chip-zero");

    await userEvent.click(chip(/0 Working/));
    expect(screen.queryByRole("region", { name: "Working" })).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Needs you" })).not.toBeInTheDocument();

    await userEvent.click(chip(/2 Needs you/));
    expect(section("Needs you")).toBeVisible();
    expect(screen.queryByRole("region", { name: "Waiting" })).not.toBeInTheDocument();

    await userEvent.click(chip(/2 Needs you/));
    expect(screen.getAllByRole("region")).toHaveLength(3);
    expect(screen.getAllByText("Nothing here right now.")).toHaveLength(2);
  });
});

describe("issues board row", () => {
  it("makes the title the disclosure button, described by the headline, hint, stage and wait", async () => {
    stubFetch();
    renderBoard({
      in_flight: [card(145, needs("review-rounds", "2026-10-12T11:00:00Z", "Review dispute"), {
        pr: 61, pr_url: "https://github.com/o/r/pull/61",
        rail: rail({ current: "review", furthest: "review", review_rounds: 5, ci_runs: 3 }),
      })],
    });

    const toggle = screen.getByRole("button", { name: "Issue 145 title" });
    expect(toggle).toHaveAccessibleDescription(
      /^Review dispute hint for Review dispute .*CI,? 3 runs.*Review,? round 5.* Waiting: 1h$/,
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

  it("links the issue and PR numbers and clamps the title with its full text as a tooltip", async () => {
    stubFetch();
    const long = "A".repeat(120);
    renderBoard({ in_flight: [card(160, needs("question", null), { title: long, pr: 61, pr_url: "https://github.com/o/r/pull/61" })] });

    expect(screen.getByText(long)).toHaveAttribute("title", long);
    expect(screen.getByRole("link", { name: "#160" })).toHaveAttribute("href", "https://github.com/o/r/issues/160");
    await userEvent.click(screen.getByRole("link", { name: "PR #61" }));
    expect(screen.getByRole("button", { name: long })).toHaveAttribute("aria-expanded", "false");
  });

  it("draws the rail counts, amber thresholds and a rewound stage", () => {
    stubFetch();
    renderBoard({
      in_flight: [
        card(170, needs("attempts", null), { rail: rail({ current: "ci", furthest: "review", review_rounds: 4, ci_runs: 3, resolve_attempts: 2 }) }),
        card(171, needs("attempts", null), { rail: rail({ current: "review", furthest: "review", review_rounds: 3, ci_runs: 2 }) }),
      ],
    });

    const hot = within(row("Issue 170 title"));
    expect(hot.getByText("4 rounds")).toHaveAttribute("data-tip", "4 or more review rounds: above typical");
    expect(hot.getByText("run 3")).toHaveAttribute("data-tip", "3 or more CI runs: above typical");
    expect(hot.getByText("2 attempts")).not.toHaveClass("rail-hot");
    expect(hot.getByText("Review").closest("li")).toHaveAttribute("data-tip", "Reached earlier; now back at CI");
    const calm = within(row("Issue 171 title"));
    expect(calm.getByText("round 3")).not.toHaveClass("rail-hot");
    expect(calm.getByText("2 runs")).not.toHaveClass("rail-hot");
    expect(calm.getByText("Merge").closest("li")).toHaveClass("rail-future");
  });
});

describe("issues board drawer", () => {
  it("shows the stop comment as plain text, the stage-visit history and the facts, without another request", async () => {
    const calls = stubFetch();
    renderBoard({
      in_flight: [card(180, needs("attempts", "2026-10-12T10:00:00Z", "Attempts exhausted", "**Stopped:** RESOLVE ran 3 times without progress."), {
        cost_usd: 10.63, estimated: true, legs: 7, labels: ["ai-ready", "needs-human"],
      })],
    });

    await userEvent.click(row("Issue 180 title"));

    const drawer = document.getElementById("board-drawer-180") as HTMLElement;
    expect(within(drawer).getByText(/Stopped: RESOLVE ran 3 times without progress\./)).toBeVisible();
    expect(within(drawer).queryByText(/\*\*/)).not.toBeInTheDocument();
    expect(within(drawer).getByText("↩ back from Review")).toBeVisible();
    expect(within(drawer).getByText("Show 1 earlier step")).toBeVisible();
    expect(within(drawer).getByText("$10.63 est.")).toBeVisible();
    expect(within(drawer).getByText("7")).toBeVisible();
    expect(within(drawer).getByText("ai-ready, needs-human")).toBeVisible();
    expect(calls).toEqual([]);
  });

  it("opens the dashboard's own issue page from the drawer", async () => {
    stubFetch();
    const onOpenIssue = vi.fn();
    render(
      <IssuesTab
        repo="demo"
        sub="inflight"
        issues={polled({ in_flight: [card(185, needs("attempts", null))] })}
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
    renderBoard({ in_flight: [card(181, needs("attempts", null))] });
    await userEvent.click(row("Issue 181 title"));
    const drawer = document.getElementById("board-drawer-181") as HTMLElement;

    await userEvent.click(within(drawer).getByRole("button", { name: "Show 1 earlier step" }));
    expect(within(drawer).getAllByRole("listitem").filter((node) => node.classList.contains("board-visit"))).toHaveLength(4);

    const chips = () => drawer.querySelectorAll(".board-step").length;
    expect(chips()).toBe(7);

    await userEvent.click(within(drawer).getByRole("button", { name: "Hide earlier steps" }));
    expect(chips()).toBe(6);
    expect(within(drawer).getByRole("button", { name: "Show 1 earlier step" })).toBeVisible();
  });

  it("says when there is no agent work yet", async () => {
    stubFetch();
    renderBoard({ in_flight: [card(182, { status: "working", activity: "queued", act: "RESOLVE", headline: "Resolve next", reason: "Dispatches when a slot is free.", since_ts: null }, { rail: rail({ steps: 0 }), history: [] })] });

    await userEvent.click(row("Issue 182 title"));

    expect(screen.getByText("No agent work yet.")).toBeVisible();
  });
});

describe("issues board actions", () => {
  it.each([
    ["question", "issue", "open", "Open issue"],
    ["question", "pr", "open", "Open PR"],
    ["attempts", "issue", "unblock", "Unblock"],
    ["labelled", "issue", "unblock", "Unblock"],
  ] as const)("offers one primary action for a %s stop on the %s: %s", async (reason, target, action, label) => {
    stubFetch();
    renderBoard({ in_flight: [card(200, needs(reason, null, "Stopped", null, target), { action, pr: 7, pr_url: "https://github.com/o/r/pull/7" })] });

    await userEvent.click(row("Issue 200 title"));
    const actions = document.querySelector("#board-drawer-200 .board-actions") as HTMLElement;

    const controls = within(actions).queryAllByRole("button").concat(within(actions).queryAllByRole("link"));
    expect(controls.map((node) => node.textContent)).toEqual([label]);
  });

  it("offers no action on a Working or Waiting row", async () => {
    stubFetch();
    renderBoard({
      in_flight: [
        card(210, { status: "working", activity: "agent", act: "RESOLVE", headline: "Resolve running", reason: "", since_ts: null }),
        card(211, { status: "waiting", on: "headroom", headline: "Waiting for spend headroom", reason: "", since_ts: null }),
      ],
    });

    await userEvent.click(row("Issue 210 title"));
    await userEvent.click(row("Issue 211 title"));

    expect(document.querySelectorAll(".board-actions")).toHaveLength(0);
  });

  it("unblocks a needs-human issue through one confirmed label move", async () => {
    const calls = stubFetch();
    const refresh = vi.fn();
    const issues = polled({ in_flight: [card(220, needs("labelled", null, "Parked"))] });
    render(<IssuesTab repo="demo" sub="inflight" issues={{ ...issues, refresh }} onOpenIssue={vi.fn()} />);

    await userEvent.click(row("Issue 220 title"));
    await userEvent.click(screen.getByRole("button", { name: "Unblock" }));
    expect(calls).toEqual([]);
    expect(screen.getByText(/No other label changes/)).toBeVisible();

    await userEvent.click(screen.getByRole("button", { name: "Unblock #220" }));
    expect(calls.filter(({ method }) => method === "POST").map(({ url }) => url)).toEqual(["/api/repos/demo/issues/220/unblock"]);
    expect(refresh).toHaveBeenCalled();
  });

  it("shows CI waiting on a runner or a leg waiting on a slot in yellow, and CI executing in green", () => {
    stubFetch();
    const working = (activity: "ci" | "ci_queued" | "queued", headline: string): BoardStatus => ({
      status: "working", activity, act: null, headline, reason: "r", since_ts: null,
    });
    renderBoard({
      in_flight: [
        card(180, working("ci_queued", "CI queued"), { rail: rail({ current: "ci" }) }),
        card(181, working("ci", "CI running"), { rail: rail({ current: "ci" }) }),
        card(182, working("queued", "Fix CI next"), { rail: rail({ current: "ci" }) }),
      ],
    });

    const queued = row("Issue 180 title");
    expect(queued.closest(".board-item")).toHaveClass("board-tone-warn");
    expect(within(queued).getByText("CI queued")).toBeVisible();
    expect(queued.querySelector(".lifecycle-rail")).toHaveClass("rail-tone-warn");
    const running = row("Issue 181 title");
    expect(running.closest(".board-item")).not.toHaveClass("board-tone-warn");
    expect(running.querySelector(".lifecycle-rail")).toHaveClass("rail-tone-ok");
    const slot = row("Issue 182 title");
    expect(slot.closest(".board-item")).toHaveClass("board-tone-warn");
    expect(slot.querySelector(".lifecycle-rail")).toHaveClass("rail-tone-warn");
  });
});
