import type { ReactNode } from "react";
import type { Tone } from "../../lib/semantics";
import { utilTone } from "../../lib/semantics";
import { pct as fmtPct, usd } from "../../lib/format";

/* --------------------------------------------------------------- Meter ---
 * A single-value magnitude bar (usage windows, caps utilization).
 * Per the dataviz mark spec: the fill carries severity, the track is a lighter
 * step of the SAME hue so the state reads across the whole bar, and the value
 * is direct-labelled (a meter has one series, so no legend box).
 */

interface MeterProps {
  label: string;
  /** 0..100; null renders the degraded "n/a" state */
  pct: number | null;
  /** overrides the automatic severity tone (e.g. neutral for informational) */
  tone?: Tone;
  /** right-hand annotation: reset time, $ of cap, … */
  note?: ReactNode;
  /** shown in the n/a tooltip when the source errored */
  unavailableReason?: string | null;
}

export function Meter({ label, pct, tone, note, unavailableReason }: MeterProps) {
  const t = tone ?? utilTone(pct);
  const clamped = pct === null ? 0 : Math.max(0, Math.min(100, pct));
  const na = pct === null;
  return (
    <div className="meter">
      <div className="meter-hd">
        <span className="meter-label trunc">{label}</span>
        {na ? (
          <span
            className="meter-val faint"
            title={unavailableReason ?? "no data from this source"}
          >
            n/a
          </span>
        ) : (
          <span className={`meter-val num tone-${t}`}>{fmtPct(pct)}</span>
        )}
      </div>
      <div
        className={`meter-track tone-${na ? "neutral" : t}`}
        role="meter"
        aria-valuenow={na ? undefined : clamped}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={label}
      >
        {na ? null : <div className="meter-fill" style={{ width: `${clamped}%` }} />}
      </div>
      {note ? <div className="meter-note faint">{note}</div> : null}
    </div>
  );
}

/* ----------------------------------------------------------- Stat tile ---
 * label (sentence case) · value (proportional figures — tabular-nums only in
 * columns) · optional sub-line. `alert` turns the tile red: the dashboard's
 * job is that trouble is unmissable at a glance.
 */

export function StatTile({
  label,
  value,
  sub,
  tone = "neutral",
  alert,
  title,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  tone?: Tone;
  alert?: boolean;
  title?: string;
}) {
  return (
    <div className={`tile${alert ? " tile-alert" : ""}`} title={title}>
      <div className="tile-label">{label}</div>
      <div className={`tile-value tone-${alert ? "fail" : tone}`}>{value}</div>
      {sub ? <div className="tile-sub faint">{sub}</div> : null}
    </div>
  );
}

/* --------------------------------------------------------- Stacked bar ---
 * Composition of one total (cost mix). Fixed slot colours by identity, a 2px
 * surface gap between every segment (the separator is the gap, never a stroke),
 * a legend that is always present, and direct labels only where they fit.
 */

export interface StackSegment {
  key: string;
  label: string;
  value: number;
  varName: string;
}

export function StackedBar({
  segments,
  total,
  formatValue = (v: number) => usd(v),
  emptyText = "no spend recorded yet",
}: {
  segments: StackSegment[];
  total?: number;
  formatValue?: (v: number) => string;
  emptyText?: string;
}) {
  const sum = total ?? segments.reduce((a, s) => a + s.value, 0);
  if (!(sum > 0)) return <div className="empty empty-inline">{emptyText}</div>;

  return (
    <div className="stack">
      <div className="stack-bar">
        {segments
          .filter((s) => s.value > 0)
          .map((s) => {
            const share = (s.value / sum) * 100;
            const label = `${Math.round(share)}%`;
            // Only label inside the segment when the text comfortably fits —
            // never clip, never overflow. ~7% of the bar per character.
            const fits = share >= 14;
            return (
              <div
                key={s.key}
                className="stack-seg"
                style={{ width: `${share}%`, background: `var(${s.varName})` }}
                title={`${s.label}: ${formatValue(s.value)} (${share.toFixed(1)}%)`}
              >
                {fits ? <span className="stack-seg-label num">{label}</span> : null}
              </div>
            );
          })}
      </div>
      <div className="legend">
        {segments.map((s) => (
          <div key={s.key} className="legend-item" title={`${s.label}: ${formatValue(s.value)}`}>
            <span className="legend-swatch" style={{ background: `var(${s.varName})` }} />
            <span className="legend-label">{s.label}</span>
            <span className="legend-val num mono">{formatValue(s.value)}</span>
            <span className="legend-share faint num">
              {sum > 0 ? `${Math.round((s.value / sum) * 100)}%` : "—"}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

/* -------------------------------------------------- Progress to an ETA ---
 * Elapsed-vs-projection for an in-flight issue, segmented by the leg families
 * that have run so far. Rendered ONLY when the projection is non-null (the
 * contract says projections are null until the repo has a completed issue) —
 * a progress bar against an unknown target is a lie.
 */

export function EtaProgress({
  elapsedS,
  etaS,
  segments,
}: {
  elapsedS: number;
  etaS: number;
  /** family var-names in order of occurrence, weighted by their cost share */
  segments: { key: string; varName: string; weight: number }[];
}) {
  const frac = Math.max(0, Math.min(1, elapsedS / etaS));
  const over = elapsedS > etaS;
  const weightSum = segments.reduce((a, s) => a + s.weight, 0);
  const parts =
    weightSum > 0
      ? segments.map((s) => ({ ...s, share: (s.weight / weightSum) * frac * 100 }))
      : [{ key: "all", varName: "--info", share: frac * 100 }];

  return (
    <div
      className={`eta${over ? " eta-over" : ""}`}
      role="progressbar"
      aria-valuenow={Math.round(frac * 100)}
      aria-valuemin={0}
      aria-valuemax={100}
      title={
        over
          ? "past the repo's mean issue wall clock"
          : `${Math.round(frac * 100)}% of the repo's mean issue wall clock`
      }
    >
      {parts.map((p) => (
        <div
          key={p.key}
          className="eta-seg"
          style={{ width: `${p.share}%`, background: `var(${p.varName})` }}
        />
      ))}
    </div>
  );
}
