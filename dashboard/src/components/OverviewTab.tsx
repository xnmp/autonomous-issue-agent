import { api } from "../api/client";
import { usePolled } from "../api/hooks";
import { useNow } from "../lib/clock";
import type {
  AttentionItem,
  HealthTrendDay,
  InterventionEvent,
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
import { ago, localHour, localTime, num, rate, usd, usdCompact } from "../lib/format";
import { Chip } from "./ui/Chip";
import { Histogram, Meter, StackedBar, StatTile } from "./ui/Charts";
import { EmptyState, ErrorBanner, Loading, SectionTitle, SkeletonRows, WarningList } from "./ui/Feedback";
import { ResizableTable, useSortedRows } from "./ui/ResizableTable";

const POLL_MS = 10_000;

/**
 * The "is this on rails?" view. Reading order is deliberate: the stat tiles
 * answer *how much and how well*, the cost mix answers *where the money went*,
 * the caps answer *how much runway is left*, and the health cards answer *is
 * anything dead* — with red accents on escalations, recurrences, stale daemons
 * and near-limit caps so trouble is visible without reading a number.
 */
export function OverviewTab({
  repo,
  onOpenIssues,
}: {
  repo: string;
  onOpenIssues: (sub: "inflight" | "done") => void;
}) {
  const ov = usePolled(
    `overview:${repo}`,
    (signal) => api.overview(repo, signal),
    POLL_MS,
  );

  const m = ov.data?.metrics ?? null;
  const caps = ov.data?.caps ?? null;
  const mix = ov.data?.cost_mix ?? null;
  const spendRows = useSortedRows(
    m?.by_kind ?? [],
    {
      kind: (row) => row.kind,
      tasks: (row) => row.runs,
      usd: (row) => row.usd,
      "per-task": (row) => row.runs > 0 ? row.usd / row.runs : null,
    },
    { key: "usd", direction: "desc" },
    "overview-spend",
  );

  const escalations = m?.escalations ?? 0;
  const recurrences = m?.recurrences ?? 0;
  const spendBlocks = m?.spend_blocks ?? 0;
  const byClass = new Map(
    (m?.interventions_by_class ?? []).map((row) => [row.name, row.count]),
  );

  const dailyPct =
    caps?.today_usd != null && caps?.daily_cap_usd != null && caps.daily_cap_usd > 0
      ? (caps.today_usd / caps.daily_cap_usd) * 100
      : null;

  const slices = mixSlices(mix).map((s) => ({
    key: s.family,
    label: s.label,
    value: s.usd,
    varName: s.varName,
  }));
  const repairShare = (() => {
    const total = slices.reduce((a, s) => a + s.value, 0);
    const repair = slices.find((s) => s.key === "repair")?.value ?? 0;
    return total > 0 ? repair / total : null;
  })();

  return (
    <div className="tabbody">
      {ov.error ? (
        <ErrorBanner message={ov.error} onRetry={ov.refresh} stale={!!ov.data} />
      ) : null}
      <WarningList warnings={ov.data?.warnings} />
      {ov.loading ? <SkeletonRows what="overview" rows={5} /> : null}

      <NeedsAttention items={ov.data?.needs_attention ?? []} loading={ov.loading} />

      <div className="tiles">
        <StatTile
          label="Done"
          value={num(m?.done_issues)}
          sub={ov.data?.first_observed_day ? `since ${ov.data.first_observed_day}` : "deployment start unknown"}
          tone="ok"
          title="open done issues"
          onClick={() => onOpenIssues("done")}
        />
        <StatTile
          label="In flight"
          value={num(m?.in_flight_issues)}
          tone={(m?.in_flight_issues ?? 0) > 0 ? "running" : "neutral"}
          title="open in-flight and pending issues"
          onClick={() => onOpenIssues("inflight")}
        />
        <StatTile
          label="$ per done"
          value={usdCompact(m?.cost_per_done_usd)}
          sub={m?.total_cost_usd != null ? `${usd(m.total_cost_usd)} total` : null}
          tone="info"
          title="the primary efficiency number: total journalled spend / done issues"
        />
        <StatTile
          label="$ today"
          value={usdCompact(m?.today_usd)}
          sub={caps?.daily_cap_usd != null ? `cap ${usd(caps.daily_cap_usd)}` : "no daily cap"}
          tone={utilTone(dailyPct) === "ok" ? "neutral" : utilTone(dailyPct)}
          alert={dailyPct !== null && dailyPct >= 85}
        />
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
          sub={spendBlocks > 0 ? `${spendBlocks} spend-blocked` : null}
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

      <div className="grid-2">
        <section className="panel">
          <div className="panel-hd">
            <span className="panel-title">Cost mix by task family</span>
            {repairShare !== null ? (
              <Chip tone={repairShare > 0.25 ? "fail" : "neutral"} title="repair tasks are pure overhead; their share should shrink">
                repair {rate(repairShare)}
              </Chip>
            ) : null}
          </div>
          <div className="panel-bd">
            <StackedBar segments={slices} emptyText="no journalled spend for this deployment yet" />
          </div>
        </section>

        <section className="panel">
          <div className="panel-hd">
            <span className="panel-title">Caps</span>
          </div>
          <div className="panel-bd col" style={{ gap: "var(--s3)" }}>
            <Meter
              label="Daily cap"
              pct={dailyPct}
              unavailableReason="no daily cap configured for this deployment"
              note={
                <span className="num">
                  {usd(caps?.today_usd)} today
                  {caps?.daily_cap_usd != null ? ` of ${usd(caps.daily_cap_usd)}` : " (uncapped)"}
                </span>
              }
            />
            <div className="usage-line">
              <span className="muted">Per-issue cap</span>
              <span className="spacer" />
              <span className="mono num">{usd(caps?.issue_cap_usd)}</span>
            </div>
            <div className="usage-line">
              <span className="muted">Spend blocks</span>
              <span className="spacer" />
              <span className={`mono num${spendBlocks > 0 ? " tone-fail" : ""}`}>
                {num(spendBlocks)}
              </span>
            </div>
          </div>
        </section>
      </div>

      <SpendPerHour buckets={ov.data?.spend_per_hour ?? []} />

      <HealthTrend rows={ov.data?.health_trend ?? []} loading={ov.loading} />

      <SectionTitle>Daemon health</SectionTitle>
      <div className="health-cards">
        {(ov.data?.health ?? []).length === 0 && !ov.loading ? (
          <EmptyState>no role journals found for this deployment</EmptyState>
        ) : null}
        {(ov.data?.health ?? []).map((h) => (
          <HealthCard key={h.role} health={h} />
        ))}
      </div>

      <UnattributedCount count={ov.data?.unattributed_count ?? 0} />

      <section className="panel">
        <div className="panel-hd">
          <span className="panel-title">Operator actions</span>
          <span className="faint">what a human had to do, newest first</span>
        </div>
        <div className="panel-bd flush">
          <InterventionList
            rows={ov.data?.interventions ?? []}
            loading={ov.loading}
          />
        </div>
      </section>

      <div className="by-kind">
        <SectionTitle>Spend by task type</SectionTitle>
        {(m?.by_kind ?? []).length === 0 ? (
          <EmptyState>no tasks recorded yet</EmptyState>
        ) : (
          <div className="tbl-wrap panel">
            <ResizableTable storageKey="overview-spend" columns={[
              { key: "kind", label: "Kind", width: 220 },
              { key: "tasks", label: "Tasks", className: "r", width: 90 },
              { key: "usd", label: "USD", className: "r", width: 100 },
              { key: "per-task", label: "$/task", className: "r", width: 100 },
            ]} sort={spendRows.sort} onSort={spendRows.onSort}>
              <tbody>
                {spendRows.rows.map((k) => (
                  <tr key={k.kind}>
                    <td className="mono">{k.kind}</td>
                    <td className="r num">{num(k.runs)}</td>
                    <td className="r mono">{usd(k.usd)}</td>
                    <td className="r mono faint">{k.runs > 0 ? usd(k.usd / k.runs) : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </ResizableTable>
          </div>
        )}
      </div>
    </div>
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
  const sorted = useSortedRows(
    rows,
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
        {rows.length === 0 && !loading ? (
          <EmptyState>no issue activity or recurrences recorded yet</EmptyState>
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

function HealthCard({ health }: { health: RoleHealth }) {
  const h = roleHealth(health);
  const tone = HEALTH_TONE[h];
  useNow(); // keeps the "last event Nm ago" honest without a re-fetch
  return (
    <div className={`hcard tone-border-${tone}`}>
      <div className="hcard-hd">
        <span className={`dot dot-${tone}`} aria-hidden="true" />
        <span className="hcard-role">{health.role}</span>
        <Chip tone={tone}>{HEALTH_LABEL[h]}</Chip>
        <span className="spacer" />
        <span className="faint num">{num(health.events)} ev</span>
      </div>
      <div className="hcard-line">
        <span className="muted">last</span>
        <span className="mono trunc">{health.last_event ?? "—"}</span>
        <span className="spacer" />
        <span className="num faint" title={health.last_event_ts ?? ""}>
          {ago(health.last_event_ts)}
        </span>
      </div>
      {health.log_path ? (
        <div className="hcard-line">
          <span className="muted">log</span>
          <span className="mono faint trunc" title={health.log_path}>
            {health.log_path}
          </span>
        </div>
      ) : null}
    </div>
  );
}

const ATTENTION_TONE: Record<string, "fail" | "running" | "meta" | "info"> = {
  blocked: "fail",
  escalated: "fail",
  "needs-budget-review": "running",
  "spend-blocked": "running",
  held: "meta",
};

const ATTENTION_LABEL: Record<string, string> = {
  blocked: "asked a question",
  escalated: "escalated",
  "needs-budget-review": "needs budget review",
  "spend-blocked": "spend blocked",
  held: "held",
};

/**
 * What is waiting on a person right now — first thing on the page, because
 * every other panel on this tab describes the past. An empty panel is the
 * good outcome and says so rather than disappearing, so "nothing needs me"
 * and "the overview failed to load" never look alike.
 */
function NeedsAttention({ items, loading }: { items: AttentionItem[]; loading: boolean }) {
  return (
    <section className="panel">
      <div className="panel-hd">
        <span className="panel-title">Needs attention</span>
        <span className="faint">{items.length > 0 ? `${items.length} waiting on a human` : "nothing waiting"}</span>
      </div>
      <div className="panel-bd col" style={{ gap: "var(--s2)" }}>
        {loading && items.length === 0 ? <Loading what="attention" /> : null}
        {!loading && items.length === 0 ? (
          <EmptyState>no issue is waiting on a human</EmptyState>
        ) : null}
        {items.map((item) => (
          <div className="usage-line" key={`${item.issue}-${item.kind}`}>
            <Chip tone={ATTENTION_TONE[item.kind] ?? "info"} square>
              {ATTENTION_LABEL[item.kind] ?? item.kind}
            </Chip>
            <span className="mono">#{item.issue}</span>
            <span className="trunc" title={item.title ?? undefined}>
              {item.title ?? <span className="faint">title unavailable</span>}
            </span>
            <span className="spacer" />
            {item.detail ? (
              <span className="faint trunc" title={item.detail}>{item.detail}</span>
            ) : null}
          </div>
        ))}
      </div>
    </section>
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
  );
}
