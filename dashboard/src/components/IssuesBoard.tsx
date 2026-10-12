import { useMemo, useState } from "react";
import { api } from "../api/client";
import { usePolled } from "../api/hooks";
import type { IssueCard } from "../api/types";
import {
  COLUMN_HEADINGS,
  GROUP_META,
  GROUP_ORDER,
  OUTCOME_MARK,
  STAGES,
  TIERS,
  groupCounts,
  groupItems,
  historyView,
  operatorOwned,
  overspendOf,
  plainHarnessMessage,
  primaryAction,
  railNodes,
  stageLabel,
  waitCell,
  type BoardGroup,
  type BoardItem,
  type PrimaryAction,
  type StageId,
  type StageVisit,
  type Tier,
} from "../features/issues/board";
import { ago, duration, usd } from "../lib/format";
import { liveClocks, useNow, useSnapshotOffset } from "../lib/clock";
import { kindLabel } from "../lib/semantics";
import { Chip } from "./ui/Chip";
import { Icon } from "./ui/Icon";
import { useLegacyUnblockRefusal } from "./legacy-unblock";
import { ResolveControls } from "./resolve/ResolveControls";
import { LifecycleStepper } from "./ui/LifecycleStepper";
import { tipProps } from "./ui/Tip";
import { ErrorBanner, SkeletonRows } from "./ui/Feedback";
import "./issues-board.css";

/** The reader's display options, from the options menu. */
interface Display {
  showProjectedCost: boolean;
  showDurationSeconds: boolean;
}

/** A title this long is clamped on the row, so the drawer repeats it whole. */
const CLAMPED_TITLE_CHARS = 70;

export function IssuesBoard({
  repo,
  items,
  loading,
  onRefresh,
  onOpenIssue,
  showProjectedCost = true,
  showDurationSeconds = true,
}: {
  repo: string;
  items: BoardItem[];
  loading: boolean;
  onRefresh: () => void;
  onOpenIssue: (issue: number) => void;
  showProjectedCost?: boolean;
  showDurationSeconds?: boolean;
}) {
  const display = { showProjectedCost, showDurationSeconds };
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
                      display={display}
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
  display,
}: {
  repo: string;
  item: BoardItem;
  open: boolean;
  onToggle: () => void;
  onRefresh: () => void;
  onOpenIssue: (issue: number) => void;
  display: Display;
}) {
  const { board } = item;
  const nowMs = useNow(30_000);
  const wait = waitCell(board, nowMs);
  const overspend = overspendOf(item.card);
  const drawerId = `board-drawer-${item.issue}`;
  // The title button is described by what a sighted reader scans across the
  // row: the headline and reason, the rail (its label names every count) and
  // the wait under its column heading.
  const ids = ["say", "rail", "waithead", "wait"].map((part) => `board-row-${item.issue}-${part}`);
  const [sayId, railId, waitHeadId, waitId] = ids;
  const live = board.status === "working" && board.activity === "agent";
  return (
    <div className={`board-item${open ? " open" : ""}`}>
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
            {overspend ? (
              <span
                className="board-overspend"
                {...tipProps(`${usd(overspend.cost)} spent of a ${usd(overspend.budget)} projection`)}
              >
                {overspend.ratio.toFixed(1)}× budget
              </span>
            ) : null}
          </div>
        </div>
        <div id={sayId} className={`board-say${operatorOwned(board) ? " board-say-owned" : ""}`}>
          <span className="board-headline">{board.headline}</span>
          <span className="board-reason">{board.reason}</span>
        </div>
        <div className="board-rail">
          {item.rail ? (
            <LifecycleStepper
              rail={railNodes(item.rail)}
              tone={GROUP_META[board.status].tone}
              live={item.rail.live || live}
              label={`Issue #${item.issue} stage`}
              id={railId}
            />
          ) : <span className="faint">—</span>}
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
          display={display}
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
  display,
}: {
  id: string;
  repo: string;
  item: BoardItem;
  onRefresh: () => void;
  onOpenIssue: (issue: number) => void;
  display: Display;
}) {
  const { board, card } = item;
  const hasHistory = (item.rail?.steps ?? 0) > 0;
  const detail = usePolled(
    hasHistory ? `issue:${repo}:${item.issue}` : null,
    (signal) => api.issue(repo, item.issue, signal),
    5_000,
  );
  const note = plainHarnessMessage(detail.data?.harness_message)
    ?? (operatorOwned(board) ? board.reason : null);
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
      {/* #329: each counted infra death (time, leg, cause) is listed here,
          under the harness message, once that projection exists. */}
      <div>
        <div className="board-klabel">
          History{item.rail ? <> · <span className="num">{item.rail.steps}</span> steps</> : null}
        </div>
        {!hasHistory ? (
          <span className="faint">No agent work yet.</span>
        ) : detail.data ? (
          <History visits={detail.data.history ?? []} />
        ) : detail.error ? (
          <ErrorBanner message={detail.error} onRetry={detail.refresh} />
        ) : (
          <span className="faint">loading history…</span>
        )}
      </div>
      <Facts item={item} card={card} display={display} />
      {/* Navigation, not an action: the issue page keeps the step table and
          per-PR review controls the board leaves out. */}
      <button type="button" className="board-linkbtn board-openfull" onClick={() => onOpenIssue(item.issue)}>
        Open full issue
      </button>
      <Action repo={repo} item={item} onRefresh={onRefresh} onDetailRefresh={detail.refresh} />
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

function Facts({
  item,
  card,
  display,
}: {
  item: BoardItem;
  card: IssueCard | null;
  display: Display;
}) {
  const offset = useSnapshotOffset();
  const nowMs = useNow(30_000);
  const clocks = card ? liveClocks(card, offset) : null;
  const models = card ? (card.model_badges?.map(({ model }) => model) ?? card.models) : [];
  return (
    <dl className="board-facts">
      <div><dt>Difficulty</dt><dd><TierBadge tier={item.tier} /></dd></div>
      {card && card.cost_usd !== null ? (
        <div>
          <dt>Cost</dt>
          <dd className="num">
            {usd(card.cost_usd)}
            {display.showProjectedCost && card.projected_cost_usd !== null ? ` of ${usd(card.projected_cost_usd)}` : null}
          </dd>
        </div>
      ) : null}
      {clocks && clocks.working !== null ? (
        <div><dt>Agent time</dt><dd className="num">{duration(clocks.working, display.showDurationSeconds)}</dd></div>
      ) : null}
      {clocks && clocks.idle !== null ? (
        <div><dt>Time idle</dt><dd className="num">{duration(clocks.idle, display.showDurationSeconds)}</dd></div>
      ) : null}
      {card?.last_ts ? (
        <div><dt>Last activity</dt><dd className="num">{ago(card.last_ts, nowMs)}</dd></div>
      ) : null}
      {models.length > 0 ? <div><dt>Models</dt><dd className="num">{models.join(", ")}</dd></div> : null}
      {item.labels.length > 0 ? <div><dt>Labels</dt><dd className="num">{item.labels.join(", ")}</dd></div> : null}
    </dl>
  );
}

function TierBadge({ tier }: { tier: Tier | null }) {
  const rung = tier === null ? -1 : TIERS.indexOf(tier);
  if (rung < 0) {
    return <span className="board-tier board-tier-none" {...tipProps("Not yet triaged")}>untriaged</span>;
  }
  return (
    <span className="board-tier" {...tipProps(`Difficulty tier ${rung + 1} of ${TIERS.length}`)}>
      <span className="board-tier-meter" aria-hidden="true">
        {TIERS.map((name, index) => <b key={name} className={index <= rung ? "on" : undefined} />)}
      </span>
      {tier}
    </span>
  );
}

/**
 * The row's single action. Re-arm and budget approval open the issue's own
 * resolve controls, which own the guarded prepare/confirm flow and the
 * legacy-unblock gate; the board never issues those writes itself.
 */
function Action({
  repo,
  item,
  onRefresh,
  onDetailRefresh,
}: {
  repo: string;
  item: BoardItem;
  onRefresh: () => void;
  onDetailRefresh: () => void;
}) {
  const action = primaryAction(item.board);
  if (action === null) return null;
  return (
    <div className="board-actions">
      <ActionControl
        repo={repo}
        item={item}
        action={action}
        onChanged={() => {
          onDetailRefresh();
          onRefresh();
        }}
      />
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
  const [revealed, setRevealed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = async (write: () => Promise<unknown>): Promise<boolean> => {
    setBusy(true);
    setError(null);
    try {
      await write();
      onChanged();
      return true;
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
      return false;
    } finally {
      setBusy(false);
    }
  };
  switch (action.kind) {
    case "open_issue":
      return item.url ? (
        <a className="btn btn-primary" href={item.url} target="_blank" rel="noreferrer">{action.label}</a>
      ) : null;
    case "open_pr":
      return item.prUrl ? (
        <a className="btn btn-primary" href={item.prUrl} target="_blank" rel="noreferrer">{action.label}</a>
      ) : null;
    case "unpark":
      return (
        <>
          <button
            type="button"
            className="btn btn-primary"
            disabled={busy}
            {...tipProps("Restore the PR to the harness: review, merge and overlap serialization resume")}
            onClick={() => void run(() => api.unpark(repo, item.issue))}
          >
            {action.label}
          </button>
          {error ? <ErrorBanner message={error} /> : null}
        </>
      );
    case "rearm":
    case "budget":
      return revealed ? (
        <HeldControls repo={repo} issue={item.issue} onChanged={onChanged} />
      ) : (
        <button type="button" className="btn btn-primary" onClick={() => setRevealed(true)}>
          {action.label}
        </button>
      );
  }
}

/** The issue page's resolve controls, for a row the harness holds for a person. */
function HeldControls({
  repo,
  issue,
  onChanged,
}: {
  repo: string;
  issue: number;
  onChanged: () => void;
}) {
  const legacyRefusal = useLegacyUnblockRefusal(repo, true);
  return (
    <div className="board-held-controls">
      <ResolveControls
        deployment={repo}
        issue={issue}
        heldForHuman
        legacyRefusal={legacyRefusal}
        onChanged={onChanged}
      />
    </div>
  );
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
