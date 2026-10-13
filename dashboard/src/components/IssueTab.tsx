import { useMemo, useState, type KeyboardEvent, type MouseEvent } from "react";
import { api } from "../api/client";
import { usePolled } from "../api/hooks";
import type { IssueStep } from "../api/types";
import { legNote, outcomeMark, prStateTone } from "../lib/semantics";
import { duration, localTime, tokens, usd } from "../lib/format";
import { GROUP_META, boardTone, plainHarnessMessage, railNodes } from "../features/issues/board";
import { Chip } from "./ui/Chip";
import { Icon } from "./ui/Icon";
import { JsonBlock } from "./ui/Json";
import { EmptyState, ErrorBanner, SkeletonRows, StoreUnavailable, WarningList } from "./ui/Feedback";
import { UnblockControl } from "./UnblockControl";
import { ColumnMenu, type ColumnChoice } from "./ui/ColumnMenu";
import { CiRunningBadge, StepBadge, StepStatusChip } from "./ui/StepBadge";
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
  { key: "runtime", label: "Runtime / model" },
  { key: "status", label: "Outcome" },
  { key: "duration", label: "Duration" },
  { key: "cost", label: "Cost" },
  { key: "basis", label: "Basis" },
  { key: "tokens", label: "Tokens" },
  { key: "when", label: "When" },
];

const BOARD_TONE = { needs_you: "fail", working: "ok", waiting: "neutral" } as const;

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
 * One issue, leg by leg. A leg is one paid agent invocation, read from the
 * control store; the status is the state the server derived for the item.
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
    ...(visible.has("runtime") ? [{ key: "runtime", label: "Runtime / model", width: 190, minWidth: 90 }] : []),
    ...(visible.has("status") ? [{ key: "status", label: "Outcome", width: 190, minWidth: 96 }] : []),
    ...(visible.has("duration") ? [{ key: "duration", label: "Duration", className: "r", width: 100, minWidth: 84 }] : []),
    ...(visible.has("cost") ? [{ key: "cost", label: "Cost", className: "r", width: 90, minWidth: 76 }] : []),
    ...(visible.has("basis") ? [{ key: "basis", label: "Basis", width: 160, minWidth: 90 }] : []),
    ...(visible.has("tokens") ? [{ key: "tokens", label: "Tokens", width: 260 }] : []),
    ...(visible.has("when") ? [{ key: "when", label: "When", width: 150 }] : []),
  ];
  const setColumn = (key: string, shown: boolean) => {
    const next = { ...columnVisibility, [key]: shown };
    setColumnVisibility(next);
    try {
      localStorage.setItem(ISSUE_COLUMNS_KEY, JSON.stringify(next));
    } catch {
      // In-memory preferences still work when storage is unavailable.
    }
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
  const filteredSteps = useMemo(() => {
    const query = taskQuery.trim().toLowerCase();
    if (!query) return d?.steps ?? [];
    return (d?.steps ?? []).filter((step) => [
      step.idx,
      step.kind,
      step.role,
      step.runtime,
      step.model,
      step.outcome,
      step.summary,
      step.question,
      step.verdict,
      step.session_id,
    ].some((value) => String(value ?? "").toLowerCase().includes(query)));
  }, [d?.steps, taskQuery]);
  const sortedSteps = useSortedRows(filteredSteps, {
    operation: (step) => step.kind,
    role: (step) => step.role,
    runtime: (step) => `${step.runtime ?? ""} ${step.model ?? ""}`,
    status: (step) => step.outcome,
    duration: (step) => step.duration_s,
    cost: (step) => step.cost_usd,
    basis: (step) => step.pricing_basis,
    tokens: (step) => (
      (step.usage?.input_tokens ?? 0)
      + (step.usage?.output_tokens ?? 0)
    ),
    when: (step) => step.started_ts,
  }, { key: "when", direction: "asc" }, "issue-tasks");

  // `needs-human` on the issue or its PR: only a person releases it.
  const heldForHuman = !!d && !d.done && d.held_for_human;
  const openPr = d?.pull_requests.find((pr) => pr.state === "open") ?? null;
  const board = d?.board ?? null;
  const stopNote = board?.status === "needs_you" ? plainHarnessMessage(board.detail) : null;

  return (
    <div className="tabbody" onContextMenu={openColumnMenu}>
      {detail.error ? (
        <ErrorBanner message={detail.error} onRetry={detail.refresh} stale={!!d} />
      ) : null}
      <StoreUnavailable detail={d?.store_error} />
      <WarningList warnings={d?.warnings} />
      {detail.loading ? <SkeletonRows what={`issue #${issue}`} rows={6} /> : null}

      {d ? (
        <>
          <header className="issue-hd panel">
            <div className="issue-hd-line issue-hd-primary">
              <a
                className="issue-num mono"
                href={d.url ?? `https://github.com/${githubRepo.split("/").map(encodeURIComponent).join("/")}/issues/${d.issue}`}
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
              {board && !d.done ? (
                <Chip
                  tone={BOARD_TONE[board.status]}
                  pulse={board.status === "working" && board.activity === "agent"}
                  title={board.headline}
                >
                  {GROUP_META[board.status].label}
                </Chip>
              ) : null}
              {d.done ? <Chip tone="ok">done</Chip> : null}
              <CiRunningBadge state={openPr?.ci_state} hasPullRequest={openPr !== null} />
            </div>
            <div className="issue-hd-line issue-hd-meta">
              {board && !d.done ? (
                <span className={board.status === "needs_you" ? "issue-attention" : "faint"}>
                  {board.headline}: {board.status === "needs_you" ? board.hint : board.reason}
                </span>
              ) : null}
              {d.pull_requests.map((pr) => {
                const chip = (
                  <Chip tone={prStateTone(pr.state)} square>
                    <Icon name="git-pull-request" size={12} />
                    PR #{pr.number}
                  </Chip>
                );
                return pr.url ? (
                  <a key={pr.number} href={pr.url} target="_blank" rel="noreferrer" title={`open PR #${pr.number} on GitHub`}>
                    {chip}
                  </a>
                ) : <span key={pr.number}>{chip}</span>;
              })}
              <span className="spacer" />
              <span className="issue-stat">
                <span className="faint">cost</span>{" "}
                <span className="mono">{usd(d.cost_usd)}{d.estimated ? " est." : ""}</span>
              </span>
              <span className="issue-stat">
                <span className="faint">legs</span>{" "}
                <span className="mono">{d.steps.length + (d.carried?.legs ?? 0)}</span>
              </span>
            </div>
          </header>

          {d.rail ? (
            <section className="panel issue-lifecycle-panel" aria-labelledby={`issue-${issue}-lifecycle-title`}>
              <div className="panel-hd">
                <span className="panel-title" id={`issue-${issue}-lifecycle-title`}>Lifecycle</span>
                <span className="faint num">{d.steps.length} execution legs</span>
              </div>
              <LifecycleStepper
                rail={railNodes(d.rail)}
                tone={board ? boardTone(board) : "ok"}
                live={d.rail.live}
                label={`Issue #${issue} lifecycle`}
              />
            </section>
          ) : null}

          {stopNote ? (
            <div className="banner banner-error" role="status">
              <span className="banner-mark" aria-hidden="true">!</span>
              {/* Agent or harness prose: rendered as text, never as Markdown. */}
              <span className="banner-msg">{stopNote}</span>
            </div>
          ) : null}
          {d.done ? null : (
            <UnblockControl
              deployment={repo}
              issue={issue}
              heldForHuman={heldForHuman}
              onChanged={detail.refresh}
            />
          )}

          <ColumnMenu
            columns={ISSUE_COLUMNS}
            visible={visible}
            onChange={setColumn}
            position={columnMenuAt}
            onClose={() => setColumnMenuAt(null)}
          />

          {d.carried ? (
            <p className="faint carried-note">
              {d.carried.legs} {d.carried.legs === 1 ? "leg" : "legs"} before the cutover ·{" "}
              <span className="mono">{usd(d.carried.cost_usd)}</span> (money history; not attempts)
            </p>
          ) : null}
          {d.steps.length === 0 ? (
            <EmptyState>{d.carried ? "no legs since the cutover" : "no legs recorded for this issue yet"}</EmptyState>
          ) : (
            <section aria-label={`Legs · ${d.steps.length}`}>
              {d.steps.length > 10 ? (
                <TableFilter
                  label="Filter legs"
                  value={taskQuery}
                  onChange={setTaskQuery}
                  shown={filteredSteps.length}
                  total={d.steps.length}
                />
              ) : null}
              <div className="panel steps">
                {filteredSteps.length === 0 ? (
                  <EmptyState>no legs match “{taskQuery.trim()}”</EmptyState>
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
                            key={s.leg_id}
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
            </section>
          )}
        </>
      ) : null}
    </div>
  );
}

function StepRow({
  issue,
  step,
  open,
  onToggle,
  visible,
  colSpan,
  showDurationSeconds,
}: {
  issue: number;
  step: IssueStep;
  open: boolean;
  onToggle: () => void;
  visible: ReadonlySet<string>;
  colSpan: number;
  showDurationSeconds: boolean;
}) {
  const tone = outcomeMark(step.outcome).tone;
  const u = step.usage;
  const note = legNote(step);
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
          <Chip tone="info" square>
            {step.role ?? "—"}
          </Chip>
        </td> : null}
        {visible.has("runtime") ? <td className="mono trunc" title={step.model ?? undefined}>
          {step.runtime ?? <span className="faint">—</span>}
          {step.model ? <span className="faint"> · {step.model}</span> : null}
        </td> : null}
        {visible.has("status") ? <td className="row-wrap">
          <StepStatusChip outcome={step.outcome} />
          {step.end_reason && step.end_reason !== "normal_return" ? (
            <span className="faint mono trunc" title={step.end_reason}>
              {step.end_reason.replaceAll("_", " ")}
            </span>
          ) : null}
        </td> : null}
        {visible.has("duration") ? (
          <td className="r mono">{duration(step.duration_s, showDurationSeconds)}</td>
        ) : null}
        {visible.has("cost") ? <td className="r mono">
          {usd(step.cost_usd)}
          {step.estimated ? <span className="faint" title="estimated or still reserved"> ~</span> : null}
        </td> : null}
        {visible.has("basis") ? <td className="mono faint trunc">
          {step.pricing_basis?.replaceAll("_", " ") ?? (step.state === "ended" ? "—" : "reserved")}
        </td> : null}
        {visible.has("tokens") ? <td className="mono faint nowrap">
          {u ? <TokenSummary usage={u} /> : "—"}
        </td> : null}
        {visible.has("when") ? <td className="mono faint" title={step.started_ts ?? ""}>
          {localTime(step.started_ts)}
        </td> : null}
      </tr>
      {/* The note is why this leg mattered — a verdict, a question, a summary.
        * It gets a line of its own so no column width can hide it. */}
      {note ? (
        <tr className={`step-note-row tone-border-${tone}`}>
          <td colSpan={colSpan}>
            <span className="task-note">{note}</span>
          </td>
        </tr>
      ) : null}
      {open ? (
        <tr className="detail-row">
          <td colSpan={colSpan}>
            <StepDetail id={detailId} step={step} />
          </td>
        </tr>
      ) : null}
    </>
  );
}

type DetailPane = "summary" | "raw";
const PANES: readonly DetailPane[] = ["summary", "raw"];

function StepDetail({ id, step }: { id: string; step: IssueStep }) {
  const [pane, setPane] = useState<DetailPane>("summary");
  return (
    <div className="stepdetail" id={id}>
      <div className="subtabs stepdetail-tabs" role="tablist" aria-label="Leg detail">
        {PANES.map((p) => (
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
            {p === "summary" ? "Summary" : "Raw"}
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
          <StepFacts step={step} />
          {step.verdict_excerpt ? (
            <blockquote className="step-review-quote">
              <div className="step-review-quote-label">
                <span>Reviewer output{step.verdict ? ` · ${step.verdict.toLowerCase().replaceAll("_", " ")}` : ""}</span>
                <Chip tone="running">untrusted</Chip>
              </div>
              <p>{step.verdict_excerpt}</p>
            </blockquote>
          ) : null}
          {step.question ? (
            <blockquote className="step-review-quote">
              <div className="step-review-quote-label">
                <span>Agent question</span>
                <Chip tone="running">untrusted</Chip>
              </div>
              <p>{step.question}</p>
            </blockquote>
          ) : null}
          {step.summary ? <p className="step-summary-exit">{step.summary}</p> : null}
        </div>
      ) : (
        <div
          id={`${id}-raw-panel`}
          role="tabpanel"
          aria-labelledby={`${id}-raw-tab`}
        >
          <JsonBlock value={step} />
        </div>
      )}
    </div>
  );
}

function StepFacts({ step }: { step: IssueStep }) {
  const facts = [
    { label: "runtime", value: step.runtime, mono: false },
    { label: "model", value: step.model ?? null, mono: true },
    { label: "state", value: step.state, mono: false },
    { label: "pricing", value: step.pricing_basis ? `${step.pricing_basis.replaceAll("_", " ")}${step.estimated ? " · estimated" : ""}` : null, mono: false },
    { label: "ended", value: step.end_reason?.replaceAll("_", " ") ?? null, mono: false },
    { label: "PR", value: step.pr ? `#${step.pr}` : null, mono: true },
    { label: "head", value: step.head ? step.head.slice(0, 12) : null, mono: true },
    { label: "session id", value: step.session_id, mono: true },
    { label: "leg", value: step.leg_id, mono: true },
  ].filter((fact): fact is { label: string; value: string; mono: boolean } => (
    typeof fact.value === "string" && fact.value.trim() !== ""
  ));
  return (
    <dl className="step-summary-facts" role="group" aria-label="Leg execution details">
      {facts.map((fact) => (
        <div className="step-summary-fact" key={fact.label}>
          <dt>{fact.label}</dt>
          <dd className={fact.mono ? "mono" : undefined}>{fact.value}</dd>
        </div>
      ))}
    </dl>
  );
}

function moveTabFocus(
  event: KeyboardEvent<HTMLButtonElement>,
  pane: DetailPane,
  select: (pane: DetailPane) => void,
) {
  const current = PANES.indexOf(pane);
  let next: number | null = null;
  if (event.key === "ArrowRight") next = (current + 1) % PANES.length;
  if (event.key === "ArrowLeft") next = (current - 1 + PANES.length) % PANES.length;
  if (event.key === "Home") next = 0;
  if (event.key === "End") next = PANES.length - 1;
  if (next === null) return;
  event.preventDefault();
  select(PANES[next] as DetailPane);
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
        <span aria-hidden="true">◇</span> {tokens(usage.cache_read_tokens)}
      </span>
    </span>
  );
}
