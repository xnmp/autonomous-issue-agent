import { useState } from "react";
import { api } from "../api/client";
import { usePolled, useLiveElapsed } from "../api/hooks";
import type { IssueStep } from "../api/types";
import { kindLabel, prStateTone, stateTone, stageTone, statusTone } from "../lib/semantics";
import { duration, durationShort, localTime, num, tokens, usd } from "../lib/format";
import { shortModel } from "../lib/models";
import { Chip } from "./ui/Chip";
import { Collapsible, JsonBlock } from "./ui/Json";
import { EmptyState, ErrorBanner, Loading, SectionTitle, SkeletonRows, WarningList } from "./ui/Feedback";
import { EventTimeline } from "./EventTimeline";

const POLL_MS = 5_000;

/**
 * One issue, run by run. A run is one agent invocation.
 */
export function IssueTab({ repo, issue }: { repo: string; issue: number }) {
  const detail = usePolled(
    `issue:${repo}:${issue}`,
    (signal) => api.issue(repo, issue, signal),
    POLL_MS,
  );
  const [openStep, setOpenStep] = useState<number | null>(null);

  const d = detail.data;
  const elapsed = useLiveElapsed(d?.done ? d.elapsed_s : (d?.elapsed_s ?? null), detail.updatedAt);

  return (
    <div className="tabbody">
      {detail.error ? (
        <ErrorBanner message={detail.error} onRetry={detail.refresh} stale={!!d} />
      ) : null}
      <WarningList warnings={d?.warnings} />
      {detail.loading ? <SkeletonRows what={`issue #${issue}`} rows={6} /> : null}

      {d ? (
        <>
          <header className="issue-hd panel">
            <div className="issue-hd-line">
              <span className="issue-num mono">#{d.issue}</span>
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
            <div className="issue-hd-line">
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
                      PR #{pr.number} ↗
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
                <span className="faint">{d.done ? "duration" : "elapsed"}</span>{" "}
                <span className="mono">{duration(d.done ? d.elapsed_s : elapsed)}</span>
              </span>
              <span className="issue-stat">
                <span className="faint">runs</span>{" "}
                <span className="mono">{d.steps.length}</span>
              </span>
            </div>
          </header>

          <SectionTitle>Runs</SectionTitle>

          {d.steps.length === 0 ? (
            <EmptyState>no runs recorded for this issue yet</EmptyState>
          ) : (
            <div className="panel steps">
              <div className="tbl-wrap">
                <table className="tbl wide">
                  <thead>
                    <tr>
                      <th style={{ width: 34 }} />
                      <th style={{ width: 90 }}>Operation</th>
                      <th style={{ width: 80 }}>Role</th>
                      <th style={{ width: 140 }}>Model</th>
                      <th style={{ width: 190 }}>Status</th>
                      <th className="r" style={{ width: 80 }}>Duration</th>
                      <th className="r" style={{ width: 80 }}>Cost</th>
                      <th>Tokens</th>
                      <th style={{ width: 130 }}>When</th>
                    </tr>
                  </thead>
                  <tbody>
                    {d.steps.map((s) => (
                      <StepRow
                        key={s.idx}
                        repo={repo}
                        issue={issue}
                        step={s}
                        open={openStep === s.idx}
                        onToggle={() => setOpenStep((cur) => (cur === s.idx ? null : s.idx))}
                      />
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          <div className="issue-events">
            <Collapsible
              title={<strong>All events for this issue</strong>}
              right={<span className="faint num">{d.events.length}</span>}
            >
              <EventTimeline events={d.events} emptyText="no journal events for this issue" />
            </Collapsible>
          </div>
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
}: {
  repo: string;
  issue: number;
  step: IssueStep;
  open: boolean;
  onToggle: () => void;
}) {
  const tone = stateTone(step.state);
  const u = step.usage;
  return (
    <>
      <tr className={`clickable${open ? " expanded" : ""}`} onClick={onToggle} tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            onToggle();
          }
        }}
      >
        <td className="faint">
          <span className={`chev${open ? " open" : ""}`} aria-hidden="true">
            ▸
          </span>
        </td>
        <td>
          <Chip tone={tone} square pulse={step.state === "running"} title={step.kind}>
            {kindLabel(step.kind)}
          </Chip>
        </td>
        <td>
          <Chip tone={step.role === "steward" ? "meta" : "info"} square>
            {step.role ?? "—"}
          </Chip>
        </td>
        <td>
          {step.model ? (
            <Chip tone="link" square title={step.model}>
              {shortModel(step.model)}
            </Chip>
          ) : (
            <span className="faint">—</span>
          )}
        </td>
        <td className="row-wrap">
          <Chip tone={step.state === "running" ? "running" : statusTone(step.status)}>
            {step.status ?? step.state}
          </Chip>
          {step.exit_reason ? (
            <span className="faint mono trunc" title={step.exit_reason}>
              {step.exit_reason}
            </span>
          ) : null}
        </td>
        <td className="r mono">{durationShort(step.duration_s)}</td>
        <td className="r mono">
          {usd(step.cost_usd)}
          {step.estimated ? <span className="faint" title="synthetic dollars"> ~</span> : null}
        </td>
        <td className="mono faint nowrap">
          {u ? (
            <span title={JSON.stringify(u, null, 2)}>
              in {tokens(u.input_tokens)} · out {tokens(u.output_tokens)} · cache r{" "}
              {tokens(u.cache_read_input_tokens)} / w {tokens(u.cache_creation_input_tokens)}
            </span>
          ) : (
            "—"
          )}
          {step.turns !== null ? <span className="faint"> · {num(step.turns)} turns</span> : null}
        </td>
        <td className="mono faint" title={step.ts ?? ""}>
          {localTime(step.ts)}
        </td>
      </tr>
      {open ? (
        <tr className="detail-row">
          <td colSpan={9}>
            <StepDetail repo={repo} issue={issue} step={step} />
          </td>
        </tr>
      ) : null}
    </>
  );
}

type DetailPane = "events" | "cost" | "raw";

function StepDetail({ repo, issue, step }: { repo: string; issue: number; step: IssueStep }) {
  const [pane, setPane] = useState<DetailPane>("events");
  const events = usePolled(
    pane === "events" ? `stepev:${repo}:${issue}:${step.idx}` : null,
    (signal) => api.stepEvents(repo, issue, step.idx, signal),
    step.state === "running" ? POLL_MS : 0,
  );

  const chartable = !!step.session_id && step.has_transcript;

  return (
    <div className="stepdetail">
      <div className="subtabs subtabs-sm" role="tablist">
        {(["events", "cost", "raw"] as DetailPane[]).map((p) => (
          <button
            key={p}
            role="tab"
            aria-selected={p === pane}
            className={`subtab${p === pane ? " active" : ""}`}
            onClick={() => setPane(p)}
          >
            {p === "events" ? "Events" : p === "cost" ? "Cost chart" : "Raw"}
          </button>
        ))}
      </div>

      {pane === "events" ? (
        <>
          {events.error ? (
            <ErrorBanner message={events.error} onRetry={events.refresh} />
          ) : null}
          <WarningList warnings={events.data?.warnings} />
          {events.loading ? (
            <Loading what="step events" />
          ) : (
            <EventTimeline
              dense
              events={events.data?.events ?? []}
              emptyText="no journal events inside this run's window"
            />
          )}
        </>
      ) : null}

      {pane === "cost" ? (
        chartable ? (
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
              : "this run recorded no session id, so there is no transcript to chart"}
          </EmptyState>
        )
      ) : null}

      {pane === "raw" ? <JsonBlock value={step} /> : null}
    </div>
  );
}
