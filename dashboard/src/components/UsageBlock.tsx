import { useState } from "react";
import type { Polled } from "../api/hooks";
import type { UsagePayload } from "../api/types";
import { Meter } from "./ui/Charts";
import { ErrorBanner } from "./ui/Feedback";
import { pct, untilLabel, usd } from "../lib/format";
import { utilTone } from "../lib/semantics";
import { Icon } from "./ui/Icon";

/**
 * Compact quota block: the three subscription windows plus harness dollars.
 *
 * Degradation is the point here — any of the three sources can be missing
 * (no codex install, expired oauth, no caps configured). A missing source
 * renders "n/a" with the backend's error string as the tooltip; it never hides
 * the row, because a silently absent meter reads as "0 %".
 */
export function UsageBlock({
  usage,
  collapsed = false,
}: {
  usage: Polled<UsagePayload>;
  collapsed?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const u = usage.data;
  const claude = u?.claude ?? null;
  const codex = u?.codex ?? null;
  const harness = u?.harness ?? null;

  const today = harness?.today_all_repos_usd ?? null;
  const cap = harness?.global_daily_cap_usd ?? null;
  const capPct = today !== null && cap !== null && cap > 0 ? (today / cap) * 100 : null;
  const compactMeters = [
    ["5h", claude?.five_hour_pct ?? null, claude?.error],
    ["7d", claude?.seven_day_pct ?? null, claude?.error],
    ["Cx", codex?.weekly_pct ?? null, codex?.error],
    ["$", capPct, null],
  ] as const;

  return (
    <div className={`usage-inline${collapsed ? " collapsed" : ""}`}>
      <button
        className={`rail-btn quota-trigger${collapsed ? " icon-only" : ""}`}
        aria-expanded={open}
        aria-controls="quota-card"
        aria-label="Quota"
        title="Quota"
        onClick={() => setOpen((current) => !current)}
      >
        <span className="rail-btn-mark quota-icon" aria-hidden="true">
          <i />
          <i />
          <i />
        </span>
        {!collapsed ? <span>Quota</span> : null}
      </button>
      {open ? <section className={`usage quota-card panel${collapsed ? " compact" : ""}`} id="quota-card" aria-label="Quota">
        {collapsed ? (
          <div className="compact-quota">
            {compactMeters.map(([label, value, error]) => {
              const clamped = value === null
                ? undefined
                : Math.max(0, Math.min(100, value));
              return (
                <div
                  className="compact-quota-meter"
                  key={label}
                  title={`${label}: ${value === null ? error ?? "unavailable" : pct(value)}`}
                  role="meter"
                  aria-label={`${label} quota usage`}
                  aria-valuemin={0}
                  aria-valuemax={100}
                  aria-valuenow={clamped}
                  aria-valuetext={value === null ? error ?? "unavailable" : pct(value)}
                >
                  <span className="compact-quota-label">{label}</span>
                  <span className={`compact-quota-track tone-bg-${utilTone(value)}`} aria-hidden="true">
                    <span style={{ width: `${Math.max(4, clamped ?? 0)}%` }} />
                  </span>
                </div>
              );
            })}
          </div>
        ) : (
        <>
        <div className="usage-hd">
          <button className="btn btn-xs btn-ghost" aria-label="Close quota" onClick={() => setOpen(false)}><Icon name="close" size={13} /></button>
        </div>
        <div className="usage-body">
        {usage.error ? (
          <ErrorBanner message={usage.error} onRetry={usage.refresh} stale={!!u} />
        ) : null}

        <Meter
          label="Claude 5h"
          pct={claude?.five_hour_pct ?? null}
          unavailableReason={claude?.error ?? "claude usage unavailable"}
          note={
            claude?.five_hour_resets_at
              ? `resets in ${untilLabel(claude.five_hour_resets_at)}`
              : null
          }
        />
        <Meter
          label="Claude 7d"
          pct={claude?.seven_day_pct ?? null}
          unavailableReason={claude?.error ?? "claude usage unavailable"}
          note={
            claude?.seven_day_resets_at
              ? `resets in ${untilLabel(claude.seven_day_resets_at)}`
              : null
          }
        />
        <Meter
          label={`Codex weekly${codex?.plan ? ` (${codex.plan})` : ""}`}
          pct={codex?.weekly_pct ?? null}
          unavailableReason={codex?.error ?? "no codex rollout found"}
          note={codex?.resets_at ? `resets in ${untilLabel(codex.resets_at)}` : null}
        />

        <div className="usage-sep" />

        {cap !== null ? (
          <Meter
            label="Harness $ today"
            pct={capPct}
            note={
              <span className="num">
                {usd(today)} of {usd(cap)} global cap
              </span>
            }
          />
        ) : (
          <div className="usage-line">
            <span className="muted">Harness $ today</span>
            <span className={`num mono tone-${utilTone(null)}`}>{usd(today)}</span>
          </div>
        )}

        </div>
        </>
        )}
      </section> : null}
    </div>
  );
}
