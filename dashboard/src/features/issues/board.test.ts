import { describe, expect, it } from "vitest";
import type { BacklogCard, IssueCard, IssuesPayload } from "../../api/types";
import {
  boardItems,
  countLabel,
  groupItems,
  historyView,
  primaryAction,
  queuedCard,
  railNodes,
  railSummary,
  sayLine,
  waitCell,
  waitCompact,
  type BoardStatus,
  type StageRail,
  type StageVisit,
} from "./board";

const NOW = Date.parse("2026-10-12T12:00:00Z");

const rail = (over: Partial<StageRail> = {}): StageRail => ({
  current: "review", furthest: "review", resolve_attempts: 1, review_rounds: 1, ci_runs: 0,
  steps: 3, live: false, ...over,
});

function card(issue: number, board: BoardStatus, over: Partial<IssueCard> = {}): IssueCard {
  return {
    issue, title: `Issue ${issue}`, url: null, labels: [], pr: null, pr_url: null, pr_ci_state: null,
    board, action: null, rail: rail(), history: [], cost_usd: 0, estimated: false, legs: 0,
    started_ts: null, last_ts: null, leg_running: false, ...over,
  };
}

const needs = (since: string | null, reason: "question" | "attempts" | "labelled" = "attempts", target: "issue" | "pr" = "issue"): BoardStatus =>
  ({ status: "needs_you", reason, headline: "Attempts exhausted", hint: "Reply or unblock.", detail: "**stop** text", target, since_ts: since });
const working: BoardStatus = { status: "working", activity: "agent", act: "RESOLVE", headline: "Resolving", reason: "An agent leg holds this item.", since_ts: null };
const waiting: BoardStatus = { status: "waiting", on: "branch-protection", headline: "Waiting on branch protection", reason: "GitHub blocks the merge.", since_ts: null };

function payload(over: Partial<IssuesPayload>): IssuesPayload {
  return { in_flight: [], done: [], backlog: [], gh_ok: true, merged_partial: false, observed_at: null, notifications: [], warnings: [], ...over };
}

describe("boardItems", () => {
  it("lists each in-flight issue once, with the server's status and rail", () => {
    const items = boardItems(payload({
      in_flight: [card(1, working), card(2, waiting), card(1, needs(null))],
    }));

    expect(items.map((item) => [item.issue, item.board.status])).toEqual([[1, "working"], [2, "waiting"]]);
    expect(items[0]?.rail.current).toBe("review");
  });

  it("is empty for an absent payload", () => {
    expect(boardItems(null)).toEqual([]);
    expect(boardItems(undefined)).toEqual([]);
  });
});

describe("queuedCard", () => {
  it("words a promotion the way the server words a queued item, and adds ai-ready once", () => {
    const backlog: BacklogCard = {
      issue: 7, title: "t", url: null, body: null, labels: ["bug", "ai-ready"], age_days: 1, comments: 0,
    };
    const queued = queuedCard(backlog);

    expect(queued.board).toMatchObject({ status: "working", activity: "queued", headline: "Queued" });
    expect(queued.labels).toEqual(["bug", "ai-ready"]);
    expect(queued.rail.current).toBe("resolve");
  });
});

describe("sayLine", () => {
  it("says what releases a stop on Needs you, and the server's reason elsewhere", () => {
    expect(sayLine(needs(null))).toBe("Reply or unblock.");
    expect(sayLine(working)).toBe("An agent leg holds this item.");
    expect(sayLine(waiting)).toBe("GitHub blocks the merge.");
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

    expect(sections.map(({ group }) => group)).toEqual(["needs_you", "working", "waiting"]);
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

  it("says what an undated row is instead of a wait, and reads a future start as no wait", () => {
    expect(waitCell({ ...working, since_ts: null }, NOW).text).toBe("queued");
    expect(waitCell(waiting, NOW).text).toBe("—");
    // A park has no stop comment to date it: it waits on you, it is not queued.
    expect(waitCell(needs(null), NOW).text).toBe("needs you");
    expect(waitCell(needs("garbage"), NOW).text).toBe("needs you");
    expect(waitCell(needs("2026-10-13T12:00:00Z"), NOW).text).toBe("<1m");
    expect(waitCell(needs("2026-10-12T11:00:00Z"), Number.NaN).text).toBe("needs you");
  });

  it("formats huge waits without switching unit", () => {
    expect(waitCompact(400 * 86_400)).toBe("400d");
  });
});

describe("the rail", () => {
  it("words counts in the singular on the current stage and the plural elsewhere", () => {
    expect(countLabel("review", 5, true)).toBe("round 5");
    expect(countLabel("review", 5, false)).toBe("5 rounds");
    expect(countLabel("ci", 1, false)).toBe("1 run");
    expect(countLabel("resolve", 1, true)).toBeNull();
    expect(countLabel("resolve", 2, false)).toBe("2 attempts");
    expect(countLabel("merge", 3, true)).toBeNull();
    expect(countLabel("review", 0, true)).toBeNull();
    expect(countLabel("review", Number.NaN, true)).toBeNull();
  });

  it("turns review amber at 4 rounds and CI at 3 runs", () => {
    const below = railNodes(rail({ review_rounds: 3, ci_runs: 2 }));
    const at = railNodes(rail({ review_rounds: 4, ci_runs: 3 }));

    expect(below.filter((node) => node.hot)).toEqual([]);
    expect(at.filter((node) => node.hot).map((node) => [node.id, node.hotTip])).toEqual([
      ["ci", "3 or more CI runs: above typical"],
      ["review", "4 or more review rounds: above typical"],
    ]);
  });

  it("marks stages reached earlier and then left as rewound", () => {
    const nodes = railNodes(rail({ current: "ci", furthest: "review", ci_runs: 2, review_rounds: 2 }));

    expect(nodes.map((node) => [node.id, node.state])).toEqual([
      ["triage", "done"], ["resolve", "done"], ["ci", "current"], ["review", "rewound"], ["merge", "future"],
    ]);
    expect(nodes.find((node) => node.id === "ci")?.count).toBe("run 2");
    expect(nodes.find((node) => node.id === "review")?.count).toBe("2 rounds");
  });
});

describe("historyView", () => {
  const visits: StageVisit[] = [
    { stage: "resolve", steps: [{ kind: "RESOLVE", outcome: "failed", count: 1 }, { kind: "RESOLVE", outcome: "done", count: 1 }] },
    { stage: "review", steps: [{ kind: "REVIEW", outcome: "passed", count: 1 }] },
    { stage: "ci", steps: [{ kind: "REBASE", outcome: "failed", count: 1 }, { kind: "FIX_CI", outcome: "done", count: 2 }] },
    { stage: "review", steps: [{ kind: "REVIEW", outcome: "changes_requested", count: 3 }, { kind: "ADDRESS_REVIEW", outcome: "running", count: 1 }] },
  ];

  it("keeps the newest chips and counts the hidden steps, not chips", () => {
    const view = historyView(visits, false, 3);

    expect(view.lines.map((line) => [line.stage, line.chips.map((chip) => chip.kind)])).toEqual([
      ["ci", ["FIX_CI"]],
      ["review", ["REVIEW", "ADDRESS_REVIEW"]],
    ]);
    expect(view.hidden).toBe(4);
    expect(view.total).toBe(10);
  });

  it("marks a rewind against the visit before it in the full history, even when that one is hidden", () => {
    const view = historyView(visits, false, 4);

    expect(view.lines).toEqual([
      { stage: "ci", chips: visits[2]!.steps, backFrom: "review" },
      { stage: "review", chips: visits[3]!.steps, backFrom: null },
    ]);
  });

  it("shows everything when expanded", () => {
    const view = historyView(visits, true);

    expect(view.hidden).toBe(0);
    // CI now comes before review: going from review back to CI is the rewind.
    expect(view.lines.map((line) => line.backFrom)).toEqual([null, null, "review", null]);
  });

  it("handles an empty history and a very long one", () => {
    expect(historyView([], false)).toEqual({ lines: [], hidden: 0, total: 0 });
    const long: StageVisit[] = Array.from({ length: 600 }, (_, index) => ({
      stage: index % 2 ? "ci" : "review",
      steps: [{ kind: index % 2 ? "FIX_CI" : "ADDRESS_REVIEW", outcome: "done", count: 1 }],
    }));
    const view = historyView(long, false);
    expect(view.lines.flatMap((line) => line.chips)).toHaveLength(6);
    expect(view.hidden).toBe(594);
  });
});

describe("primaryAction", () => {
  it("gives a Needs-you row exactly the action the server chose, and every other row none", () => {
    expect(primaryAction(needs(null), "unblock")?.label).toBe("Unblock");
    expect(primaryAction(needs(null, "question"), "open")?.label).toBe("Open issue");
    expect(primaryAction(needs(null, "question", "pr"), "open")?.label).toBe("Open PR");
    expect(primaryAction(needs(null), null)?.label).toBe("Open issue");
    expect(primaryAction(working, "unblock")).toBeNull();
    expect(primaryAction(waiting, null)).toBeNull();
  });
});

describe("the rail in words", () => {
  it("names the current stage with its count, then every other count", () => {
    expect(railSummary(railNodes(rail({ current: "review", furthest: "review", review_rounds: 5, ci_runs: 3, resolve_attempts: 2 }))))
      .toBe("Review, round 5; Resolve 2 attempts; CI 3 runs");
    expect(railSummary(railNodes(rail({ current: "resolve", furthest: "resolve", review_rounds: 0, resolve_attempts: 0 }))))
      .toBe("Resolve");
  });
});
