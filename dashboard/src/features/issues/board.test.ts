import { describe, expect, it } from "vitest";
import type { BacklogCard, IssueCard, IssuesPayload, PendingCard } from "../../api/types";
import {
  boardItems,
  countLabel,
  groupItems,
  historyView,
  isUntriaged,
  overspendOf,
  primaryAction,
  railNodes,
  railSummary,
  waitCell,
  waitCompact,
  type BoardStatus,
  type StageRail,
  type StageVisit,
} from "./board";

const NOW = Date.parse("2026-10-12T12:00:00Z");

function card(issue: number, board: BoardStatus | null, over: Partial<IssueCard> = {}): IssueCard {
  return {
    issue, title: `Issue ${issue}`, url: null, labels: [], pr: null, pr_url: null, pr_state: null,
    stage: null, done: false, steps: [], cost_usd: null, estimated: false, projected_cost_usd: null,
    started_ts: null, last_ts: null, elapsed_s: null, working_s: null, idle_s: null, idle_active: false,
    leg_running: false, models: [], model_badges: [], rounds: null,
    board, rail: null, ...over,
  } as IssueCard;
}

const needs = (since: string | null, ask: "rearm" | "escalated" | "budget" | "awaiting_reply" | "merge" | "stalled" = "escalated"): BoardStatus =>
  ({ status: "needs_you", ask, headline: "Answer the agent", reason: "why", since_ts: since });
const working: BoardStatus = { status: "working", activity: "agent", headline: "Resolving", reason: "", since_ts: null };
const queued: BoardStatus = {
  status: "blocked", on: { kind: "queue", priority: null }, headline: "Queued", reason: "", since_ts: null,
};

function payload(over: Partial<IssuesPayload>): IssuesPayload {
  return { in_flight: [], pending: [], done: [], backlog: [], gh_ok: true, notifications: [], warnings: [], ...over } as IssuesPayload;
}

const rail = (over: Partial<StageRail> = {}): StageRail => ({
  current: "review", furthest: "review", resolve_attempts: 1, review_rounds: 1, ci_runs: 0,
  steps: 3, live: false, ...over,
});

describe("boardItems", () => {
  it("merges in-flight, queued and triage-blocked backlog rows, each issue once", () => {
    const items = boardItems(payload({
      in_flight: [card(1, working), card(9, null, { done: true })],
      pending: [{ issue: 1, title: "dup", url: null, labels: [], priority: null, held: false, hold_reason: null, overlap: null, board: queued } as PendingCard,
        { issue: 2, title: "queued", url: null, labels: [], priority: "P2", held: false, hold_reason: null, overlap: null } as PendingCard],
      backlog: [
        { issue: 3, title: "asked", url: null, body: null, labels: [], recurrence: false, recurrence_count: 0, steward_state: "blocked_on_human_reply", age_days: null, comments: null,
          board: { status: "blocked", on: { kind: "triage", why: "awaiting_reply" }, headline: "Waiting on triage", reason: "", since_ts: null } },
        { issue: 4, title: "raw", url: null, body: null, labels: [], recurrence: false, recurrence_count: 0, steward_state: "raw", age_days: null, comments: null },
      ],
    }));

    expect(items.map((item) => [item.issue, item.board.status])).toEqual([
      [1, "working"], [2, "blocked"], [3, "blocked"],
    ]);
    expect(items.find((item) => item.issue === 1)?.title).toBe("Issue 1");
  });

  it("words a queued card without a server status the way the server words a queue", () => {
    const [item] = boardItems(payload({
      pending: [{ issue: 2, title: "t", url: null, labels: [], priority: "P1", held: false, hold_reason: null, overlap: null } as PendingCard],
    }));

    expect(item?.board).toMatchObject({ status: "blocked", headline: "Queued", reason: "Starts when the fixer picks it up (P1)." });
  });

  it("keeps an unclassified running card on the board as Working rather than dropping it", () => {
    const [item] = boardItems(payload({ in_flight: [card(5, null)] }));

    expect(item?.board).toMatchObject({ status: "working", headline: "Status unavailable" });
  });

  it("is empty for an absent payload", () => {
    expect(boardItems(null)).toEqual([]);
    expect(boardItems(undefined)).toEqual([]);
  });
});

describe("groupItems", () => {
  it("always yields the three groups in order, longest Needs-you wait first and unknown waits last", () => {
    const items = boardItems(payload({
      in_flight: [
        card(10, working),
        card(11, needs("2026-10-12T11:00:00Z")),
        card(12, needs(null)),
        card(13, needs("2026-09-21T12:00:00Z")),
        card(14, needs("not a time")),
      ],
    }));

    const sections = groupItems(items);

    expect(sections.map(({ group }) => group)).toEqual(["needs_you", "working", "blocked"]);
    expect(sections[0]?.items.map((item) => item.issue)).toEqual([13, 11, 12, 14]);
    expect(sections[2]?.items).toEqual([]);
  });
});

describe("waitCell", () => {
  it("counts a wait in days, amber from 24 hours", () => {
    expect(waitCell(needs("2026-09-21T12:00:00Z"), NOW)).toMatchObject({ text: "21d", stale: true });
    expect(waitCell(needs("2026-10-11T12:00:01Z"), NOW)).toMatchObject({ text: "23h", stale: false });
    expect(waitCell(needs("2026-10-11T12:00:00Z"), NOW)).toMatchObject({ text: "1d", stale: true });
  });

  it("reads Working rows as time since the last activity", () => {
    expect(waitCell({ ...working, since_ts: "2026-10-12T11:48:00Z" }, NOW)).toMatchObject({ text: "12m ago", stale: false });
  });

  it("reads a row with no usable wait start as queued, and a future start as no wait", () => {
    expect(waitCell(queued, NOW).text).toBe("queued");
    expect(waitCell(needs("garbage"), NOW).text).toBe("queued");
    expect(waitCell(needs("2026-10-13T12:00:00Z"), NOW).text).toBe("<1m");
    expect(waitCell(needs("2026-10-12T11:00:00Z"), Number.NaN).text).toBe("queued");
  });

  it("formats huge waits without switching unit", () => {
    expect(waitCompact(400 * 86_400)).toBe("400d");
  });
});

describe("overspendOf", () => {
  it("alarms only at twice the projection", () => {
    expect(overspendOf(card(1, working, { overspend_ratio: 2, cost_usd: 10, projected_cost_usd: 5 })))
      .toEqual({ ratio: 2, cost: 10, budget: 5 });
    expect(overspendOf(card(1, working, { overspend_ratio: 1.99 }))).toBeNull();
    expect(overspendOf(card(1, working, { overspend_ratio: null }))).toBeNull();
    expect(overspendOf(card(1, working, { overspend_ratio: Number.POSITIVE_INFINITY }))).toBeNull();
    expect(overspendOf(null)).toBeNull();
  });
});

describe("the rail", () => {
  it("words counts in the singular on the current stage and the plural elsewhere", () => {
    expect(countLabel("review", 5, true)).toBe("round 5");
    expect(countLabel("review", 5, false)).toBe("5 rounds");
    expect(countLabel("ci", 1, false)).toBe("1 run");
    expect(countLabel("resolve", 1, true)).toBeNull();
    expect(countLabel("resolve", 2, false)).toBe("2 attempts");
    expect(countLabel("triage", 3, true)).toBeNull();
    expect(countLabel("review", 0, true)).toBeNull();
    expect(countLabel("review", Number.NaN, true)).toBeNull();
  });

  it("turns review amber at 4 rounds and CI at 3 runs", () => {
    const below = railNodes(rail({ review_rounds: 3, ci_runs: 2 }));
    const at = railNodes(rail({ review_rounds: 4, ci_runs: 3 }));

    expect(below.filter((node) => node.hot)).toEqual([]);
    expect(at.filter((node) => node.hot).map((node) => [node.id, node.hotTip])).toEqual([
      ["review", "4 or more review rounds: above typical"],
      ["ci", "3 or more CI runs: above typical"],
    ]);
  });

  it("marks stages reached earlier and then left as rewound", () => {
    const nodes = railNodes(rail({ current: "review", furthest: "ci", ci_runs: 2 }));

    expect(nodes.map((node) => node.state)).toEqual(["done", "done", "current", "rewound", "future"]);
    expect(nodes.find((node) => node.id === "ci")?.count).toBe("2 runs");
  });
});

describe("historyView", () => {
  const visits: StageVisit[] = [
    { stage: "resolve", steps: [{ kind: "ROUTE", outcome: "done", count: 1 }, { kind: "RESOLVE", outcome: "done", count: 1 }] },
    { stage: "review", steps: [{ kind: "REVIEW", outcome: "passed", count: 1 }] },
    { stage: "ci", steps: [{ kind: "CI", outcome: "failed", count: 1 }, { kind: "FIX_CI", outcome: "done", count: 2 }] },
    { stage: "review", steps: [{ kind: "RE_REVIEW", outcome: "changes_requested", count: 3 }, { kind: "ADDRESS_REVIEW", outcome: "running", count: 1 }] },
  ];

  it("keeps the newest chips and counts the hidden steps, not chips", () => {
    const view = historyView(visits, false, 3);

    expect(view.lines.map((line) => [line.stage, line.chips.map((chip) => chip.kind)])).toEqual([
      ["ci", ["FIX_CI"]],
      ["review", ["RE_REVIEW", "ADDRESS_REVIEW"]],
    ]);
    expect(view.hidden).toBe(4);
    expect(view.total).toBe(10);
  });

  it("marks a rewind against the visit before it in the full history, even when that one is hidden", () => {
    const view = historyView(visits, false, 2);

    expect(view.lines).toEqual([{ stage: "review", chips: visits[3]!.steps, backFrom: "ci" }]);
  });

  it("shows everything when expanded", () => {
    const view = historyView(visits, true);

    expect(view.hidden).toBe(0);
    expect(view.lines.map((line) => line.backFrom)).toEqual([null, null, null, "ci"]);
  });

  it("handles an empty history and a very long one", () => {
    expect(historyView([], false)).toEqual({ lines: [], hidden: 0, total: 0 });
    const long: StageVisit[] = Array.from({ length: 600 }, (_, index) => ({
      stage: index % 2 ? "ci" : "review",
      steps: [{ kind: index % 2 ? "FIX_CI" : "RE_REVIEW", outcome: "done", count: 1 }],
    }));
    const view = historyView(long, false);
    expect(view.lines.flatMap((line) => line.chips)).toHaveLength(6);
    expect(view.hidden).toBe(594);
  });
});

describe("primaryAction", () => {
  it("gives Needs you and triage-blocked rows exactly one action, and every other row none", () => {
    expect(primaryAction(needs(null, "rearm"))?.label).toBe("Re-arm");
    expect(primaryAction(needs(null, "budget"))?.label).toBe("Approve spend");
    expect(primaryAction(needs(null, "merge"))?.label).toBe("Open PR");
    for (const ask of ["escalated", "awaiting_reply", "stalled"] as const) {
      expect(primaryAction(needs(null, ask))?.label).toBe("Open issue");
    }
    expect(primaryAction({ ...queued, on: { kind: "triage", why: "parked" } })?.label).toBe("Unpark");
    expect(primaryAction({ ...queued, on: { kind: "triage", why: "awaiting_reply" } })?.label).toBe("Promote to ai-ready");
    expect(primaryAction(queued)).toBeNull();
    expect(primaryAction({ ...queued, on: { kind: "issue", issue: 4 } })).toBeNull();
    expect(primaryAction(working)).toBeNull();
  });
});

describe("the rail in words", () => {
  it("names the current stage with its count, then every other count", () => {
    expect(railSummary(railNodes(rail({ current: "review", furthest: "ci", review_rounds: 5, ci_runs: 3, resolve_attempts: 2 }))))
      .toBe("Review, round 5; Resolve 2 attempts; CI 3 runs");
    expect(railSummary(railNodes(rail({ current: "triage", furthest: "triage", review_rounds: 0, resolve_attempts: 0 }))))
      .toBe("Triage");
  });
});

describe("isUntriaged", () => {
  it("is only a backlog issue the steward has not seen", () => {
    const backlog = (steward_state: string) => ({ steward_state }) as BacklogCard;
    expect(isUntriaged(backlog("raw"))).toBe(true);
    expect(isUntriaged(backlog("processed"))).toBe(false);
    expect(isUntriaged(backlog("blocked_on_human_reply"))).toBe(false);
  });
});
