import { useState, type KeyboardEvent, type PointerEvent } from "react";
import type { Polled } from "../api/hooks";
import type { HarnessRepoSpend, RepoSummary, ReposPayload, UsagePayload } from "../api/types";
import { HEALTH_LABEL, HEALTH_TONE, roleHealth, utilTone } from "../lib/semantics";
import { ago, pct, usd } from "../lib/format";
import { Chip, Dot } from "./ui/Chip";
import { EmptyState, ErrorBanner, SkeletonRows, WarningList } from "./ui/Feedback";
import { UsageBlock } from "./UsageBlock";
import { OptionsMenu, type DashboardOptions } from "./ui/OptionsMenu";
import { Icon } from "./ui/Icon";

const WIDTH_KEY = "issue-agent.dashboard.sidebar-width";
const MIN_WIDTH = 220;
const MAX_WIDTH = 480;
const DEFAULT_WIDTH = 248;

/** The rail's width is a whole number of CSS pixels, always within the range
 * the stylesheet is built for — including when it comes back from storage,
 * where anything at all may have been written. */
function clampWidth(value: number): number {
  if (!Number.isFinite(value)) return DEFAULT_WIDTH;
  return Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, Math.round(value)));
}

function storedWidth(): number {
  try {
    const raw = localStorage.getItem(WIDTH_KEY);
    // `Number("")` is 0, and a blank entry is an absent width, not a width of
    // zero: everything unusable has to reach clampWidth as NaN.
    return clampWidth(raw === null || raw.trim() === "" ? Number.NaN : Number(raw));
  } catch {
    return DEFAULT_WIDTH;
  }
}

function rememberWidth(value: number): void {
  try {
    localStorage.setItem(WIDTH_KEY, String(value));
  } catch {
    // The resized width remains active for this session.
  }
}

interface SidebarProps {
  repos: Polled<ReposPayload>;
  usage: Polled<UsagePayload>;
  activeRepo: string | null;
  verificationActive: boolean;
  options: DashboardOptions;
  onOptionsChange: (options: DashboardOptions) => void;
  onSelectRepo: (name: string) => void;
  onOpenVerification: () => void;
}

/**
 * Left rail: active deployments (with live role-health dots) on top, past ones
 * below, then verification + the usage meters. The rail starts nothing: the
 * dashboard's only writes are label changes.
 *
 * The list arrives pre-sorted by frecency (server-side); the client only
 * partitions active/inactive and never re-sorts — the operator's muscle memory
 * for row position is worth more than any client-side cleverness.
 */
export function Sidebar({
  repos,
  usage,
  activeRepo,
  verificationActive,
  options,
  onOptionsChange,
  onSelectRepo,
  onOpenVerification,
}: SidebarProps) {
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return localStorage.getItem("issue-agent.dashboard.sidebar-collapsed") === "true";
    } catch {
      return false;
    }
  });
  const [width, setWidth] = useState(storedWidth);

  const list = repos.data?.repos ?? [];
  const active = list.filter((r) => r.active === true);
  const unknown = list.filter((r) => r.active === null);
  const past = list.filter((r) => r.active === false);
  const quotaByRepo = new Map(
    (usage.data?.harness?.per_repo ?? []).map((quota) => [quota.name, quota]),
  );
  const toggleCollapsed = () => {
    setCollapsed((current) => {
      try {
        localStorage.setItem("issue-agent.dashboard.sidebar-collapsed", String(!current));
      } catch {
        // The control still works for this session.
      }
      return !current;
    });
  };
  // What gets remembered is the width the drag asked for, never the width the
  // rail was measured at: a measurement is in fractional CSS pixels once the
  // browser is zoomed, and at a narrow viewport the stylesheet caps the rail —
  // reading either back would have quietly rewritten the operator's choice.
  const beginResize = (event: PointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    const originX = event.clientX;
    const originWidth = width;
    let latest = originWidth;
    const move = (next: globalThis.PointerEvent) => {
      latest = clampWidth(originWidth + next.clientX - originX);
      setWidth(latest);
    };
    const stop = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", stop);
      rememberWidth(latest);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop, { once: true });
  };
  const resizeFromKeyboard = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const next = clampWidth(width + (event.key === "ArrowRight" ? 12 : -12));
    setWidth(next);
    rememberWidth(next);
  };

  // Fully out of the way when collapsed: no rail, no border — just a floating
  // settings button and the affordance to bring the sidebar back.
  if (collapsed) {
    return (
      <div className="sidebar-float">
        <OptionsMenu options={options} onChange={onOptionsChange} collapsed />
        <button
          className="sidebar-toggle sidebar-toggle-float"
          onClick={toggleCollapsed}
          aria-label="Expand sidebar"
          title="Expand sidebar"
        >
          <Icon name="chevron-right" />
        </button>
      </div>
    );
  }

  return (
    <aside className="sidebar" style={{ width }}>
      <div className="sidebar-brand">
        <span className="brand-mark" aria-hidden="true"><Icon name="agent" size={18} /></span>
        <span className="brand-copy">
          <strong>Issue Agent</strong>
          <span>Operator console</span>
        </span>
      </div>
      <button
        className="sidebar-toggle"
        onClick={toggleCollapsed}
        aria-label="Collapse sidebar"
        title="Collapse sidebar"
      >
        <Icon name="chevron-left" />
      </button>
      <div className="sidebar-scroll">
        {repos.error ? (
          <ErrorBanner message={repos.error} onRetry={repos.refresh} stale={!!repos.data} />
        ) : null}
        <WarningList warnings={repos.data?.warnings} />

        {repos.loading ? <SkeletonRows what="deployments" rows={4} /> : null}

        <div className="rail-group">
          <div className="rail-heading">
            Active <span className="faint num">{active.length}</span>
          </div>
          {!repos.loading && active.length === 0 ? (
            <EmptyState>no daemons running</EmptyState>
          ) : null}
          {active.map((r) => (
            <RepoRow
              key={r.name}
              repo={r}
              selected={r.name === activeRepo}
              onSelect={() => onSelectRepo(r.name)}
              quota={options.showPerRepoQuotas ? quotaByRepo.get(r.name) : undefined}
            />
          ))}
        </div>

        {unknown.length > 0 ? (
          <div className="rail-group">
            <div className="rail-heading">
              Unknown <span className="faint num">{unknown.length}</span>
            </div>
            {unknown.map((r) => (
              <RepoRow
                key={r.name}
                repo={r}
                selected={r.name === activeRepo}
                onSelect={() => onSelectRepo(r.name)}
                quota={options.showPerRepoQuotas ? quotaByRepo.get(r.name) : undefined}
              />
            ))}
          </div>
        ) : null}

        {past.length > 0 ? (
          <details className="rail-group rail-past">
            <summary className="rail-heading">
              Past <span className="num">{past.length}</span>
            </summary>
            <div className="rail-group-body">
              {past.map((r) => (
                <RepoRow
                  key={r.name}
                  repo={r}
                  selected={r.name === activeRepo}
                  onSelect={() => onSelectRepo(r.name)}
                  quota={options.showPerRepoQuotas ? quotaByRepo.get(r.name) : undefined}
                />
              ))}
            </div>
          </details>
        ) : null}
      </div>
      <div className="sidebar-foot">
        <UsageBlock usage={usage} />
        <OptionsMenu options={options} onChange={onOptionsChange} />
        <button
          className={`rail-btn${verificationActive ? " active" : ""}`}
          onClick={onOpenVerification}
          aria-label="Verification ledger"
          title="Verification ledger"
        >
          <span className="rail-btn-mark" aria-hidden="true"><Icon name="check-circle" /></span>
          <span>Verification ledger</span>
        </button>
      </div>
      <div
        className="sidebar-resizer"
        role="separator"
        aria-label="Resize sidebar"
        aria-orientation="vertical"
        aria-valuemin={MIN_WIDTH}
        aria-valuemax={MAX_WIDTH}
        aria-valuenow={width}
        tabIndex={0}
        onPointerDown={beginResize}
        onKeyDown={resizeFromKeyboard}
      />

    </aside>
  );
}

function RepoRow({
  repo,
  selected,
  onSelect,
  quota,
}: {
  repo: RepoSummary;
  selected: boolean;
  onSelect: () => void;
  quota?: HarnessRepoSpend;
}) {
  const cls = [
    "repo-row",
    selected ? "selected" : "",
    repo.is_self ? "is-self" : "",
    repo.active === false ? "inactive" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div className={cls}>
      <button className="repo-main" onClick={onSelect} title={repo.repo ?? repo.name}>
        <div className="repo-line1">
          <span className="repo-name trunc">{repo.name}</span>
          {repo.is_self ? (
            <Chip tone="meta" title="the harness's own repository">
              <strong>issue-agent</strong>
            </Chip>
          ) : null}
        </div>
        <div className="repo-line2">
          {repo.active === true ? (
            <span className="row" style={{ gap: 3 }}>
              {repo.roles.map((role) => {
                const h = roleHealth(role);
                return (
                  <Dot
                    key={role.role}
                    tone={HEALTH_TONE[h]}
                    title={`${role.role}: ${HEALTH_LABEL[h]} · heartbeat ${ago(role.heartbeat_ts)}`}
                  />
                );
              })}
            </span>
          ) : repo.active === null ? (
            <span className="faint">liveness unknown</span>
          ) : (
            <span className="faint">idle</span>
          )}
          {repo.in_flight !== null ? (
            <span className="repo-counts faint num">
              {repo.in_flight} flying · {repo.done ?? 0} done
            </span>
          ) : null}
          <span className="spacer" />
          <span className="faint num" title={repo.last_activity_ts ?? "no activity"}>
            {ago(repo.last_activity_ts)}
          </span>
        </div>
        {quota ? <RepoQuota quota={quota} /> : null}
      </button>
    </div>
  );
}

function RepoQuota({ quota }: { quota: HarnessRepoSpend }) {
  const value =
    quota.today_usd !== null && quota.daily_cap_usd !== null && quota.daily_cap_usd > 0
      ? (quota.today_usd / quota.daily_cap_usd) * 100
      : null;
  return (
    <div
      className="repo-quota"
      title={`${quota.name}: ${usd(quota.today_usd)} of ${usd(quota.daily_cap_usd)} today`}
    >
      <div className={`repo-quota-fill tone-bg-${utilTone(value)}`} style={{ width: `${Math.min(100, value ?? 0)}%` }} />
      <span>{value === null ? `${usd(quota.today_usd)} · no cap` : `${pct(value)} quota`}</span>
    </div>
  );
}
