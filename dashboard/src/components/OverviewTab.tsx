import { useMemo, useState } from "react";
import { api } from "../api/client";
import { usePolled } from "../api/hooks";
import { useNow } from "../lib/clock";
import type {
  AttentionItem,
  EscalationReason,
  HealthTrendDay,
  InterventionEvent,
  IssueCard,
  OverviewPayload,
  RoleHealth,
} from "../api/types";
import {
  HEALTH_LABEL,
  HEALTH_TONE,
  mixSlices,
  roleHealth,
  stageTone,
  utilTone,
} from "../lib/semantics";
import { ago, localHour, localTime, num, rate, usd, usdCompact } from "../lib/format";
import { Chip } from "./ui/Chip";
import { Histogram, Meter, StackedBar, StatTile } from "./ui/Charts";
import { EmptyState, ErrorBanner, Loading, SectionTitle, SkeletonRows, WarningList } from "./ui/Feedback";
import { ResizableTable, useSortedRows } from "./ui/ResizableTable";
import { TableFilter } from "./ui/TableFilter";
import { LifecycleStepper } from "./ui/LifecycleStepper";

const POLL_MS = 10_000;

/**
 * The "is this on rails?" view. Live human blockers and operator actions lead;
 * measurements and historical diagnostics follow in progressively quieter
 * groups so urgent work never competes with lifetime counters.
 */
export function OverviewTab({
  repo,
  onOpenIssues,
  activeIssues = [],
  activeIssuesLoading = false,
  onOpenIssue,
}: {
  repo: string;
  onOpenIssues: (sub: "inflight" | "done") => void;
  activeIssues?: IssueCard[];
  activeIssuesLoading?: boolean;
  onOpenIssue?: (issue: number) => void;
}) {
  const ov = usePolled(
    `overview:${repo}`,
    (signal) => api.overview(repo, signal),
    POLL_MS,
  );

  const m = ov.data?.metrics ?? null;
  const caps = ov.data?.caps ?? null;
  const mix = ov.data?.cost_mix ?? null;
  const [spendQuery, setSpendQuery] = useState("");
  const filteredSpend = useMemo(() => {
    const query = spendQuery.trim().toLowerCase();
    if (!query) return m?.by_kind ?? [];
    return (m?.by_kind ?? []).filter((row) => row.kind.toLowerCase().includes(query));
  }, [m?.by_kind, spendQuery]);
  const spendRows = useSortedRows(
    filteredSpend,
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
      <header className="page-heading">
        <div>
          <h1>Overview</h1>
          <p>Live operating state for <span className="mono">{repo}</span></p>
        </div>
        <span className="page-heading-status"><span className="repo-live-dot" />Reconciles every 10s</span>
      </header>
      {ov.error ? (
        <ErrorBanner message={ov.error} onRetry={ov.refresh} stale={!!ov.data} />
      ) : null}
      <WarningList warnings={ov.data?.warnings} />
      {ov.loading ? <SkeletonRows what="overview" rows={5} /> : null}

      <NeedsAttention items={ov.data?.needs_attention ?? []} loading={ov.loading} />

      <section className="overview-health panel">
        <div className="panel-hd">
          <span className="panel-title">Daemon health</span>
          <span className="faint">live role journals</span>
        </div>
        <div className="panel-bd health-cards">
          {(ov.data?.health ?? []).length === 0 && !ov.loading ? (
            <EmptyState>no role journals found for this deployment</EmptyState>
          ) : null}
          {(ov.data?.health ?? []).map((h) => (
            <HealthCard key={h.role} health={h} />
          ))}
        </div>
      </section>

      <LiveWorkPanel
        cards={activeIssues}
        loading={activeIssuesLoading}
        onOpenIssue={onOpenIssue}
      />

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
        <StatTile
          label="$ today"
          value={usdCompact(m?.today_usd)}
          sub={caps?.daily_cap_usd != null ? `cap ${usd(caps.daily_cap_usd)}` : "no daily cap"}
          tone={utilTone(dailyPct) === "ok" ? "neutral" : utilTone(dailyPct)}
          alert={dailyPct !== null && dailyPct >= 85}
          primary
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

      <div className="grid-2 overview-support">
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

      <UnattributedCount count={ov.data?.unattributed_count ?? 0} />

      <div className="by-kind">
        <SectionTitle>Spend by task type</SectionTitle>
        {(m?.by_kind ?? []).length === 0 ? (
          <EmptyState>no tasks recorded yet</EmptyState>
        ) : (
          <>
            {(m?.by_kind ?? []).length > 10 ? (
              <TableFilter
                label="Filter task types"
                value={spendQuery}
                onChange={setSpendQuery}
                shown={filteredSpend.length}
                total={(m?.by_kind ?? []).length}
              />
            ) : null}
            <div className="tbl-wrap panel">
              {filteredSpend.length === 0 ? (
                <EmptyState>no task types match “{spendQuery.trim()}”</EmptyState>
              ) : (
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
              )}
            </div>
          </>
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

function attentionFor(card: IssueCard): "blocked" | "escalated" | null {
  if (card.human_action === "escalated") return "escalated";
  if (
    card.human_action === "awaiting-reply"
    || card.stage === "blocked"
    || card.flags.includes("blocked")
  ) return "blocked";
  return null;
}

function LiveWorkPanel({
  cards,
  loading,
  onOpenIssue,
}: {
  cards: IssueCard[];
  loading: boolean;
  onOpenIssue?: (issue: number) => void;
}) {
  return (
    <section className="panel overview-live-work">
      <div className="panel-hd">
        <span className="panel-title">Active lifecycle</span>
        <span className="faint num">{cards.length} running</span>
      </div>
      <div className="overview-live-list">
        {loading && cards.length === 0 ? <Loading what="active work" /> : null}
        {!loading && cards.length === 0 ? (
          <EmptyState>no lifecycle is active — the deployment is on rails and idle</EmptyState>
        ) : null}
        {cards.map((card) => (
          <article className="overview-live-row" key={card.issue}>
            <div className="overview-live-summary">
              <div className="overview-live-title">
                {onOpenIssue ? (
                  <button type="button" onClick={() => onOpenIssue(card.issue)}>
                    <span className="mono">#{card.issue}</span>
                    <span>{card.title ?? "Untitled issue"}</span>
                  </button>
                ) : (
                  <span>
                    <span className="mono">#{card.issue}</span>{" "}
                    {card.title ?? "Untitled issue"}
                  </span>
                )}
              </div>
              <div className="overview-live-meta">
                <Chip tone={stageTone(card.stage)}>{card.stage ?? "active"}</Chip>
                {card.pr ? <span className="mono faint">PR #{card.pr}</span> : null}
              </div>
            </div>
            <LifecycleStepper
              steps={card.steps}
              stage={card.stage}
              done={card.done}
              hasPullRequest={card.pr !== null}
              attention={attentionFor(card)}
              label={`Issue #${card.issue} lifecycle`}
              onSelectStep={onOpenIssue ? () => onOpenIssue(card.issue) : undefined}
            />
            <div className="overview-live-measure">
              <span className="faint">spend</span>
              <strong className="mono num">{usd(card.cost_usd)}</strong>
              <span className="faint">{card.steps.length} legs</span>
            </div>
          </article>
        ))}
      </div>
    </section>
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
    <section className={`panel attention-panel${items.length > 0 ? " attention-panel-active" : " attention-panel-clear"}`}>
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
          <div className="usage-line attention-row" key={`${item.issue}-${item.kind}`}>
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
  const [query, setQuery] = useState("");
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return rows;
    return rows.filter((row) => [
      row.ts,
      row.action_class,
      row.issue,
      row.pr,
      row.kind,
      row.detail,
    ].some((value) => String(value ?? "").toLowerCase().includes(needle)));
  }, [query, rows]);
  const sorted = useSortedRows(
    filtered,
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
    <>
      {rows.length > 10 ? (
        <div className="panel-table-tools">
          <TableFilter
            label="Filter operator actions"
            value={query}
            onChange={setQuery}
            shown={filtered.length}
            total={rows.length}
          />
        </div>
      ) : null}
      {filtered.length === 0 ? (
        <EmptyState>no operator actions match “{query.trim()}”</EmptyState>
      ) : (
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
      )}
    </>
  );
}
