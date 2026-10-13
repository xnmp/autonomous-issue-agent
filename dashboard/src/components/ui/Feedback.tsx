import type { ReactNode } from "react";
import { Chip } from "./Chip";
import { Icon } from "./Icon";

/**
 * Per-panel error banner. The rule the whole app follows: a failed fetch
 * degrades ONE panel to a banner (over stale data when there is any), never the
 * screen to blank.
 */
export function ErrorBanner({
  message,
  onRetry,
  stale,
}: {
  message: string;
  onRetry?: () => void;
  stale?: boolean;
}) {
  return (
    <div className="banner banner-error" role="status">
      <span className="banner-mark" aria-hidden="true"><Icon name="alert-triangle" size={15} /></span>
      <span className="banner-msg">{message}</span>
      {stale ? <span className="faint">showing last known data</span> : null}
      {onRetry ? (
        <button className="btn btn-xs btn-ghost" onClick={onRetry}>
          Retry
        </button>
      ) : null}
    </div>
  );
}

/**
 * The control store could not be read, so the server derived no board: the
 * daemons' view of each item (legs, leases, caps) is unknown, not guessed.
 */
export function StoreUnavailable({ detail }: { detail: string | null | undefined }) {
  if (!detail) return null;
  return <ErrorBanner message={`Board unavailable — ${detail}`} />;
}

/** First GitHub observation not landed yet: loading, not a warning. */
export function ReadingGitHub({ loaded, observedAt }: { loaded: boolean; observedAt: string | null | undefined }) {
  return loaded && !observedAt ? <div className="row faint">Reading GitHub…</div> : null;
}

/** Backend `warnings[]` — degraded collectors, not failures. */
export function WarningList({ warnings }: { warnings: string[] | undefined }) {
  if (!warnings || warnings.length === 0) return null;
  return (
    <div className="banner banner-warn" role="status">
      <span className="banner-mark" aria-hidden="true"><Icon name="alert-triangle" size={15} /></span>
      <span className="banner-msg">
        {warnings.length === 1 ? warnings[0] : `${warnings.length} collector warnings`}
      </span>
      {warnings.length > 1 ? (
        <details className="warn-details">
          <summary>details</summary>
          <ul>
            {warnings.map((w, i) => (
              <li key={i}>{w}</li>
            ))}
          </ul>
        </details>
      ) : null}
    </div>
  );
}

/** Never a blank pane: every list says why it is empty. */
export function EmptyState({ children }: { children: ReactNode }) {
  return (
    <div className="empty">
      <span className="empty-icon" aria-hidden="true"><Icon name="inbox" size={17} /></span>
      <span className="empty-message">{children}</span>
    </div>
  );
}

export function Loading({ what }: { what: string }) {
  return (
    <div className="loading-inline" role="status">
      <span className="loading-spinner" aria-hidden="true" />
      <span>Loading {what}…</span>
    </div>
  );
}

export function SkeletonRows({
  what,
  rows = 4,
}: {
  what: string;
  rows?: number;
}) {
  return (
    <div className="skeleton-list" role="status" aria-label={`Loading ${what}`}>
      {Array.from({ length: rows }, (_, index) => (
        <div className="skeleton-row" key={index}>
          <span className="skeleton skeleton-short" />
          <span className="skeleton skeleton-grow" />
          <span className="skeleton skeleton-medium" />
        </div>
      ))}
    </div>
  );
}

export function GhUnavailableChip() {
  return (
    <Chip tone="fail" title="A GitHub read failed — the board, titles, PR state and backlog may be missing or stale">
      GitHub data unavailable
    </Chip>
  );
}

/** Section heading inside a tab body. */
export function SectionTitle({ children, right }: { children: ReactNode; right?: ReactNode }) {
  return (
    <div className="section-title">
      <span>{children}</span>
      {right ? <span className="section-right">{right}</span> : null}
    </div>
  );
}
