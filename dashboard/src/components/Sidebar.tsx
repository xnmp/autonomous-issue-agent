import { useState } from "react";
import { api } from "../api/client";
import type { Polled } from "../api/hooks";
import type { RepoSummary, ReposPayload, UsagePayload } from "../api/types";
import { HEALTH_LABEL, HEALTH_TONE, roleHealth } from "../lib/semantics";
import { ago } from "../lib/format";
import { Chip, Dot } from "./ui/Chip";
import { ConfirmModal } from "./ui/Modal";
import { EmptyState, ErrorBanner, SkeletonRows, WarningList } from "./ui/Feedback";
import { UsageBlock } from "./UsageBlock";

interface SidebarProps {
  repos: Polled<ReposPayload>;
  usage: Polled<UsagePayload>;
  activeRepo: string | null;
  verificationActive: boolean;
  onSelectRepo: (name: string) => void;
  onOpenVerification: () => void;
}

/**
 * Left rail: active deployments (with live role-health dots) on top, past ones
 * below with a start button, then verification + the usage meters.
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
  onSelectRepo,
  onOpenVerification,
}: SidebarProps) {
  const [confirmStart, setConfirmStart] = useState<RepoSummary | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const list = repos.data?.repos ?? [];
  const active = list.filter((r) => r.active);
  const past = list.filter((r) => !r.active);

  const doStart = async () => {
    if (!confirmStart) return;
    setBusy(true);
    setActionError(null);
    try {
      const res = await api.startDaemons(confirmStart.name);
      setConfirmStart(null);
      setNotice(res.detail || `started ${confirmStart.name}`);
      repos.refresh();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <aside className="sidebar">
      <div className="sidebar-scroll">
        {repos.data?.liveness === "journal-heuristic" ? (
          <Chip tone="running" title="/proc scan unavailable — liveness inferred from journal staleness">
            heuristic liveness
          </Chip>
        ) : null}
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
            />
          ))}
        </div>

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
                  onStart={() => {
                    setActionError(null);
                    setConfirmStart(r);
                  }}
                />
              ))}
            </div>
          </details>
        ) : null}
        <div className="sidebar-foot">
          {notice ? (
            <div className="banner banner-ok" role="status">
              <span className="banner-msg trunc" title={notice}>
                {notice}
              </span>
              <button className="btn btn-xs btn-ghost" onClick={() => setNotice(null)}>
                ✕
              </button>
            </div>
          ) : null}
          <div className="rail-heading">Operations</div>
          <button
            className={`rail-btn${verificationActive ? " active" : ""}`}
            onClick={onOpenVerification}
          >
            <span className="rail-btn-mark" aria-hidden="true">
              ✓
            </span>
            Verification ledger
          </button>
          <UsageBlock usage={usage} />
        </div>
      </div>

      {confirmStart ? (
        <ConfirmModal
          title="Start daemons"
          confirmLabel={`Start ${confirmStart.name}`}
          busy={busy}
          error={actionError}
          onConfirm={() => void doStart()}
          onCancel={() => setConfirmStart(null)}
          body={
            <>
              <p>
                Launch all three role daemons for{" "}
                <strong className="mono">{confirmStart.name}</strong>
                {confirmStart.repo ? (
                  <>
                    {" "}
                    (<span className="mono">{confirmStart.repo}</span>)
                  </>
                ) : null}
                ?
              </p>
              <p className="muted">
                Runs <span className="mono">make point-at</span> detached. The fixer will begin
                picking up <span className="mono">ai-ready</span> issues and spending against this
                deployment&apos;s caps.
              </p>
            </>
          }
        />
      ) : null}
    </aside>
  );
}

function RepoRow({
  repo,
  selected,
  onSelect,
  onStart,
}: {
  repo: RepoSummary;
  selected: boolean;
  onSelect: () => void;
  onStart?: () => void;
}) {
  const cls = [
    "repo-row",
    selected ? "selected" : "",
    repo.is_self ? "is-self" : "",
    repo.active ? "" : "inactive",
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
          {repo.active ? (
            <span className="row" style={{ gap: 3 }}>
              {repo.roles.map((role) => {
                const h = roleHealth(role);
                return (
                  <Dot
                    key={role.role}
                    tone={HEALTH_TONE[h]}
                    title={`${role.role}: ${HEALTH_LABEL[h]} · ${role.events} events · last ${ago(role.last_event_ts)}`}
                  />
                );
              })}
            </span>
          ) : (
            <span className="faint">idle</span>
          )}
          <span className="repo-counts faint num">
            {repo.in_flight} flying · {repo.done} done
          </span>
          <span className="spacer" />
          <span className="faint num" title={repo.last_activity_ts ?? "no activity"}>
            {ago(repo.last_activity_ts)}
          </span>
        </div>
      </button>
      {onStart ? (
        <button
          className="btn btn-xs repo-start"
          onClick={onStart}
          title="start daemons"
          aria-label={`Start daemons for ${repo.name}`}
        >
          ▶
        </button>
      ) : null}
    </div>
  );
}
