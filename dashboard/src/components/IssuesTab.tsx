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
import { SnapshotAnchor, useNow } from "../lib/clock";
import type {
  BacklogCard,
  CardStep,
  IssueCard,
  IssueStep,
  IssuesPayload,
} from "../api/types";
import type { IssuesSub } from "../routing/hash";
import {
  difficultyTone,
  impactTone,
  issueTypeTone,
  labelTone,
  modelTone,
  prReviewMark,
  stepOutcomeTone,
  type Tone,
} from "../lib/semantics";
import {
  ageCompact,
  duration,
  localDate,
  num,
  parseTs,
  usd,
} from "../lib/format";
import { shortModel } from "../lib/models";
import { boardItems, matchesQuery } from "../features/issues/board";
import { Chip } from "./ui/Chip";
import { Icon } from "./ui/Icon";
import { StepBadge } from "./ui/StepBadge";
import { TableFilter } from "./ui/TableFilter";
import { ColumnMenu, type ColumnChoice } from "./ui/ColumnMenu";
import {
  ResizableTable,
  useSortedRows,
  type TableColumn,
} from "./ui/ResizableTable";
import { PromoteConfirm } from "./PromoteConfirm";
import {
  EmptyState,
  ErrorBanner,
  GhUnavailableChip,
  SkeletonRows,
  SectionTitle,
  WarningList,
} from "./ui/Feedback";
import { IssuesBoard } from "./IssuesBoard";

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

/**
 * The two column sets; the in-flight board has none.
 */
type TableSub = "done" | "backlog";
type Visibility = Record<TableSub, Record<string, boolean>>;

const COLUMN_STORAGE = "issue-agent.dashboard.columns";
const COLUMN_CHOICES: Record<TableSub, ColumnChoice[]> = {
  done: [
    { key: "cost", label: "Cost" },
    { key: "duration", label: "Duration" },
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

function storedVisibility(): Visibility {
  try {
    const parsed = JSON.parse(localStorage.getItem(COLUMN_STORAGE) ?? "{}") as Partial<Visibility>;
    return {
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
  onOpenIssue,
  onBacklogPromotion = () => {},
  showProjectedCost = true,
  showDurationSeconds = true,
}: IssuesTabProps) {
  const [visibility, setVisibility] = useState<Visibility>(storedVisibility);
  const [queries, setQueries] = useState<Record<IssuesSub, string>>({
    inflight: "",
    done: "",
    backlog: "",
  });
  const [columnMenuAt, setColumnMenuAt] = useState<{ x: number; y: number } | null>(null);
  const data = issues.data;
  const ghOk = data?.gh_ok ?? true;
  const table: TableSub | null = sub === "inflight" ? null : sub;
  const query = queries[sub];
  const normalizedQuery = query.trim().toLowerCase();
  const visible = useMemo(
    () => new Set(
      table === null
        ? []
        : Object.entries(visibility[table]).filter(([, shown]) => shown).map(([key]) => key),
    ),
    [table, visibility],
  );
  // In-flight, queued and triage-blocked backlog rows are one board; the
  // server already decided each row's group.
  const allBoard = useMemo(() => boardItems(data), [data]);
  const filteredBoard = useMemo(
    () => allBoard.filter((item) => matchesQuery(item, normalizedQuery)),
    [allBoard, normalizedQuery],
  );
  const filteredDone = useMemo(
    () => filterCards(data?.done ?? [], normalizedQuery),
    [data?.done, normalizedQuery],
  );
  const filteredBacklog = useMemo(
    () => filterCards(data?.backlog ?? [], normalizedQuery),
    [data?.backlog, normalizedQuery],
  );
  const sourceCount = {
    inflight: allBoard.length,
    done: data?.done.length ?? 0,
    backlog: data?.backlog.length ?? 0,
  }[sub];
  const filteredCount = {
    inflight: filteredBoard.length,
    done: filteredDone.length,
    backlog: filteredBacklog.length,
  }[sub];
  const hasMatches = !normalizedQuery || filteredCount > 0;
  // Persisted by the handler, never by the state updater: React may invoke an
  // updater more than once for a single click, and a reducer that writes to
  // storage is not a pure function of its input.
  const setColumn = (key: string, shown: boolean) => {
    if (table === null) return;
    const next = { ...visibility, [table]: { ...visibility[table], [key]: shown } };
    setVisibility(next);
    try {
      localStorage.setItem(COLUMN_STORAGE, JSON.stringify(next));
    } catch {
      // URL navigation and in-memory preferences remain usable without storage.
    }
  };
  const openColumnMenu = (event: MouseEvent<HTMLDivElement>) => {
    if (table === null) return;
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
      {/* The visible <h1> was removed by design; screen-reader heading
          navigation still needs the page identified. */}
      <h1 className="sr-only">
        {{ inflight: "In-flight issues", done: "Done issues", backlog: "Backlog" }[sub]}
      </h1>
      {!ghOk ? <div className="row"><span className="spacer" /><GhUnavailableChip /></div> : null}
      {table !== null ? (
        <ColumnMenu
          columns={COLUMN_CHOICES[table]}
          visible={visible}
          onChange={setColumn}
          position={columnMenuAt}
          onClose={() => setColumnMenuAt(null)}
        />
      ) : null}

      {issues.error ? (
        <ErrorBanner message={issues.error} onRetry={issues.refresh} stale={!!data} />
      ) : null}
      <WarningList warnings={data?.warnings} />
      {sourceCount > 10 ? (
        <TableFilter
          label={`Filter ${sub === "inflight" ? "active issues" : `${sub} issues`}`}
          value={query}
          onChange={(value) => setQueries((current) => ({ ...current, [sub]: value }))}
          shown={filteredCount}
          total={sourceCount}
        />
      ) : null}
      {!hasMatches ? <EmptyState>no issues match “{query.trim()}”</EmptyState> : null}

      {sub === "inflight" && hasMatches ? (
        <IssuesBoard
          repo={repo}
          items={filteredBoard}
          loading={issues.loading}
          onRefresh={issues.refresh}
          onOpenIssue={onOpenIssue}
          showProjectedCost={showProjectedCost}
          showDurationSeconds={showDurationSeconds}
        />
      ) : null}

      {sub === "done" && hasMatches ? (
        <>
          <SectionTitle>Done</SectionTitle>
          <DoneTable
            repo={repo}
            cards={filteredDone}
            loading={issues.loading}
            visible={visible}
            onOpen={onOpenIssue}
            showDurationSeconds={showDurationSeconds}
          />
        </>
      ) : null}

      {sub === "backlog" && hasMatches ? (
        <BacklogTable
          repo={repo}
          cards={filteredBacklog}
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

type FilterableCard = IssueCard | BacklogCard;

/** Search only operator-facing fields, not arbitrary payload internals. */
function filterCards<T extends FilterableCard>(cards: T[], query: string): T[] {
  if (!query) return cards;
  return cards.filter((card) => {
    const issue = `#${card.issue}`;
    const common = [issue, card.issue, card.title, ...card.labels];
    const details = isIssueCard(card)
      ? [card.stage, card.pr, ...card.models]
      : [card.issue_type, card.difficulty, card.impact, card.body];
    return [...common, ...details]
      .some((value) => String(value ?? "").toLowerCase().includes(query));
  });
}

function isIssueCard(card: FilterableCard): card is IssueCard {
  return typeof (card as IssueCard).done === "boolean";
}

function DoneTable({
  repo,
  cards,
  loading,
  visible,
  onOpen,
  showDurationSeconds,
}: {
  repo: string;
  cards: IssueCard[];
  loading: boolean;
  visible: ReadonlySet<string>;
  onOpen: (issue: number) => void;
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
  storageKey = "issues-backlog",
}: {
  repo: string;
  cards: BacklogCard[];
  loading: boolean;
  ghOk: boolean;
  visible: ReadonlySet<string>;
  onRefresh: () => void;
  onPromotion: (card: BacklogCard, phase: BacklogPromotionPhase) => void;
  storageKey?: string;
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
  }, { key: "issue", direction: "asc" }, storageKey, sortableAndShown);
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
          storageKey={storageKey}
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
                            {card.url ? <a href={card.url} target="_blank" rel="noreferrer">Open on GitHub</a> : null}
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
        <PromoteConfirm
          issue={promote.issue}
          title={promote.title}
          onConfirm={() => void doPromote()}
          onCancel={() => setPromote(null)}
        />
      ) : null}
    </>
  );
}

/**
 * What actually happened to this merged issue, in order: one line per task,
 * carrying the note the backend wrote for it (a review verdict, the failure
 * reason, an operator intervention). Money is the quietest thing on the line.
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
}: {
  repo: string;
  card: IssueCard;
  colSpan: number;
  onOpen: () => void;
}) {
  const detail = usePolled(
    `issue:${repo}:${card.issue}`,
    (signal) => api.issue(repo, card.issue, signal),
    0,
  );
  const steps: (CardStep | IssueStep)[] = detail.data?.steps ?? card.steps;
  return (
    <tr className="detail-row">
      <td colSpan={colSpan}>
        <div className="row-detail">
          <div className="row-detail-hd">
            <strong>What happened</strong>
            <button className="btn btn-xs" onClick={onOpen}>Open full issue</button>
          </div>
          {steps.length > 0 ? (
            <ol className="task-story">
              {steps.map((step, index) => (
                <TaskStoryLine key={(step as { idx?: number }).idx ?? `${step.kind}-${index}`} step={step} />
              ))}
            </ol>
          ) : <EmptyState>no tasks recorded yet</EmptyState>}
        </div>
      </td>
    </tr>
  );
}

/** One task, one line. `note` is the payload; everything else is context. */
function TaskStoryLine({ step }: { step: CardStep | IssueStep }) {
  const tone = stepOutcomeTone(step.kind, step.state, step.status);
  const note = stepNote(step);
  const cost = step.cost_usd ?? 0;
  const took = stepDuration(step);
  return (
    <li className={`task-story-line tone-border-${tone}`}>
      <StepBadge step={step} label={taskName(step)} title={step.kind} />
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
  return (
    <div className="issue-cell">
      <div className="issue-summary">
        <span className={`chev${open ? " open" : ""}`} aria-hidden="true">▸</span>
        <div className="issue-summary-main">
          <span className="issue-summary-title" title={card.title ?? undefined}>
            {card.title ?? <span className="faint">title unavailable</span>}
          </span>
          <div className="row-wrap">
            <IssueNumber issue={card.issue} url={card.url} />
            <PrLink card={card} />
          </div>
        </div>
      </div>
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

interface PrLinkCard {
  pr?: number | null;
  pr_url?: string | null;
  pr_state?: string | null;
  pr_review_state?: IssueCard["pr_review_state"];
  pr_ci_state?: IssueCard["pr_ci_state"];
}

function PrLink({ card }: { card: PrLinkCard }) {
  if (card.pr == null) return null;
  const review = prReviewMark(card.pr_review_state);
  // The complete control carries CI's answer; the review glyph remains a
  // redundant second signal when GitHub has also published that decision.
  const ciTone: Tone =
    card.pr_ci_state === "passing" ? "ok"
    : card.pr_ci_state === "failing" ? "fail"
    : card.pr_ci_state === "pending" ? "running"
    : "neutral";
  const chip = (
    <Chip
      tone={ciTone}
      square
      title={[
        card.pr_state ?? "pull request",
        `CI ${card.pr_ci_state ?? "unknown"}`,
        review?.label,
      ].filter(Boolean).join(" · ")}
    >
      <Icon name="git-pull-request" size={12} />
      <span>PR #{card.pr}</span>
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
