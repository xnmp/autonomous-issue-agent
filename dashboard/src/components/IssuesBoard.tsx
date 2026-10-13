import { useMemo, useState } from "react";
import type { IssueCard } from "../api/types";
import {
  COLUMN_HEADINGS,
  GROUP_META,
  GROUP_ORDER,
  OUTCOME_MARK,
  STAGES,
  boardTone,
  groupCounts,
  groupItems,
  historyView,
  plainHarnessMessage,
  primaryAction,
  railNodes,
  sayLine,
  stageLabel,
  waitCell,
  type BoardGroup,
  type BoardItem,
  type PrimaryAction,
  type StageId,
  type StageVisit,
} from "../features/issues/board";
import { ago, usd } from "../lib/format";
import { useNow } from "../lib/clock";
import { kindLabel } from "../lib/semantics";
import { Chip } from "./ui/Chip";
import { Icon } from "./ui/Icon";
import { UnblockControl } from "./UnblockControl";
import { LifecycleStepper } from "./ui/LifecycleStepper";
import { tipProps } from "./ui/Tip";
import { SkeletonRows } from "./ui/Feedback";
import "./issues-board.css";

/** A title this long is clamped on the row, so the drawer repeats it whole. */
const CLAMPED_TITLE_CHARS = 70;

export function IssuesBoard({
  repo,
  items,
  loading,
  onRefresh,
  onOpenIssue,
}: {
  repo: string;
  items: BoardItem[];
  loading: boolean;
  onRefresh: () => void;
  onOpenIssue: (issue: number) => void;
}) {
  const [filter, setFilter] = useState<BoardGroup | null>(null);
  const [open, setOpen] = useState<ReadonlySet<number>>(() => new Set());
  const counts = useMemo(() => groupCounts(items), [items]);
  const sections = useMemo(() => groupItems(items), [items]);
  const toggle = (issue: number) => setOpen((current) => {
    const next = new Set(current);
    if (!next.delete(issue)) next.add(issue);
    return next;
  });
  if (loading) return <SkeletonRows what="issues" rows={5} />;

  return (
    <div className="board">
      <div className="board-summary" role="group" aria-label="Filter by status">
        <button
          type="button"
          className="board-chip"
          aria-pressed={filter === null}
          onClick={() => setFilter(null)}
        >
          <span className="board-chip-n num">{items.length}</span>
          <span className="board-chip-label">All</span>
        </button>
        {GROUP_ORDER.map((group) => (
          <button
            key={group}
            type="button"
            className={`board-chip board-tone-${GROUP_META[group].tone}${counts[group] === 0 ? " board-chip-zero" : ""}`}
            aria-pressed={filter === group}
            onClick={() => setFilter((current) => (current === group ? null : group))}
          >
            <i aria-hidden="true" />
            <span className="board-chip-n num">{counts[group]}</span>
            <span className="board-chip-label">{GROUP_META[group].label}</span>
          </button>
        ))}
      </div>
      {sections
        // With a filter on, only that group shows, and not at all when empty.
        .filter(({ group, items: members }) => filter === null || (group === filter && members.length > 0))
        .map(({ group, items: members }) => (
          <section
            key={group}
            className={`board-group board-tone-${GROUP_META[group].tone}`}
            aria-labelledby={`board-group-${group}`}
          >
            <div className="board-group-hd">
              <h2 id={`board-group-${group}`}><i aria-hidden="true" />{GROUP_META[group].label}</h2>
              <span className="board-group-count num">{members.length}</span>
              <span className="board-group-hint">{GROUP_META[group].hint}</span>
            </div>
            <div className="board-panel panel">
              {members.length === 0 ? (
                <div className="board-empty">Nothing here right now.</div>
              ) : (
                <>
                  <div className="board-cols board-grid" aria-hidden="true">
                    <span>Issue</span>
                    <span>{COLUMN_HEADINGS[group].now}</span>
                    <span>Stage</span>
                    <span>{COLUMN_HEADINGS[group].wait}</span>
                    <span />
                  </div>
                  {members.map((item) => (
                    <BoardRow
                      key={item.issue}
                      repo={repo}
                      item={item}
                      open={open.has(item.issue)}
                      onToggle={() => toggle(item.issue)}
                      onRefresh={onRefresh}
                      onOpenIssue={onOpenIssue}
                    />
                  ))}
                </>
              )}
            </div>
          </section>
        ))}
    </div>
  );
}

function BoardRow({
  repo,
  item,
  open,
  onToggle,
  onRefresh,
  onOpenIssue,
}: {
  repo: string;
  item: BoardItem;
  open: boolean;
  onToggle: () => void;
  onRefresh: () => void;
  onOpenIssue: (issue: number) => void;
}) {
  const { board } = item;
  const nowMs = useNow(30_000);
  const wait = waitCell(board, nowMs);
  const drawerId = `board-drawer-${item.issue}`;
  // The title button is described by what a sighted reader scans across the
  // row: the headline and reason, the rail (its label names every count) and
  // the wait under its column heading.
  const ids = ["say", "rail", "waithead", "wait"].map((part) => `board-row-${item.issue}-${part}`);
  const [sayId, railId, waitHeadId, waitId] = ids;
  const live = board.status === "working" && board.activity === "agent";
  const tone = boardTone(board);
  return (
    <div className={`board-item${open ? " open" : ""}${tone === "warn" ? " board-tone-warn" : ""}`}>
      {/* The whole row toggles for a pointer; the title is the disclosure
          button for keyboards and assistive tech, so the issue and PR links
          beside it are siblings rather than controls nested in a button. */}
      <div className="board-row board-grid" onClick={onToggle}>
        <div className="board-issue">
          <button
            type="button"
            className="board-title-btn"
            aria-expanded={open}
            aria-controls={open ? drawerId : undefined}
            aria-describedby={ids.join(" ")}
          >
            <span className="board-title" title={item.title ?? undefined}>
              {item.title ?? <span className="faint">title unavailable</span>}
            </span>
          </button>
          <span id={waitHeadId} className="sr-only">{COLUMN_HEADINGS[board.status].wait}:</span>
          <div className="board-refs">
            <IssueLink issue={item.issue} url={item.url} />
            {item.pr !== null ? <PrNumber pr={item.pr} url={item.prUrl} /> : null}
          </div>
        </div>
        <div id={sayId} className={`board-say${board.status === "needs_you" ? " board-say-owned" : ""}`}>
          <span className="board-headline">{board.headline}</span>
          <span className="board-reason">{sayLine(board)}</span>
        </div>
        <div className="board-rail">
          <LifecycleStepper
            rail={railNodes(item.rail)}
            tone={tone}
            live={item.rail.live || live}
            label={`Issue #${item.issue} stage`}
            id={railId}
          />
        </div>
        <span
          id={waitId}
          className={`board-wait num${wait.stale ? " board-wait-stale" : ""}${wait.text === "queued" ? " faint" : ""}`}
          {...(wait.title ? tipProps(wait.title) : {})}
        >
          {wait.text}
        </span>
        <span className="board-chev" aria-hidden="true">›</span>
      </div>
      {open ? (
        <BoardDrawer
          id={drawerId}
          repo={repo}
          item={item}
          onRefresh={onRefresh}
          onOpenIssue={onOpenIssue}
        />
      ) : null}
    </div>
  );
}

function BoardDrawer({
  id,
  repo,
  item,
  onRefresh,
  onOpenIssue,
}: {
  id: string;
  repo: string;
  item: BoardItem;
  onRefresh: () => void;
  onOpenIssue: (issue: number) => void;
}) {
  const { board, card } = item;
  const note = board.status === "needs_you" ? plainHarnessMessage(board.detail) : null;
  return (
    <div className="board-drawer" id={id}>
      {item.title && item.title.length > CLAMPED_TITLE_CHARS ? (
        <div className="board-fulltitle">{item.title}</div>
      ) : null}
      {note ? (
        <p className="board-note">
          <span className="board-note-label">Harness:</span> {note}
        </p>
      ) : null}
      <div>
        <div className="board-klabel">
          History · <span className="num">{item.rail.steps}</span> steps
        </div>
        <History visits={card.history ?? []} />
      </div>
      <Facts item={item} card={card} />
      {/* Navigation, not an action: the issue page keeps the step table and
          per-PR review controls the board leaves out. */}
      <button type="button" className="board-linkbtn board-openfull" onClick={() => onOpenIssue(item.issue)}>
        Open full issue
      </button>
      <Action repo={repo} item={item} onRefresh={onRefresh} />
    </div>
  );
}

function History({ visits }: { visits: StageVisit[] }) {
  const [full, setFull] = useState(false);
  const view = historyView(visits, full);
  if (view.lines.length === 0) return <span className="faint">No agent work yet.</span>;
  const expandable = full || view.hidden > 0;
  return (
    <>
      {expandable ? (
        <button type="button" className="board-linkbtn" onClick={() => setFull(!full)}>
          {full ? "Hide earlier steps" : `Show ${view.hidden} earlier ${view.hidden === 1 ? "step" : "steps"}`}
        </button>
      ) : null}
      <ol className="board-visits">
        {view.lines.map((line, index) => (
          <li className="board-visit" key={`${index}-${line.stage}`}>
            <MiniTrack stage={line.stage} />
            <span className="board-vstage">{stageLabel(line.stage)}</span>
            <span className="board-vchips">
              {line.chips.map((chip, chipIndex) => {
                const mark = OUTCOME_MARK[chip.outcome];
                return (
                  <span
                    key={`${chip.kind}-${chipIndex}`}
                    className={`board-step board-step-${chip.outcome}`}
                    {...tipProps(`${kindLabel(chip.kind)}: ${mark.label}`)}
                  >
                    <span className="board-step-glyph" aria-label={mark.label}>{mark.glyph}</span>
                    {kindLabel(chip.kind)}
                    {chip.count > 1 ? <span className="board-step-rep num">×{chip.count}</span> : null}
                  </span>
                );
              })}
              {line.backFrom ? (
                <span className="board-back">↩ back from {stageLabel(line.backFrom)}</span>
              ) : null}
            </span>
          </li>
        ))}
      </ol>
    </>
  );
}

function MiniTrack({ stage }: { stage: StageId }) {
  const at = STAGES.findIndex(({ id }) => id === stage);
  return (
    <span className="board-mini" aria-hidden="true">
      {STAGES.map(({ id }, index) => (
        <b key={id} className={index < at ? "done" : index === at ? "on" : undefined} />
      ))}
    </span>
  );
}

function Facts({ item, card }: { item: BoardItem; card: IssueCard }) {
  const nowMs = useNow(30_000);
  return (
    <dl className="board-facts">
      <div>
        <dt>Cost</dt>
        <dd className="num" {...(card.estimated ? tipProps("Includes estimated or still-reserved spend") : {})}>
          {usd(card.cost_usd)}{card.estimated ? " est." : null}
        </dd>
      </div>
      <div><dt>Legs</dt><dd className="num">{card.legs}</dd></div>
      {card.started_ts ? (
        <div><dt>Started</dt><dd className="num">{ago(card.started_ts, nowMs)}</dd></div>
      ) : null}
      {card.last_ts ? (
        <div><dt>Last activity</dt><dd className="num">{ago(card.last_ts, nowMs)}</dd></div>
      ) : null}
      {item.labels.length > 0 ? <div><dt>Labels</dt><dd className="num">{item.labels.join(", ")}</dd></div> : null}
    </dl>
  );
}

/** The row's single action: open the issue or PR to reply, or Unblock. */
function Action({
  repo,
  item,
  onRefresh,
}: {
  repo: string;
  item: BoardItem;
  onRefresh: () => void;
}) {
  const action = primaryAction(item.board, item.card.action);
  if (action === null) return null;
  return (
    <div className="board-actions">
      <ActionControl repo={repo} item={item} action={action} onChanged={onRefresh} />
    </div>
  );
}

function ActionControl({
  repo,
  item,
  action,
  onChanged,
}: {
  repo: string;
  item: BoardItem;
  action: PrimaryAction;
  onChanged: () => void;
}) {
  switch (action.kind) {
    case "open_issue":
      return item.url ? (
        <a className="btn btn-primary" href={item.url} target="_blank" rel="noreferrer">{action.label}</a>
      ) : null;
    case "open_pr":
      return item.prUrl ? (
        <a className="btn btn-primary" href={item.prUrl} target="_blank" rel="noreferrer">{action.label}</a>
      ) : null;
    case "unblock":
      return <UnblockControl deployment={repo} issue={item.issue} heldForHuman onChanged={onChanged} />;
  }
}

function IssueLink({ issue, url }: { issue: number; url: string | null }) {
  if (!url) return <span className="num">#{issue}</span>;
  return (
    <a
      className="num"
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

function PrNumber({ pr, url }: { pr: number; url: string | null }) {
  const badge = (
    <Chip tone="meta" square className="board-pr">
      <Icon name="git-pull-request" size={12} />
      <span className="sr-only">PR </span>#{pr}
    </Chip>
  );
  if (!url) return badge;
  return (
    <a
      className="board-pr-link"
      href={url}
      target="_blank"
      rel="noreferrer"
      title="open pull request on GitHub"
      onClick={(event) => event.stopPropagation()}
    >
      {badge}
    </a>
  );
}
