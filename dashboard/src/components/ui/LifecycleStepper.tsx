import type { CardStep, IssueStep } from "../../api/types";
import { duration, usd } from "../../lib/format";
import { kindLabel, stepOutcomeMark, type Tone } from "../../lib/semantics";
import { tipProps } from "./Tip";

export type LifecycleStageId = "triage" | "resolve" | "review" | "ci" | "merge";
export type LifecycleStageState = "completed" | "active" | "failed" | "blocked" | "future";
export type LifecycleCiState = "passing" | "failing" | "pending" | null;

type TimelineStep = CardStep | IssueStep;

interface LifecycleStage {
  id: LifecycleStageId;
  label: string;
  state: LifecycleStageState;
  attempts: number;
  steps: TimelineStep[];
  selectedStepIndex: number | null;
  tone: Tone;
  glyph: string | null;
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
  if (/(review|address|change.*request)/.test(normalized)) return "review";
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
  ciState = null,
}: {
  steps: TimelineStep[];
  stage?: string | null;
  done?: boolean;
  hasPullRequest?: boolean;
  attention?: "blocked" | "escalated" | null;
  ciState?: LifecycleCiState;
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
    } else if (id === "ci") {
      if (ciState === "passing") state = "completed";
      else if (ciState === "failing") state = "failed";
      else if (ciState === "pending") state = "active";
      else if (!done && id === current) state = "active";
      else if (latest && isRunning(latest)) state = "active";
      else if (latest && isFailed(latest)) state = "failed";
    } else if (!done && id === current) {
      state = latest && isFailed(latest) ? "failed" : "active";
    } else if (latest && isRunning(latest)) {
      state = "active";
    } else if (latest && isFailed(latest)) {
      state = "failed";
    } else if (
      done
      || (currentIndex > canonicalIndex && canonicalIndex >= 0)
      || (
        lastObservedIndex > canonicalIndex
        && stageSteps.length > 0
        && stageSteps.every(isComplete)
      )
      || (stageSteps.length > 0 && stageSteps.every(isComplete) && id !== attentionTarget)
    ) {
      state = "completed";
    }
    // A FIX_CI task's outcome describes the repair agent, not the checks on
    // the resulting head. CI's node must therefore take both its state and
    // glyph from the current-head rollup above.
    const mark = latest && id !== "ci"
      ? stepOutcomeMark(latest.kind, latest.state, latest.status)
      : null;
    return {
      id,
      label,
      state,
      attempts: stageSteps.reduce((total, step) => total + countFor(step), 0),
      steps: stageSteps,
      selectedStepIndex: latest ? indexFor(latest) : null,
      tone: mark?.tone ?? (
        state === "failed" || state === "blocked"
          ? "fail"
          : state === "active"
            ? "running"
            : state === "completed"
              ? "ok"
              : "neutral"
      ),
      glyph: mark?.glyph ?? null,
    };
  });
}

function detailFor(stage: LifecycleStage): string {
  const tasks = stage.steps.map((step) => {
    const attempts = countFor(step);
    return [
      step.kind,
      step.state,
      step.status && step.status !== step.state ? step.status : null,
      attempts > 1 ? `${attempts} attempts` : null,
    ].filter(Boolean).join(" · ");
  });
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
    tasks.length > 0 ? `tasks ${tasks.join(" → ")}` : null,
    models.length > 0 ? `model ${models.join(", ")}` : null,
    verdicts.length > 0 ? `verdict ${verdicts.join(" → ")}` : null,
    durations.length > 0 ? `duration ${duration(durations.reduce((total, value) => total + value, 0))}` : null,
    cost > 0 ? `cost ${usd(cost)}` : null,
  ].filter(Boolean).join("\n");
}

function StateGlyph({ state, glyph }: { state: LifecycleStageState; glyph: string | null }) {
  if (glyph) {
    return <span className="status-mark" data-status-glyph={glyph} />;
  }
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

const COMPACT_TASK_TAIL = 5;

function taskDetailFor(step: TimelineStep): string {
  const attempts = countFor(step);
  const model = (step as { model?: unknown }).model;
  const durationS = (step as { duration_s?: unknown }).duration_s;
  return [
    step.kind,
    step.state,
    step.status && step.status !== step.state ? step.status : null,
    attempts > 1 ? `${attempts} attempts` : null,
    typeof model === "string" && model ? `model ${model}` : null,
    typeof durationS === "number" && Number.isFinite(durationS)
      ? `duration ${duration(durationS)}`
      : null,
    step.cost_usd ? `cost ${usd(step.cost_usd)}` : null,
  ].filter(Boolean).join(" · ");
}

function taskLifecycleState(step: TimelineStep, tone: Tone): LifecycleStageState {
  if (step.state === "running") return "active";
  if (tone === "fail") return step.state === "blocked" ? "blocked" : "failed";
  return "completed";
}

function CompactTaskLifecycle({
  steps,
  label,
}: {
  steps: TimelineStep[];
  label: string;
}) {
  if (steps.length === 0) return <span className="faint">no tasks yet</span>;
  const hidden = steps.length > COMPACT_TASK_TAIL
    ? steps.slice(0, -COMPACT_TASK_TAIL)
    : [];
  const shown = hidden.length > 0 ? steps.slice(-COMPACT_TASK_TAIL) : steps;
  return (
    <ol className="lifecycle lifecycle-compact lifecycle-task-compact" aria-label={label}>
      {hidden.length > 0 ? (
        <li className="life-stage life-future life-tone-neutral life-elided">
          <span
            className="life-node"
            tabIndex={0}
            {...tipProps(hidden.map(taskDetailFor).join("\n"), { labelled: true })}
          >
            <span className="life-node-ring life-elided-count">+{hidden.length}</span>
            <span className="life-compact-label">earlier</span>
          </span>
        </li>
      ) : null}
      {shown.map((step, index) => {
        const mark = stepOutcomeMark(step.kind, step.state, step.status);
        const state = taskLifecycleState(step, mark.tone);
        const attempts = countFor(step);
        return (
          <li
            className={`life-stage life-${state} life-tone-${mark.tone}${index > 0 || hidden.length > 0 ? " life-connected" : ""}`}
            key={`${step.kind}-${indexFor(step) ?? index}`}
          >
            <span className="life-node" {...tipProps(taskDetailFor(step), { labelled: true })}>
              <span className="life-node-ring">
                <StateGlyph state={state} glyph={mark.glyph} />
              </span>
              <span className="life-compact-label">{kindLabel(step.kind)}</span>
            </span>
            {attempts > 1 ? <RetryArc count={attempts} /> : null}
          </li>
        );
      })}
    </ol>
  );
}

export function LifecycleStepper({
  steps,
  stage,
  done = false,
  hasPullRequest = false,
  attention = null,
  ciState = null,
  compact = false,
  label = "Issue lifecycle",
  onSelectStep,
}: {
  steps: TimelineStep[];
  stage?: string | null;
  done?: boolean;
  hasPullRequest?: boolean;
  attention?: "blocked" | "escalated" | null;
  ciState?: LifecycleCiState;
  compact?: boolean;
  label?: string;
  onSelectStep?: (index: number) => void;
}) {
  if (compact) return <CompactTaskLifecycle steps={steps} label={label} />;
  const stages = deriveLifecycleStages({
    steps,
    stage,
    done,
    hasPullRequest,
    attention,
    ciState,
  });
  return (
    <ol className={`lifecycle${compact ? " lifecycle-compact" : ""}`} aria-label={label}>
      {stages.map((item, index) => {
        const previous = stages[index - 1];
        const connected = previous?.state === "completed" && item.state !== "future";
        const retries = item.attempts > 1;
        const detail = detailFor(item);
        const content = (
          <>
            <span className="life-node-ring"><StateGlyph state={item.state} glyph={item.glyph} /></span>
            {!compact ? (
              <span className={`life-label${item.state === "active" ? " life-label-active" : ""}`}>
                {item.label}
              </span>
            ) : null}
            {!compact ? <span className="life-state-label">{item.state}</span> : null}
          </>
        );
        return (
          <li
            className={`life-stage life-${item.state} life-tone-${item.tone}${connected ? " life-connected" : ""}`}
            key={item.id}
          >
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
            {retries ? <RetryArc count={item.attempts} /> : null}
          </li>
        );
      })}
    </ol>
  );
}
