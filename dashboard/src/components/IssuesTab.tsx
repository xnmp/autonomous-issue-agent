import {
  Fragment,
  lazy,
  Suspense,
  useMemo,
  useState,
  type KeyboardEvent,
  type MouseEvent,
} from "react";
import { api } from "../api/client";
import { useClock, type Polled } from "../api/hooks";
import type {
  BacklogCard,
  CardStep,
  IssueCard,
  IssuesPayload,
  PendingCard,
  Priority,
} from "../api/types";
import { ISSUES_SUBS, ISSUES_SUB_LABELS, type IssuesSub } from "../routing/hash";
import {
  combinedVerificationStatus,
  familyVar,
  flagTone,
  modelTone,
  prStateTone,
  stateTone,
  verificationTone,
} from "../lib/semantics";
import { ageCompact, ago, duration, durationShort, localTime, num, usd } from "../lib/format";
import { shortModel } from "../lib/models";
import { Chip } from "./ui/Chip";
import { EtaProgress } from "./ui/Charts";
import { StepBadgeRow } from "./ui/StepBadge";
import { ColumnMenu, type ColumnChoice } from "./ui/ColumnMenu";
import {
  ResizableTable,
  useSortedRows,
  type TableColumn,
} from "./ui/ResizableTable";
import { ConfirmModal } from "./ui/Modal";
import {
  EmptyState,
  ErrorBanner,
  GhUnavailableChip,
  SkeletonRows,
  SectionTitle,
  WarningList,
} from "./ui/Feedback";
import { SubTabs } from "./TabStrip";

const MarkdownBody = lazy(() => import("./ui/MarkdownBody"));

interface IssuesTabProps {
  repo: string;
  sub: IssuesSub;
  issues: Polled<IssuesPayload>;
  onSetSub: (sub: IssuesSub) => void;
  onOpenIssue: (issue: number) => void;
  showProjectedCost?: boolean;
}

type Visibility = Record<IssuesSub, Record<string, boolean>>;

const COLUMN_STORAGE = "issue-agent.dashboard.columns";
const COLUMN_CHOICES: Record<IssuesSub, ColumnChoice[]> = {
  inflight: [
    { key: "tasks", label: "Tasks" },
    { key: "cost", label: "Cost" },
    { key: "elapsed", label: "Duration" },
    { key: "progress", label: "Progress" },
    { key: "model", label: "Model" },
    { key: "pending-priority", label: "Pending priority" },
    { key: "pending-overlap", label: "Pending overlap" },
    { key: "pending-hold", label: "Pending state" },
    { key: "pending-labels", label: "Pending labels" },
  ],
  done: [
    { key: "cost", label: "Cost" },
    { key: "duration", label: "Duration" },
    { key: "verification", label: "Verification" },
    { key: "model", label: "Model" },
    { key: "rounds", label: "Rounds" },
    { key: "merged", label: "Merged" },
  ],
  backlog: [
    { key: "recurrence", label: "Recurrence" },
    { key: "age", label: "Age" },
    { key: "comments", label: "# comments" },
    { key: "labels", label: "Labels" },
  ],
};

const DEFAULT_VISIBILITY: Visibility = {
  inflight: Object.fromEntries(COLUMN_CHOICES.inflight.map(({ key }) => [key, true])),
  done: Object.fromEntries(
    COLUMN_CHOICES.done.map(({ key }) => [key, key !== "rounds"]),
  ),
  backlog: Object.fromEntries(
    COLUMN_CHOICES.backlog.map(({ key }) => [
      key,
      key !== "recurrence" && key !== "comments",
    ]),
  ),
};

function storedVisibility(): Visibility {
  try {
    const parsed = JSON.parse(localStorage.getItem(COLUMN_STORAGE) ?? "{}") as Partial<Visibility>;
    return {
      inflight: { ...DEFAULT_VISIBILITY.inflight, ...parsed.inflight },
      done: { ...DEFAULT_VISIBILITY.done, ...parsed.done },
      backlog: { ...DEFAULT_VISIBILITY.backlog, ...parsed.backlog },
    };
  } catch {
    return DEFAULT_VISIBILITY;
  }
}

export function IssuesTab({
  repo,
  sub,
  issues,
  onSetSub,
  onOpenIssue,
  showProjectedCost = true,
}: IssuesTabProps) {
  const [visibility, setVisibility] = useState<Visibility>(storedVisibility);
  const [columnMenuAt, setColumnMenuAt] = useState<{ x: number; y: number } | null>(null);
  const data = issues.data;
  const ghOk = data?.gh_ok ?? true;
  const visible = useMemo(
    () => new Set(Object.entries(visibility[sub]).filter(([, shown]) => shown).map(([key]) => key)),
    [sub, visibility],
  );
  const setColumn = (key: string, shown: boolean) => {
    setVisibility((current) => {
      const next = { ...current, [sub]: { ...current[sub], [key]: shown } };
      try {
        localStorage.setItem(COLUMN_STORAGE, JSON.stringify(next));
      } catch {
        // URL navigation and in-memory preferences remain usable without storage.
      }
      return next;
    });
  };
  const openColumnMenu = (event: MouseEvent<HTMLDivElement>) => {
    if (!(event.target instanceof Element) || !event.target.closest("th")) return;
    event.preventDefault();
    setColumnMenuAt({
      x: Math.min(event.clientX, window.innerWidth - 220),
      y: Math.min(event.clientY, window.innerHeight - 320),
    });
  };

  return (
    <div className="tabbody" onContextMenu={openColumnMenu}>
      <div className="row">
        <SubTabs
          value={sub}
          options={ISSUES_SUBS}
          labels={ISSUES_SUB_LABELS}
          counts={{
            inflight: (data?.in_flight.length ?? 0) + (data?.pending.length ?? 0),
            done: data?.done.length ?? 0,
            backlog: data?.backlog.length ?? 0,
          }}
          onChange={onSetSub}
        />
        <span className="spacer" />
        {!ghOk ? <GhUnavailableChip /> : null}
      </div>
      <ColumnMenu
        columns={COLUMN_CHOICES[sub]}
        visible={visible}
        onChange={setColumn}
        position={columnMenuAt}
        onClose={() => setColumnMenuAt(null)}
      />

      {issues.error ? (
        <ErrorBanner message={issues.error} onRetry={issues.refresh} stale={!!data} />
      ) : null}
      <WarningList warnings={data?.warnings} />

      {sub === "inflight" ? (
        <>
          <SectionTitle>In flight</SectionTitle>
          <InFlightTable
            cards={data?.in_flight ?? []}
            anchorMs={issues.updatedAt}
            loading={issues.loading}
            visible={visible}
            onOpen={onOpenIssue}
            showProjectedCost={showProjectedCost}
          />
          <SectionTitle>Pending</SectionTitle>
          <PendingTable
            repo={repo}
            cards={data?.pending ?? []}
            loading={issues.loading}
            ghOk={ghOk}
            visible={visible}
            onRefresh={issues.refresh}
          />
        </>
      ) : null}

      {sub === "done" ? (
        <DoneTable
          cards={data?.done ?? []}
          loading={issues.loading}
          visible={visible}
          onOpen={onOpenIssue}
        />
      ) : null}

      {sub === "backlog" ? (
        <BacklogTable
          repo={repo}
          cards={data?.backlog ?? []}
          loading={issues.loading}
          ghOk={ghOk}
          visible={visible}
          onRefresh={issues.refresh}
        />
      ) : null}
    </div>
  );
}

function InFlightTable({
  cards,
  anchorMs,
  loading,
  visible,
  onOpen,
  showProjectedCost,
}: {
  cards: IssueCard[];
  anchorMs: number | null;
  loading: boolean;
  visible: ReadonlySet<string>;
  onOpen: (issue: number) => void;
  showProjectedCost: boolean;
}) {
  const [expanded, setExpanded] = useState<number | null>(null);
  const sorted = useSortedRows(cards, {
    issue: (card) => card.issue,
    tasks: (card) => card.steps.length,
    cost: (card) => card.cost_usd,
    elapsed: (card) => card.elapsed_s,
    progress: (card) => (
      card.elapsed_s !== null && card.projected_eta_s
        ? card.elapsed_s / card.projected_eta_s
        : null
    ),
    model: (card) => card.models.join(" "),
  }, { key: "issue", direction: "asc" });
  const now = useClock();
  if (loading) return <SkeletonRows what="in-flight issues" rows={5} />;
  if (cards.length === 0) return <EmptyState>no issues in flight — the fixer is idle</EmptyState>;
  const offset = anchorMs === null ? 0 : Math.max(0, (now - anchorMs) / 1000);
  const columns: TableColumn[] = [
    { key: "issue", label: "Issue / PR", width: 340 },
    ...(visible.has("tasks") ? [{ key: "tasks", label: "Tasks", width: 240 }] : []),
    ...(visible.has("cost") ? [{ key: "cost", label: "Cost", className: "r", width: 150 }] : []),
    ...(visible.has("elapsed") ? [{ key: "elapsed", label: "Duration", className: "r", width: 180 }] : []),
    ...(visible.has("progress") ? [{ key: "progress", label: "Progress", width: 160 }] : []),
    ...(visible.has("model") ? [{ key: "model", label: "Model", width: 180 }] : []),
  ];

  return (
    <div className="tbl-wrap panel">
      <ResizableTable
        storageKey="issues-inflight"
        columns={columns}
        className="wide issue-list-table"
        sort={sorted.sort}
        onSort={sorted.onSort}
      >
        <tbody>
          {sorted.rows.map((card) => {
            const elapsed = card.elapsed_s === null ? null : card.elapsed_s + offset;
            const working = card.working_s === null
              ? null
              : card.working_s + (card.idle_active ? 0 : offset);
            const idle = card.idle_s === null
              ? null
              : card.idle_s + (card.idle_active ? offset : 0);
            const open = expanded === card.issue;
            const alert = card.flags.some((flag) => flag === "escalated" || flag === "spend-blocked");
            return (
              <Fragment key={card.issue}>
                <tr
                  className={`clickable${open ? " expanded" : ""}${alert ? " row-alert" : ""}`}
                  tabIndex={0}
                  aria-expanded={open}
                  onClick={() => setExpanded(open ? null : card.issue)}
                  onKeyDown={(event) => toggleRowFromKeyboard(event, () => setExpanded(open ? null : card.issue))}
                >
                  <td><IssueSummary card={card} open={open} /></td>
                  {visible.has("tasks") ? <td><StepBadgeRow steps={card.steps} /></td> : null}
                  {visible.has("cost") ? (
                    <td className="r mono nowrap">
                      {usd(card.cost_usd)}
                      {card.estimated ? <span className="faint" title="synthetic dollars (subscription runtime)"> ~</span> : null}
                      {showProjectedCost && card.projected_cost_usd !== null ? (
                        <span
                          className="faint"
                          title={`mean completed issue cost for ${card.projected_model ?? "this model"}`}
                        >
                          {" "}/ {usd(card.projected_cost_usd)}
                        </span>
                      ) : null}
                    </td>
                  ) : null}
                  {visible.has("elapsed") ? (
                    <td className="r mono" title={card.started_ts ? `started ${localTime(card.started_ts)}` : ""}>
                      <DurationBreakdown working={working} idle={idle} />
                    </td>
                  ) : null}
                  {visible.has("progress") ? (
                    <td>
                      {elapsed !== null && card.projected_eta_s !== null ? (
                        <EtaProgress
                          elapsedS={elapsed}
                          etaS={card.projected_eta_s}
                          segments={card.steps.map((step, index) => ({
                            key: `${step.kind}-${index}`,
                            varName: familyVar(step.kind),
                            weight: Math.max(step.cost_usd ?? 0, 0.01),
                          }))}
                        />
                      ) : <span className="faint">no projection</span>}
                    </td>
                  ) : null}
                  {visible.has("model") ? <td><ModelList models={card.model_badges ?? card.models.map((model) => ({ model, effort: null }))} /></td> : null}
                </tr>
                {open ? (
                  <TaskDetailRow card={card} colSpan={columns.length} onOpen={() => onOpen(card.issue)} />
                ) : null}
              </Fragment>
            );
          })}
        </tbody>
      </ResizableTable>
    </div>
  );
}

function PendingTable({
  repo,
  cards,
  loading,
  ghOk,
  visible,
  onRefresh,
}: {
  repo: string;
  cards: PendingCard[];
  loading: boolean;
  ghOk: boolean;
  visible: ReadonlySet<string>;
  onRefresh: () => void;
}) {
  const [busyIssue, setBusyIssue] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const sorted = useSortedRows(cards, {
    issue: (card) => card.issue,
    priority: (card) => card.priority,
    overlap: (card) => card.overlap === null ? null : String(card.overlap),
    state: (card) => card.hold_reason ?? "pending",
    labels: (card) => card.labels.join(" "),
  }, { key: "issue", direction: "asc" });
  if (loading) return <SkeletonRows what="pending issues" rows={3} />;
  if (cards.length === 0) {
    return <EmptyState>{ghOk ? "nothing is pending" : "pending needs GitHub data"}</EmptyState>;
  }

  const setPriority = async (issue: number, priority: Priority) => {
    setBusyIssue(issue);
    setError(null);
    try {
      await api.setPriority(repo, issue, priority);
      onRefresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusyIssue(null);
    }
  };
  const columns: TableColumn[] = [
    { key: "issue", label: "Issue", width: 360 },
    ...(visible.has("pending-priority") ? [{ key: "priority", label: "Priority", width: 220 }] : []),
    ...(visible.has("pending-overlap") ? [{ key: "overlap", label: "Overlap", width: 110 }] : []),
    ...(visible.has("pending-hold") ? [{ key: "state", label: "State", width: 220 }] : []),
    ...(visible.has("pending-labels") ? [{ key: "labels", label: "Labels", width: 260 }] : []),
  ];

  return (
    <>
      {error ? <ErrorBanner message={error} /> : null}
      <div className="tbl-wrap panel">
        <ResizableTable
          storageKey="issues-pending"
          columns={columns}
          sort={sorted.sort}
          onSort={sorted.onSort}
        >
          <tbody>
            {sorted.rows.map((card) => (
              <tr key={card.issue}>
                <td><CompactIssue issue={card.issue} title={card.title} url={card.url} /></td>
                {visible.has("pending-priority") ? <td>
                  <PrioritySelector
                    value={card.priority}
                    busy={busyIssue === card.issue}
                    onChange={(priority) => void setPriority(card.issue, priority)}
                  />
                </td> : null}
                {visible.has("pending-overlap") ? (
                  <td><span className="faint" title="steward overlap prediction not built yet">n/a</span></td>
                ) : null}
                {visible.has("pending-hold") ? <td>
                  {card.held ? (
                    <Chip tone="info" title={card.hold_reason ?? "pending"}>
                      {card.hold_reason ?? "pending"}
                    </Chip>
                  ) : <Chip tone="info">pending</Chip>}
                </td> : null}
                {visible.has("pending-labels") ? <td><LabelList labels={card.labels} /></td> : null}
              </tr>
            ))}
          </tbody>
        </ResizableTable>
      </div>
    </>
  );
}

const PRIORITIES: { value: Priority; label: string }[] = [
  { value: "P1", label: "P1" },
  { value: "P2", label: "P2" },
  { value: "P3", label: "P3" },
  { value: null, label: "none" },
];

function PrioritySelector({
  value,
  busy,
  onChange,
}: {
  value: Priority;
  busy: boolean;
  onChange: (priority: Priority) => void;
}) {
  return (
    <div className={`seg${busy ? " busy" : ""}`} role="group" aria-label="priority">
      {PRIORITIES.map((priority) => (
        <button
          key={priority.label}
          className={`seg-btn${priority.value === value ? " active" : ""}`}
          disabled={busy}
          aria-pressed={priority.value === value}
          onClick={() => {
            if (priority.value !== value) onChange(priority.value);
          }}
        >
          {priority.label}
        </button>
      ))}
    </div>
  );
}

function DoneTable({
  cards,
  loading,
  visible,
  onOpen,
}: {
  cards: IssueCard[];
  loading: boolean;
  visible: ReadonlySet<string>;
  onOpen: (issue: number) => void;
}) {
  const [expanded, setExpanded] = useState<number | null>(null);
  const sorted = useSortedRows(cards, {
    issue: (card) => card.issue,
    cost: (card) => card.cost_usd,
    duration: (card) => card.elapsed_s,
    verification: (card) => combinedVerificationStatus(
      card.issue_verification,
      card.pr_verification,
    ),
    model: (card) => card.models.join(" "),
    rounds: (card) => card.rounds,
    merged: (card) => card.merged_ts ?? card.last_ts,
  }, { key: "merged", direction: "desc" });
  if (loading) return <SkeletonRows what="done issues" rows={5} />;
  if (cards.length === 0) return <EmptyState>nothing merged by the harness in this deployment yet</EmptyState>;
  const columns: TableColumn[] = [
    { key: "issue", label: "Issue / PR", width: 380 },
    ...(visible.has("cost") ? [{ key: "cost", label: "Cost", className: "r", width: 100 }] : []),
    ...(visible.has("duration") ? [{ key: "duration", label: "Duration", className: "r", width: 180 }] : []),
    ...(visible.has("verification") ? [{ key: "verification", label: "Verification", width: 190 }] : []),
    ...(visible.has("model") ? [{ key: "model", label: "Model", width: 190 }] : []),
    ...(visible.has("rounds") ? [{ key: "rounds", label: "Rounds", className: "r", width: 90 }] : []),
    ...(visible.has("merged") ? [{ key: "merged", label: "Merged", width: 120 }] : []),
  ];

  return (
    <div className="tbl-wrap panel">
      <ResizableTable
        storageKey="issues-done"
        columns={columns}
        className="issue-list-table"
        sort={sorted.sort}
        onSort={sorted.onSort}
      >
        <tbody>
          {sorted.rows.map((card) => {
            const open = expanded === card.issue;
            return (
              <Fragment key={card.issue}>
                <tr
                  className={`clickable${open ? " expanded" : ""}`}
                  tabIndex={0}
                  aria-expanded={open}
                  onClick={() => setExpanded(open ? null : card.issue)}
                  onKeyDown={(event) => toggleRowFromKeyboard(event, () => setExpanded(open ? null : card.issue))}
                >
                  <td><IssueSummary card={card} open={open} /></td>
                  {visible.has("cost") ? (
                    <td className="r mono">
                      {usd(card.cost_usd)}
                      {card.estimated ? <span className="faint" title="synthetic dollars"> ~</span> : null}
                    </td>
                  ) : null}
                  {visible.has("duration") ? (
                    <td className="r mono">
                      <DurationBreakdown working={card.working_s} idle={card.idle_s} compact />
                    </td>
                  ) : null}
                  {visible.has("verification") ? (
                    <td><VerificationStatus card={card} /></td>
                  ) : null}
                  {visible.has("model") ? <td><ModelList models={card.model_badges ?? card.models.map((model) => ({ model, effort: null }))} /></td> : null}
                  {visible.has("rounds") ? <td className="r num">{num(card.rounds)}</td> : null}
                  {visible.has("merged") ? (
                    <td className="mono faint" title={card.merged_ts ?? card.last_ts ?? ""}>
                      {card.merged_ts ? ago(card.merged_ts) : ago(card.last_ts)}
                    </td>
                  ) : null}
                </tr>
                {open ? (
                  <TaskDetailRow card={card} colSpan={columns.length} onOpen={() => onOpen(card.issue)} />
                ) : null}
              </Fragment>
            );
          })}
        </tbody>
      </ResizableTable>
    </div>
  );
}

function BacklogTable({
  repo,
  cards,
  loading,
  ghOk,
  visible,
  onRefresh,
}: {
  repo: string;
  cards: BacklogCard[];
  loading: boolean;
  ghOk: boolean;
  visible: ReadonlySet<string>;
  onRefresh: () => void;
}) {
  const [expanded, setExpanded] = useState<number | null>(null);
  const [promote, setPromote] = useState<BacklogCard | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const sorted = useSortedRows(cards, {
    issue: (card) => card.issue,
    recurrence: (card) => card.recurrence_count,
    age: (card) => card.age_s ?? (card.age_days === null ? null : card.age_days * 86_400),
    comments: (card) => card.comments,
    labels: (card) => card.labels.join(" "),
  }, { key: "issue", direction: "asc" });
  if (loading) return <SkeletonRows what="backlog" rows={5} />;
  if (cards.length === 0) {
    return <EmptyState>{ghOk ? "no unlabelled open issues — the backlog is clear" : "backlog needs GitHub data, which is unavailable"}</EmptyState>;
  }
  const columns: TableColumn[] = [
    { key: "issue", label: "Issue", width: 420 },
    ...(visible.has("recurrence") ? [{ key: "recurrence", label: "Recurrence", width: 150 }] : []),
    ...(visible.has("age") ? [{ key: "age", label: "Age", className: "r", width: 90 }] : []),
    ...(visible.has("comments") ? [{ key: "comments", label: "# comments", className: "r", width: 110 }] : []),
    ...(visible.has("labels") ? [{ key: "labels", label: "Labels", width: 260 }] : []),
  ];

  const doPromote = async () => {
    if (!promote) return;
    setBusy(true);
    setError(null);
    try {
      await api.promote(repo, promote.issue);
      setPromote(null);
      onRefresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="tbl-wrap panel">
        <ResizableTable
          storageKey="issues-backlog"
          columns={columns}
          className="issue-list-table"
          sort={sorted.sort}
          onSort={sorted.onSort}
        >
          <tbody>
            {sorted.rows.map((card) => {
              const open = expanded === card.issue;
              return (
                <Fragment key={card.issue}>
                  <tr
                    className={`clickable${open ? " expanded" : ""}`}
                    tabIndex={0}
                    aria-expanded={open}
                    onClick={() => setExpanded(open ? null : card.issue)}
                    onKeyDown={(event) => toggleRowFromKeyboard(event, () => setExpanded(open ? null : card.issue))}
                  >
                    <td>
                      <div className="issue-summary">
                        <span className={`chev${open ? " open" : ""}`} aria-hidden="true">▸</span>
                        <div className="issue-summary-main">
                          <div className="row-wrap">
                            <IssueNumber issue={card.issue} url={card.url} />
                            <button
                              className="btn btn-xs promote-icon"
                              title={`Promote #${card.issue} to ai-ready`}
                              aria-label={`Promote #${card.issue} to ai-ready`}
                              onClick={(event) => {
                                event.stopPropagation();
                                setError(null);
                                setPromote(card);
                              }}
                            >
                              ↑
                            </button>
                          </div>
                          <span
                            className={`issue-summary-title recurrence-${Math.min(card.recurrence_count, 2)}`}
                            title={card.title ?? undefined}
                          >
                            {card.title ?? <span className="faint">title unavailable</span>}
                          </span>
                        </div>
                      </div>
                    </td>
                    {visible.has("recurrence") ? (
                      <td>
                        {card.recurrence_count > 0 ? (
                          <Chip tone={card.recurrence_count >= 2 ? "fail" : "running"}>
                            {card.recurrence_count}× recurrence
                          </Chip>
                        ) : <span className="faint">—</span>}
                      </td>
                    ) : null}
                    {visible.has("age") ? <td className="r num">{ageCompact(card.age_s ?? (card.age_days === null ? null : card.age_days * 86400))}</td> : null}
                    {visible.has("comments") ? <td className="r num">{num(card.comments)}</td> : null}
                    {visible.has("labels") ? <td><LabelList labels={card.labels} /></td> : null}
                  </tr>
                  {open ? (
                    <tr className="detail-row">
                      <td colSpan={columns.length}>
                        <div className="row-detail">
                          <div className="row-detail-hd">
                            <strong>Issue context</strong>
                            {card.url ? <a href={card.url} target="_blank" rel="noreferrer">Open on GitHub ↗</a> : null}
                          </div>
                          <FormattedIssueBody body={card.body} />
                        </div>
                      </td>
                    </tr>
                  ) : null}
                </Fragment>
              );
            })}
          </tbody>
        </ResizableTable>
      </div>

      {promote ? (
        <ConfirmModal
          title="Promote to ai-ready"
          confirmLabel={`Promote #${promote.issue}`}
          busy={busy}
          error={error}
          onConfirm={() => void doPromote()}
          onCancel={() => setPromote(null)}
          body={
            <>
              <p>Add the <span className="mono">ai-ready</span> label to <strong className="mono">#{promote.issue}</strong>{promote.title ? ` — ${promote.title}` : ""}?</p>
              <p className="muted">The fixer picks up ai-ready issues on its next tick and will start spending.</p>
            </>
          }
        />
      ) : null}
    </>
  );
}

function TaskDetailRow({
  card,
  colSpan,
  onOpen,
}: {
  card: IssueCard;
  colSpan: number;
  onOpen: () => void;
}) {
  return (
    <tr className="detail-row">
      <td colSpan={colSpan}>
        <div className="row-detail">
          <div className="row-detail-hd">
            <strong>Tasks</strong>
            <button className="btn btn-xs" onClick={onOpen}>Open full issue</button>
          </div>
          {card.steps.length > 0 ? (
            <div className="task-summary-list">
              {card.steps.map((step, index) => (
                <div className="task-summary" key={`${step.kind}-${index}`}>
                  <Chip tone={stateTone(step.state)} square pulse={step.state === "running"}>
                    {taskName(step)}
                  </Chip>
                  <span className={`task-state tone-${stateTone(step.state)}`}>
                    {step.state === "ok" ? "done" : step.state}
                  </span>
                  <span className="spacer" />
                  <span className="mono faint">{usd(step.cost_usd)}</span>
                </div>
              ))}
            </div>
          ) : <EmptyState>no tasks recorded yet</EmptyState>}
        </div>
      </td>
    </tr>
  );
}

function taskName(step: CardStep): string {
  return step.kind.replaceAll("_", " ").toLowerCase();
}

function IssueSummary({ card, open }: { card: IssueCard; open: boolean }) {
  return (
    <div className="issue-summary">
      <span className={`chev${open ? " open" : ""}`} aria-hidden="true">▸</span>
      <div className="issue-summary-main">
        <span className="issue-summary-title" title={card.title ?? undefined}>
          {card.title ?? <span className="faint">title unavailable</span>}
        </span>
        <div className="row-wrap">
          <IssueNumber issue={card.issue} url={card.url} />
          <PrLink card={card} />
          {card.flags.length > 0 ? <FlagList flags={card.flags} /> : null}
        </div>
      </div>
    </div>
  );
}

function CompactIssue({
  issue,
  title,
  url,
}: {
  issue: number;
  title: string | null;
  url: string | null;
}) {
  return (
    <div className="compact-issue">
      <IssueNumber issue={issue} url={url} />
      <span className="trunc" title={title ?? undefined}>{title ?? <span className="faint">title unavailable</span>}</span>
    </div>
  );
}

function IssueNumber({ issue, url }: { issue: number; url: string | null }) {
  if (!url) return <span className="mono">#{issue}</span>;
  return (
    <a
      className="mono"
      href={url}
      target="_blank"
      rel="noreferrer"
      title="open issue on GitHub"
      onClick={(event) => event.stopPropagation()}
    >
      #{issue}
    </a>
  );
}

function PrLink({ card }: { card: IssueCard }) {
  if (card.pr === null) return null;
  const chip = (
    <Chip tone={prStateTone(card.pr_state)} square title={card.pr_state ?? "pull request"}>
      PR #{card.pr}
    </Chip>
  );
  if (!card.pr_url) return chip;
  return (
    <a href={card.pr_url} target="_blank" rel="noreferrer" onClick={(event) => event.stopPropagation()}>
      {chip}
    </a>
  );
}

function ModelList({ models }: { models: NonNullable<IssueCard["model_badges"]> }) {
  if (models.length === 0) return <span className="faint">—</span>;
  return (
    <span className="model-list">
      {models.map(({ model, effort }) => (
        <Chip
          key={`${model}-${effort ?? ""}`}
          tone={modelTone(model, effort)}
          square
          title={`${model}${effort ? ` · ${effort} effort` : ""}`}
        >
          {shortModel(model)}
        </Chip>
      ))}
    </span>
  );
}

function VerificationStatus({ card }: { card: IssueCard }) {
  const status = combinedVerificationStatus(
    card.issue_verification,
    card.pr_verification,
  );
  if (!status) return <span className="faint">—</span>;
  return (
    <Chip
      tone={verificationTone(status)}
      title={`Issue: ${card.issue_verification ?? "unavailable"} · PR: ${card.pr_verification ?? "unavailable"}`}
    >
      {status}
    </Chip>
  );
}

function DurationBreakdown({
  working,
  idle,
  compact = false,
}: {
  working: number | null;
  idle: number | null;
  compact?: boolean;
}) {
  const format = compact ? durationShort : duration;
  return (
    <span className="duration-breakdown">
      <span title="time actively progressing through harness work">
        <span className="faint">work</span> {format(working)}
      </span>
      <span title="time waiting on a dependency or hold">
        <span className="faint">idle</span> {format(idle)}
      </span>
    </span>
  );
}

function FlagList({ flags }: { flags: string[] }) {
  if (flags.length === 0) return <span className="faint">—</span>;
  return (
    <span className="row-wrap">
      {flags.map((flag) => <Chip key={flag} tone={flagTone(flag)}>{flag}</Chip>)}
    </span>
  );
}

function LabelList({ labels }: { labels: string[] }) {
  if (labels.length === 0) return <span className="faint">—</span>;
  return (
    <span className="row-wrap">
      {labels.map((label) => <Chip key={label} tone="neutral">{label}</Chip>)}
    </span>
  );
}

function FormattedIssueBody({ body }: { body: string | null }) {
  if (!body?.trim()) return <EmptyState>this issue has no description</EmptyState>;
  return (
    <div className="issue-context">
      <Suspense fallback={<span className="faint">formatting issue context…</span>}>
        <MarkdownBody body={body} />
      </Suspense>
    </div>
  );
}

function toggleRowFromKeyboard(
  event: KeyboardEvent<HTMLTableRowElement>,
  toggle: () => void,
) {
  if (
    event.target instanceof Element
    && event.target !== event.currentTarget
    && event.target.closest("a, button, input, select, textarea, [role='button']")
  ) {
    return;
  }
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    toggle();
  }
}
