import { Fragment } from "react";
import type { CardStep, IssueStep } from "../../api/types";
import { isOperatorStep, kindLabel, stepOutcomeMark } from "../../lib/semantics";
import { usd } from "../../lib/format";
import { Chip } from "./Chip";
import { tipProps } from "./Tip";

export function CiRunningBadge({ state, hasPullRequest }: {
  state?: "passing" | "failing" | "pending" | null;
  hasPullRequest: boolean;
}) {
  return hasPullRequest && state === "pending"
    ? <Chip tone="running" square pulse title="Checks pending on the current PR head">CI running</Chip>
    : null;
}

/**
 * A review that finished by asking for changes did its job, but green would
 * read as "converged" — the verdict outranks the exit for the badge's colour.
 * Yellow = the loop is still going; green stays for approvals and clean legs.
 */
type BadgeStep = Pick<CardStep, "kind" | "state" | "status" | "cost_usd"> & {
  count?: number | null;
};

function episodeCount(step: BadgeStep | IssueStep): number {
  return typeof step.count === "number" ? step.count : 1;
}

function episodeTitle(step: BadgeStep): string {
  const count = episodeCount(step);
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
export function StepBadge({
  step,
  label,
  title,
}: {
  step: BadgeStep | IssueStep;
  label?: string;
  title?: string;
}) {
  const mark = stepOutcomeMark(step.kind, step.state, step.status);
  const count = episodeCount(step);
  return (
    <Chip tone={mark.tone} square pulse={step.state === "running"} title={title ?? episodeTitle(step)}>
      <span className="status-mark" data-status-glyph={mark.glyph} aria-hidden="true" />
      <span className="step-label">{label ?? kindLabel(step.kind)}</span>
      {count > 1 ? <span className="step-count">×{count}</span> : null}
    </Chip>
  );
}

/** A verdict chip uses the same mark and tone as the leg chip beside it. */
export function StepStatusChip({ step }: { step: BadgeStep | IssueStep }) {
  const mark = stepOutcomeMark(step.kind, step.state, step.status);
  const label = step.status === "ok" || (!step.status && step.state === "ok")
    ? "done"
    : (step.status ?? step.state);
  return (
    <Chip tone={mark.tone} title={mark.label}>
      <span className="status-mark" data-status-glyph={mark.glyph} aria-hidden="true" />
      {label}
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
  const tone = stepOutcomeMark(step.kind, step.state, step.status).tone;
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
