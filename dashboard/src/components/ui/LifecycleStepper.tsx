import type { CardStep, IssueStep } from "../../api/types";
import { duration, usd } from "../../lib/format";
import { tipProps } from "./Tip";

export type LifecycleStageId = "triage" | "resolve" | "review" | "ci" | "merge";
export type LifecycleStageState = "completed" | "active" | "failed" | "blocked" | "future";

type TimelineStep = CardStep | IssueStep;

interface LifecycleStage {
  id: LifecycleStageId;
  label: string;
  state: LifecycleStageState;
  attempts: number;
  steps: TimelineStep[];
  selectedStepIndex: number | null;
}

const CANONICAL: { id: LifecycleStageId; label: string }[] = [
  { id: "triage", label: "Triage" },
  { id: "resolve", label: "Resolve" },
  { id: "review", label: "Review" },
  { id: "ci", label: "CI" },
  { id: "merge", label: "Merge" },
];

function stageForKind(kind: string): LifecycleStageId | null {
  const normalized = kind.trim().toUpperCase();
  if (["TRIAGE", "STEWARD_DRIVE", "JIRA_SYNC"].includes(normalized)) return "triage";
  if (["RESOLVE", "RESUME", "ROUTE"].includes(normalized)) return "resolve";
  if (["REVIEW", "RE_REVIEW", "ADDRESS_REVIEW"].includes(normalized)) return "review";
  if (["FIX_CI", "REBASE", "CI"].includes(normalized)) return "ci";
  if (normalized === "MERGE") return "merge";
  return null;
}

function stageForStatus(stage: string | null | undefined): LifecycleStageId | null {
  const normalized = stage?.trim().toLowerCase() ?? "";
  if (/(triage|steward|backlog|route)/.test(normalized)) return "triage";
  if (/(review|address)/.test(normalized)) return "review";
  if (/(\bci\b|check|rebase)/.test(normalized)) return "ci";
  if (/(merge|done|complete)/.test(normalized)) return "merge";
  if (/(resolve|fix|implement|running)/.test(normalized)) return "resolve";
  return null;
}

function countFor(step: TimelineStep): number {
  const value = (step as { count?: unknown }).count;
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : 1;
}

function indexFor(step: TimelineStep): number | null {
  const value = (step as { idx?: unknown }).idx;
  return typeof value === "number" ? value : null;
}

function isFailed(step: TimelineStep): boolean {
  const status = step.status?.toUpperCase() ?? "";
  return step.state === "failed" || /(FAIL|ERROR|TIMEOUT|ABORT|KILLED)/.test(status);
}

function isRunning(step: TimelineStep): boolean {
  return step.state === "running";
}

function isComplete(step: TimelineStep): boolean {
  return step.state === "ok" || step.state === "done";
}

function includedStages(
  grouped: Map<LifecycleStageId, TimelineStep[]>,
  current: LifecycleStageId | null,
  done: boolean,
  hasPullRequest: boolean,
): Set<LifecycleStageId> {
  const included = new Set(grouped.keys());
  included.add("resolve");
  if (done) {
    if (hasPullRequest) included.add("merge");
    return included;
  }
  const currentIndex = current ? CANONICAL.findIndex(({ id }) => id === current) : 1;
  for (const { id } of CANONICAL) {
    const index = CANONICAL.findIndex((candidate) => candidate.id === id);
    if (id !== "triage" && index >= Math.max(1, currentIndex)) included.add(id);
  }
  if (hasPullRequest) included.add("merge");
  return included;
}

export function deriveLifecycleStages({
  steps,
  stage,
  done = false,
  hasPullRequest = false,
  attention = null,
}: {
  steps: TimelineStep[];
  stage?: string | null;
  done?: boolean;
  hasPullRequest?: boolean;
  attention?: "blocked" | "escalated" | null;
}): LifecycleStage[] {
  const grouped = new Map<LifecycleStageId, TimelineStep[]>();
  for (const step of steps) {
    const id = stageForKind(step.kind);
    if (!id) continue;
    grouped.set(id, [...(grouped.get(id) ?? []), step]);
  }
  const current = stageForStatus(stage);
  const included = includedStages(grouped, current, done, hasPullRequest);
  const currentIndex = current ? CANONICAL.findIndex(({ id }) => id === current) : -1;
  const lastObservedIndex = Math.max(
    -1,
    ...Array.from(grouped.keys()).map((id) => CANONICAL.findIndex((stageItem) => stageItem.id === id)),
  );
  const attentionTarget = current ?? CANONICAL[lastObservedIndex]?.id ?? "resolve";

  return CANONICAL.filter(({ id }) => included.has(id)).map(({ id, label }) => {
    const stageSteps = grouped.get(id) ?? [];
    const canonicalIndex = CANONICAL.findIndex((candidate) => candidate.id === id);
    const latest = stageSteps.at(-1);
    let state: LifecycleStageState = "future";
    if (!done && attention && id === attentionTarget) {
      state = attention === "blocked" ? "blocked" : "failed";
    } else if (!done && id === current) {
      state = latest && isFailed(latest) ? "failed" : "active";
    } else if (latest && isRunning(latest)) {
      state = "active";
    } else if (latest && isFailed(latest)) {
      state = "failed";
    } else if (
      done
      || (currentIndex > canonicalIndex && canonicalIndex >= 0)
      || (lastObservedIndex > canonicalIndex && stageSteps.every(isComplete))
      || (stageSteps.length > 0 && stageSteps.every(isComplete) && id !== attentionTarget)
    ) {
      state = "completed";
    }
    return {
      id,
      label,
      state,
      attempts: stageSteps.reduce((total, step) => total + countFor(step), 0),
      steps: stageSteps,
      selectedStepIndex: latest ? indexFor(latest) : null,
    };
  });
}

function detailFor(stage: LifecycleStage): string {
  const durations = stage.steps
    .map((step) => (step as { duration_s?: unknown }).duration_s)
    .filter((value): value is number => typeof value === "number" && Number.isFinite(value));
  const models = Array.from(new Set(stage.steps
    .map((step) => (step as { model?: unknown }).model)
    .filter((value): value is string => typeof value === "string" && value.length > 0)));
  const verdicts = Array.from(new Set(stage.steps
    .map((step) => step.status)
    .filter((value): value is string => typeof value === "string" && value.length > 0)));
  const cost = stage.steps.reduce((total, step) => total + (step.cost_usd ?? 0), 0);
  return [
    `${stage.label} · ${stage.state}${stage.attempts > 1 ? ` · ${stage.attempts} attempts` : ""}`,
    models.length > 0 ? `model ${models.join(", ")}` : null,
    verdicts.length > 0 ? `verdict ${verdicts.join(" → ")}` : null,
    durations.length > 0 ? `duration ${duration(durations.reduce((total, value) => total + value, 0))}` : null,
    cost > 0 ? `cost ${usd(cost)}` : null,
  ].filter(Boolean).join("\n");
}

function StateGlyph({ state }: { state: LifecycleStageState }) {
  if (state === "completed") {
    return <svg viewBox="0 0 16 16"><path d="m3.5 8.2 2.8 2.7 6.2-6" /></svg>;
  }
  if (state === "failed") {
    return <svg viewBox="0 0 16 16"><path d="M8 2.4 14 13H2L8 2.4Z" /><path d="M8 6v3.2m0 1.8h.01" /></svg>;
  }
  if (state === "blocked") {
    return <svg viewBox="0 0 16 16"><path d="M5.7 4.5v7m4.6-7v7" /></svg>;
  }
  return <span className="life-node-core" />;
}

function RetryArc({ count }: { count: number }) {
  return (
    <span className="life-retry" aria-label={`${count} attempts`}>
      <svg viewBox="0 0 46 18" aria-hidden="true">
        <path d="M39 3c0 8-6 12-16 12H8" />
        <path d="m12 11-4 4 4 3" />
      </svg>
      <span className="life-retry-count num">×{count}</span>
    </span>
  );
}

export function LifecycleStepper({
  steps,
  stage,
  done = false,
  hasPullRequest = false,
  attention = null,
  compact = false,
  label = "Issue lifecycle",
  onSelectStep,
}: {
  steps: TimelineStep[];
  stage?: string | null;
  done?: boolean;
  hasPullRequest?: boolean;
  attention?: "blocked" | "escalated" | null;
  compact?: boolean;
  label?: string;
  onSelectStep?: (index: number) => void;
}) {
  const stages = deriveLifecycleStages({ steps, stage, done, hasPullRequest, attention });
  return (
    <ol className={`lifecycle${compact ? " lifecycle-compact" : ""}`} aria-label={label}>
      {stages.map((item, index) => {
        const previous = stages[index - 1];
        const connected = previous?.state === "completed" && item.state !== "future";
        const retries = (item.id === "review" || item.id === "ci") && item.attempts > 1;
        const detail = detailFor(item);
        const content = (
          <>
            <span className="life-node-ring"><StateGlyph state={item.state} /></span>
            {!compact ? <span className="life-label">{item.label}</span> : null}
            {!compact ? <span className="life-state-label">{item.state}</span> : null}
          </>
        );
        return (
          <li
            className={`life-stage life-${item.state}${connected ? " life-connected" : ""}`}
            key={item.id}
          >
            {compact ? (
              <span className="life-node" {...tipProps(detail, { labelled: true })}>{content}</span>
            ) : (
              <button
                className="life-node"
                type="button"
                {...tipProps(detail)}
                onClick={item.selectedStepIndex !== null && onSelectStep
                  ? (event) => {
                      event.stopPropagation();
                      onSelectStep(item.selectedStepIndex as number);
                    }
                  : undefined}
              >
                {content}
              </button>
            )}
            {retries ? <RetryArc count={item.attempts} /> : null}
          </li>
        );
      })}
    </ol>
  );
}
