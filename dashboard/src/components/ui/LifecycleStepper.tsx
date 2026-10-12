import type { CardStep, IssueStep } from "../../api/types";
import { duration, usd } from "../../lib/format";
import { stepOutcomeMark, type Tone } from "../../lib/semantics";
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

export const CANONICAL: readonly { id: LifecycleStageId; label: string }[] = [
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

/** One stage of the board's rail, from the server's stage-visit fold. */
export interface RailNode {
  id: LifecycleStageId;
  label: string;
  /** Rewound: reached earlier, then sent back (e.g. a CI fix re-reviewed). */
  state: "done" | "current" | "rewound" | "future";
  count: string | null;
  /** Above typical: amber, with `hotTip` saying why. */
  hot: boolean;
  hotTip: string | null;
}

export type RailTone = "fail" | "ok" | "muted";

/** The rail in words: the current stage with its count, then every other count. */
export function railSummary(nodes: readonly RailNode[]): string {
  const current = nodes.find((node) => node.state === "current");
  const here = current ? [current.label, current.count].filter(Boolean).join(", ") : "unknown";
  const elsewhere = nodes
    .filter((node) => node.state !== "current" && node.count)
    .map((node) => `${node.label} ${node.count}`);
  return [here, ...elsewhere].join("; ");
}

interface StepsProps {
  steps: TimelineStep[];
  stage?: string | null;
  done?: boolean;
  hasPullRequest?: boolean;
  attention?: "blocked" | "escalated" | null;
  ciState?: LifecycleCiState;
  label?: string;
  onSelectStep?: (index: number) => void;
}

interface RailProps {
  /** The projected rail; the five stages are always all drawn. */
  rail: RailNode[];
  /** The current node takes its row's group colour. */
  tone: RailTone;
  /** A leg is running: the current node pulses. */
  live: boolean;
  label?: string;
  /** For a row control that names the rail in its description. */
  id?: string;
}

/**
 * The five-stage track. The issue page derives it from the steps it shows;
 * the issues board passes the server's projected rail, with round counts.
 */
export function LifecycleStepper(props: StepsProps | RailProps) {
  return "rail" in props ? <RailStepper {...props} /> : <StepsStepper {...props} />;
}

function RailStepper({ rail, tone, live, label = "Stage", id }: RailProps) {
  const current = rail.find((node) => node.state === "current");
  return (
    <ol id={id} className={`lifecycle-rail rail-tone-${tone}`} aria-label={`${label}: ${railSummary(rail)}`}>
      {rail.map((node) => (
        <li
          key={node.id}
          className={`rail-stage rail-${node.state}${node.state === "current" && live ? " rail-live" : ""}`}
          aria-current={node.state === "current" ? "step" : undefined}
          {...(node.state === "rewound" && current
            ? tipProps(`Reached earlier; now back at ${current.label}`)
            : {})}
        >
          <span className="rail-node" aria-hidden="true" />
          <span className="rail-label">{node.label}</span>
          {node.count ? (
            <span
              className={`rail-count num${node.hot ? " rail-hot" : ""}`}
              {...(node.hotTip ? tipProps(node.hotTip) : {})}
            >
              {node.count}
            </span>
          ) : null}
        </li>
      ))}
    </ol>
  );
}

function StepsStepper({
  steps,
  stage,
  done = false,
  hasPullRequest = false,
  attention = null,
  ciState = null,
  label = "Issue lifecycle",
  onSelectStep,
}: StepsProps) {
  const stages = deriveLifecycleStages({
    steps,
    stage,
    done,
    hasPullRequest,
    attention,
    ciState,
  });
  return (
    <ol className="lifecycle" aria-label={label}>
      {stages.map((item, index) => {
        const previous = stages[index - 1];
        const connected = previous?.state === "completed" && item.state !== "future";
        const retries = item.attempts > 1;
        const detail = detailFor(item);
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
              <span className="life-node-ring"><StateGlyph state={item.state} glyph={item.glyph} /></span>
              <span className={`life-label${item.state === "active" ? " life-label-active" : ""}`}>
                {item.label}
              </span>
              <span className="life-state-label">{item.state}</span>
            </button>
            {retries ? <RetryArc count={item.attempts} /> : null}
          </li>
        );
      })}
    </ol>
  );
}
