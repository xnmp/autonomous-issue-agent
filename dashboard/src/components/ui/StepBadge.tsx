import type { CardStep } from "../../api/types";
import { kindLabel, stateTone } from "../../lib/semantics";
import { usd } from "../../lib/format";
import { Chip } from "./Chip";

/**
 * A leg badge uses the explicit operation name: operators should not need to
 * decode RES/RTE/ADR while diagnosing an incident.
 */
export function StepBadge({ step }: { step: CardStep }) {
  const tone = stateTone(step.state);
  const bits = [step.kind, step.state, step.status, step.cost_usd != null ? usd(step.cost_usd) : null]
    .filter(Boolean)
    .join(" · ");
  return (
    <Chip tone={tone} square pulse={step.state === "running"} title={bits}>
      {kindLabel(step.kind)}
    </Chip>
  );
}

export function StepBadgeRow({ steps }: { steps: CardStep[] }) {
  if (steps.length === 0) return <span className="faint">—</span>;
  return (
    <span className="row-wrap">
      {steps.map((s, i) => (
        <StepBadge key={`${s.kind}-${i}`} step={s} />
      ))}
    </span>
  );
}
