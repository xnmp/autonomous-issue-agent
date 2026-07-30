/**
 * The semantic layer: everything that maps a domain value to a *tone*.
 *
 * Tones are the only colour vocabulary components speak (they become
 * `chip-<tone>` classes / `var(--<tone>)`), so the contract's mapping —
 * green = ok/done/approve, yellow = running/pending, red = failed/escalated/
 * blocked, blue = informational/queued, magenta = steward/meta/self,
 * cyan = links/models — holds everywhere by construction.
 */

import type { CostMix, IssueFlag, RoleHealth, StepState } from "../api/types";

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
      return "running";
    default:
      return "neutral";
  }
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

/** Every flag is a problem; severity splits red from orange-ish yellow. */
export function flagTone(flag: IssueFlag): Tone {
  if (flag === "waiting") return "info";
  return flag === "escalated" || flag === "spend-blocked" || flag === "blocked"
    ? "fail"
    : "running";
}

export function prStateTone(state: string | null | undefined): Tone {
  if (state === "merged") return "ok";
  if (state === "closed") return "fail";
  if (state === "open") return "info";
  return "neutral";
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

export function modelTone(model: string, effort?: string | null): Tone {
  const name = model.toLowerCase();
  const stance = (effort ?? "").toLowerCase();
  const aboveMedium = ["high", "xhigh", "max", "ultra"].includes(stance);
  const premium = name.includes("fable") || name.includes("opus") || name.includes("sol");
  if (premium && aboveMedium) {
    return "tier-high";
  }
  if (name.includes("fable") || name.includes("opus")) return "tier-sol";
  if (name.includes("sol")) return "tier-sol";
  if (name.includes("terra") || name.includes("sonnet")) return "tier-terra";
  if (name.includes("luna") || name.includes("haiku")) return "tier-luna";
  return "neutral";
}

/* ----------------------------------------------------- daemon health */

export type HealthState = "ok" | "stale" | "dead";

export function roleHealth(role: RoleHealth): HealthState {
  if (!role.alive) return "dead";
  return role.stale ? "stale" : "ok";
}

export const HEALTH_TONE: Record<HealthState, Tone> = {
  ok: "ok",
  stale: "running",
  dead: "fail",
};

export const HEALTH_LABEL: Record<HealthState, string> = {
  ok: "ok",
  stale: "stale",
  dead: "dead",
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
