/**
 * The semantic layer: everything that maps a domain value to a *tone*.
 *
 * Tones are the only colour vocabulary components speak (they become
 * `chip-<tone>` classes / `var(--<tone>)`), so the contract's mapping —
 * green = ok/done/approve, yellow = running/pending, red = failed/escalated/
 * blocked, blue = informational/queued, magenta = meta/self,
 * cyan = links/models — holds everywhere by construction.
 */

import type { IssueStep, KindCost, RoleHealth, TrackChip } from "../api/types";

export type Tone =
  | "ok" | "running" | "fail" | "info" | "meta" | "link" | "neutral"
  | "tier-luna" | "tier-terra" | "tier-sol" | "tier-high";

/* --------------------------------------------------------- leg kinds */

const KIND_LABEL: Record<string, string> = {
  RESOLVE: "Resolve",
  REVIEW: "Review",
  FIX_CI: "Fix CI",
  ADDRESS_REVIEW: "Address review",
  REBASE: "Rebase",
};

export function kindLabel(kind: string): string {
  return KIND_LABEL[kind] ?? kind.replaceAll("_", " ").toLowerCase();
}

export type LegFamily = "resolve" | "review" | "repair" | "other";

export function familyOf(kind: string): LegFamily {
  if (kind === "RESOLVE") return "resolve";
  if (kind === "REVIEW") return "review";
  if (kind === "FIX_CI" || kind === "ADDRESS_REVIEW" || kind === "REBASE") return "repair";
  return "other";
}

/* ---------------------------------------------------------- outcomes */

export type Outcome = TrackChip["outcome"];

export interface StatusMark {
  glyph: string;
  label: string;
  tone: Tone;
}

/**
 * The shared icon + colour vocabulary for a leg's outcome. A review that
 * requested changes did its job, but green would read as "converged": it is
 * yellow, the loop still going. An agent's question is the operator's to
 * answer, so it reads as an alarm.
 */
const OUTCOME_MARKS: Record<Outcome, StatusMark> = {
  running: { glyph: "…", label: "running", tone: "running" },
  passed: { glyph: "✓", label: "approved", tone: "ok" },
  changes_requested: { glyph: "±", label: "changes requested", tone: "running" },
  asked: { glyph: "?", label: "asked a question", tone: "fail" },
  failed: { glyph: "×", label: "failed", tone: "fail" },
  done: { glyph: "✓", label: "completed", tone: "ok" },
};

export function outcomeMark(outcome: Outcome): StatusMark {
  return OUTCOME_MARKS[outcome];
}

/** What a leg reported, as text: the agent's question, the review, or its summary. */
export function legNote(step: Pick<IssueStep, "question" | "verdict_excerpt" | "summary">): string | null {
  const text = step.question ?? step.verdict_excerpt ?? step.summary ?? null;
  return text?.trim() ? text.trim() : null;
}

export function prStateTone(state: string | null | undefined): Tone {
  if (state === "merged") return "ok";
  if (state === "closed") return "fail";
  if (state === "open") return "info";
  return "neutral";
}

/* -------------------------------------------- backlog triage taxonomy ---
 *
 * Three orthogonal axes carried as GitHub labels. ONE mapping,
 * consumed by both the backlog columns and the label chips, so a row can never
 * say two different things about the same value:
 *
 *   difficulty — what it costs us. trivial/low = ok (cheap), standard/medium =
 *                running (attention), complex/high = fail (alarm: this is where
 *                cost per merged PR goes wrong).
 *   impact     — what it is worth. Ascending value, never severity:
 *                low = neutral, medium = info, high = ok (worth doing).
 *   type       — identity, not severity. bug = fail (something is broken),
 *                enhancement = ok, feature = info, docs/refactor/chore/test =
 *                meta (housekeeping).
 *
 * Every table is total: an unrecognised word tones neutral rather than
 * guessing, because these values come from operator-authored labels.
 * `enhancement` is a frontend rendering concern only.
 */

const TYPE_TONES: Record<string, Tone> = {
  bug: "fail",
  defect: "fail",
  regression: "fail",
  incident: "fail",
  enhancement: "ok",
  improvement: "ok",
  feature: "info",
  question: "info",
  docs: "meta",
  documentation: "meta",
  refactor: "meta",
  chore: "meta",
  test: "meta",
  tests: "meta",
};

const DIFFICULTY_TONES: Record<string, Tone> = {
  trivial: "ok",
  easy: "ok",
  low: "ok",
  small: "ok",
  standard: "running",
  moderate: "running",
  medium: "running",
  complex: "fail",
  exceptional: "fail",
  hard: "fail",
  high: "fail",
  large: "fail",
};

const IMPACT_TONES: Record<string, Tone> = {
  low: "neutral",
  minor: "neutral",
  medium: "info",
  moderate: "info",
  high: "ok",
  major: "ok",
  critical: "ok",
};

function lookupTone(table: Record<string, Tone>, value: string | null | undefined): Tone {
  if (typeof value !== "string") return "neutral";
  return table[value.trim().toLowerCase()] ?? "neutral";
}

export function issueTypeTone(value: string | null | undefined): Tone {
  return lookupTone(TYPE_TONES, value);
}

export function difficultyTone(value: string | null | undefined): Tone {
  return lookupTone(DIFFICULTY_TONES, value);
}

export function impactTone(value: string | null | undefined): Tone {
  return lookupTone(IMPACT_TONES, value);
}

/** Prefixes the backend's `_label_value` reads, mirrored for the chip colour. */
const LABEL_AXES: { prefixes: string[]; tone: (value: string) => Tone }[] = [
  { prefixes: ["type:", "kind:"], tone: issueTypeTone },
  { prefixes: ["difficulty:", "effort:"], tone: difficultyTone },
  { prefixes: ["impact:"], tone: impactTone },
];

/**
 * A raw GitHub label's tone. Labels that encode one of the triage axes take
 * that axis's colour so the chip and the column agree; everything else stays
 * neutral, which is what most labels are.
 */
export function labelTone(label: string): Tone {
  const value = label.trim().toLowerCase();
  for (const axis of LABEL_AXES) {
    for (const prefix of axis.prefixes) {
      if (value.startsWith(prefix) && value.length > prefix.length) {
        return axis.tone(value.slice(prefix.length));
      }
    }
  }
  // Bare `bug` / `enhancement` / `feature` are the taxonomy without a prefix —
  // the same shortcut the backend's `exact` set already honours.
  return TYPE_TONES[value] ?? "neutral";
}

export function verificationTone(status: string | null | undefined): Tone {
  if (!status) return "neutral";
  const s = status.toUpperCase();
  if (s.includes("VERIFIED")) return "ok";
  if (s.includes("MISBEHAVED")) return "fail";
  if (s.includes("PENDING")) return "running";
  return "neutral";
}

/* ----------------------------------------------------- daemon health */

export type HealthState = "ok" | "stale" | "dead" | "unknown";

export function roleHealth(role: RoleHealth): HealthState {
  if (role.alive === null) return "unknown";
  if (!role.alive) return "dead";
  return role.stale ? "stale" : "ok";
}

export const HEALTH_TONE: Record<HealthState, Tone> = {
  ok: "ok",
  stale: "running",
  dead: "fail",
  unknown: "neutral",
};

export const HEALTH_LABEL: Record<HealthState, string> = {
  ok: "ok",
  stale: "stale",
  dead: "dead",
  unknown: "unknown",
};

/* ------------------------------------------------------ utilization */

/** Meter severity thresholds: green < 60 %, yellow < 85 %, red at/above. */
export function utilTone(pct: number | null | undefined): Tone {
  if (pct === null || pct === undefined || !Number.isFinite(pct)) return "neutral";
  if (pct >= 85) return "fail";
  if (pct >= 60) return "running";
  return "ok";
}

/* -------------------------------------------------------- cost mix */

export interface MixSlice {
  family: Exclude<LegFamily, "other">;
  label: string;
  usd: number;
  /** fixed slot — assigned by identity, never by rank */
  varName: string;
}

/**
 * Spend by leg family, from the per-kind fold. Fixed slot order, validated
 * with the dataviz validator against --panel: blue / bright-cyan / yellow
 * pass CVD separation, the normal-vision floor and 3:1 contrast. Never
 * reorder by size.
 */
export function mixSlices(byKind: readonly KindCost[] | null | undefined): MixSlice[] {
  const total = (family: LegFamily) => (byKind ?? [])
    .filter((row) => familyOf(row.kind) === family && Number.isFinite(row.usd))
    .reduce((sum, row) => sum + row.usd, 0);
  return [
    { family: "resolve", label: "Resolve", usd: total("resolve"), varName: "--series-1" },
    { family: "review", label: "Review", usd: total("review"), varName: "--series-2" },
    { family: "repair", label: "Repair", usd: total("repair"), varName: "--series-3" },
  ];
}
