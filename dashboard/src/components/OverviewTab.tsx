import { useMemo, useState } from "react";
import { api } from "../api/client";
import { usePolled } from "../api/hooks";
import type {
  AttentionItem,
  HealthTrendDay,
  OverviewPayload,
  RoleHealth,
} from "../api/types";
import {
  HEALTH_LABEL,
  HEALTH_TONE,
  mixSlices,
  roleHealth,
  utilTone,
} from "../lib/semantics";
import { ago, localHour, num, rate, usd, usdCompact } from "../lib/format";
import { Chip } from "./ui/Chip";
import { Histogram, Meter, StatTile } from "./ui/Charts";
import { EmptyState, ErrorBanner, Loading, SkeletonRows, StoreUnavailable, WarningList } from "./ui/Feedback";
import { ResizableTable, useSortedRows } from "./ui/ResizableTable";
import { TableFilter } from "./ui/TableFilter";

const POLL_MS = 10_000;

/**
 * The "is this on rails?" view. Items waiting on a human lead; measurements
 * (all folded from `legs` and GitHub merges) follow in progressively quieter
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
  const needsHuman = m?.needs_human ?? 0;
  const mix = mixSlices(m?.by_kind);

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
      <StoreUnavailable detail={ov.data?.store_error} />
      <WarningList warnings={ov.data?.warnings} />
      {ov.loading ? <SkeletonRows what="overview" rows={5} /> : null}

      <div className="tiles overview-metrics overview-metrics-primary">
        <StatTile
          label="Merged"
          value={m?.merged_prs_partial ? `≥${num(m.merged_prs)}` : num(m?.merged_prs)}
          sub={m?.merged_prs == null ? "GitHub not read yet" : m.merged_prs_partial ? "newest harness PRs only" : "harness PRs"}
          tone="ok"
          title="open done issues"
          onClick={() => onOpenIssues("done")}
          primary
        />
        <StatTile
          label="In flight"
          value={num(m?.open_items)}
          tone={(m?.open_items ?? 0) > 0 ? "running" : "neutral"}
          title="open in-flight issues"
          onClick={() => onOpenIssues("inflight")}
          primary
        />
        <StatTile
          label="$ per merged PR"
          value={m?.merged_prs_partial && m.cost_per_merged_pr_usd != null
            ? `≤${usdCompact(m.cost_per_merged_pr_usd)}` : usdCompact(m?.cost_per_merged_pr_usd)}
          sub={m ? `${usd(m.total_cost_usd)} total${m.merged_prs_partial ? " · older merges not listed" : ""}` : null}
          tone="info"
          title="the primary efficiency number: total leg spend / merged harness PRs"
          primary
        />
        <CapsTile
          todayUsd={todayUsd}
          dailyCapUsd={caps?.daily_cap_usd ?? null}
          issueCapUsd={caps?.issue_cap_usd ?? null}
          globalCapUsd={caps?.global_daily_cap_usd ?? null}
          dailyPct={dailyPct}
        />
      </div>
      <div className="tiles overview-metrics overview-metrics-secondary">
        <StatTile
          label="Needs you"
          value={num(m?.needs_human)}
          tone={needsHuman > 0 ? "fail" : "neutral"}
          title="items labelled needs-human: nothing moves until a person acts"
          onClick={() => onOpenIssues("inflight")}
        />
        <StatTile
          label="First-pass rate"
          value={rate(m?.first_pass_rate)}
          sub={m ? `${num(m.first_pass_approvals)} approve / ${num(m.first_pass_changes)} changes` : null}
          tone={(m?.first_pass_rate ?? 1) < 0.5 ? "running" : "ok"}
          title="review APPROVE vs REQUEST_CHANGES on the first round — the power guardrail"
        />
        <StatTile label="Rounds / PR" value={num(m?.rounds_per_pr, 2)} tone="neutral" />
        <StatTile
          label="Legs"
          value={num(m?.legs)}
          sub={mix.map((slice) => `${slice.label.toLowerCase()} ${usdCompact(slice.usd)}`).join(" · ")}
          tone="neutral"
          title="paid agent legs, with spend by leg family"
        />
        <StatTile
          label="Machine failures"
          value={num(m?.machine_legs)}
          tone={(m?.machine_legs ?? 0) > 0 ? "running" : "neutral"}
          title="legs that ended without a usable return (crash, timeout, cancellation) — the infra-death guardrail"
        />
      </div>

      {showDaemonHealth ? (
        <section className="overview-health panel">
          <div className="panel-hd">
            <span className="panel-title">Daemon health</span>
            <span className="faint">role heartbeats</span>
          </div>
          <div className="panel-bd health-list">
            {(ov.data?.health ?? []).length === 0 && !ov.loading ? (
              <EmptyState>no role heartbeats found for this deployment</EmptyState>
            ) : null}
            {(ov.data?.health ?? []).map((h) => (
              <HealthRow key={h.role} health={h} />
            ))}
          </div>
        </section>
      ) : null}

      <section className={`panel operator-actions-panel${(ov.data?.needs_attention ?? []).length > 0 ? " operator-actions-active" : ""}`}>
        <div className="panel-hd">
          <span className="panel-title">Needs you</span>
          <span className="faint">stopped and labelled needs-human</span>
        </div>
        <div className="panel-bd flush">
          <AttentionList rows={ov.data?.needs_attention ?? []} loading={ov.loading} />
        </div>
      </section>

      <SpendPerHour buckets={ov.data?.spend_per_hour ?? []} />

      {showIssueHealthTrend ? (
        <HealthTrend rows={ov.data?.health_trend ?? []} loading={ov.loading} />
      ) : null}
    </div>
  );
}

function CapsTile({
  todayUsd,
  dailyCapUsd,
  issueCapUsd,
  globalCapUsd,
  dailyPct,
}: {
  todayUsd: number | null;
  dailyCapUsd: number | null;
  issueCapUsd: number | null;
  globalCapUsd: number | null;
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
        {globalCapUsd !== null ? (
          <span className="faint">All repos <span className="mono">{usd(globalCapUsd)}</span></span>
        ) : null}
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
        <span className="faint">last {bars.length || 48} hours, from legs</span>
      </div>
      <div className="panel-bd">
        <Histogram
          bars={bars}
          unit="spend"
          emptyText="no leg spend in the last 48 hours"
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
    return rows.filter((row) => [row.day, row.opened, row.closed, row.net_open]
      .some((value) => String(value).toLowerCase().includes(needle)));
  }, [query, rows]);
  const sorted = useSortedRows(
    filtered,
    {
      day: (row) => row.day,
      opened: (row) => row.opened,
      closed: (row) => row.closed,
      net: (row) => row.net_open,
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
          <EmptyState>no issue activity recorded yet</EmptyState>
        ) : rows.length > 0 && filtered.length === 0 ? (
          <EmptyState>no health rows match “{query.trim()}”</EmptyState>
        ) : (
          <div className="tbl-wrap">
            <ResizableTable storageKey="overview-health-trend" columns={[
              { key: "day", label: "Local day", width: 160 },
              { key: "opened", label: "Opened", className: "r", width: 90 },
              { key: "closed", label: "Closed", className: "r", width: 90 },
              { key: "net", label: "Net", className: "r", width: 90 },
            ]} sort={sorted.sort} onSort={sorted.onSort}>
              <tbody>{sorted.rows.map((row) => (
                <tr key={row.day}>
                  <td className="mono">{row.day}</td>
                  <td className="r num">{num(row.opened)}</td><td className="r num">{num(row.closed)}</td>
                  <td className={`r num${row.net_open > 0 ? " tone-fail" : ""}`}>{row.net_open > 0 ? `+${row.net_open}` : num(row.net_open)}</td>
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
      <span className="faint num" title={health.heartbeat_ts ?? undefined}>
        {health.heartbeat_ts ? `heartbeat ${ago(health.heartbeat_ts)}` : "no heartbeat"}
      </span>
    </div>
  );
}

/** The items a person must move: the stop's headline and, as text, its comment. */
function AttentionList({ rows, loading }: { rows: AttentionItem[]; loading: boolean }) {
  const sorted = useSortedRows(
    rows,
    {
      issue: (row) => row.issue,
      reason: (row) => row.headline,
    },
    { key: "issue", direction: "asc" },
    "overview-attention",
  );
  if (loading) return <Loading what="items waiting on you" />;
  if (rows.length === 0) {
    return <EmptyState>nothing is waiting on you — the harness is running unattended</EmptyState>;
  }
  return (
    <div className="operator-actions-table">
      <ResizableTable storageKey="overview-attention" columns={[
        { key: "issue", label: "Issue", width: 260 },
        { key: "reason", label: "Why", width: 520 },
      ]} sort={sorted.sort} onSort={sorted.onSort}>
        <tbody>
          {sorted.rows.map((row) => (
            <tr key={row.issue}>
              <td className="trunc" title={row.title ?? undefined}>
                <span className="mono">#{row.issue}</span>
                {row.title ? <span className="faint"> {row.title}</span> : null}
              </td>
              <td className="trunc" title={row.detail ?? row.headline}>
                <Chip tone="fail" square>{row.headline}</Chip>
                {row.detail ? <span className="faint"> {row.detail}</span> : null}
              </td>
            </tr>
          ))}
        </tbody>
      </ResizableTable>
    </div>
  );
}
