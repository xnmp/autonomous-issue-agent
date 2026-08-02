import { Fragment } from "react";
import type { CardStep } from "../../api/types";
import { kindLabel, stepKindTone } from "../../lib/semantics";
import { usd } from "../../lib/format";
import { Chip } from "./Chip";

/**
 * A leg badge uses the explicit operation name: operators should not need to
 * decode RES/RTE/ADR while diagnosing an incident.
 */
export function StepBadge({ step }: { step: CardStep }) {
  const tone = stepKindTone(step.kind, step.state);
  const count = step.count ?? 1;
  const bits = [
    step.kind,
    count > 1 ? `${count} attempts` : null,
    step.state,
    step.status,
    step.cost_usd ? usd(step.cost_usd) : null,
  ]
    .filter(Boolean)
    .join(" · ");
  return (
    <Chip tone={tone} square pulse={step.state === "running"} title={bits}>
      {kindLabel(step.kind)}
      {count > 1 ? <span className="step-count">×{count}</span> : null}
    </Chip>
  );
}

/**
 * The tasks cell is a condensed timeline of the drill-down: episode chips in
 * the order things happened, joined by faint chevrons so the strip reads
 * left-to-right as a story ("resolve › review ×2 › fix CI › review") instead
 * of an unordered pile of badges. Retries live inside their episode's ×N;
 * operator interventions sit where they happened, toned as meta.
 */
export function StepBadgeRow({ steps }: { steps: CardStep[] }) {
  if (steps.length === 0) return <span className="faint">—</span>;
  return (
    <span className="step-flow">
      {steps.map((s, i) => (
        <Fragment key={`${s.kind}-${i}`}>
          {i > 0 ? (
            <span className="step-arrow" aria-hidden="true">
              ›
            </span>
          ) : null}
          <StepBadge step={s} />
        </Fragment>
      ))}
    </span>
  );
}
