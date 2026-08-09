import { useMemo, useState, type KeyboardEvent, type MouseEvent } from "react";
import { api } from "../api/client";
import { usePolled } from "../api/hooks";
import { liveClocks, offsetSince, useNow } from "../lib/clock";
import type { IssueStep } from "../api/types";
import {
  commitSubjectTone,
  modelTone,
  prStateTone,
  stageTone,
  stepOutcomeTone,
} from "../lib/semantics";
import { clockTime, duration, localTime, num, tokens, usd } from "../lib/format";
import { shortModel } from "../lib/models";
import { Chip } from "./ui/Chip";
import { JsonBlock } from "./ui/Json";
import { EmptyState, ErrorBanner, Loading, SkeletonRows, WarningList } from "./ui/Feedback";
import { EventTimeline } from "./EventTimeline";
import { UnblockModal } from "./UnblockModal";
import { ColumnMenu, type ColumnChoice } from "./ui/ColumnMenu";
import { StepBadge, StepStatusChip } from "./ui/StepBadge";
import { LifecycleStepper } from "./ui/LifecycleStepper";
import { TableFilter } from "./ui/TableFilter";
import {
  ResizableTable,
  useSortedRows,
  type TableColumn,
} from "./ui/ResizableTable";

const POLL_MS = 5_000;
const ISSUE_COLUMNS_KEY = "issue-agent.dashboard.issue-columns";
const ISSUE_COLUMNS: ColumnChoice[] = [
  { key: "role", label: "Role" },
  { key: "model", label: "Model" },
  { key: "status", label: "Status" },
  { key: "duration", label: "Duration" },
  { key: "cost", label: "Cost" },
  { key: "tokens", label: "Tokens" },
  { key: "when", label: "When" },
];

function storedIssueColumns(): Record<string, boolean> {
  const defaults = Object.fromEntries(
    ISSUE_COLUMNS.map(({ key }) => [key, key !== "role"]),
  );
  try {
    return {
      ...defaults,
      ...JSON.parse(localStorage.getItem(ISSUE_COLUMNS_KEY) ?? "{}"),
    };
  } catch {
    return defaults;
  }
}

/**
 * One issue, task by task. A task is one agent invocation.
 */
export function IssueTab({
  repo,
  githubRepo = repo,
  issue,
  showDurationSeconds = true,
}: {
  repo: string;
  githubRepo?: string;
  issue: number;
  showDurationSeconds?: boolean;
}) {
  const detail = usePolled(
    `issue:${repo}:${issue}`,
    (signal) => api.issue(repo, issue, signal),
    POLL_MS,
  );
  const [openStep, setOpenStep] = useState<number | null>(null);
  const [confirmUnblock, setConfirmUnblock] = useState(false);
  const [unblocking, setUnblocking] = useState(false);
  const [unblockError, setUnblockError] = useState<string | null>(null);
  const [pane, setPane] = useState<"tasks" | "events">("tasks");
  const [taskQuery, setTaskQuery] = useState("");
  const [columnVisibility, setColumnVisibility] = useState(storedIssueColumns);
  const [columnMenuAt, setColumnMenuAt] = useState<{ x: number; y: number } | null>(null);
  const visible = useMemo(
    () => new Set(Object.entries(columnVisibility).filter(([, shown]) => shown).map(([key]) => key)),
    [columnVisibility],
  );
  const columns: TableColumn[] = [
    { key: "operation", label: "Operation", width: 158, minWidth: 142 },
    ...(visible.has("role") ? [{ key: "role", label: "Role", width: 90 }] : []),
    ...(visible.has("model") ? [{ key: "model", label: "Model", width: 160, minWidth: 100 }] : []),
    ...(visible.has("status") ? [{ key: "status", label: "Status", width: 190, minWidth: 96 }] : []),
    ...(visible.has("duration") ? [{ key: "duration", label: "Duration", className: "r", width: 100, minWidth: 84 }] : []),
    ...(visible.has("cost") ? [{ key: "cost", label: "Cost", className: "r", width: 90, minWidth: 76 }] : []),
    ...(visible.has("tokens") ? [{ key: "tokens", label: "Tokens", width: 260 }] : []),
    ...(visible.has("when") ? [{ key: "when", label: "When", width: 150 }] : []),
  ];
  const setColumn = (key: string, shown: boolean) => {
    setColumnVisibility((current) => {
      const next = { ...current, [key]: shown };
      try {
        localStorage.setItem(ISSUE_COLUMNS_KEY, JSON.stringify(next));
      } catch {
        // In-memory preferences still work when storage is unavailable.
      }
      return next;
    });
  };
  const openColumnMenu = (event: MouseEvent<HTMLDivElement>) => {
    if (!(event.target instanceof Element) || !event.target.closest("th")) return;
    event.preventDefault();
    setColumnMenuAt({
      x: Math.min(event.clientX, window.innerWidth - 220),
      y: Math.min(event.clientY, window.innerHeight - 300),
    });
  };

  const d = detail.data;
  // Same rule as the board's rows, from the same module: whichever clock the
  // server says is still moving is the one that ticks here.
  const { working, idle } = liveClocks(
    d ?? {},
    offsetSince(detail.updatedAt, useNow()),
  );
  const filteredSteps = useMemo(() => {
    const query = taskQuery.trim().toLowerCase();
    if (!query) return d?.steps ?? [];
    return (d?.steps ?? []).filter((step) => [
      step.idx,
      step.kind,
      step.role,
      step.model,
      step.effort,
      step.status,
      step.state,
      step.note,
      step.session_id,
    ].some((value) => String(value ?? "").toLowerCase().includes(query)));
  }, [d?.steps, taskQuery]);
  const sortedSteps = useSortedRows(filteredSteps, {
    operation: (step) => step.kind,
    role: (step) => step.role,
    model: (step) => step.model,
    status: (step) => step.status ?? step.state,
    duration: (step) => step.duration_s,
    cost: (step) => step.cost_usd,
    tokens: (step) => (
      (step.usage?.input_tokens ?? 0)
      + (step.usage?.output_tokens ?? 0)
    ),
    when: (step) => step.ts,
  }, { key: "when", direction: "asc" }, "issue-tasks");

  // The detail payload carries no `human_action`; a live issue that escalated,
  // blocked, or asked a question is exactly the set an unblock applies to.
  const heldForHuman = !!d && !d.done && (
    !!d.escalation_reason
    || d.stage === "blocked"
    || d.stage === "escalated"
    || d.stage === "awaiting human reply"
  );
  const doUnblock = async () => {
    setUnblocking(true);
    setUnblockError(null);
    try {
      await api.unblock(repo, issue);
      setConfirmUnblock(false);
      detail.refresh();
    } catch (caught) {
      setUnblockError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setUnblocking(false);
    }
  };

  return (
    <div className="tabbody" onContextMenu={openColumnMenu}>
      {detail.error ? (
        <ErrorBanner message={detail.error} onRetry={detail.refresh} stale={!!d} />
      ) : null}
      <WarningList warnings={d?.warnings} />
      {detail.loading ? <SkeletonRows what={`issue #${issue}`} rows={6} /> : null}

      {d ? (
        <>
          <header className="issue-hd panel">
            <div className="issue-hd-line issue-hd-primary">
              <a
                className="issue-num mono"
                href={`https://github.com/${githubRepo.split("/").map(encodeURIComponent).join("/")}/issues/${d.issue}`}
                target="_blank"
                rel="noopener"
                title={`Open issue #${d.issue} on GitHub`}
              >
                #{d.issue}
              </a>
              <span className="issue-title trunc" title={d.title ?? undefined}>
                {d.title ?? <span className="faint">title unavailable (GitHub data)</span>}
              </span>
              <span className="spacer" />
              {d.stage && !d.done ? (
                <Chip tone={stageTone(d.stage)} pulse={!d.done && stageTone(d.stage) === "running"}>
                  {d.stage}
                </Chip>
              ) : null}
              {d.done ? <Chip tone="ok">done</Chip> : null}
            </div>
            <div className="issue-hd-line issue-hd-meta">
              {d.stage === "awaiting human reply" && d.attention_reason ? (
                <span className="issue-attention">{d.attention_reason}</span>
              ) : null}
              {heldForHuman ? (
                <button
                  className="btn btn-xs"
                  onClick={() => {
                    setUnblockError(null);
                    setConfirmUnblock(true);
                  }}
                >
                  Unblock
                </button>
              ) : null}
              {d.pull_requests.map((pr) =>
                pr.url ? (
                  <a
                    key={pr.number}
                    href={pr.url}
                    target="_blank"
                    rel="noreferrer"
                    title={`open PR #${pr.number} on GitHub`}
                  >
                    <Chip tone={prStateTone(pr.state)} square>
                      PR #{pr.number}
                    </Chip>
                  </a>
                ) : (
                  <Chip key={pr.number} tone={prStateTone(pr.state)} square>
                    PR #{pr.number}
                  </Chip>
                ),
              )}
              <span className="spacer" />
              <span className="issue-stat">
                <span className="faint">cost</span>{" "}
                <span className="mono">{usd(d.cost_usd)}</span>
              </span>
              <span className="issue-stat">
                <span className="faint">working</span>{" "}
                <span className="mono">{duration(working, showDurationSeconds)}</span>
              </span>
              <span className="issue-stat">
                <span className="faint">idle</span>{" "}
                <span className="mono">{duration(idle, showDurationSeconds)}</span>
              </span>
              <span className="issue-stat">
                <span className="faint">tasks</span>{" "}
                <span className="mono">{d.steps.length}</span>
              </span>
            </div>
          </header>

          <section className="panel issue-lifecycle-panel" aria-labelledby={`issue-${issue}-lifecycle-title`}>
            <div className="panel-hd">
              <span className="panel-title" id={`issue-${issue}-lifecycle-title`}>Lifecycle</span>
              <span className="faint num">{d.steps.length} execution legs</span>
            </div>
            <LifecycleStepper
              steps={d.steps}
              stage={d.stage}
              done={d.done}
              hasPullRequest={d.prs.length > 0 || d.pull_requests.length > 0}
              ciState={d.pr_ci_state ?? null}
              attention={d.stage === "awaiting human reply" || d.stage === "blocked"
                ? "blocked"
                : d.escalation_reason || d.stage === "escalated"
                  ? "escalated"
                  : null}
              label={`Issue #${issue} lifecycle`}
              onSelectStep={(index) => {
                setPane("tasks");
                setOpenStep(index);
              }}
            />
          </section>

          {d.escalation_reason ? (
            <div className="banner banner-error" role="status">
              <span className="banner-mark" aria-hidden="true">!</span>
              <span className="banner-msg">
                <strong>Escalated to a human.</strong> {d.escalation_reason}
              </span>
            </div>
          ) : null}
          {unblockError ? <ErrorBanner message={unblockError} /> : null}

          <div className="subtabs issue-section-tabs" role="tablist" aria-label="Issue detail">
            {(["tasks", "events"] as const).map((option) => (
              <button
                key={option}
                id={`issue-${issue}-${option}-tab`}
                className={`subtab${pane === option ? " active" : ""}`}
                role="tab"
                aria-selected={pane === option}
                aria-controls={`issue-${issue}-${option}-panel`}
                tabIndex={pane === option ? 0 : -1}
                onClick={() => setPane(option)}
                onKeyDown={(event) => {
                  if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
                  event.preventDefault();
                  const next = option === "tasks" ? "events" : "tasks";
                  setPane(next);
                  event.currentTarget.parentElement
                    ?.querySelector<HTMLButtonElement>(`#issue-${issue}-${next}-tab`)
                    ?.focus();
                }}
              >
                {option === "tasks" ? `Tasks · ${d.steps.length}` : `Events · ${d.events.length}`}
              </button>
            ))}
          </div>
          <ColumnMenu
            columns={ISSUE_COLUMNS}
            visible={visible}
            onChange={setColumn}
            position={columnMenuAt}
            onClose={() => setColumnMenuAt(null)}
          />

          {pane === "tasks" && d.steps.length === 0 ? (
            <div
              id={`issue-${issue}-tasks-panel`}
              role="tabpanel"
              aria-labelledby={`issue-${issue}-tasks-tab`}
            >
              <EmptyState>no tasks recorded for this issue yet</EmptyState>
            </div>
          ) : null}
          {pane === "tasks" && d.steps.length > 0 ? (
            <div id={`issue-${issue}-tasks-panel`} role="tabpanel" aria-labelledby={`issue-${issue}-tasks-tab`}>
              {d.steps.length > 10 ? (
                <TableFilter
                  label="Filter tasks"
                  value={taskQuery}
                  onChange={setTaskQuery}
                  shown={filteredSteps.length}
                  total={d.steps.length}
                />
              ) : null}
              <div className="panel steps">
                {filteredSteps.length === 0 ? (
                  <EmptyState>no tasks match “{taskQuery.trim()}”</EmptyState>
                ) : (
                  <div className="tbl-wrap">
                    <ResizableTable
                      storageKey="issue-tasks"
                      columns={columns}
                      sort={sortedSteps.sort}
                      onSort={sortedSteps.onSort}
                    >
                      <tbody>
                        {sortedSteps.rows.map((s) => (
                          <StepRow
                            key={s.idx}
                            repo={repo}
                            issue={issue}
                            step={s}
                            open={openStep === s.idx}
                            onToggle={() => setOpenStep((cur) => (cur === s.idx ? null : s.idx))}
                            visible={visible}
                            colSpan={columns.length}
                            showDurationSeconds={showDurationSeconds}
                          />
                        ))}
                      </tbody>
                    </ResizableTable>
                  </div>
                )}
              </div>
            </div>
          ) : null}

          {pane === "events" ? (
            <div
              className="panel issue-events-pane"
              id={`issue-${issue}-events-panel`}
              role="tabpanel"
              aria-labelledby={`issue-${issue}-events-tab`}
            >
              <EventTimeline events={d.events} emptyText="no journal events for this issue" />
            </div>
          ) : null}

          {confirmUnblock ? (
            <UnblockModal
              issue={issue}
              busy={unblocking}
              error={unblockError}
              onConfirm={() => void doUnblock()}
              onCancel={() => setConfirmUnblock(false)}
            />
          ) : null}
        </>
      ) : null}
    </div>
  );
}

function StepRow({
  repo,
  issue,
  step,
  open,
  onToggle,
  visible,
  colSpan,
  showDurationSeconds,
}: {
  repo: string;
  issue: number;
  step: IssueStep;
  open: boolean;
  onToggle: () => void;
  visible: ReadonlySet<string>;
  colSpan: number;
  showDurationSeconds: boolean;
}) {
  const tone = stepOutcomeTone(step.kind, step.state, step.status);
  const u = step.usage;
  const detailId = `task-detail-${issue}-${step.idx}`;
  return (
    <>
      <tr
        className={`clickable${open ? " expanded" : ""}`}
        onClick={onToggle}
      >
        <td className="step-operation">
          <button
            className="step-disclosure"
            aria-expanded={open}
            aria-controls={detailId}
            onClick={(event) => {
              // The row handles the toggle; without this the click counts twice
              // and the row reopens in the same gesture.
              event.stopPropagation();
              onToggle();
            }}
          >
            <span className={`chev${open ? " open" : ""}`} aria-hidden="true">
              ▸
            </span>
            <StepBadge step={step} />
          </button>
        </td>
        {visible.has("role") ? <td>
          <Chip tone={step.role === "steward" ? "meta" : "info"} square>
            {step.role ?? "—"}
          </Chip>
        </td> : null}
        {visible.has("model") ? <td>
          {step.model ? (
            <Chip
              tone={modelTone(step.model, step.effort)}
              square
              title={`${step.model}${step.effort ? ` · ${step.effort} effort` : ""}`}
            >
              {shortModel(step.model)}
            </Chip>
          ) : (
            <span className="faint">—</span>
          )}
        </td> : null}
        {visible.has("status") ? <td className="row-wrap">
          <StepStatusChip step={step} />
          {step.exit_reason && step.exit_reason.toLowerCase() !== "ok" ? (
            <span className="faint mono trunc" title={step.exit_reason}>
              {step.exit_reason}
            </span>
          ) : null}
        </td> : null}
        {visible.has("duration") ? (
          <td className="r mono">{duration(step.duration_s, showDurationSeconds)}</td>
        ) : null}
        {visible.has("cost") ? <td className="r mono">
          {usd(step.cost_usd)}
          {step.estimated ? <span className="faint" title="synthetic dollars"> ~</span> : null}
        </td> : null}
        {visible.has("tokens") ? <td className="mono faint nowrap">
          {u ? <TokenSummary usage={u} /> : "—"}
          {step.turns !== null ? <span className="faint"> · {num(step.turns)} turns</span> : null}
        </td> : null}
        {visible.has("when") ? <td className="mono faint" title={step.ts ?? ""}>
          {localTime(step.ts)}
        </td> : null}
      </tr>
      {/* The note is why this task mattered — a verdict, a question, a failure
        * reason. It gets a line of its own so no column width can hide it. */}
      {step.note ? (
        <tr className={`step-note-row tone-border-${tone}`}>
          <td colSpan={colSpan}>
            <span className="task-note">{step.note}</span>
          </td>
        </tr>
      ) : null}
      {open ? (
        <tr className="detail-row">
          <td colSpan={colSpan}>
            <StepDetail id={detailId} repo={repo} issue={issue} step={step} />
          </td>
        </tr>
      ) : null}
    </>
  );
}

type DetailPane = "summary" | "events" | "cost" | "raw";

/** The prose lines a task's window carries: what the agent actually did to
 * the world (relabels, comments, PR opens), in order. Everything else in the
 * window is machine bookkeeping and stays in the Events/Raw panes. */
function narrateWindow(events: { [key: string]: unknown }[]): { ts: string | null; desc: string }[] {
  return events
    .filter((e) => typeof e.desc === "string" && e.desc.trim() !== "")
    .map((e) => ({
      ts: typeof e.ts === "string" ? e.ts : null,
      desc: (e.desc as string).trim(),
    }));
}

function StepDetail({
  id,
  repo,
  issue,
  step,
}: {
  id: string;
  repo: string;
  issue: number;
  step: IssueStep;
}) {
  const [pane, setPane] = useState<DetailPane>("summary");
  const events = usePolled(
    pane === "summary" || pane === "events"
      ? `stepev:${repo}:${issue}:${step.idx}`
      : null,
    (signal) => api.stepEvents(repo, issue, step.idx, signal),
    step.state === "running" ? POLL_MS : 0,
  );

  const chartable = !!step.session_id && step.has_transcript;

  return (
    <div className="stepdetail" id={id}>
      <div className="subtabs stepdetail-tabs" role="tablist" aria-label="Task detail">
        {(["summary", "events", "cost", "raw"] as DetailPane[]).map((p) => (
          <button
            key={p}
            id={`${id}-${p}-tab`}
            role="tab"
            aria-selected={p === pane}
            aria-controls={`${id}-${p}-panel`}
            tabIndex={p === pane ? 0 : -1}
            className={`subtab${p === pane ? " active" : ""}`}
            onClick={() => setPane(p)}
            onKeyDown={(event) => moveTabFocus(event, p, setPane)}
          >
            {p === "summary" ? "Summary" : p === "events" ? "Events" : p === "cost" ? "Cost chart" : "Raw"}
          </button>
        ))}
      </div>

      {pane === "summary" ? (
        <div
          id={`${id}-summary-panel`}
          role="tabpanel"
          aria-labelledby={`${id}-summary-tab`}
          className="step-summary"
        >
          <StepSummaryHeader step={step} />
          <StepContextLine step={step} />
          {step.exit_reason && step.exit_reason.toLowerCase() !== "ok" ? (
            <p className="step-summary-exit">
              ended: <span className="mono">{step.exit_reason}</span>
            </p>
          ) : null}
          {step.review?.verdict_excerpt ? (
            <blockquote className="step-review-quote">
              <div className="step-review-quote-label">
                <span>Reviewer output</span>
                <Chip tone="running">untrusted</Chip>
              </div>
              <p>{step.review.verdict_excerpt}</p>
            </blockquote>
          ) : null}
          {step.change ? <StepChangeSummary change={step.change} /> : null}
          {events.error ? (
            <ErrorBanner message={events.error} onRetry={events.refresh} />
          ) : null}
          {events.loading ? (
            <Loading what="task actions" />
          ) : (
            (() => {
              const lines = narrateWindow(events.data?.events ?? []);
              return lines.length > 0 ? (
                <ul className="step-summary-actions">
                  {lines.map((line, index) => (
                    <li key={index}>
                      <span className="mono faint step-summary-when">{clockTime(line.ts)}</span>
                      <span>{line.desc}</span>
                    </li>
                  ))}
                </ul>
              ) : null;
            })()
          )}
        </div>
      ) : null}

      {pane === "events" ? (
        <div
          id={`${id}-events-panel`}
          role="tabpanel"
          aria-labelledby={`${id}-events-tab`}
        >
          {events.error ? (
            <ErrorBanner message={events.error} onRetry={events.refresh} />
          ) : null}
          <WarningList warnings={events.data?.warnings} />
          {events.loading ? (
            <Loading what="task events" />
          ) : (
            <EventTimeline
              dense
              events={events.data?.events ?? []}
              emptyText="no journal events inside this task's window"
            />
          )}
        </div>
      ) : null}

      {pane === "cost" ? (
        <div
          id={`${id}-cost-panel`}
          role="tabpanel"
          aria-labelledby={`${id}-cost-tab`}
        >
          {chartable ? (
            <iframe
              className="costchart"
              title={`cost chart for ${step.kind} #${step.idx}`}
              // The chart is built from transcript text the agent read from
              // issues, PRs and the web. Sandboxed into an opaque origin so
              // nothing in it can reach the action endpoints.
              sandbox=""
              src={api.costChartUrl(repo, issue, step.session_id as string)}
            />
          ) : (
            <EmptyState>
              {step.session_id
                ? "no transcript on disk for this session — the cost chart needs ~/.claude/projects/*/<session_id>.jsonl"
                : "this task recorded no session id, so there is no transcript to chart"}
            </EmptyState>
          )}
        </div>
      ) : null}

      {pane === "raw" ? (
        <div
          id={`${id}-raw-panel`}
          role="tabpanel"
          aria-labelledby={`${id}-raw-tab`}
        >
          <JsonBlock value={step} />
        </div>
      ) : null}
    </div>
  );
}

function StepSummaryHeader({ step }: { step: IssueStep }) {
  const escalation = step.escalation;
  const escalationPath = escalation?.from_effort && escalation.to_effort
    ? `${escalation.from_effort}→${escalation.to_effort}`
    : escalation?.to_effort
      ? `→${escalation.to_effort}`
      : "tier";
  const escalationValue = escalation
    ? `+${escalation.notches} ${escalationPath}${
      escalation.after_review_round ? ` after r${escalation.after_review_round}` : ""
    }`
    : null;
  const facts = [
    { label: "model", value: step.model, mono: true },
    { label: "effort", value: step.effort, mono: false },
    { label: "runtime", value: step.runtime, mono: false },
    { label: "escalation", value: escalationValue, mono: false },
    { label: "session id", value: step.session_id, mono: true },
    step.exit_reason && step.exit_reason.toLowerCase() !== "ok"
      ? { label: "exit", value: step.exit_reason, mono: true }
      : null,
  ].filter((fact): fact is { label: string; value: string; mono: boolean } => (
    fact !== null && typeof fact.value === "string" && fact.value.trim() !== ""
  ));

  return facts.length > 0 ? (
    <dl className="step-summary-facts" role="group" aria-label="Task execution details">
      {facts.map((fact) => (
        <div className="step-summary-fact" key={fact.label}>
          <dt>{fact.label}</dt>
          <dd className={fact.mono ? "mono" : undefined}>{fact.value}</dd>
        </div>
      ))}
    </dl>
  ) : null;
}

function capFraction(value: number, cap: number | null | undefined): string {
  if (cap === 0) return `${value} / off`;
  return cap === null || cap === undefined ? String(value) : `${value}/${cap}`;
}

function StepContextLine({ step }: { step: IssueStep }) {
  const segments: string[] = [];
  if (step.review?.rejection !== null && step.review?.rejection !== undefined) {
    segments.push(`change-request verdicts observed ${step.review.rejection}`);
  }
  if (
    step.review?.unproductive_rounds !== null
    && step.review?.unproductive_rounds !== undefined
  ) {
    segments.push(
      `unproductive rounds ${capFraction(
        step.review.unproductive_rounds,
        step.review.unproductive_cap,
      )}`,
    );
  }
  if (step.cost_usd !== null && step.cost_usd !== undefined) {
    segments.push(
      step.budget_cap_usd === 0
        ? `leg ${usd(step.cost_usd)} · cap off`
        : step.budget_cap_usd !== null && step.budget_cap_usd !== undefined
          ? `leg ${usd(step.cost_usd)} / ${usd(step.budget_cap_usd)} cap`
          : `leg ${usd(step.cost_usd)}`,
    );
  }
  if (step.pricing_basis) {
    segments.push(
      `${step.pricing_basis.replaceAll("_", " ")}${step.estimated ? " · estimated" : ""}`,
    );
  } else if (step.estimated) {
    segments.push("estimated pricing");
  }
  return segments.length > 0 ? (
    <p className="step-context-line mono">
      {segments.map((segment, index) => (
        <span key={segment}>
          {index > 0 ? (
            <span className="step-context-separator" aria-hidden="true">·</span>
          ) : null}
          {segment}
        </span>
      ))}
    </p>
  ) : null;
}

function StepChangeSummary({
  change,
}: {
  change: NonNullable<IssueStep["change"]>;
}) {
  const subjects = change.commit_subjects ?? [];
  const changeFacts = [
    change.branch ? `pushed ${change.branch}` : `PR #${change.pr}`,
    change.files_changed_count !== null && change.files_changed_count !== undefined
      ? `${num(change.files_changed_count)} file${change.files_changed_count === 1 ? "" : "s"} changed`
      : null,
  ].filter((value): value is string => value !== null);
  return (
    <section className="step-change-summary" aria-label="Pushed change summary">
      <p className="step-change-facts mono">{changeFacts.join(" · ")}</p>
      {subjects.length > 0 ? (
        <ol className="step-commit-progression">
          {subjects.map((subject, index) => {
            const match = /^(red|green):\s*/i.exec(subject);
            return (
              <li key={`${subject}-${index}`}>
                <Chip tone={commitSubjectTone(subject)} square>
                  {match?.[1]?.toLowerCase() ?? "commit"}
                </Chip>
                <span>{match ? subject.slice(match[0].length) : subject}</span>
              </li>
            );
          })}
        </ol>
      ) : null}
    </section>
  );
}

function moveTabFocus(
  event: KeyboardEvent<HTMLButtonElement>,
  pane: DetailPane,
  select: (pane: DetailPane) => void,
) {
  const panes: DetailPane[] = ["summary", "events", "cost", "raw"];
  const current = panes.indexOf(pane);
  let next: number | null = null;
  if (event.key === "ArrowRight") next = (current + 1) % panes.length;
  if (event.key === "ArrowLeft") next = (current - 1 + panes.length) % panes.length;
  if (event.key === "Home") next = 0;
  if (event.key === "End") next = panes.length - 1;
  if (next === null) return;
  event.preventDefault();
  select(panes[next] as DetailPane);
  const tabs = event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>(
    '[role="tab"]',
  );
  tabs?.[next]?.focus();
}

function TokenSummary({ usage }: { usage: NonNullable<IssueStep["usage"]> }) {
  return (
    <span className="token-summary" aria-label="Token usage">
      <span className="token-in" title="Input tokens">
        <span aria-hidden="true">↓</span> {tokens(usage.input_tokens)}
      </span>
      <span className="token-out" title="Output tokens">
        <span aria-hidden="true">↑</span> {tokens(usage.output_tokens)}
      </span>
      <span className="token-cache" title="Cache read tokens">
        <span aria-hidden="true">◇</span> {tokens(usage.cache_read_input_tokens)}
      </span>
    </span>
  );
}
