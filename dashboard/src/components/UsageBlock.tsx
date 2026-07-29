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
  const u = usage.data;
  const claude = u?.claude ?? null;
  const codex = u?.codex ?? null;
  const harness = u?.harness ?? null;

  const today = harness?.today_all_repos_usd ?? null;
  const cap = harness?.global_daily_cap_usd ?? null;
  const capPct = today !== null && cap !== null && cap > 0 ? (today / cap) * 100 : null;

  return (
    <details className="usage">
      <summary className="usage-hd">
        <span className="panel-title">Quota</span>
        {usage.error ? null : (
          <span className="faint num">{u?.claude?.source ?? ""}</span>
        )}
      </summary>

      <div className="usage-body">
        {usage.error ? (
          <ErrorBanner message={usage.error} onRetry={usage.refresh} stale={!!u} />
        ) : null}
        {claude?.error && (claude.five_hour_pct !== null || claude.seven_day_pct !== null) ? (
          <span className="faint usage-source-note" title={claude.error}>
            Claude: last successful reading
          </span>
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

        {(harness?.per_repo ?? []).length > 0 ? (
          <details className="usage-repos">
            <summary>Per-repo quotas <span className="num">{harness?.per_repo.length}</span></summary>
            <div className="usage-repos-body">
              {(harness?.per_repo ?? []).map((r) => {
                const p =
                  r.today_usd !== null && r.daily_cap_usd !== null && r.daily_cap_usd > 0
                    ? (r.today_usd / r.daily_cap_usd) * 100
                    : null;
                return (
                  <div className="usage-line" key={r.name} title={`${r.name} spend today vs its daily cap`}>
                    <span className="muted trunc">{r.name}</span>
                    <span className="spacer" />
                    <span className={`num mono tone-${utilTone(p)}`}>{usd(r.today_usd)}</span>
                    <span className="faint num">{r.daily_cap_usd !== null ? `/ ${usd(r.daily_cap_usd)}` : "/ no cap"}</span>
                  </div>
                );
              })}
            </div>
          </details>
        ) : null}
      </div>
    </details>
  );
}
