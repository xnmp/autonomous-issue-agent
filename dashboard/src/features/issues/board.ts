/**
 * The issues board view model: pure functions from the issues payload to
 * what the board renders. Every status decision is the server's (`board` on
 * each card); this module only merges the three lanes into one list, groups
 * and orders it, and words the counts.
 */

import type {
  BacklogCard,
  BoardStatus,
  IssueCard,
  IssuesPayload,
  StageId,
  StageRail,
  StageVisit,
  TrackChip,
} from "../../api/types";
import { parseTs } from "../../lib/format";
import { CANONICAL, railSummary, type RailNode } from "../../components/ui/LifecycleStepper";

export type { BoardStatus, StageId, StageRail, StageVisit, TrackChip };
/** The server's lane for a row (`board.group`); `board.status` stays the variant. */
export type BoardGroup = BoardStatus["group"];

export const GROUP_ORDER: readonly BoardGroup[] = ["needs_you", "agent", "ci", "slot", "waiting"];

export const GROUP_META: Record<BoardGroup, { label: string; hint: string; tone: "fail" | "ok" | "muted" | "warn" }> = {
  needs_you: {
    label: "Needs you",
    hint: "The harness stopped and labelled it needs-human. Nothing moves until you act.",
    tone: "fail",
  },
  agent: {
    label: "Agent running",
    hint: "A leg is running. The count is heavy legs against the dispatch slots; steward and triage run outside the slots.",
    tone: "ok",
  },
  ci: {
    label: "CI & GitHub",
    hint: "CI is running or queued, or the harness is about to act on GitHub next tick (open a PR, update the branch, merge, release, post triage).",
    tone: "ok",
  },
  slot: {
    label: "Waiting for a slot",
    hint: "Ready to run; it starts when a dispatch slot frees up.",
    tone: "warn",
  },
  waiting: {
    label: "Waiting",
    hint: "Held by branch protection, a human review, spend headroom, a retry backoff, an open blocking issue, GitHub or a pending harness reload. Nothing for you to do.",
    tone: "muted",
  },
};

export type BoardTone = "fail" | "ok" | "muted" | "warn";

/** CI waiting on a runner reads yellow like the slot lane: nothing is
 * executing yet, unlike the green of work in progress. */
export function boardTone(board: BoardStatus): BoardTone {
  return board.status === "working" && board.activity === "ci_queued" ? "warn" : GROUP_META[board.group].tone;
}

export const COLUMN_HEADINGS: Record<BoardGroup, { now: string; wait: string }> = {
  needs_you: { now: "What to do", wait: "Waiting" },
  agent: { now: "Now", wait: "In step" },
  ci: { now: "Now", wait: "In step" },
  slot: { now: "Waiting for", wait: "Waiting" },
  waiting: { now: "Waiting for", wait: "Waiting" },
};

/** The four stages, in rail order: the same track the issue page draws. */
export const STAGES = CANONICAL;

export function stageLabel(id: StageId): string {
  return STAGES.find((stage) => stage.id === id)?.label ?? id;
}

/** One board row: a harness item and the state the server derived for it. */
export interface BoardItem {
  issue: number;
  title: string | null;
  url: string | null;
  pr: number | null;
  prUrl: string | null;
  board: BoardStatus;
  rail: StageRail;
  labels: string[];
  card: IssueCard;
}

/**
 * A backlog promotion the client shows before the next observation confirms
 * it, worded as the server words a queued item.
 */
export function queuedCard(card: BacklogCard): IssueCard {
  return {
    issue: card.issue,
    title: card.title,
    url: card.url,
    labels: Array.from(new Set([...card.labels, "ai-ready"])),
    pr: null,
    pr_url: null,
    board: {
      status: "waiting",
      group: "slot",
      on: "slot",
      headline: "Queued",
      reason: "Starts when the fixer picks it up.",
      since_ts: null,
    },
    action: null,
    rail: { current: "resolve", furthest: "resolve", resolve_attempts: 0, review_rounds: 0, ci_runs: 0, steps: 0, live: false },
    history: [],
    cost_usd: 0,
    estimated: false,
    legs: 0,
    started_ts: null,
    last_ts: null,
    leg_running: false,
  };
}

/** The in-flight rows, once per issue. */
export function boardItems(payload: Pick<IssuesPayload, "in_flight"> | null | undefined): BoardItem[] {
  if (!payload) return [];
  const seen = new Set<number>();
  const items: BoardItem[] = [];
  for (const card of payload.in_flight ?? []) {
    if (seen.has(card.issue)) continue;
    seen.add(card.issue);
    items.push({
      issue: card.issue,
      title: card.title,
      url: card.url,
      pr: card.pr,
      prUrl: card.pr_url,
      board: card.board,
      rail: card.rail,
      labels: card.labels ?? [],
      card,
    });
  }
  return items;
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
    const members = items.filter((item) => item.board.group === group);
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
  const counts: Record<BoardGroup, number> = { needs_you: 0, agent: 0, ci: 0, slot: 0, waiting: 0 };
  for (const item of items) counts[item.board.group] += 1;
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
    item.board.status === "needs_you" ? item.board.detail : item.board.reason,
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
 * The fourth column. Needs you, Waiting for a slot and Waiting count how long
 * the row has waited on the operator or its blocker; the agent and CI lanes
 * say how long the current step has run. A row with no start says what it is
 * instead: a slot row is queued, a needs-you row (a park, say, which has no
 * stop comment to date it) waits on you.
 */
const UNDATED: Record<BoardGroup, string> = { needs_you: "needs you", slot: "queued", agent: "—", ci: "—", waiting: "—" };

function stepName(board: BoardStatus): string {
  return board.status === "working" ? board.headline.toLowerCase() : "step";
}

export function waitCell(board: BoardStatus, nowMs: number): WaitCell {
  const since = parseTs(board.since_ts);
  if (since === null || !Number.isFinite(nowMs)) return { text: UNDATED[board.group], stale: false, title: null };
  const seconds = Math.max(0, (nowMs - since) / 1000);
  const text = waitCompact(seconds);
  if (board.status === "working") {
    return { text, stale: seconds >= DAY_S, title: `In ${stepName(board)} for ${text}` };
  }
  return {
    text,
    stale: seconds >= DAY_S,
    title: board.status === "needs_you" ? `Waiting on you for ${text}` : `Waiting for ${text}`,
  };
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
  asked: { glyph: "?", label: "asked a question" },
  done: { glyph: "✓", label: "done" },
  running: { glyph: "…", label: "running" },
};

/* ------------------------------------------------------------ the one action */

export type PrimaryAction =
  | { kind: "open_issue"; label: "Open issue" }
  | { kind: "open_pr"; label: "Open PR" }
  | { kind: "unblock"; label: "Unblock" };

/**
 * At most one action per row, and only on Needs you: the server says whether
 * a reply (open the issue or PR) or removing `needs-human` (Unblock) releases
 * the stop.
 */
export function primaryAction(board: BoardStatus, action: IssueCard["action"]): PrimaryAction | null {
  if (board.status !== "needs_you") return null;
  if (action === "unblock") return { kind: "unblock", label: "Unblock" };
  return board.target === "pr" ? { kind: "open_pr", label: "Open PR" } : { kind: "open_issue", label: "Open issue" };
}

/** Where the headline links, if anywhere: a blocked wait names its blocker. */
export function headlineUrl(board: BoardStatus): string | null {
  return board.status === "waiting" ? (board.headline_url ?? null) : null;
}

/** The row's second line: why it is in its group, in one line. */
export function sayLine(board: BoardStatus): string {
  return board.status === "needs_you" ? board.hint : board.reason;
}

/** The harness comment as plain text: markdown emphasis markers dropped. */
export function plainHarnessMessage(message: string | null | undefined): string | null {
  const text = message?.replaceAll("**", "").replaceAll("__", "").trim();
  return text ? text : null;
}
