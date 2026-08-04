import {
  Fragment,
  lazy,
  memo,
  Suspense,
  useMemo,
  useState,
  type KeyboardEvent,
  type MouseEvent,
} from "react";
import { api } from "../api/client";
import { usePolled, type Polled } from "../api/hooks";
import { liveClocks, SnapshotAnchor, useNow, useSnapshotOffset } from "../lib/clock";
import { tipProps } from "./ui/Tip";
import type {
  BacklogCard,
  CardStep,
  IssueCard,
  IssueStep,
  IssuesPayload,
  PendingCard,
  Priority,
} from "../api/types";
import type { IssuesSub } from "../routing/hash";
import {
  blocksOnHuman,
  combinedVerificationStatus,
  difficultyTone,
  familyVar,
  flagTone,
  impactTone,
  issueTypeTone,
  labelTone,
  modelTone,
  prReviewMark,
  prStateTone,
  stateTone,
  stepKindTone,
  verificationGlyph,
  verificationSummary,
  verificationTone,
  type Tone,
} from "../lib/semantics";
import {
  DASH,
  ageCompact,
  duration,
  localDate,
  localTime,
  num,
  parseTs,
  usd,
} from "../lib/format";
import { shortModel } from "../lib/models";
import { readFlag, writeFlag } from "../lib/prefs";
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
import { UnblockModal } from "./UnblockModal";
import {
  EmptyState,
  ErrorBanner,
  GhUnavailableChip,
  SkeletonRows,
  SectionTitle,
  WarningList,
} from "./ui/Feedback";

const MarkdownBody = lazy(() => import("./ui/MarkdownBody"));

export type BacklogPromotionPhase = "pending" | "succeeded" | "failed";

interface IssuesTabProps {
  repo: string;
  sub: IssuesSub;
  issues: Polled<IssuesPayload>;
  onOpenIssue: (issue: number) => void;
  onBacklogPromotion?: (card: BacklogCard, phase: BacklogPromotionPhase) => void;
  showProjectedCost?: boolean;
  showDurationSeconds?: boolean;
}

type Visibility = Record<IssuesSub, Record<string, boolean>>;

const COLUMN_STORAGE = "issue-agent.dashboard.columns";
const TASK_HISTORY_COLUMN_STORAGE = "issue-agent.dashboard.task-history-columns";
// Task is the row identity and remains visible, like Issue in the parent
// tables. The menu controls the three optional measurements beside it.
const TASK_HISTORY_COLUMN_CHOICES: ColumnChoice[] = [
  { key: "time", label: "Time" },
  { key: "cost", label: "Cost" },
  { key: "since", label: "Time since" },
];
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
    { key: "labels", label: "Labels" },
    { key: "since", label: "Time since done" },
    { key: "merged", label: "Date" },
  ],
  backlog: [
    { key: "type", label: "Type" },
    { key: "difficulty", label: "Difficulty" },
    { key: "impact", label: "Impact" },
    { key: "recurrence", label: "Recurrence" },
    { key: "age", label: "Age" },
    { key: "comments", label: "# comments" },
    { key: "labels", label: "Labels" },
  ],
};

const DEFAULT_VISIBILITY: Visibility = {
  inflight: Object.fromEntries(COLUMN_CHOICES.inflight.map(({ key }) => [key, true])),
  // "how long ago" is what an operator scanning the done list actually asks;
  // the absolute date is available beside it, off by default.
  done: Object.fromEntries(
    COLUMN_CHOICES.done.map(({ key }) => [
      key,
      key !== "rounds" && key !== "merged" && key !== "labels",
    ]),
  ),
  backlog: Object.fromEntries(
    COLUMN_CHOICES.backlog.map(({ key }) => [
      key,
      key !== "recurrence" && key !== "comments" && key !== "labels",
    ]),
  ),
};

/** Done rows carry one wordy badge (the verification verdict); an operator
 * scanning fifty merged issues wants the column narrow, not readable. */
const ICON_BADGES_STORAGE = "issue-agent.dashboard.done-icon-badges";

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

function storedTaskHistoryVisibility(storageKey: string): Record<string, boolean> {
  const defaults = Object.fromEntries(
    TASK_HISTORY_COLUMN_CHOICES.map(({ key }) => [key, true]),
  );
  try {
    const all = JSON.parse(
      localStorage.getItem(TASK_HISTORY_COLUMN_STORAGE) ?? "{}",
    ) as Record<string, Record<string, boolean>>;
    return { ...defaults, ...all[storageKey] };
  } catch {
    return defaults;
  }
}

function persistTaskHistoryVisibility(
  storageKey: string,
  visibility: Record<string, boolean>,
) {
  try {
    const all = JSON.parse(
      localStorage.getItem(TASK_HISTORY_COLUMN_STORAGE) ?? "{}",
    ) as Record<string, Record<string, boolean>>;
    localStorage.setItem(
      TASK_HISTORY_COLUMN_STORAGE,
      JSON.stringify({ ...all, [storageKey]: visibility }),
    );
  } catch {
    // This table remains configurable for the mounted session without storage.
  }
}

export function IssuesTab({
  repo,
  sub,
  issues,
  onOpenIssue,
  onBacklogPromotion = () => {},
  showProjectedCost = true,
  showDurationSeconds = true,
}: IssuesTabProps) {
  const [visibility, setVisibility] = useState<Visibility>(storedVisibility);
  const [iconBadges, setIconBadges] = useState(() => readFlag(ICON_BADGES_STORAGE, false));
  const [columnMenuAt, setColumnMenuAt] = useState<{ x: number; y: number } | null>(null);
  const data = issues.data;
  const ghOk = data?.gh_ok ?? true;
  const visible = useMemo(
    () => new Set(Object.entries(visibility[sub]).filter(([, shown]) => shown).map(([key]) => key)),
    [sub, visibility],
  );
  // One in-flight payload, split by who owns the next move. The backend already
  // decided that per card; the board only groups by it.
  const [blocked, running] = useMemo(() => {
    const cards = data?.in_flight ?? [];
    return [cards.filter(blocksOnHuman), cards.filter((card) => !blocksOnHuman(card))];
  }, [data?.in_flight]);
  // Persisted by the handler, never by the state updater: React may invoke an
  // updater more than once for a single click, and a reducer that writes to
  // storage is not a pure function of its input.
  const setColumn = (key: string, shown: boolean) => {
    const next = { ...visibility, [sub]: { ...visibility[sub], [key]: shown } };
    setVisibility(next);
    try {
      localStorage.setItem(COLUMN_STORAGE, JSON.stringify(next));
    } catch {
      // URL navigation and in-memory preferences remain usable without storage.
    }
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
    // Every live duration below this point ticks from the instant THIS
    // snapshot was received, so no descendant needs its own clock or its own
    // anchor prop, and none of them can drift apart.
    <SnapshotAnchor.Provider value={issues.updatedAt}>
    <div className="tabbody" onContextMenu={openColumnMenu}>
      {!ghOk ? <div className="row"><span className="spacer" /><GhUnavailableChip /></div> : null}
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
          {/* Blocked leads the board: it is the only section whose rows cannot
              move until a person acts, so it must not be scrolled past. It
              disappears entirely when nothing is waiting, rather than standing
              as a permanent empty heading. */}
          {blocked.length > 0 ? (
            <>
              <SectionTitle>Blocked</SectionTitle>
              <InFlightTable
                storageKey="issues-blocked"
                repo={repo}
                cards={blocked}
                onRefresh={issues.refresh}
                loading={issues.loading}
                visible={visible}
                onOpen={onOpenIssue}
                showProjectedCost={showProjectedCost}
                showDurationSeconds={showDurationSeconds}
              />
            </>
          ) : null}
          <SectionTitle>In flight</SectionTitle>
          <InFlightTable
            repo={repo}
            cards={running}
            onRefresh={issues.refresh}
            loading={issues.loading}
            visible={visible}
            onOpen={onOpenIssue}
            showProjectedCost={showProjectedCost}
            showDurationSeconds={showDurationSeconds}
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
        <>
          <SectionTitle
            right={
              <button
                className="btn btn-xs"
                type="button"
                aria-pressed={iconBadges}
                title="Show the verification verdict as an icon only; the full verdict and its reason stay on hover"
                onClick={() => {
                  const next = !iconBadges;
                  setIconBadges(next);
                  writeFlag(ICON_BADGES_STORAGE, next);
                }}
              >
                Badges: {iconBadges ? "icons" : "words"}
              </button>
            }
          >
            Done
          </SectionTitle>
          <DoneTable
            repo={repo}
            cards={data?.done ?? []}
            loading={issues.loading}
            visible={visible}
            iconBadges={iconBadges}
            onOpen={onOpenIssue}
            onRefresh={issues.refresh}
            showDurationSeconds={showDurationSeconds}
          />
        </>
      ) : null}

      {sub === "backlog" ? (
        <BacklogTable
          repo={repo}
          cards={data?.backlog ?? []}
          loading={issues.loading}
          ghOk={ghOk}
          visible={visible}
          onRefresh={issues.refresh}
          onPromotion={onBacklogPromotion}
        />
      ) : null}
    </div>
    </SnapshotAnchor.Provider>
  );
}

function InFlightTable({
  repo,
  cards,
  loading,
  visible,
  onOpen,
  onRefresh,
  showProjectedCost,
  showDurationSeconds,
  // Two of these are on screen at once. Sharing one storage key would give a
  // single stored width/order/sort two live owners, and a drag in one table
  // would leave the other showing the old layout until the next reload.
  storageKey = "issues-inflight",
}: {
  repo: string;
  cards: IssueCard[];
  loading: boolean;
  visible: ReadonlySet<string>;
  onOpen: (issue: number) => void;
  onRefresh: () => void;
  storageKey?: string;
  showProjectedCost: boolean;
  showDurationSeconds: boolean;
}) {
  const [expanded, setExpanded] = useState<number | null>(null);
  const sortableAndShown = useMemo(() => new Set(["issue", ...visible]), [visible]);
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
  }, { key: "issue", direction: "asc" }, storageKey, sortableAndShown);
  const offset = useSnapshotOffset();
  if (loading) return <SkeletonRows what="in-flight issues" rows={5} />;
  if (cards.length === 0) return <EmptyState>no issues in flight — the fixer is idle</EmptyState>;
  const columns: TableColumn[] = [
    { key: "issue", label: "Issue / PR", width: 320 },
    ...(visible.has("tasks") ? [{ key: "tasks", label: "Tasks", width: 190 }] : []),
    ...(visible.has("cost") ? [{ key: "cost", label: "Cost", className: "r", width: 120 }] : []),
    ...(visible.has("elapsed") ? [{ key: "elapsed", label: "Duration", className: "r", width: 150 }] : []),
    ...(visible.has("progress") ? [{ key: "progress", label: "Progress", width: 150 }] : []),
    ...(visible.has("model") ? [{ key: "model", label: "Model", width: 180 }] : []),
  ];

  return (
    <div className="tbl-wrap panel">
      <ResizableTable
        storageKey={storageKey}
        columns={columns}
        className="issue-list-table"
        sort={sorted.sort}
        onSort={sorted.onSort}
      >
        <tbody>
          {sorted.rows.map((card) => {
            // Which clock moves is one rule, stated once, in lib/clock.ts:
            // a running leg ticks work, an idle row ticks idle, and only a
            // genuinely quiescent row freezes both.
            const { elapsed, working, idle } = liveClocks(card, offset);
            const humanOwned = !!card.human_action;
            const open = expanded === card.issue;
            // Alarm means "a human is needed NOW". `escalated`/`spend-blocked`
            // flags are lifetime counters — an issue that escalated once and
            // was re-armed carries them forever, and painting it red for life
            // buried the real alarms (keifu #25: approved, awaiting merge,
            // still red, 2026-08-02). Current attention arrives as
            // human_action or a blocked stage/leg; history stays visible as
            // chips without the row alarm.
            const alert = (
              humanOwned
              || card.stage === "blocked"
              || card.stage === "escalated"
              || card.flags.some((flag) => flag === "blocked")
            );
            // Merge-ready is attention, not alarm: same treatment, green.
            const alertClass = !alert
              ? ""
              : card.human_action === "merge-ready" ? " row-alert row-alert-ready" : " row-alert";
            return (
              <Fragment key={card.issue}>
                <tr
                  className={`clickable${open ? " expanded" : ""}${alertClass}`}
                  tabIndex={0}
                  aria-expanded={open}
                  onClick={() => setExpanded(open ? null : card.issue)}
                  onKeyDown={(event) => toggleRowFromKeyboard(event, () => setExpanded(open ? null : card.issue))}
                >
                  <td><IssueSummary card={card} open={open} /></td>
                  {visible.has("tasks") ? <td><StepBadgeRow steps={card.steps} /></td> : null}
                  {visible.has("cost") ? (
                    <td
                      className={`r mono nowrap cost-cell${overBudget(card) ? " cost-over" : ""}`}
                      title={overBudget(card) ? "over the projected cost for this model" : undefined}
                    >
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
                      <DurationBreakdown
                        working={working}
                        idle={idle}
                        showSeconds={showDurationSeconds}
                      />
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
                  <TaskDetailRow
                    repo={repo}
                    card={card}
                    colSpan={columns.length}
                    onOpen={() => onOpen(card.issue)}
                    onRefresh={onRefresh}
                    taskTableStorageKey={`${storageKey}-task-history`}
                  />
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
  const [expanded, setExpanded] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  // This table's column choices are namespaced (`pending-*`) because it shares
  // the in-flight sub-tab's menu, so the visible set has to be translated into
  // this table's own sort keys.
  const sortableAndShown = useMemo(() => new Set([
    "issue",
    ...(visible.has("pending-priority") ? ["priority"] : []),
    ...(visible.has("pending-overlap") ? ["overlap"] : []),
    ...(visible.has("pending-hold") ? ["state"] : []),
    ...(visible.has("pending-labels") ? ["labels"] : []),
  ]), [visible]);
  const sorted = useSortedRows(cards, {
    issue: (card) => card.issue,
    priority: (card) => card.priority,
    overlap: (card) => card.overlap === null ? null : String(card.overlap),
    state: (card) => card.hold_reason ?? "pending",
    labels: (card) => card.labels.join(" "),
  }, { key: "issue", direction: "asc" }, "issues-pending", sortableAndShown);
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
  const unpark = async (issue: number) => {
    setBusyIssue(issue);
    setError(null);
    try {
      await api.unpark(repo, issue);
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
                        <CompactIssue issue={card.issue} title={card.title} url={card.url} />
                      </div>
                    </td>
                    {visible.has("pending-priority") ? <td onClick={(event) => event.stopPropagation()}>
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
                      {card.hold_reason === "parked" ? (
                        <Chip tone="meta" title="operator parked: a human owns its PR; the queue no longer waits on it">
                          parked
                        </Chip>
                      ) : card.held ? (
                        <Chip tone="info" title={card.hold_reason ?? "pending"}>
                          {card.hold_reason ?? "pending"}
                        </Chip>
                      ) : <Chip tone="info">pending</Chip>}
                    </td> : null}
                    {visible.has("pending-labels") ? <td><LabelList labels={card.labels} /></td> : null}
                  </tr>
                  {open ? (
                    <tr className="detail-row">
                      <td colSpan={columns.length}>
                        <div className="row-detail">
                          <div className="row-detail-hd">
                            <strong>Issue context</strong>
                            <span className="row-wrap">
                              {card.hold_reason === "parked" ? (
                                <button
                                  className="btn btn-xs"
                                  disabled={busyIssue === card.issue}
                                  {...tipProps("Restore the PR to the harness: review, merge and overlap serialization resume")}
                                  onClick={(event) => {
                                    event.stopPropagation();
                                    void unpark(card.issue);
                                  }}
                                >
                                  Unpark
                                </button>
                              ) : null}
                              {card.url ? <a href={card.url} target="_blank" rel="noreferrer">Open on GitHub ↗</a> : null}
                            </span>
                          </div>
                          <FormattedIssueBody body={card.body ?? null} />
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
  repo,
  cards,
  loading,
  visible,
  iconBadges,
  onOpen,
  onRefresh,
  showDurationSeconds,
}: {
  repo: string;
  cards: IssueCard[];
  loading: boolean;
  visible: ReadonlySet<string>;
  iconBadges: boolean;
  onOpen: (issue: number) => void;
  onRefresh: () => void;
  showDurationSeconds: boolean;
}) {
  const [expanded, setExpanded] = useState<number | null>(null);
  // The issue column is always rendered, so it is always a legal remembered
  // sort; everything else is the reader's current column choice.
  const sortableAndShown = useMemo(() => new Set(["issue", ...visible]), [visible]);
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
    labels: (card) => card.labels.join(" "),
    // One clock, two renderings: "5d ago" and the calendar date sort together.
    since: (card) => doneAtMs(card),
    merged: (card) => doneAtMs(card),
  }, { key: "since", direction: "desc" }, "issues-done", sortableAndShown);
  if (loading) return <SkeletonRows what="done issues" rows={5} />;
  if (cards.length === 0) return <EmptyState>nothing merged by the harness in this deployment yet</EmptyState>;
  const columns: TableColumn[] = [
    { key: "issue", label: "Issue / PR", width: 340 },
    ...(visible.has("cost") ? [{ key: "cost", label: "Cost", className: "r", width: 100 }] : []),
    ...(visible.has("duration") ? [{ key: "duration", label: "Duration", className: "r", width: 150 }] : []),
    ...(visible.has("verification") ? [{ key: "verification", label: "Verification", width: 160 }] : []),
    ...(visible.has("model") ? [{ key: "model", label: "Model", width: 170 }] : []),
    ...(visible.has("rounds") ? [{ key: "rounds", label: "Rounds", className: "r", width: 90 }] : []),
    ...(visible.has("labels") ? [{ key: "labels", label: "Labels", width: 240 }] : []),
    ...(visible.has("since") ? [{ key: "since", label: "Since done", className: "r", width: 110 }] : []),
    ...(visible.has("merged") ? [{ key: "merged", label: "Date", width: 140 }] : []),
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
                    <td className={`r mono cost-cell${overBudget(card) ? " cost-over" : ""}`}>
                      {usd(card.cost_usd)}
                      {card.estimated ? <span className="faint" title="synthetic dollars"> ~</span> : null}
                    </td>
                  ) : null}
                  {visible.has("duration") ? (
                    <td className="r mono">
                      <DurationBreakdown
                        working={card.working_s}
                        idle={card.idle_s}
                        showSeconds={showDurationSeconds}
                      />
                    </td>
                  ) : null}
                  {visible.has("verification") ? (
                    <td><VerificationStatus card={card} iconOnly={iconBadges} /></td>
                  ) : null}
                  {visible.has("model") ? <td><ModelList models={card.model_badges ?? card.models.map((model) => ({ model, effort: null }))} /></td> : null}
                  {visible.has("rounds") ? <td className="r num">{num(card.rounds)}</td> : null}
                  {visible.has("labels") ? <td><LabelList labels={card.labels} /></td> : null}
                  {visible.has("since") ? (
                    <td className="r num" title={card.merged_ts ?? card.last_ts ?? ""}>
                      <TimeSinceDone card={card} />
                    </td>
                  ) : null}
                  {visible.has("merged") ? (
                    <td className="mono faint" title={card.merged_ts ?? card.last_ts ?? ""}>
                      {localDate(card.merged_ts ?? card.last_ts)}
                    </td>
                  ) : null}
                </tr>
                {open ? (
                  <TaskDetailRow
                    repo={repo}
                    card={card}
                    colSpan={columns.length}
                    onOpen={() => onOpen(card.issue)}
                    onRefresh={onRefresh}
                  />
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
  onPromotion,
}: {
  repo: string;
  cards: BacklogCard[];
  loading: boolean;
  ghOk: boolean;
  visible: ReadonlySet<string>;
  onRefresh: () => void;
  onPromotion: (card: BacklogCard, phase: BacklogPromotionPhase) => void;
}) {
  const [expanded, setExpanded] = useState<number | null>(null);
  const [promote, setPromote] = useState<BacklogCard | null>(null);
  const [error, setError] = useState<string | null>(null);
  const sortableAndShown = useMemo(() => new Set(["issue", ...visible]), [visible]);
  const sorted = useSortedRows(cards, {
    issue: (card) => card.issue,
    type: (card) => card.issue_type,
    difficulty: (card) => card.difficulty,
    impact: (card) => card.impact,
    recurrence: (card) => card.recurrence_count,
    age: (card) => card.age_s ?? (card.age_days === null ? null : card.age_days * 86_400),
    comments: (card) => card.comments,
    labels: (card) => card.labels.join(" "),
  }, { key: "issue", direction: "asc" }, "issues-backlog", sortableAndShown);
  if (loading) return <SkeletonRows what="backlog" rows={5} />;
  if (cards.length === 0) {
    return <EmptyState>{ghOk ? "no unlabelled open issues — the backlog is clear" : "backlog needs GitHub data, which is unavailable"}</EmptyState>;
  }
  const columns: TableColumn[] = [
    { key: "issue", label: "Issue", width: 350 },
    ...(visible.has("type") ? [{ key: "type", label: "Type", width: 100 }] : []),
    ...(visible.has("difficulty") ? [{ key: "difficulty", label: "Difficulty", width: 120 }] : []),
    ...(visible.has("impact") ? [{ key: "impact", label: "Impact", width: 100 }] : []),
    ...(visible.has("recurrence") ? [{ key: "recurrence", label: "Recurrence", width: 150 }] : []),
    ...(visible.has("age") ? [{ key: "age", label: "Age", className: "r", width: 90 }] : []),
    ...(visible.has("comments") ? [{ key: "comments", label: "# comments", className: "r", width: 110 }] : []),
    ...(visible.has("labels") ? [{ key: "labels", label: "Labels", width: 260 }] : []),
  ];

  const doPromote = async () => {
    if (!promote) return;
    const card = promote;
    setError(null);
    setPromote(null);
    onPromotion(card, "pending");
    try {
      await api.promote(repo, card.issue);
      onPromotion(card, "succeeded");
      onRefresh();
    } catch (caught) {
      onPromotion(card, "failed");
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  };

  return (
    <>
      {error ? <ErrorBanner message={error} /> : null}
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
                    {visible.has("type") ? (
                      <td><TriageValue value={card.issue_type} tone={issueTypeTone(card.issue_type)} axis="type" /></td>
                    ) : null}
                    {visible.has("difficulty") ? (
                      <td><TriageValue value={card.difficulty} tone={difficultyTone(card.difficulty)} axis="difficulty" /></td>
                    ) : null}
                    {visible.has("impact") ? (
                      <td><TriageValue value={card.impact} tone={impactTone(card.impact)} axis="impact" /></td>
                    ) : null}
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
          busy={false}
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

/**
 * What actually happened to this issue, in order: one line per task, carrying
 * the note the backend wrote for it (a review verdict, the question that
 * blocked it, the failure reason, an operator intervention). Money is the
 * quietest thing on the line — it only matters when it runs over.
 *
 * The card's own `steps` collapse retries and carry no prose, so the story
 * comes from the issue-detail endpoint; the collapsed strip is the fallback
 * while that loads or if it fails.
 */
function TaskDetailRow({
  repo,
  card,
  colSpan,
  onOpen,
  onRefresh,
  taskTableStorageKey,
}: {
  repo: string;
  card: IssueCard;
  colSpan: number;
  onOpen: () => void;
  onRefresh: () => void;
  taskTableStorageKey?: string;
}) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const detail = usePolled(
    `issue:${repo}:${card.issue}`,
    (signal) => api.issue(repo, card.issue, signal),
    card.done ? 0 : 5_000,
  );
  const steps = detail.data?.steps ?? null;
  const unblockable = blocksOnHuman(card);
  // Park only makes sense for the blocked-with-an-open-PR shape: a human is
  // taking the PR over, and the queue must stop waiting for it.
  const parkable = unblockable && card.pr !== null && card.pr_state === "open";

  const doUnblock = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.unblock(repo, card.issue);
      setConfirming(false);
      detail.refresh();
      onRefresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };

  const doPark = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.park(repo, card.issue);
      detail.refresh();
      onRefresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <tr className="detail-row">
      <td colSpan={colSpan}>
        <div className="row-detail">
          <div className="row-detail-hd">
            <strong>What happened</strong>
            <span className="row-wrap">
              {unblockable ? (
                <button
                  className="btn btn-xs"
                  onClick={(event) => {
                    event.stopPropagation();
                    setError(null);
                    setConfirming(true);
                  }}
                >
                  Unblock
                </button>
              ) : null}
              {parkable ? (
                <button
                  className="btn btn-xs"
                  disabled={busy}
                  {...tipProps("Hand the PR to a human and free the queue: other issues stop waiting on its files")}
                  onClick={(event) => {
                    event.stopPropagation();
                    void doPark();
                  }}
                >
                  Park
                </button>
              ) : null}
              <button className="btn btn-xs" onClick={onOpen}>Open full issue</button>
            </span>
          </div>
          {error ? <ErrorBanner message={error} /> : null}
          {card.escalation_reason ? (
            <div className="task-escalation">{card.escalation_reason}</div>
          ) : null}
          {taskTableStorageKey && (steps ?? card.steps).length > 0 ? (
            <TaskStoryTable
              issue={card.issue}
              steps={steps ?? card.steps}
              storageKey={taskTableStorageKey}
            />
          ) : null}
          {!taskTableStorageKey && steps !== null && steps.length > 0 ? (
            <ol className="task-story">
              {steps.map((step) => <TaskStoryLine key={step.idx} step={step} />)}
            </ol>
          ) : null}
          {!taskTableStorageKey && steps === null && card.steps.length > 0 ? (
            <ol className="task-story">
              {card.steps.map((step, index) => (
                <TaskStoryLine key={`${step.kind}-${index}`} step={step} />
              ))}
            </ol>
          ) : null}
          {(steps ?? card.steps).length === 0 ? (
            <EmptyState>no tasks recorded yet</EmptyState>
          ) : null}
          {confirming ? (
            <UnblockModal
              issue={card.issue}
              busy={busy}
              error={error}
              onConfirm={() => void doUnblock()}
              onCancel={() => setConfirming(false)}
            />
          ) : null}
        </div>
      </td>
    </tr>
    );
}

/**
 * Blocked and live rows are operational queues, so their expansion optimises
 * for scanning what happened, when, what it cost, and how old it is.
 */
function TaskStoryTable({
  issue,
  steps,
  storageKey,
}: {
  issue: number;
  steps: (CardStep | IssueStep)[];
  storageKey: string;
}) {
  const nowMs = useNow(60_000);
  const [columnVisibility, setColumnVisibility] = useState(
    () => storedTaskHistoryVisibility(storageKey),
  );
  const [columnMenuAt, setColumnMenuAt] = useState<{ x: number; y: number } | null>(null);
  const visible = useMemo(
    () => new Set(
      Object.entries(columnVisibility)
        .filter(([, shown]) => shown)
        .map(([key]) => key),
    ),
    [columnVisibility],
  );
  const columns: TableColumn[] = [
    { key: "task", label: "Task", width: 260, minWidth: 140 },
    ...(visible.has("time")
      ? [{ key: "time", label: "Time", width: 120, minWidth: 84 }]
      : []),
    ...(visible.has("cost")
      ? [{ key: "cost", label: "Cost", className: "r", width: 90, minWidth: 76 }]
      : []),
    ...(visible.has("since")
      ? [{ key: "since", label: "Time since", className: "r", width: 110, minWidth: 92 }]
      : []),
  ];
  const setColumn = (key: string, shown: boolean) => {
    const next = { ...columnVisibility, [key]: shown };
    setColumnVisibility(next);
    persistTaskHistoryVisibility(storageKey, next);
  };
  const openColumnMenu = (event: MouseEvent<HTMLDivElement>) => {
    if (!(event.target instanceof Element) || !event.target.closest("th")) return;
    event.preventDefault();
    // The board owns a separate parent-table menu on the same bubbling path.
    event.stopPropagation();
    setColumnMenuAt({
      x: Math.min(event.clientX, window.innerWidth - 220),
      y: Math.min(event.clientY, window.innerHeight - 220),
    });
  };

  return (
    <div className="task-story-table-shell" onContextMenu={openColumnMenu}>
      <ColumnMenu
        columns={TASK_HISTORY_COLUMN_CHOICES}
        visible={visible}
        onChange={setColumn}
        position={columnMenuAt}
        onClose={() => setColumnMenuAt(null)}
      />
      <div className="task-story-table-wrap">
        <ResizableTable
          storageKey={storageKey}
          columns={columns}
          className="task-story-table"
          ariaLabel={`Issue #${issue} task history`}
        >
          <tbody>
          {steps.map((step, index) => {
            const ts = stepTimestamp(step);
            const tone = stateTone(step.state);
            const note = stepNote(step);
            const took = stepDuration(step);
            const rawIndex = (step as { idx?: unknown }).idx;
            const key = typeof rawIndex === "number" ? rawIndex : `${step.kind}-${index}`;
            return (
              <tr key={key}>
                <td className={`task-story-context tone-border-${tone}`}>
                  <div className="task-story-event">
                    <Chip
                      tone={stepKindTone(step.kind, step.state)}
                      square
                      pulse={step.state === "running"}
                      title={step.kind}
                    >
                      {taskName(step)}
                    </Chip>
                    {step.state === "ok" || step.state === "done" ? null : (
                      <span className={`task-state tone-${tone}`}>{step.state}</span>
                    )}
                    {note ? <span className="task-note" title={note}>{note}</span> : null}
                  </div>
                </td>
                {visible.has("time") ? (
                  <td className="mono nowrap" title="task wall time">{duration(took)}</td>
                ) : null}
                {visible.has("cost") ? (
                  <td className="r mono nowrap">{taskCost(step.cost_usd)}</td>
                ) : null}
                {visible.has("since") ? (
                  <td className="r mono nowrap">{timeSince(ts, nowMs)}</td>
                ) : null}
              </tr>
            );
          })}
          </tbody>
        </ResizableTable>
      </div>
    </div>
  );
}

/** One task, one line. `note` is the payload; everything else is context. */
function TaskStoryLine({ step }: { step: CardStep | IssueStep }) {
  const tone = stateTone(step.state);
  const note = stepNote(step);
  const cost = step.cost_usd ?? 0;
  const took = stepDuration(step);
  return (
    <li className={`task-story-line tone-border-${tone}`}>
      <Chip
        tone={stepKindTone(step.kind, step.state)}
        square
        pulse={step.state === "running"}
        title={step.kind}
      >
        {taskName(step)}
      </Chip>
      {/* Success is already the chip's tone; the word "done" beside every
          finished task was pure noise (and in the Done tab, doubly so). */}
      {step.state === "ok" || step.state === "done" ? null : (
        <span className={`task-state tone-${tone}`}>{step.state}</span>
      )}
      {note ? <span className="task-note" title={note}>{note}</span> : null}
      <span className="spacer" />
      {took !== null ? (
        <span className="mono faint task-took" title="task wall time">{duration(took)}</span>
      ) : null}
      {cost > 0 ? <span className="mono faint task-cost">{usd(step.cost_usd)}</span> : null}
    </li>
  );
}

/** Only detailed steps carry prose; card steps are the note-less fallback. */
function stepNote(step: CardStep | IssueStep): string | null {
  const value = (step as { note?: unknown }).note;
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

/** Only detailed steps carry timing; a still-running task has none yet. */
function stepDuration(step: CardStep | IssueStep): number | null {
  const value = (step as { duration_s?: unknown }).duration_s;
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : null;
}

/** Card summaries have no timestamp; detailed rows do. */
function stepTimestamp(step: CardStep | IssueStep): string | null {
  const value = (step as { ts?: unknown }).ts;
  return typeof value === "string" && parseTs(value) !== null ? value : null;
}

function timeSince(ts: string | null, nowMs: number): string {
  const at = parseTs(ts);
  return at === null ? ageCompact(null) : ageCompact(Math.max(0, (nowMs - at) / 1000));
}

function taskCost(cost: number | null | undefined): string {
  return cost === 0 ? DASH : usd(cost);
}

function taskName(step: CardStep | IssueStep): string {
  return step.kind.replaceAll("_", " ").toLowerCase();
}

/** Cost is noise until it exceeds what this model usually spends. */
function overBudget(card: IssueCard): boolean {
  const projected = card.projected_cost_usd;
  return (
    projected !== null
    && projected > 0
    && card.cost_usd !== null
    && card.cost_usd > projected
  );
}

function IssueSummary({ card, open }: { card: IssueCard; open: boolean }) {
  // Ticked from the server's own measurement plus the age of this snapshot,
  // not from parsing the server's timestamp against the browser's clock: a
  // client a few minutes off would otherwise show a wait that never matches
  // the one the harness is acting on.
  const offset = useSnapshotOffset();
  const waitS = card.human_wait_s;
  const humanWait = typeof waitS === "number" && Number.isFinite(waitS)
    ? Math.max(0, waitS) + offset
    : null;
  const secondaryFlags = card.flags.filter((flag) => flag !== card.human_action);
  const status = humanStatus(card);
  return (
    <div className="issue-summary">
      <span className={`chev${open ? " open" : ""}`} aria-hidden="true">▸</span>
      <div className="issue-summary-main">
        <span className="issue-summary-title" title={card.title ?? undefined}>
          {card.title ?? <span className="faint">title unavailable</span>}
        </span>
        {status ? (
          <span
            className={`issue-attention${card.human_action === "merge-ready" ? " issue-attention-ready" : ""}`}
            title={status}
          >
            {status}
          </span>
        ) : null}
        <div className="row-wrap">
          <IssueNumber issue={card.issue} url={card.url} />
          <PrLink card={card} />
          {card.stage === "blocked" || card.stage === "escalated" ? (
            <span className="attention-stage">{card.stage}</span>
          ) : null}
          {card.human_action ? (
            <span className={`human-action human-action-${card.human_action}`}>
              {card.human_action.replace("-", " ")}
              {humanWait !== null ? ` · ${duration(humanWait, false)}` : ""}
            </span>
          ) : null}
          {secondaryFlags.length > 0 ? <FlagList flags={secondaryFlags} /> : null}
        </div>
      </div>
    </div>
  );
}

/**
 * The row's second line: what a human would have to read the issue to learn.
 *
 * The chip beside it already names the *kind* of attention, so this line
 * carries the reason and never repeats the kind. Needs-budget-review arrives as
 * `escalated` with the cap decision stated in `escalation_reason`.
 */
function humanStatus(card: IssueCard): string | null {
  const prose = (text: string | null | undefined) => text?.trim() || null;
  switch (card.human_action) {
    case "escalated":
      return prose(card.escalation_reason)
        ?? prose(card.attention_reason)
        ?? "escalated — see the issue comments";
    case "awaiting-reply":
      return prose(card.attention_reason)
        ?? "the agent asked a question — reply on GitHub to unblock it";
    case "merge-ready":
      return "reviewed and green — merge when you are ready";
    default:
      break;
  }
  if (card.stage === "awaiting human reply") {
    const why = prose(card.attention_reason);
    return why ? `awaiting human reply · ${why}` : "awaiting human reply";
  }
  return prose(card.escalation_reason);
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

/**
 * When the harness finished with this issue: the merge stamp when gh knows it,
 * otherwise the last journalled event. Null when neither is parseable — an
 * unknown instant must render as "—", never as "0s ago".
 */
function doneAtMs(card: IssueCard): number | null {
  return parseTs(card.merged_ts ?? card.last_ts);
}

/**
 * Subscribes to the clock itself rather than taking `now` from the table.
 * A "3d" string changes once a day, but a table-level clock re-rendered every
 * merged issue — chips, badges, expanded detail and all — once a second.
 */
const TimeSinceDone = memo(function TimeSinceDone({ card }: { card: IssueCard }) {
  const nowMs = useNow();
  const at = doneAtMs(card);
  if (at === null) return <span className="faint">—</span>;
  return <>{ageCompact(Math.max(0, (nowMs - at) / 1000))}</>;
});

function PrLink({ card }: { card: IssueCard }) {
  if (card.pr === null) return null;
  const review = prReviewMark(card.pr_review_state);
  // The chip's number is CI's answer at a glance: green passing, red failing,
  // amber running. No checks or unknown → inherit, claiming nothing.
  const ciTone =
    card.pr_ci_state === "passing" ? "ok"
    : card.pr_ci_state === "failing" ? "fail"
    : card.pr_ci_state === "pending" ? "running"
    : null;
  const chip = (
    <Chip
      tone={prStateTone(card.pr_state)}
      square
      title={[
        card.pr_state ?? "pull request",
        card.pr_ci_state ? `CI ${card.pr_ci_state}` : null,
        review?.label,
      ].filter(Boolean).join(" · ")}
    >
      <span className={ciTone ? `tone-${ciTone}` : undefined}>PR #{card.pr}</span>
      {review ? (
        <span className={`pr-review-mark tone-${review.tone}`} role="img" aria-label={review.label}>
          {review.glyph}
        </span>
      ) : null}
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

/**
 * The verdict badge. Its title is the only place the ledger's *reason* is
 * readable, so the icon-only form must keep both the word and the prose there —
 * a glyph with no hover text would be a verdict nobody can question.
 */
function VerificationStatus({
  card,
  iconOnly = false,
}: {
  card: IssueCard;
  iconOnly?: boolean;
}) {
  const status = combinedVerificationStatus(
    card.issue_verification,
    card.pr_verification,
  );
  if (!status) return <span className="faint">—</span>;
  const title = verificationSummary({
    issueStatus: card.issue_verification,
    issueReason: card.issue_verification_reason,
    prStatus: card.pr_verification,
    prReason: card.pr_verification_reason,
  });
  return (
    <Chip
      tone={verificationTone(status)}
      title={`${status}\n${title}`}
      className={iconOnly ? "chip-icon" : undefined}
    >
      {iconOnly ? (
        <span role="img" aria-label={status}>{verificationGlyph(status)}</span>
      ) : status}
    </Chip>
  );
}

function DurationBreakdown({
  working,
  idle,
  showSeconds,
}: {
  working: number | null;
  idle: number | null;
  showSeconds: boolean;
}) {
  return (
    <span className="duration-breakdown">
      <span title="time actively progressing through harness work">
        <span className="faint">work</span> {duration(working, showSeconds)}
      </span>
      <span title="time waiting on a dependency, hold, or human input">
        <span className="faint">idle</span> {duration(idle, showSeconds)}
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

/** One triage axis value. Unclassified reads as an absence, not as a value. */
function TriageValue({
  value,
  tone,
  axis,
}: {
  value: string | null | undefined;
  tone: Tone;
  axis: string;
}) {
  if (!value) return <span className="faint">—</span>;
  return <Chip tone={tone} square title={`${axis}: ${value}`}>{value}</Chip>;
}

/** Labels carry the triage colours too, so a chip and its column agree. */
function LabelList({ labels }: { labels: string[] }) {
  if (labels.length === 0) return <span className="faint">—</span>;
  return (
    <span className="row-wrap">
      {labels.map((label) => <Chip key={label} tone={labelTone(label)}>{label}</Chip>)}
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
