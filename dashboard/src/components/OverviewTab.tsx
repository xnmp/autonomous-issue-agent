import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import { usePolled } from "../api/hooks";
import type {
  EscalationReason,
  HealthTrendDay,
  InterventionEvent,
  OverviewPayload,
  RoleHealth,
} from "../api/types";
import {
  HEALTH_LABEL,
  HEALTH_TONE,
  roleHealth,
  utilTone,
} from "../lib/semantics";
import { localHour, localTime, num, rate, usd, usdCompact } from "../lib/format";
import { Chip } from "./ui/Chip";
import { Histogram, Meter, StatTile } from "./ui/Charts";
import { EmptyState, ErrorBanner, Loading, SkeletonRows, WarningList } from "./ui/Feedback";
import { Icon } from "./ui/Icon";
import { ResizableTable, useSortedRows } from "./ui/ResizableTable";
import { TableFilter } from "./ui/TableFilter";
import { tipProps } from "./ui/Tip";

const POLL_MS = 10_000;

/**
 * The "is this on rails?" view. Live human blockers and operator actions lead;
 * measurements and historical diagnostics follow in progressively quieter
 * groups so urgent work never competes with lifetime counters.
 */
export function OverviewTab({
  repo,
  onOpenIssues,
  showDaemonHealth = false,
  showIssueHealthTrend = false,
}: {
  repo: string;
  onOpenIssues: (sub: "inflight" | "done") => void;
  showDaemonHealth?: boolean;
  showIssueHealthTrend?: boolean;
}) {
  const ov = usePolled(
    `overview:${repo}`,
    (signal) => api.overview(repo, signal),
    POLL_MS,
  );

  const m = ov.data?.metrics ?? null;
  const caps = ov.data?.caps ?? null;

  const escalations = m?.escalations ?? 0;
  const recurrences = m?.recurrences ?? 0;
  const spendBlocks = m?.spend_blocks ?? 0;
  const byClass = new Map(
    (m?.interventions_by_class ?? []).map((row) => [row.name, row.count]),
  );

  const todayUsd = caps?.today_usd ?? m?.today_usd ?? null;
  const dailyPct =
    todayUsd != null && caps?.daily_cap_usd != null && caps.daily_cap_usd > 0
      ? (todayUsd / caps.daily_cap_usd) * 100
      : null;

  return (
    <div className="tabbody">
      {/* The visible <h1> was removed by design; screen-reader heading
          navigation still needs the page identified. */}
      <h1 className="sr-only">Overview</h1>
      {ov.error ? (
        <ErrorBanner message={ov.error} onRetry={ov.refresh} stale={!!ov.data} />
      ) : null}
      <WarningList warnings={ov.data?.warnings} />
      {ov.loading ? <SkeletonRows what="overview" rows={5} /> : null}

      <div className="tiles overview-metrics overview-metrics-primary">
        <StatTile
          label="Done"
          value={num(m?.done_issues)}
          sub={ov.data?.first_observed_day ? `since ${ov.data.first_observed_day}` : "deployment start unknown"}
          tone="ok"
          title="open done issues"
          onClick={() => onOpenIssues("done")}
          primary
        />
        <StatTile
          label="In flight"
          value={num(m?.in_flight_issues)}
          tone={(m?.in_flight_issues ?? 0) > 0 ? "running" : "neutral"}
          title="open in-flight and pending issues"
          onClick={() => onOpenIssues("inflight")}
          primary
        />
        <StatTile
          label="$ per done"
          value={usdCompact(m?.cost_per_done_usd)}
          sub={m?.total_cost_usd != null ? `${usd(m.total_cost_usd)} total` : null}
          tone="info"
          title="the primary efficiency number: total journalled spend / done issues"
          primary
        />
        <CapsTile
          todayUsd={todayUsd}
          dailyCapUsd={caps?.daily_cap_usd ?? null}
          issueCapUsd={caps?.issue_cap_usd ?? null}
          spendBlocks={spendBlocks}
          dailyPct={dailyPct}
        />
      </div>
      <div className="tiles overview-metrics overview-metrics-secondary">
        <StatTile
          label="First-pass rate"
          value={rate(m?.first_pass_rate)}
          sub={
            m?.first_pass_approvals != null || m?.first_pass_changes != null
              ? `${num(m?.first_pass_approvals)} approve / ${num(m?.first_pass_changes)} changes`
              : null
          }
          tone={(m?.first_pass_rate ?? 1) < 0.5 ? "running" : "ok"}
          title="review APPROVE vs REQUEST_CHANGES on the first round — the power guardrail"
        />
        <StatTile label="Rounds / PR" value={num(m?.rounds_per_pr, 2)} tone="neutral" />
        <StatTile
          label="Interventions / PR"
          value={num(m?.interventions_per_merged_pr, 2)}
          sub={`${byClass.get("rearm") ?? 0} rearm / ${byClass.get("policy") ?? 0} policy / ${byClass.get("other") ?? 0} other`}
          tone={(m?.interventions_per_merged_pr ?? 0) > 0 ? "running" : "ok"}
          title="journalled operator re-arms per merged PR — the unattended-operation metric; policy promotes are counted but excluded from the ratio"
        />
        {/* Lifetime counters, not live alarms: the value carries the tone,
            the tile frame stays quiet. A red-bordered tile that can never go
            calm again teaches the operator to ignore red. */}
        <StatTile
          label="Escalations"
          value={num(escalations)}
          sub={
            <EscalationDetail
              reasons={ov.data?.escalation_reasons ?? []}
              spendBlocks={spendBlocks}
            />
          }
          tone={escalations > 0 || spendBlocks > 0 ? "fail" : "neutral"}
          title="needs-human-review relabels, retry exhaustion, infra deaths"
        />
        <StatTile
          label="Recurrences"
          value={num(recurrences)}
          tone={recurrences > 0 ? "fail" : "neutral"}
          title="harness-resolved issues that came back — the ground-truth guardrail"
        />
      </div>

      {showDaemonHealth ? (
        <section className="overview-health panel">
          <div className="panel-hd">
            <span className="panel-title">Daemon health</span>
            <span className="faint">live role journals</span>
          </div>
          <div className="panel-bd health-list">
            {(ov.data?.health ?? []).length === 0 && !ov.loading ? (
              <EmptyState>no role journals found for this deployment</EmptyState>
            ) : null}
            {(ov.data?.health ?? []).map((h) => (
              <HealthRow key={h.role} health={h} />
            ))}
          </div>
        </section>
      ) : null}

      <section className={`panel operator-actions-panel${(ov.data?.interventions ?? []).length > 0 ? " operator-actions-active" : ""}`}>
        <div className="panel-hd">
          <span className="panel-title">Operator actions</span>
          <span className="faint">newest first</span>
        </div>
        <div className="panel-bd flush">
          <InterventionList
            rows={ov.data?.interventions ?? []}
            loading={ov.loading}
          />
        </div>
      </section>

      <SpendPerHour buckets={ov.data?.spend_per_hour ?? []} />

      {showIssueHealthTrend ? (
        <HealthTrend rows={ov.data?.health_trend ?? []} loading={ov.loading} />
      ) : null}

      <UnattributedCount count={ov.data?.unattributed_count ?? 0} />
    </div>
  );
}

function CapsTile({
  todayUsd,
  dailyCapUsd,
  issueCapUsd,
  spendBlocks,
  dailyPct,
}: {
  todayUsd: number | null;
  dailyCapUsd: number | null;
  issueCapUsd: number | null;
  spendBlocks: number;
  dailyPct: number | null;
}) {
  const tone = utilTone(dailyPct);
  const alert = dailyPct !== null && dailyPct >= 85;
  return (
    <section
      className={`tile tile-primary overview-cap-tile${alert ? " tile-alert" : ""}`}
      aria-label="Spending caps"
    >
      <div className="tile-label">
        <span className={`tile-signal tone-bg-${alert ? "fail" : tone}`} aria-hidden="true" />
        Caps
      </div>
      <div className="cap-spend">
        <span className={`tile-value tone-${alert ? "fail" : tone}`}>
          {usdCompact(todayUsd)}
        </span>
        <span className="faint">
          today {dailyCapUsd !== null ? `/ ${usd(dailyCapUsd)}` : "· uncapped"}
        </span>
      </div>
      <Meter
        label="Daily spend"
        pct={dailyPct}
        unavailableReason="no daily cap configured for this deployment"
      />
      <div className="cap-meta">
        <span className="faint">Per issue <span className="mono">{usd(issueCapUsd)}</span></span>
        <span className={spendBlocks > 0 ? "tone-fail" : "faint"}>
          {num(spendBlocks)} {spendBlocks === 1 ? "block" : "blocks"}
        </span>
      </div>
    </section>
  );
}

/**
 * Where the last two days of money went, hour by hour. The series arrives
 * dense (every empty hour is an explicit 0.0) precisely so the shape is
 * honest, so it is rendered as-is: no gap-filling, no re-spacing, no dropping
 * of quiet hours.
 */
function SpendPerHour({ buckets }: { buckets: OverviewPayload["spend_per_hour"] }) {
  const bars = buckets.map((bucket) => ({
    key: bucket.hour_start_iso,
    label: localHour(bucket.hour_start_iso),
    value: bucket.usd,
  }));
  return (
    <section className="panel">
      <div className="panel-hd">
        <span className="panel-title">Spend per hour</span>
        <span className="faint">last {bars.length || 48} hours, journal-settled</span>
      </div>
      <div className="panel-bd">
        <Histogram
          bars={bars}
          unit="spend"
          emptyText="no journalled spend in the last 48 hours"
        />
      </div>
    </section>
  );
}

function HealthTrend({ rows, loading }: { rows: HealthTrendDay[]; loading: boolean }) {
  const [query, setQuery] = useState("");
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return rows;
    return rows.filter((row) => [row.day, row.opened, row.closed, row.net_open, row.recurrences]
      .some((value) => String(value).toLowerCase().includes(needle)));
  }, [query, rows]);
  const sorted = useSortedRows(
    filtered,
    {
      day: (row) => row.day,
      opened: (row) => row.opened,
      closed: (row) => row.closed,
      net: (row) => row.net_open,
      recurrences: (row) => row.recurrences,
    },
    { key: "day", direction: "desc" },
    "overview-health-trend",
  );
  return (
    <section className="panel">
      <div className="panel-hd">
        <span className="panel-title">Issue health trend</span>
      </div>
      <div className="panel-bd flush">
        {rows.length > 10 ? (
          <div className="panel-table-tools">
            <TableFilter
              label="Filter health trend"
              value={query}
              onChange={setQuery}
              shown={filtered.length}
              total={rows.length}
            />
          </div>
        ) : null}
        {rows.length === 0 && !loading ? (
          <EmptyState>no issue activity or recurrences recorded yet</EmptyState>
        ) : rows.length > 0 && filtered.length === 0 ? (
          <EmptyState>no health rows match “{query.trim()}”</EmptyState>
        ) : (
          <div className="tbl-wrap">
            <ResizableTable storageKey="overview-health-trend" columns={[
              { key: "day", label: "Local day", width: 160 },
              { key: "opened", label: "Opened", className: "r", width: 90 },
              { key: "closed", label: "Closed", className: "r", width: 90 },
              { key: "net", label: "Net", className: "r", width: 90 },
              { key: "recurrences", label: "Recurrences", className: "r", width: 120 },
            ]} sort={sorted.sort} onSort={sorted.onSort}>
              <tbody>{sorted.rows.map((row) => (
                <tr key={row.day}>
                  <td className="mono">{row.day}</td>
                  <td className="r num">{num(row.opened)}</td><td className="r num">{num(row.closed)}</td>
                  <td className={`r num${row.net_open > 0 && !row.first_observation ? " tone-fail" : ""}`}>{row.net_open > 0 ? `+${row.net_open}` : num(row.net_open)}</td>
                  <td className={`r num${row.recurrences > 0 ? " tone-fail" : ""}`}>{num(row.recurrences)}</td>
                </tr>
              ))}</tbody>
            </ResizableTable>
          </div>
        )}
      </div>
    </section>
  );
}

function HealthRow({ health }: { health: RoleHealth }) {
  const h = roleHealth(health);
  const tone = HEALTH_TONE[h];
  return (
    <div className={`health-row tone-border-${tone}`}>
      <span className={`dot dot-${tone}`} aria-hidden="true" />
      <span className="health-role">{health.role}</span>
      <Chip tone={tone}>{HEALTH_LABEL[h]}</Chip>
      <span className="spacer" />
      <span className="faint num">{num(health.events)} ev</span>
      {health.log_path ? <CopyLogPathButton path={health.log_path} role={health.role} /> : null}
    </div>
  );
}

function CopyLogPathButton({ path, role }: { path: string; role: string }) {
  const [notice, setNotice] = useState<"copied" | "failed" | null>(null);
  useEffect(() => {
    if (notice === null) return;
    const timer = window.setTimeout(() => setNotice(null), 2_200);
    return () => window.clearTimeout(timer);
  }, [notice]);

  const copy = async () => {
    try {
      if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
      await navigator.clipboard.writeText(path);
      setNotice("copied");
    } catch {
      setNotice("failed");
    }
  };

  return (
    <span className="log-copy">
      <button
        className="log-copy-button"
        type="button"
        aria-label={`Copy ${role} log path`}
        {...tipProps(path)}
        onClick={() => void copy()}
      >
        <Icon name="copy" size={15} />
      </button>
      {notice ? (
        <span className={`log-copy-toast${notice === "failed" ? " tone-fail" : ""}`} role="status">
          {/* No clipboard (non-localhost http) must still hand over the path:
              show it selectable instead of a dead-end error. */}
          {notice === "copied" ? "Log path copied" : `Couldn’t copy — ${path}`}
        </span>
      ) : null}
    </span>
  );
}

/**
 * The escalation count on its own says a human was needed, never what for.
 * The reasons are the currently-live ones only, so the list shrinks as they
 * are cleared — a count that only ever grows teaches the operator to ignore it.
 */
function EscalationDetail({
  reasons,
  spendBlocks,
}: {
  reasons: EscalationReason[];
  spendBlocks: number;
}) {
  const blocked = spendBlocks > 0 ? `${spendBlocks} spend-blocked` : null;
  if (reasons.length === 0) return blocked ? <>{blocked}</> : null;
  return (
    <details>
      <summary>
        {blocked ? `${blocked} · ` : ""}
        {reasons.length} still open
      </summary>
      <ul className="tile-reasons">
        {reasons.map((row) => (
          <li key={row.issue}>
            <span className="mono">#{row.issue}</span>
            <span className="trunc" title={row.reason ?? undefined}>
              {row.reason ?? <span className="faint">reason unrecorded</span>}
            </span>
          </li>
        ))}
      </ul>
    </details>
  );
}

const INTERVENTION_TONE: Record<string, "fail" | "running" | "meta" | "info"> = {
  rearm: "fail",
  guidance: "running",
  relabel: "info",
  policy: "meta",
  other: "info",
};

/**
 * An event the fold could correlate to no issue is a producer that failed to
 * stamp its correlation — a telemetry bug, not deployment news. The number
 * stays visible so the bug is noticeable; the bodies live in the journal.
 */
function UnattributedCount({ count }: { count: number }) {
  return (
    <div className="usage-line">
      <span className="muted">Uncorrelated journal events</span>
      <span className="spacer" />
      <span
        className={`mono num${count > 0 ? " tone-fail" : ""}`}
        title="events naming no issue — nonzero means a producer is not stamping correlation"
      >
        {num(count)}
      </span>
    </div>
  );
}

function InterventionList({
  rows,
  loading,
}: {
  rows: InterventionEvent[];
  loading: boolean;
}) {
  const sorted = useSortedRows(
    rows,
    {
      when: (row) => row.ts,
      class: (row) => row.action_class,
      target: (row) => row.issue ?? row.pr,
      action: (row) => row.kind,
    },
    { key: "when", direction: "desc" },
    "overview-interventions",
  );
  if (loading) return <Loading what="operator actions" />;
  if (rows.length === 0) {
    return <EmptyState>no journalled operator actions — the harness ran unattended</EmptyState>;
  }
  return (
    <div className="operator-actions-table">
      <ResizableTable storageKey="overview-interventions" columns={[
        { key: "when", label: "When", width: 160 },
        { key: "class", label: "Class", width: 110 },
        { key: "target", label: "Target", width: 110 },
        { key: "action", label: "Action", width: 420 },
      ]} sort={sorted.sort} onSort={sorted.onSort}>
        <tbody>
          {sorted.rows.map((row, i) => (
            <tr key={`${row.ts ?? "?"}-${i}`}>
              <td className="mono faint" title={row.ts ?? ""}>{localTime(row.ts)}</td>
              <td>
                <Chip tone={INTERVENTION_TONE[row.action_class] ?? "info"} square>
                  {row.action_class}
                </Chip>
              </td>
              <td className="mono">
                {row.issue !== null && row.issue !== undefined ? `#${row.issue}` : null}
                {row.pr !== null && row.pr !== undefined ? (
                  <span className="faint"> PR #{row.pr}</span>
                ) : null}
                {row.issue == null && row.pr == null ? <span className="faint">—</span> : null}
              </td>
              <td className="trunc" title={row.detail ?? row.kind}>
                <span className="mono">{row.kind}</span>
                {row.detail ? <span className="faint"> — {row.detail}</span> : null}
              </td>
            </tr>
          ))}
        </tbody>
      </ResizableTable>
    </div>
  );
}
