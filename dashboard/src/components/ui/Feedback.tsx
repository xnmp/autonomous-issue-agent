import type { ReactNode } from "react";
import { Chip } from "./Chip";

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
      <span className="banner-mark" aria-hidden="true">
        !
      </span>
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

/** Backend `warnings[]` — degraded collectors, not failures. */
export function WarningList({ warnings }: { warnings: string[] | undefined }) {
  if (!warnings || warnings.length === 0) return null;
  return (
    <div className="banner banner-warn" role="status">
      <span className="banner-mark" aria-hidden="true">
        ⚠
      </span>
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
  return <div className="empty">{children}</div>;
}

export function Loading({ what }: { what: string }) {
  return <div className="empty">loading {what}…</div>;
}

export function GhUnavailableChip() {
  return (
    <Chip tone="fail" title="gh reads failed — journal-derived data only (titles, PR state and backlog may be missing)">
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
