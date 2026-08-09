/**
 * The semantic layer: everything that maps a domain value to a *tone*.
 *
 * Tones are the only colour vocabulary components speak (they become
 * `chip-<tone>` classes / `var(--<tone>)`), so the contract's mapping —
 * green = ok/done/approve, yellow = running/pending, red = failed/escalated/
 * blocked, blue = informational/queued, magenta = steward/meta/self,
 * cyan = links/models — holds everywhere by construction.
 */

import type { CostMix, IssueCard, IssueFlag, RoleHealth, StepState } from "../api/types";
import { isElevatedEffort } from "./models";

export type Tone =
  | "ok" | "running" | "fail" | "info" | "meta" | "link" | "neutral"
  | "tier-luna" | "tier-terra" | "tier-sol" | "tier-high";

/* --------------------------------------------------------- leg kinds */

const KIND_LABEL: Record<string, string> = {
  RESOLVE: "Resolve",
  RESUME: "Resume",
  ROUTE: "Route",
  REVIEW: "Review",
  RE_REVIEW: "Re-review",
  FIX_CI: "Fix CI",
  ADDRESS_REVIEW: "Address review",
  REBASE: "Rebase",
  TRIAGE: "Triage",
  STEWARD_DRIVE: "Steward",
  JIRA_SYNC: "Jira sync",
};

export function kindLabel(kind: string): string {
  return KIND_LABEL[kind] ?? kind.replaceAll("_", " ").toLowerCase();
}

export const RESOLVE_KINDS = new Set(["RESOLVE", "RESUME", "ROUTE"]);
export const REVIEW_KINDS = new Set(["REVIEW", "RE_REVIEW"]);
export const REPAIR_KINDS = new Set(["FIX_CI", "ADDRESS_REVIEW", "REBASE"]);
export const STEWARD_KINDS = new Set(["TRIAGE", "STEWARD_DRIVE", "JIRA_SYNC"]);

export type LegFamily = "resolve" | "review" | "repair" | "steward" | "other";

export function familyOf(kind: string): LegFamily {
  if (RESOLVE_KINDS.has(kind)) return "resolve";
  if (REVIEW_KINDS.has(kind)) return "review";
  if (REPAIR_KINDS.has(kind)) return "repair";
  if (STEWARD_KINDS.has(kind)) return "steward";
  return "other";
}

/* ------------------------------------------------------------ states */

export function stateTone(state: StepState | null | undefined): Tone {
  switch (state) {
    case "ok":
      return "ok";
    case "failed":
    case "blocked":
      return "fail";
    case "running":
    case "advisory":
      return "running";
    default:
      return "neutral";
  }
}

/**
 * Steps the operator performed, not the harness: they cost nothing and are
 * named in prose by the backend (`unblock`, `relabel (operator)`). They read as
 * `meta` — the same tone the contract already reserves for steward/self work —
 * so a human intervention is never mistaken for an agent leg.
 */
export const OPERATOR_STEP_KINDS = new Set([
  "unblock",
  "unblock (api)",
  "relabel (operator)",
  "review re-arm",
  "review re-arm (api)",
]);

export function isOperatorStep(kind: string): boolean {
  return OPERATOR_STEP_KINDS.has(kind.toLowerCase());
}

/** An operator action driven over the bare API (an agent, a script) rather
 * than a human in the UI. Badged distinctly so automation's hand on the
 * levers is never mistaken for the operator's judgement. */
export function isApiOperatorStep(kind: string): boolean {
  return isOperatorStep(kind) && kind.toLowerCase().endsWith("(api)");
}

/** A step's chip tone: who ran it first, then how it ended. */
export function stepKindTone(
  kind: string,
  state: StepState | null | undefined,
): Tone {
  if (isApiOperatorStep(kind)) return "running";
  return isOperatorStep(kind) ? "meta" : stateTone(state);
}

/** `stage_for` is a heuristic string; classify it rather than enumerate it. */
export function stageTone(stage: string | null | undefined): Tone {
  if (!stage) return "neutral";
  const s = stage.toLowerCase();
  if (/(done|merged|approved|complete)/.test(s)) return "ok";
  if (/(block|fail|escalat|error|dead|stuck|human)/.test(s)) return "fail";
  if (/(queue|pending|ready|waiting)/.test(s)) return "info";
  return "running";
}

/** Terminal statuses from a leg (`APPROVE`, `REQUEST_CHANGES`, …). */
export function statusTone(status: string | null | undefined): Tone {
  if (!status) return "neutral";
  const s = status.toUpperCase();
  if (s === "APPROVE" || s === "OK" || s === "SUCCESS" || s === "DONE") return "ok";
  if (s === "REQUEST_CHANGES" || s === "CHANGES_REQUESTED") return "running";
  if (/(FAIL|ERROR|BLOCK|ABORT|TIMEOUT|KILLED)/.test(s)) return "fail";
  return "info";
}

/** Test-driven commit subjects use the harness's red/green vocabulary. */
export function commitSubjectTone(subject: string): Tone {
  const prefix = subject.trim().split(":", 1)[0]?.toLowerCase();
  if (prefix === "red") return "fail";
  if (prefix === "green") return "ok";
  return "neutral";
}

/** Every flag is a problem; severity splits red from orange-ish yellow. */
export function flagTone(flag: IssueFlag): Tone {
  if (flag === "waiting") return "info";
  if (flag === "blocked") return "fail";
  // `escalated` / `spend-blocked` are lifetime counters, not live state: the
  // row-alert treatment owns "human needed NOW", so history reads as meta —
  // a done row that once escalated must not shout red forever.
  if (flag === "escalated" || flag === "spend-blocked") return "meta";
  return "running";
}

/**
 * Whether the harness has stopped and is waiting on a person.
 *
 * `human_action` is the backend's own attention bucket, so the board reads it
 * rather than re-deriving one from stages and flags. Needs-budget-review
 * arrives as `escalated` — only a cap decision resumes it — which is why there
 * is no third arm. Merge-ready is deliberately absent: nothing is stuck there,
 * the work is finished and waiting to be shipped.
 */
export function blocksOnHuman(card: Pick<IssueCard, "human_action">): boolean {
  return card.human_action === "escalated" || card.human_action === "awaiting-reply";
}

export function prStateTone(state: string | null | undefined): Tone {
  if (state === "merged") return "ok";
  if (state === "closed") return "fail";
  if (state === "open") return "info";
  return "neutral";
}

export interface ReviewMark {
  glyph: string;
  label: string;
  tone: Tone;
}

/**
 * Merge readiness as one glyph beside a PR number.
 *
 * `null` — the contract's answer whenever the PAT lacks the scope, review has
 * not spoken, or mergeability is still computing — renders NOTHING. Claiming
 * either verdict on absent data is the one wrong answer, so absence must not
 * borrow the shape of a verdict.
 */
export function prReviewMark(state: string | null | undefined): ReviewMark | null {
  if (state === "mergeable") {
    return { glyph: "✓", label: "approved and mergeable", tone: "ok" };
  }
  if (state === "changes_requested") {
    return { glyph: "±", label: "review requested changes", tone: "running" };
  }
  return null;
}

const VERIFICATION_GLYPH: Record<string, string> = {
  verified: "✓",
  misbehaved: "✕",
  pending: "…",
  retired: "⊘",
};

/** The icon-only form of a verification badge; the word stays in the title. */
export function verificationGlyph(status: string | null | undefined): string {
  return VERIFICATION_GLYPH[(status ?? "").toLowerCase()] ?? "•";
}

/**
 * The badge's hover text: both ledger verdicts and, when the ledger recorded
 * one, the prose that justifies each. A verdict with no reason still names
 * itself rather than reading as missing data.
 */
export function verificationSummary(parts: {
  issueStatus?: string | null;
  issueReason?: string | null;
  prStatus?: string | null;
  prReason?: string | null;
}): string {
  const line = (
    subject: string,
    status: string | null | undefined,
    reason: string | null | undefined,
  ) => {
    const verdict = status?.trim() || "unavailable";
    const why = reason?.trim();
    return why ? `${subject}: ${verdict} — ${why}` : `${subject}: ${verdict}`;
  };
  return [
    line("Issue", parts.issueStatus, parts.issueReason),
    line("PR", parts.prStatus, parts.prReason),
  ].join("\n");
}

/* -------------------------------------------- backlog triage taxonomy ---
 *
 * Three orthogonal axes the steward writes as GitHub labels. ONE mapping,
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
 * `enhancement` is a frontend rendering concern only — the steward taxonomy
 * that mints the labels is untouched.
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

export function combinedVerificationStatus(
  issueStatus: string | null | undefined,
  prStatus: string | null | undefined,
): string | null {
  const statuses = [issueStatus, prStatus].filter(
    (status): status is string => Boolean(status),
  );
  if (statuses.length === 0) return null;
  if (statuses.some((status) => status.toUpperCase().includes("MISBEHAVED"))) {
    return "misbehaved";
  }
  if (statuses.some((status) => status.toUpperCase().includes("PENDING"))) {
    return "pending";
  }
  if (
    statuses.length === 2
    && statuses.every((status) => status.toUpperCase().includes("VERIFIED"))
  ) {
    return "verified";
  }
  if (statuses.some((status) => status.toUpperCase().includes("RETIRED"))) {
    return "retired";
  }
  return "pending";
}

/**
 * A model badge's tone: **effort first, capability tier second.**
 *
 * Effort is the lever that costs money on any model — a `high` stance burns
 * multiples of the tokens a `medium` one does whether it runs on haiku or on
 * opus — so `tier-high` (the red end of the tier ramp) tracks the stance alone
 * and is not gated on a premium name. Below `high`, the tone names the tier.
 */
export function modelTone(model: string, effort?: string | null): Tone {
  const name = model.toLowerCase();
  if (isElevatedEffort(effort)) {
    return "tier-high";
  }
  if (name.includes("fable") || name.includes("opus")) return "tier-sol";
  if (name.includes("sol")) return "tier-sol";
  if (name.includes("terra") || name.includes("sonnet")) return "tier-terra";
  if (name.includes("luna") || name.includes("haiku")) return "tier-luna";
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
 * Fixed slot order, validated with the dataviz validator against --panel:
 * blue / bright-cyan / yellow / bright-magenta pass CVD separation, the
 * normal-vision floor and 3:1 contrast. Never reorder by size.
 */
export function mixSlices(mix: CostMix | null): MixSlice[] {
  const at = (v: number | null | undefined) => (Number.isFinite(v as number) ? (v as number) : 0);
  return [
    { family: "resolve", label: "Resolve", usd: at(mix?.resolve_usd), varName: "--series-1" },
    { family: "review", label: "Review", usd: at(mix?.review_usd), varName: "--series-2" },
    { family: "repair", label: "Repair", usd: at(mix?.repair_usd), varName: "--series-3" },
    { family: "steward", label: "Steward", usd: at(mix?.steward_usd), varName: "--series-4" },
  ];
}

/** A step badge's colour inside an issue-row progress bar. */
export function familyVar(kind: string): string {
  switch (familyOf(kind)) {
    case "resolve":
      return "--series-1";
    case "review":
      return "--series-2";
    case "repair":
      return "--series-3";
    case "steward":
      return "--series-4";
    default:
      return "--ink-faint";
  }
}
