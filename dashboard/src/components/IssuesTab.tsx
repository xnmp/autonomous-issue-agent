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
import { useNow } from "../lib/clock";
import type {
  BacklogCard,
  DoneCard,
  IssueStep,
  IssuesPayload,
} from "../api/types";
import type { IssuesSub } from "../routing/hash";
import {
  difficultyTone,
  impactTone,
  issueTypeTone,
  labelTone,
  legNote,
  outcomeMark,
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
import { boardItems, matchesQuery } from "../features/issues/board";
import { Chip } from "./ui/Chip";
import { Icon } from "./ui/Icon";
import { StepBadge } from "./ui/StepBadge";
import { TableFilter } from "./ui/TableFilter";
import { Pager, paginate } from "./ui/Pager";
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
  StoreUnavailable,
  ReadingGitHub,
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
    { key: "legs", label: "Legs" },
    { key: "rounds", label: "Review rounds" },
    { key: "labels", label: "Labels" },
    { key: "since", label: "Time since done" },
    { key: "merged", label: "Date" },
  ],
  backlog: [
    { key: "type", label: "Type" },
    { key: "difficulty", label: "Difficulty" },
    { key: "impact", label: "Impact" },
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
      key !== "comments" && key !== "labels",
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
}: IssuesTabProps) {
  const [visibility, setVisibility] = useState<Visibility>(storedVisibility);
  const [queries, setQueries] = useState<Record<IssuesSub, string>>({
    inflight: "",
    done: "",
    backlog: "",
  });
  // Per sub-tab and event-reset: filter and sort changes zero it, and
  // paginate() clamps it when a refresh shrinks the list.
  const [pages, setPages] = useState<Record<TableSub, number>>({ done: 0, backlog: 0 });
  const setPage = (which: TableSub) => (page: number) => setPages((current) => ({ ...current, [which]: page }));
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
  // The server already derived each row's group.
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
    <div className="tabbody" onContextMenu={openColumnMenu}>
      {/* The visible <h1> was removed by design; screen-reader heading
          navigation still needs the page identified. */}
      <h1 className="sr-only">
        {{ inflight: "In-flight issues", done: "Done issues", backlog: "Backlog" }[sub]}
      </h1>
      {!ghOk ? <div className="row"><span className="spacer" /><GhUnavailableChip /></div> : null}
      <ReadingGitHub loaded={!!data} observedAt={data?.observed_at} />
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
      <StoreUnavailable detail={data?.store_error} />
      <WarningList warnings={data?.warnings} />
      {sourceCount > 10 ? (
        <TableFilter
          label={`Filter ${sub === "inflight" ? "active issues" : `${sub} issues`}`}
          value={query}
          onChange={(value) => {
            setQueries((current) => ({ ...current, [sub]: value }));
            if (table !== null) setPage(table)(0);
          }}
          shown={filteredCount}
          total={sourceCount}
        />
      ) : null}
      {!hasMatches ? <EmptyState>no issues match “{query.trim()}”</EmptyState> : null}

      {sub === "inflight" && hasMatches ? (
        <IssuesBoard
          repo={repo}
          items={filteredBoard}
          slots={data?.slots}
          loading={issues.loading}
          onRefresh={issues.refresh}
          onOpenIssue={onOpenIssue}
        />
      ) : null}

      {sub === "done" && hasMatches ? (
        <>
          <SectionTitle>Done</SectionTitle>
          {data?.merged_partial ? (
            <p className="muted">Showing only the newest merged PRs; older merges are not counted.</p>
          ) : null}
          <DoneTable
            repo={repo}
            cards={filteredDone}
            page={pages.done}
            onPage={setPage("done")}
            loading={issues.loading}
            visible={visible}
            onOpen={onOpenIssue}
          />
        </>
      ) : null}

      {sub === "backlog" && hasMatches ? (
        <BacklogTable
          repo={repo}
          cards={filteredBacklog}
          page={pages.backlog}
          onPage={setPage("backlog")}
          loading={issues.loading}
          ghOk={ghOk}
          visible={visible}
          onRefresh={issues.refresh}
          onPromotion={onBacklogPromotion}
        />
      ) : null}
    </div>
  );
}

type FilterableCard = DoneCard | BacklogCard;

/** Search only operator-facing fields, not arbitrary payload internals. */
function filterCards<T extends FilterableCard>(cards: T[], query: string): T[] {
  if (!query) return cards;
  return cards.filter((card) => {
    const issue = `#${card.issue}`;
    const common = [issue, card.issue, card.title, ...card.labels];
    const details = "pr" in card
      ? [card.pr, `PR #${card.pr}`]
      : [card.issue_type, card.difficulty, card.impact, card.body];
    return [...common, ...details]
      .some((value) => String(value ?? "").toLowerCase().includes(query));
  });
}

function DoneTable({
  repo,
  cards,
  page: requestedPage,
  onPage,
  loading,
  visible,
  onOpen,
}: {
  repo: string;
  cards: DoneCard[];
  page: number;
  onPage: (page: number) => void;
  loading: boolean;
  visible: ReadonlySet<string>;
  onOpen: (issue: number) => void;
}) {
  const [expanded, setExpanded] = useState<number | null>(null);
  // The issue column is always rendered, so it is always a legal remembered
  // sort; everything else is the reader's current column choice.
  const sortableAndShown = useMemo(() => new Set(["issue", ...visible]), [visible]);
  const sorted = useSortedRows(cards, {
    issue: (card) => card.issue,
    cost: (card) => card.cost_usd,
    legs: (card) => card.legs,
    rounds: (card) => card.review_rounds,
    labels: (card) => card.labels.join(" "),
    // One clock, two renderings: "5d ago" and the calendar date sort together.
    since: (card) => doneAtMs(card),
    merged: (card) => doneAtMs(card),
  }, { key: "since", direction: "desc" }, "issues-done", sortableAndShown);
  const page = paginate(sorted.rows, requestedPage);
  if (loading) return <SkeletonRows what="done issues" rows={5} />;
  if (cards.length === 0) return <EmptyState>nothing merged by the harness in this deployment yet</EmptyState>;
  const columns: TableColumn[] = [
    { key: "issue", label: "Issue / PR", width: 340 },
    ...(visible.has("cost") ? [{ key: "cost", label: "Cost", className: "r", width: 100 }] : []),
    ...(visible.has("legs") ? [{ key: "legs", label: "Legs", className: "r", width: 80 }] : []),
    ...(visible.has("rounds") ? [{ key: "rounds", label: "Rounds", className: "r", width: 90 }] : []),
    ...(visible.has("labels") ? [{ key: "labels", label: "Labels", width: 240 }] : []),
    ...(visible.has("since") ? [{ key: "since", label: "Since done", className: "r", width: 110 }] : []),
    ...(visible.has("merged") ? [{ key: "merged", label: "Date", width: 140 }] : []),
  ];

  return (
    <>
    <Pager page={page} onPage={onPage} />
    <div className="tbl-wrap panel">
      <ResizableTable
        storageKey="issues-done"
        columns={columns}
        className="issue-list-table"
        sort={sorted.sort}
        onSort={(key) => { sorted.onSort(key); onPage(0); }}
      >
        <tbody>
          {page.rows.map((card) => {
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
                    <td className="r mono cost-cell">{usd(card.cost_usd)}</td>
                  ) : null}
                  {visible.has("legs") ? <td className="r num">{num(card.legs)}</td> : null}
                  {visible.has("rounds") ? <td className="r num">{num(card.review_rounds)}</td> : null}
                  {visible.has("labels") ? <td><LabelList labels={card.labels} /></td> : null}
                  {visible.has("since") ? (
                    <td className="r num" title={card.merged_ts ?? ""}>
                      <TimeSinceDone card={card} />
                    </td>
                  ) : null}
                  {visible.has("merged") ? (
                    <td className="mono faint" title={card.merged_ts ?? ""}>
                      {localDate(card.merged_ts)}
                    </td>
                  ) : null}
                </tr>
                {open ? (
                  <TaskDetailRow
                    repo={repo}
                    issue={card.issue}
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
    <Pager page={page} onPage={onPage} />
    </>
  );
}

function BacklogTable({
  repo,
  cards,
  page: requestedPage,
  onPage,
  loading,
  ghOk,
  visible,
  onRefresh,
  onPromotion,
  storageKey = "issues-backlog",
}: {
  repo: string;
  cards: BacklogCard[];
  page: number;
  onPage: (page: number) => void;
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
    age: (card) => card.age_s ?? (card.age_days === null ? null : card.age_days * 86_400),
    comments: (card) => card.comments,
    labels: (card) => card.labels.join(" "),
  }, { key: "issue", direction: "asc" }, storageKey, sortableAndShown);
  const page = paginate(sorted.rows, requestedPage);
  if (loading) return <SkeletonRows what="backlog" rows={5} />;
  if (cards.length === 0) {
    return <EmptyState>{ghOk ? "no unlabelled open issues — the backlog is clear" : "backlog needs GitHub data, which is unavailable"}</EmptyState>;
  }
  const columns: TableColumn[] = [
    { key: "issue", label: "Issue", width: 350 },
    ...(visible.has("type") ? [{ key: "type", label: "Type", width: 100 }] : []),
    ...(visible.has("difficulty") ? [{ key: "difficulty", label: "Difficulty", width: 120 }] : []),
    ...(visible.has("impact") ? [{ key: "impact", label: "Impact", width: 100 }] : []),
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
      <Pager page={page} onPage={onPage} />
      <div className="tbl-wrap panel">
        <ResizableTable
          storageKey={storageKey}
          columns={columns}
          className="issue-list-table"
          sort={sorted.sort}
          onSort={(key) => { sorted.onSort(key); onPage(0); }}
        >
          <tbody>
            {page.rows.map((card) => {
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
                          <span className="issue-summary-title" title={card.title ?? undefined}>
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
      <Pager page={page} onPage={onPage} />

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
 * What actually happened to this merged issue, in order: one line per leg,
 * carrying what the leg reported (a review verdict, the agent's summary or
 * question). Money is the quietest thing on the line.
 */
function TaskDetailRow({
  repo,
  issue,
  colSpan,
  onOpen,
}: {
  repo: string;
  issue: number;
  colSpan: number;
  onOpen: () => void;
}) {
  const detail = usePolled(
    `issue:${repo}:${issue}`,
    (signal) => api.issue(repo, issue, signal),
    0,
  );
  const steps = detail.data?.steps ?? [];
  const carried = detail.data?.carried ?? null;
  return (
    <tr className="detail-row">
      <td colSpan={colSpan}>
        <div className="row-detail">
          <div className="row-detail-hd">
            <strong>What happened</strong>
            <button className="btn btn-xs" onClick={onOpen}>Open full issue</button>
          </div>
          {detail.error ? (
            <ErrorBanner message={detail.error} onRetry={detail.refresh} />
          ) : !detail.data ? (
            <span className="faint">loading legs…</span>
          ) : (
            <>
              {carried ? (
                <span className="faint carried-note">
                  {carried.legs} {carried.legs === 1 ? "leg" : "legs"} before the cutover · {usd(carried.cost_usd)}
                </span>
              ) : null}
              {steps.length > 0 ? (
                <ol className="task-story">
                  {steps.map((step) => <TaskStoryLine key={step.leg_id} step={step} />)}
                </ol>
              ) : carried ? null : <EmptyState>no legs recorded</EmptyState>}
            </>
          )}
        </div>
      </td>
    </tr>
  );
}

/** One leg, one line. The note is the payload; everything else is context. */
function TaskStoryLine({ step }: { step: IssueStep }) {
  const mark = outcomeMark(step.outcome);
  const note = legNote(step);
  return (
    <li className={`task-story-line tone-border-${mark.tone}`}>
      <StepBadge step={step} label={step.kind.replaceAll("_", " ").toLowerCase()} title={step.kind} />
      {/* Success is already the chip's tone; the word beside every finished
          leg was pure noise. */}
      {mark.tone === "ok" ? null : <span className={`task-state tone-${mark.tone}`}>{mark.label}</span>}
      {note ? <span className="task-note" title={note}>{note}</span> : null}
      <span className="spacer" />
      {step.duration_s ? (
        <span className="mono faint task-took" title="leg wall time">{duration(step.duration_s)}</span>
      ) : null}
      {step.cost_usd > 0 ? <span className="mono faint task-cost">{usd(step.cost_usd)}</span> : null}
    </li>
  );
}

function IssueSummary({ card, open }: { card: DoneCard; open: boolean }) {
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
            <PrLink pr={card.pr} url={card.pr_url} />
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
 * When the PR merged. Null when unparseable — an unknown instant must render
 * as "—", never as "0s ago".
 */
function doneAtMs(card: DoneCard): number | null {
  return parseTs(card.merged_ts);
}

/**
 * Subscribes to the clock itself rather than taking `now` from the table.
 * A "3d" string changes once a day, but a table-level clock re-rendered every
 * merged issue — chips, badges, expanded detail and all — once a second.
 */
const TimeSinceDone = memo(function TimeSinceDone({ card }: { card: DoneCard }) {
  const nowMs = useNow();
  const at = doneAtMs(card);
  if (at === null) return <span className="faint">—</span>;
  return <>{ageCompact(Math.max(0, (nowMs - at) / 1000))}</>;
});

/** A merged harness PR. */
function PrLink({ pr, url }: { pr: number; url: string | null }) {
  const chip = (
    <Chip tone="ok" square title="merged pull request">
      <Icon name="git-pull-request" size={12} />
      <span>PR #{pr}</span>
    </Chip>
  );
  if (!url) return chip;
  return (
    <a href={url} target="_blank" rel="noreferrer" onClick={(event) => event.stopPropagation()}>
      {chip}
    </a>
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
