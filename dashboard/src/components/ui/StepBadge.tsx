import { Fragment } from "react";
import type { CardStep } from "../../api/types";
import { kindLabel, stepKindTone, isOperatorStep } from "../../lib/semantics";
import { usd } from "../../lib/format";
import { Chip } from "./Chip";
import { tipProps } from "./Tip";

/**
 * A review that finished by asking for changes did its job, but green would
 * read as "converged" — the verdict outranks the exit for the badge's colour.
 * Yellow = the loop is still going; green stays for approvals and clean legs.
 */
function episodeTone(step: CardStep) {
  if (step.state === "ok" && step.status?.toUpperCase().includes("REQUEST_CHANGES")) {
    return "running";
  }
  return stepKindTone(step.kind, step.state);
}

function episodeTitle(step: CardStep): string {
  const count = step.count ?? 1;
  return [
    step.kind,
    count > 1 ? `${count} attempts` : null,
    step.state,
    step.status,
    step.cost_usd ? usd(step.cost_usd) : null,
  ]
    .filter(Boolean)
    .join(" · ");
}

/**
 * A leg badge uses the explicit operation name: operators should not need to
 * decode RES/RTE/ADR while diagnosing an incident.
 */
export function StepBadge({ step }: { step: CardStep }) {
  const tone = episodeTone(step);
  const count = step.count ?? 1;
  return (
    <Chip tone={tone} square pulse={step.state === "running"} title={episodeTitle(step)}>
      {kindLabel(step.kind)}
      {count > 1 ? <span className="step-count">×{count}</span> : null}
    </Chip>
  );
}

/**
 * Labelled chips are only worth their width while the story is short; past
 * that they overwhelm the cell (keifu #34: ~25 episodes wrapped a table row
 * to twenty lines). The tail stays labelled because "what just happened" is
 * what an operator scans a row for; earlier history compresses into
 * state-coloured dots that still show HOW MUCH churn and WHERE it went red.
 */
const TAIL_CHIPS = 3;
const MAX_DOTS = 14;

function HistoryDot({ step }: { step: CardStep }) {
  const tone = episodeTone(step);
  const operator = isOperatorStep(step.kind);
  return (
    <span
      className={`step-dot step-dot-${tone}${operator ? " step-dot-op" : ""}`}
      {...tipProps(episodeTitle(step), { labelled: true })}
    />
  );
}

/**
 * The tasks cell is a condensed timeline of the drill-down, in the order
 * things happened. Short stories render as labelled chips joined by faint
 * chevrons; long ones compress their head into per-episode dots (colour =
 * outcome, hollow = operator intervention) and keep labels for the last few
 * episodes. Retries live inside their episode's ×N; the full sequence stays
 * on hover and in the drill-down.
 */
export function StepBadgeRow({ steps }: { steps: CardStep[] }) {
  if (steps.length === 0) return <span className="faint">—</span>;
  if (steps.length <= TAIL_CHIPS + 2) {
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
  const tail = steps.slice(-TAIL_CHIPS);
  const history = steps.slice(0, -TAIL_CHIPS);
  const dots = history.slice(-MAX_DOTS);
  const elided = history.length - dots.length;
  return (
    <span className="step-flow">
      {elided > 0 ? (
        <span className="step-elided faint" {...tipProps(`${elided} earlier steps — expand the row for the full timeline`, { labelled: true })}>
          +{elided}
        </span>
      ) : null}
      {dots.map((s, i) => (
        <HistoryDot key={`dot-${s.kind}-${i}`} step={s} />
      ))}
      <span className="step-arrow" aria-hidden="true">
        ›
      </span>
      {tail.map((s, i) => (
        <Fragment key={`tail-${s.kind}-${i}`}>
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
