import { useState } from "react";
import { api } from "../api/client";
import { useClock, usePolled } from "../api/hooks";
import type {
  BacklogCard,
  IssueCard,
  IssuesPayload,
  PendingCard,
  Priority,
} from "../api/types";
import type { Polled } from "../api/hooks";
import { ISSUES_SUBS, ISSUES_SUB_LABELS, type IssuesSub } from "../routing/hash";
import { familyVar, flagTone, prStateTone, stageTone } from "../lib/semantics";
import { ago, duration, durationShort, localTime, num, usd } from "../lib/format";
import { Chip } from "./ui/Chip";
import { EtaProgress } from "./ui/Charts";
import { StepBadgeRow } from "./ui/StepBadge";
import { ConfirmModal } from "./ui/Modal";
import {
  EmptyState,
  ErrorBanner,
  GhUnavailableChip,
  Loading,
  SectionTitle,
  WarningList,
} from "./ui/Feedback";
import { SubTabs } from "./TabStrip";

const POLL_MS = 5_000;

/**
 * Hoisted to App: the Issues tab renders it, and the tab strip needs the same
 * data for live stage badges on open issue tabs. One poller, two consumers —
 * the contract's "a single poller per mounted view".
 */
export function useIssues(repo: string | null): Polled<IssuesPayload> {
  return usePolled(
    repo === null ? null : `issues:${repo}`,
    (signal) => api.issues(repo as string, signal),
    POLL_MS,
  );
}

interface IssuesTabProps {
  repo: string;
  sub: IssuesSub;
  issues: Polled<IssuesPayload>;
  onSetSub: (s: IssuesSub) => void;
  onOpenIssue: (n: number) => void;
}

export function IssuesTab({ repo, sub, issues, onSetSub, onOpenIssue }: IssuesTabProps) {
  const d = issues.data;
  const ghOk = d?.gh_ok ?? true;

  return (
    <div className="tabbody">
      <div className="row">
        <SubTabs
          value={sub}
          options={ISSUES_SUBS}
          labels={ISSUES_SUB_LABELS}
          counts={{
            inflight: (d?.in_flight.length ?? 0) + (d?.pending.length ?? 0),
            done: d?.done.length ?? 0,
            backlog: d?.backlog.length ?? 0,
          }}
          onChange={onSetSub}
        />
        <span className="spacer" />
        {!ghOk ? <GhUnavailableChip /> : null}
      </div>

      {issues.error ? (
        <ErrorBanner message={issues.error} onRetry={issues.refresh} stale={!!d} />
      ) : null}
      <WarningList warnings={d?.warnings} />
      {issues.loading ? <Loading what="issues" /> : null}

      {sub === "inflight" ? (
        <>
          <SectionTitle>In flight</SectionTitle>
          <InFlightTable
            cards={d?.in_flight ?? []}
            anchorMs={issues.updatedAt}
            loading={issues.loading}
            onOpen={onOpenIssue}
          />
          <SectionTitle>Pending — labelled ai-ready, not yet picked up</SectionTitle>
          <PendingTable
            repo={repo}
            cards={d?.pending ?? []}
            loading={issues.loading}
            ghOk={ghOk}
            onRefresh={issues.refresh}
          />
        </>
      ) : null}

      {sub === "done" ? (
        <DoneTable cards={d?.done ?? []} loading={issues.loading} onOpen={onOpenIssue} />
      ) : null}

      {sub === "backlog" ? (
        <BacklogTable
          repo={repo}
          cards={d?.backlog ?? []}
          loading={issues.loading}
          ghOk={ghOk}
          onRefresh={issues.refresh}
        />
      ) : null}
    </div>
  );
}

/* --------------------------------------------------------------- in flight */

function InFlightTable({
  cards,
  anchorMs,
  loading,
  onOpen,
}: {
  cards: IssueCard[];
  anchorMs: number | null;
  loading: boolean;
  onOpen: (n: number) => void;
}) {
  const now = useClock();
  if (loading) return <Loading what="in-flight issues" />;
  if (cards.length === 0)
    return <EmptyState>no issues in flight — the fixer is idle</EmptyState>;

  // Elapsed ticks locally from the server value + the age of that value, so the
  // clock stays honest between 5 s polls without ever jumping backwards.
  const offset = anchorMs === null ? 0 : Math.max(0, (now - anchorMs) / 1000);

  return (
    <div className="tbl-wrap panel">
      <table className="tbl wide">
        <thead>
          <tr>
            <th style={{ width: 62 }}>Issue</th>
            <th>Title</th>
            <th style={{ width: 70 }}>PR</th>
            <th style={{ width: 110 }}>Stage</th>
            <th>Steps</th>
            <th className="r" style={{ width: 110 }}>Cost</th>
            <th className="r" style={{ width: 96 }}>Elapsed</th>
            <th style={{ width: 130 }}>Progress</th>
            <th style={{ width: 140 }}>Model</th>
            <th>Flags</th>
          </tr>
        </thead>
        <tbody>
          {cards.map((c) => {
            const elapsed = c.elapsed_s === null ? null : c.elapsed_s + offset;
            const alert = c.flags.some((f) => f === "escalated" || f === "spend-blocked");
            return (
              <tr
                key={c.issue}
                className={`clickable${alert ? " row-alert" : ""}`}
                tabIndex={0}
                onClick={() => onOpen(c.issue)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    onOpen(c.issue);
                  }
                }}
              >
                <td><IssueLink card={c} /></td>
                <td className="title-cell trunc" title={c.title ?? undefined}>
                  {c.title ?? <span className="faint">title unavailable</span>}
                </td>
                <td><PrLink card={c} /></td>
                <td>
                  {c.stage ? (
                    <Chip tone={stageTone(c.stage)} pulse={stageTone(c.stage) === "running"}>
                      {c.stage}
                    </Chip>
                  ) : (
                    <span className="faint">—</span>
                  )}
                </td>
                <td><StepBadgeRow steps={c.steps} /></td>
                <td className="r mono nowrap">
                  {usd(c.cost_usd)}
                  {c.estimated ? <span className="faint" title="synthetic dollars (subscription runtime)"> ~</span> : null}
                  {c.projected_cost_usd !== null ? (
                    <span className="faint" title="repo mean $/done issue">
                      {" "}/ {usd(c.projected_cost_usd)}
                    </span>
                  ) : null}
                </td>
                <td className="r mono" title={c.started_ts ? `started ${localTime(c.started_ts)}` : ""}>
                  {duration(elapsed)}
                </td>
                <td>
                  {elapsed !== null && c.projected_eta_s !== null ? (
                    <EtaProgress
                      elapsedS={elapsed}
                      etaS={c.projected_eta_s}
                      segments={c.steps.map((s, i) => ({
                        key: `${s.kind}-${i}`,
                        varName: familyVar(s.kind),
                        weight: Math.max(s.cost_usd ?? 0, 0.01),
                      }))}
                    />
                  ) : (
                    <span className="faint" title="no completed issue in this repo yet — no projection to compare against">
                      no projection
                    </span>
                  )}
                </td>
                <td className="row-wrap">
                  {c.models.length === 0 ? (
                    <span className="faint">—</span>
                  ) : (
                    c.models.map((m) => (
                      <Chip key={m} tone="link" square title={m}>
                        {shortModel(m)}
                      </Chip>
                    ))
                  )}
                </td>
                <td className="row-wrap">
                  {c.flags.length === 0 ? (
                    <span className="faint">—</span>
                  ) : (
                    c.flags.map((f) => (
                      <Chip key={f} tone={flagTone(f)}>
                        {f}
                      </Chip>
                    ))
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/* ----------------------------------------------------------------- pending */

function PendingTable({
  repo,
  cards,
  loading,
  ghOk,
  onRefresh,
}: {
  repo: string;
  cards: PendingCard[];
  loading: boolean;
  ghOk: boolean;
  onRefresh: () => void;
}) {
  const [busyIssue, setBusyIssue] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  if (loading) return <Loading what="pending issues" />;
  if (cards.length === 0)
    return (
      <EmptyState>
        {ghOk ? "nothing labelled ai-ready is waiting" : "pending needs GitHub data"}
      </EmptyState>
    );

  const setPriority = async (n: number, p: Priority) => {
    setBusyIssue(n);
    setError(null);
    try {
      await api.setPriority(repo, n, p);
      onRefresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyIssue(null);
    }
  };

  return (
    <>
      {error ? <ErrorBanner message={error} /> : null}
      <div className="tbl-wrap panel">
        <table className="tbl">
          <thead>
            <tr>
              <th style={{ width: 62 }}>Issue</th>
              <th>Title</th>
              <th style={{ width: 180 }}>Priority</th>
              <th style={{ width: 90 }}>Overlap</th>
              <th style={{ width: 150 }}>Hold</th>
              <th>Labels</th>
            </tr>
          </thead>
          <tbody>
            {cards.map((c) => (
              <tr key={c.issue} className={c.held ? "row-alert" : undefined}>
                <td>
                  {c.url ? (
                    <a className="mono" href={c.url} target="_blank" rel="noreferrer">
                      #{c.issue}
                    </a>
                  ) : (
                    <span className="mono">#{c.issue}</span>
                  )}
                </td>
                <td className="title-cell trunc" title={c.title ?? undefined}>
                  {c.title ?? <span className="faint">title unavailable</span>}
                </td>
                <td>
                  <PrioritySelector
                    value={c.priority}
                    busy={busyIssue === c.issue}
                    onChange={(p) => void setPriority(c.issue, p)}
                  />
                </td>
                <td>
                  <span
                    className="faint"
                    title="steward overlap prediction not built yet"
                  >
                    n/a
                  </span>
                </td>
                <td>
                  {c.held ? (
                    <Chip tone="fail" title={c.hold_reason ?? "held"}>
                      held{c.hold_reason ? `: ${c.hold_reason}` : ""}
                    </Chip>
                  ) : (
                    <span className="faint">—</span>
                  )}
                </td>
                <td className="row-wrap">
                  {c.labels.length === 0 ? (
                    <span className="faint">—</span>
                  ) : (
                    c.labels.map((l) => (
                      <Chip key={l} tone="neutral">
                        {l}
                      </Chip>
                    ))
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
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
  onChange: (p: Priority) => void;
}) {
  return (
    <div className={`seg${busy ? " busy" : ""}`} role="group" aria-label="priority">
      {PRIORITIES.map((p) => (
        <button
          key={p.label}
          className={`seg-btn${p.value === value ? " active" : ""}`}
          disabled={busy}
          aria-pressed={p.value === value}
          onClick={() => {
            if (p.value !== value) onChange(p.value);
          }}
        >
          {p.label}
        </button>
      ))}
    </div>
  );
}

/* -------------------------------------------------------------------- done */

function DoneTable({
  cards,
  loading,
  onOpen,
}: {
  cards: IssueCard[];
  loading: boolean;
  onOpen: (n: number) => void;
}) {
  if (loading) return <Loading what="done issues" />;
  if (cards.length === 0)
    return <EmptyState>nothing merged by the harness in this deployment yet</EmptyState>;
  return (
    <div className="tbl-wrap panel">
      <table className="tbl">
        <thead>
          <tr>
            <th style={{ width: 62 }}>Issue</th>
            <th>Title</th>
            <th style={{ width: 80 }}>PR</th>
            <th className="r" style={{ width: 90 }}>Cost</th>
            <th className="r" style={{ width: 90 }}>Duration</th>
            <th style={{ width: 150 }}>Model</th>
            <th className="r" style={{ width: 70 }}>Rounds</th>
            <th style={{ width: 140 }}>Merged</th>
          </tr>
        </thead>
        <tbody>
          {cards.map((c) => (
            <tr
              key={c.issue}
              className="clickable"
              tabIndex={0}
              onClick={() => onOpen(c.issue)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  onOpen(c.issue);
                }
              }}
            >
              <td><IssueLink card={c} /></td>
              <td className="title-cell trunc" title={c.title ?? undefined}>
                {c.title ?? <span className="faint">title unavailable</span>}
              </td>
              <td><PrLink card={c} /></td>
              <td className="r mono">
                {usd(c.cost_usd)}
                {c.estimated ? <span className="faint" title="synthetic dollars"> ~</span> : null}
              </td>
              <td className="r mono">{durationShort(c.elapsed_s)}</td>
              <td className="row-wrap">
                {c.models.length === 0 ? (
                  <span className="faint">—</span>
                ) : (
                  c.models.map((m) => (
                    <Chip key={m} tone="link" square title={m}>
                      {shortModel(m)}
                    </Chip>
                  ))
                )}
              </td>
              <td className="r num">{num(c.rounds)}</td>
              <td className="mono faint" title={c.merged_ts ?? c.last_ts ?? ""}>
                {c.merged_ts ? ago(c.merged_ts) : ago(c.last_ts)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* ----------------------------------------------------------------- backlog */

function BacklogTable({
  repo,
  cards,
  loading,
  ghOk,
  onRefresh,
}: {
  repo: string;
  cards: BacklogCard[];
  loading: boolean;
  ghOk: boolean;
  onRefresh: () => void;
}) {
  const [promote, setPromote] = useState<BacklogCard | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (loading) return <Loading what="backlog" />;
  if (cards.length === 0)
    return (
      <EmptyState>
        {ghOk
          ? "no unlabelled open issues — the backlog is clear"
          : "backlog needs GitHub data, which is unavailable"}
      </EmptyState>
    );

  const doPromote = async () => {
    if (!promote) return;
    setBusy(true);
    setError(null);
    try {
      await api.promote(repo, promote.issue);
      setPromote(null);
      onRefresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="tbl-wrap panel">
        <table className="tbl">
          <thead>
            <tr>
              <th style={{ width: 62 }}>Issue</th>
              <th>Title</th>
              <th style={{ width: 110 }}>Recurrence</th>
              <th className="r" style={{ width: 70 }}>Age</th>
              <th className="r" style={{ width: 90 }}>Comments</th>
              <th>Labels</th>
              <th className="sticky-r" style={{ width: 90 }} />
            </tr>
          </thead>
          <tbody>
            {cards.map((c) => (
              <tr key={c.issue} className={c.recurrence ? "row-alert" : undefined}>
                <td>
                  {c.url ? (
                    <a className="mono" href={c.url} target="_blank" rel="noreferrer">
                      #{c.issue}
                    </a>
                  ) : (
                    <span className="mono">#{c.issue}</span>
                  )}
                </td>
                <td className="title-cell trunc" title={c.title ?? undefined}>
                  {c.title ?? <span className="faint">title unavailable</span>}
                </td>
                <td>
                  {c.recurrence ? (
                    <Chip tone="fail" title="labelled `recurrence` — a fixed thing came back">
                      recurrence
                    </Chip>
                  ) : (
                    <span className="faint">—</span>
                  )}
                </td>
                <td className="r num">{c.age_days !== null ? `${c.age_days}d` : "—"}</td>
                <td className="r num">{num(c.comments)}</td>
                <td className="row-wrap">
                  {c.labels.length === 0 ? (
                    <span className="faint">—</span>
                  ) : (
                    c.labels.map((l) => (
                      <Chip key={l} tone="neutral">
                        {l}
                      </Chip>
                    ))
                  )}
                </td>
                <td className="sticky-r">
                  <button
                    className="btn btn-xs"
                    onClick={() => {
                      setError(null);
                      setPromote(c);
                    }}
                  >
                    Promote
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
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
              <p>
                Add the <span className="mono">ai-ready</span> label to{" "}
                <strong className="mono">#{promote.issue}</strong>
                {promote.title ? ` — ${promote.title}` : ""}?
              </p>
              <p className="muted">
                The fixer picks up ai-ready issues on its next tick and will start spending.
              </p>
            </>
          }
        />
      ) : null}
    </>
  );
}

/* ------------------------------------------------------------------ bits */

function IssueLink({ card }: { card: IssueCard }) {
  const label = <span className="mono">#{card.issue}</span>;
  if (!card.url) return label;
  return (
    <a
      href={card.url}
      target="_blank"
      rel="noreferrer"
      className="mono"
      onClick={(e) => e.stopPropagation()}
      title="open on GitHub"
    >
      #{card.issue}
    </a>
  );
}

function PrLink({ card }: { card: IssueCard }) {
  if (card.pr === null) return <span className="faint">—</span>;
  const inner = (
    <Chip tone={prStateTone(card.pr_state)} square title={card.pr_state ?? "pull request"}>
      #{card.pr}
    </Chip>
  );
  if (!card.pr_url) return inner;
  return (
    <a href={card.pr_url} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()}>
      {inner}
    </a>
  );
}

/** `claude-sonnet-5` → `sonnet-5`; keeps the chip narrow, full id in the title. */
export function shortModel(m: string): string {
  return m.replace(/^(claude|openai|anthropic)[-/]/, "").replace(/\[1m\]$/, "");
}
