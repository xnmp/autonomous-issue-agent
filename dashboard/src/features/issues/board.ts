/**
 * The issues board view model: pure functions from the issues payload to
 * what the board renders. Every status decision is the server's (`board` on
 * each card); this module only merges the three lanes into one list, groups
 * and orders it, and words the counts.
 */

import type {
  IssueCard,
  IssuesPayload,
  PendingCard,
} from "../../api/types";
import type { components } from "../../api/generated";
import { parseTs } from "../../lib/format";
import { CANONICAL, railSummary, type RailNode } from "../../components/ui/LifecycleStepper";

type Schema = components["schemas"];

export type BoardStatus = Schema["BoardNeedsYou"] | Schema["BoardWorking"] | Schema["BoardBlocked"];
export type BoardGroup = BoardStatus["status"];
export type StageRail = Schema["StageRail"];
export type StageVisit = Schema["StageVisit"];
export type TrackChip = Schema["TrackChip"];
export type StageId = StageRail["current"];
export type Tier = NonNullable<IssueCard["tier"]>;

export const GROUP_ORDER: readonly BoardGroup[] = ["needs_you", "working", "blocked"];

export const GROUP_META: Record<BoardGroup, { label: string; hint: string; tone: "fail" | "ok" | "muted" }> = {
  needs_you: {
    label: "Needs you",
    hint: "Nothing moves until you act. The highlighted line says what to do.",
    tone: "fail",
  },
  working: {
    label: "Working",
    hint: "An agent or CI is running, or the PR is approved and green and about to merge.",
    tone: "ok",
  },
  blocked: {
    label: "Blocked",
    hint: "Waiting on triage or on another issue to merge.",
    tone: "muted",
  },
};

export const COLUMN_HEADINGS: Record<BoardGroup, { now: string; wait: string }> = {
  needs_you: { now: "What to do", wait: "Waiting" },
  working: { now: "Now", wait: "Last activity" },
  blocked: { now: "Waiting for", wait: "Waiting" },
};

/** The five stages, in rail order: the same track the issue page draws. */
export const STAGES = CANONICAL;

export function stageLabel(id: StageId): string {
  return STAGES.find((stage) => stage.id === id)?.label ?? id;
}

/** One board row, whichever lane the server put it in. */
export interface BoardItem {
  issue: number;
  title: string | null;
  url: string | null;
  pr: number | null;
  prUrl: string | null;
  board: BoardStatus;
  rail: StageRail | null;
  labels: string[];
  tier: Tier | null;
  /** Only an in-flight issue has spend, clocks and models. */
  card: IssueCard | null;
}

/**
 * A running card the server could not classify (an older server, or a field
 * lost in transit) still has to land somewhere. Working is the one group that
 * asks nothing of the operator and claims no blocker.
 */
const UNCLASSIFIED: BoardStatus = {
  status: "working",
  activity: "handoff",
  headline: "Status unavailable",
  reason: "The dashboard server did not classify this issue.",
  since_ts: null,
};

/**
 * A queued card without a server status: a backlog promotion the client
 * shows before the next poll confirms it. Worded as the server words a queue.
 */
function queued(priority: PendingCard["priority"]): BoardStatus {
  return {
    status: "blocked",
    on: { kind: "queue", priority },
    headline: "Queued",
    reason: `Starts when the fixer picks it up${priority ? ` (${priority})` : ""}.`,
    since_ts: null,
  };
}

/**
 * In-flight and queued rows as one list. An issue
 * appears once: a lane earlier in that order wins, because it carries more
 * of the issue's state.
 */
export function boardItems(payload: Pick<IssuesPayload, "in_flight" | "pending"> | null | undefined): BoardItem[] {
  if (!payload) return [];
  const seen = new Set<number>();
  const items: BoardItem[] = [];
  const add = (item: BoardItem) => {
    if (seen.has(item.issue)) return;
    seen.add(item.issue);
    items.push(item);
  };
  for (const card of payload.in_flight ?? []) {
    if (card.done) continue;
    add(fromIssueCard(card));
  }
  for (const card of payload.pending ?? []) {
    add(fromPendingCard(card, card.board ?? queued(card.priority)));
  }
  return items;
}

function fromIssueCard(card: IssueCard): BoardItem {
  return {
    issue: card.issue,
    title: card.title,
    url: card.url,
    pr: card.pr,
    prUrl: card.pr_url,
    board: card.board ?? UNCLASSIFIED,
    rail: card.rail ?? null,
    labels: card.labels ?? [],
    tier: card.tier ?? null,
    card,
  };
}

function fromPendingCard(card: PendingCard, board: BoardStatus): BoardItem {
  return {
    issue: card.issue,
    title: card.title,
    url: card.url,
    pr: card.pr ?? null,
    prUrl: card.pr_url ?? null,
    board,
    rail: card.rail ?? null,
    labels: card.labels ?? [],
    tier: null,
    card: null,
  };
}

export interface BoardSection {
  group: BoardGroup;
  items: BoardItem[];
}

/**
 * The three groups, always in order. Needs you puts the longest wait first;
 * a row with no wait start yet goes last there. The other groups keep issue
 * order so rows do not jump around between polls.
 */
export function groupItems(items: readonly BoardItem[]): BoardSection[] {
  return GROUP_ORDER.map((group) => {
    const members = items.filter((item) => item.board.status === group);
    const ordered = group === "needs_you"
      ? [...members].sort(byLongestWait)
      : [...members].sort((a, b) => a.issue - b.issue);
    return { group, items: ordered };
  });
}

function byLongestWait(a: BoardItem, b: BoardItem): number {
  const left = parseTs(a.board.since_ts);
  const right = parseTs(b.board.since_ts);
  if (left === null && right === null) return a.issue - b.issue;
  if (left === null) return 1;
  if (right === null) return -1;
  return left - right || a.issue - b.issue;
}

export function groupCounts(items: readonly BoardItem[]): Record<BoardGroup, number> {
  const counts: Record<BoardGroup, number> = { needs_you: 0, working: 0, blocked: 0 };
  for (const item of items) counts[item.board.status] += 1;
  return counts;
}

/** Operator-facing fields only, so a filter never matches payload internals. */
export function matchesQuery(item: BoardItem, query: string): boolean {
  if (!query) return true;
  return [
    `#${item.issue}`,
    item.issue,
    item.title,
    item.pr === null ? null : `PR #${item.pr}`,
    item.board.headline,
    item.board.reason,
    ...item.labels,
  ].some((value) => String(value ?? "").toLowerCase().includes(query));
}

/* ------------------------------------------------------------ waiting */

const DAY_S = 86_400;

/** Minutes, hours, then whole days: a three-week wait reads `21d`. */
export function waitCompact(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  if (s >= DAY_S) return `${Math.floor(s / DAY_S)}d`;
  if (s >= 3600) return `${Math.floor(s / 3600)}h`;
  if (s >= 60) return `${Math.floor(s / 60)}m`;
  return "<1m";
}

export interface WaitCell {
  text: string;
  /** Past a day: amber, because nothing should sit that long. */
  stale: boolean;
  title: string | null;
}

/**
 * The fourth column. Needs you and Blocked count how long the row has waited
 * on the operator or its blocker; Working says how long ago something last
 * happened. A row whose wait has not started reads `queued`.
 */
export function waitCell(board: BoardStatus, nowMs: number): WaitCell {
  const since = parseTs(board.since_ts);
  if (since === null || !Number.isFinite(nowMs)) return { text: "queued", stale: false, title: null };
  const seconds = Math.max(0, (nowMs - since) / 1000);
  const text = waitCompact(seconds);
  if (board.status === "working") {
    return { text: `${text} ago`, stale: seconds >= DAY_S, title: `Last activity ${text} ago` };
  }
  return {
    text,
    stale: seconds >= DAY_S,
    title: board.status === "needs_you" ? `Waiting on you for ${text}` : `Waiting for ${text}`,
  };
}

/* ------------------------------------------------------------ overspend */

export interface Overspend {
  ratio: number;
  cost: number | null;
  budget: number | null;
}

/** The server only sends a ratio at or past twice the projection. */
export function overspendOf(card: IssueCard | null): Overspend | null {
  const ratio = card?.overspend_ratio;
  if (typeof ratio !== "number" || !Number.isFinite(ratio) || ratio < 2) return null;
  return { ratio, cost: card?.cost_usd ?? null, budget: card?.projected_cost_usd ?? null };
}

/* ------------------------------------------------------------ the rail */

export type { RailNode };
export { railSummary };

const UNIT: Partial<Record<StageId, string>> = { resolve: "attempt", review: "round", ci: "run" };
const HOT: Partial<Record<StageId, { at: number; tip: string }>> = {
  review: { at: 4, tip: "4 or more review rounds: above typical" },
  ci: { at: 3, tip: "3 or more CI runs: above typical" },
};

function countFor(rail: StageRail, id: StageId): number {
  if (id === "resolve") return rail.resolve_attempts;
  if (id === "review") return rail.review_rounds;
  if (id === "ci") return rail.ci_runs;
  return 0;
}

/**
 * Review and CI always carry a count once they have one; Resolve only past
 * its first attempt. The current stage reads in the singular ("round 5"),
 * any other in the plural ("5 rounds").
 */
export function countLabel(id: StageId, n: number, current: boolean): string | null {
  const unit = UNIT[id];
  if (!unit || !Number.isFinite(n) || n <= 0) return null;
  if (id === "resolve" && n <= 1) return null;
  if (current) return `${unit} ${n}`;
  return `${n} ${unit}${n === 1 ? "" : "s"}`;
}

export function railNodes(rail: StageRail): RailNode[] {
  const current = STAGES.findIndex((stage) => stage.id === rail.current);
  const furthest = Math.max(current, STAGES.findIndex((stage) => stage.id === rail.furthest));
  return STAGES.map(({ id, label }, index) => {
    const n = countFor(rail, id);
    const hot = HOT[id];
    const isHot = !!hot && n >= hot.at;
    return {
      id,
      label,
      state: index < current ? "done" : index === current ? "current" : index <= furthest ? "rewound" : "future",
      count: countLabel(id, n, index === current),
      hot: isHot,
      hotTip: isHot ? hot.tip : null,
    };
  });
}

/* ------------------------------------------------------------ history */

export const HISTORY_TAIL = 6;

export interface HistoryLine {
  stage: StageId;
  chips: TrackChip[];
  /** The stage this visit went back from, when it rewound. */
  backFrom: StageId | null;
}

export interface HistoryView {
  lines: HistoryLine[];
  /** Steps (not chips) left out of the tail. */
  hidden: number;
  total: number;
}

function stageIndex(id: StageId): number {
  return STAGES.findIndex((stage) => stage.id === id);
}

/**
 * The drawer's visit list. Collapsed, it keeps the newest `tail` chips;
 * a rewind marker always compares a visit with the one before it in the
 * full history, so trimming never invents or hides a "back from".
 */
export function historyView(visits: readonly StageVisit[], full: boolean, tail = HISTORY_TAIL): HistoryView {
  const lines: HistoryLine[] = visits.map((visit, index) => {
    const previous = visits[index - 1];
    const back = previous && stageIndex(visit.stage) < stageIndex(previous.stage) ? previous.stage : null;
    return { stage: visit.stage, chips: [...visit.steps], backFrom: back };
  });
  const total = visits.reduce((sum, visit) => sum + visit.steps.reduce((n, chip) => n + chip.count, 0), 0);
  if (full) return { lines, hidden: 0, total };
  let keep = Math.max(0, tail);
  let hidden = 0;
  const kept: HistoryLine[] = [];
  for (let index = lines.length - 1; index >= 0; index -= 1) {
    const line = lines[index] as HistoryLine;
    if (keep === 0) {
      hidden += line.chips.reduce((n, chip) => n + chip.count, 0);
      continue;
    }
    const shown = line.chips.slice(-keep);
    hidden += line.chips.slice(0, line.chips.length - shown.length).reduce((n, chip) => n + chip.count, 0);
    keep -= shown.length;
    kept.unshift({ ...line, chips: shown });
  }
  return { lines: kept, hidden, total };
}

export const OUTCOME_MARK: Record<TrackChip["outcome"], { glyph: string; label: string }> = {
  passed: { glyph: "✓", label: "passed" },
  failed: { glyph: "✕", label: "failed" },
  changes_requested: { glyph: "±", label: "changes requested" },
  done: { glyph: "•", label: "done" },
  running: { glyph: "…", label: "running" },
};

/* ------------------------------------------------------------ the one action */

export type PrimaryAction =
  | { kind: "rearm"; label: "Re-arm" }
  | { kind: "budget"; label: "Approve spend" }
  | { kind: "open_issue"; label: "Open issue" }
  | { kind: "open_pr"; label: "Open PR" }
  | { kind: "unpark"; label: "Unpark" };

/**
 * At most one action per row, and only where the operator is what moves it:
 * Needs you, and Blocked on triage. Every other row has none.
 */
export function primaryAction(board: BoardStatus): PrimaryAction | null {
  if (board.status === "needs_you") {
    switch (board.ask) {
      case "rearm":
        return { kind: "rearm", label: "Re-arm" };
      case "budget":
        return { kind: "budget", label: "Approve spend" };
      case "merge":
        return { kind: "open_pr", label: "Open PR" };
      default:
        return { kind: "open_issue", label: "Open issue" };
    }
  }
  if (board.status === "blocked" && board.on.kind === "triage") {
    return { kind: "unpark", label: "Unpark" };
  }
  return null;
}

/** A Blocked row the operator unblocks reads as an alarm, not a wait. */
export function operatorOwned(board: BoardStatus): boolean {
  return board.status === "needs_you" || (board.status === "blocked" && board.on.kind === "triage");
}

/* ------------------------------------------------------------ the harness note */

/** The harness comment as plain text: markdown emphasis markers dropped. */
export function plainHarnessMessage(message: string | null | undefined): string | null {
  const text = message?.replaceAll("**", "").replaceAll("__", "").trim();
  return text ? text : null;
}

export const TIERS: readonly Tier[] = ["trivial", "standard", "complex", "exceptional"];
