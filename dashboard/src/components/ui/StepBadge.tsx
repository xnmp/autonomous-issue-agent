import type { TrackChip } from "../../api/types";
import { kindLabel, outcomeMark, type Outcome } from "../../lib/semantics";
import { usd } from "../../lib/format";
import { Chip } from "./Chip";

export function CiRunningBadge({ state, hasPullRequest }: {
  state?: "passing" | "failing" | "pending" | null;
  hasPullRequest: boolean;
}) {
  return hasPullRequest && state === "pending"
    ? <Chip tone="running" square pulse title="Checks pending on the current PR head">CI running</Chip>
    : null;
}

/** A leg, or consecutive identical legs folded into one chip. */
export interface BadgeStep {
  kind: string;
  outcome: Outcome;
  count?: TrackChip["count"];
  cost_usd?: number | null;
}

function badgeTitle(step: BadgeStep): string {
  const count = step.count ?? 1;
  return [
    kindLabel(step.kind),
    count > 1 ? `${count} attempts` : null,
    outcomeMark(step.outcome).label,
    step.cost_usd ? usd(step.cost_usd) : null,
  ]
    .filter(Boolean)
    .join(" · ");
}

/**
 * A leg badge uses the explicit operation name: operators should not need to
 * decode abbreviations while diagnosing an incident.
 */
export function StepBadge({ step, label, title }: { step: BadgeStep; label?: string; title?: string }) {
  const mark = outcomeMark(step.outcome);
  const count = step.count ?? 1;
  return (
    <Chip tone={mark.tone} square pulse={step.outcome === "running"} title={title ?? badgeTitle(step)}>
      <span className="status-mark" data-status-glyph={mark.glyph} aria-hidden="true" />
      <span className="step-label">{label ?? kindLabel(step.kind)}</span>
      {count > 1 ? <span className="step-count">×{count}</span> : null}
    </Chip>
  );
}

/** A leg's outcome in words, with the same mark and tone as its badge. */
export function StepStatusChip({ outcome }: { outcome: Outcome }) {
  const mark = outcomeMark(outcome);
  return (
    <Chip tone={mark.tone} title={mark.label}>
      <span className="status-mark" data-status-glyph={mark.glyph} aria-hidden="true" />
      {mark.label}
    </Chip>
  );
}
