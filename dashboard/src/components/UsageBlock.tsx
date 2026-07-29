import type { Polled } from "../api/hooks";
import type { UsagePayload } from "../api/types";
import { Meter } from "./ui/Charts";
import { ErrorBanner } from "./ui/Feedback";
import { untilLabel, usd } from "../lib/format";
import { utilTone } from "../lib/semantics";

/**
 * Compact quota block: the three subscription windows plus harness dollars.
 *
 * Degradation is the point here — any of the three sources can be missing
 * (no codex install, expired oauth, no caps configured). A missing source
 * renders "n/a" with the backend's error string as the tooltip; it never hides
 * the row, because a silently absent meter reads as "0 %".
 */
export function UsageBlock({ usage }: { usage: Polled<UsagePayload> }) {
  const [open, setOpen] = useState(false);
  const u = usage.data;
  const claude = u?.claude ?? null;
  const codex = u?.codex ?? null;
  const harness = u?.harness ?? null;

  const today = harness?.today_all_repos_usd ?? null;
  const cap = harness?.global_daily_cap_usd ?? null;
  const capPct = today !== null && cap !== null && cap > 0 ? (today / cap) * 100 : null;

  return (
    <div className="usage-floating">
      <button
        className="quota-fab"
        aria-expanded={open}
        aria-controls="quota-card"
        onClick={() => setOpen((current) => !current)}
      >
        <span aria-hidden="true">◔</span> Quota
      </button>
      {open ? <section className="usage quota-card panel" id="quota-card" aria-label="Quota">
        <div className="usage-hd">
          <span className="panel-title">Quota</span>
          <button className="btn btn-xs btn-ghost" aria-label="Close quota" onClick={() => setOpen(false)}>×</button>
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
      </section> : null}
    </div>
  );
}
import { useState } from "react";
