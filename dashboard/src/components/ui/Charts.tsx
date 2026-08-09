import type { ReactNode } from "react";
import type { Tone } from "../../lib/semantics";
import { utilTone } from "../../lib/semantics";
import { pct as fmtPct, usd } from "../../lib/format";
import { tipProps } from "./Tip";

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
  onClick,
  primary = false,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  tone?: Tone;
  alert?: boolean;
  title?: string;
  onClick?: () => void;
  primary?: boolean;
}) {
  const content = (
    <>
      <div className="tile-label">
        <span className={`tile-signal tone-bg-${alert ? "fail" : tone}`} aria-hidden="true" />
        {label}
      </div>
      <div className={`tile-value tone-${alert ? "fail" : tone}`}>{value}</div>
      {sub ? <div className="tile-sub faint">{sub}</div> : null}
    </>
  );
  if (onClick) {
    return (
      <button
        className={`tile tile-action${primary ? " tile-primary" : ""}${alert ? " tile-alert" : ""}`}
        {...tipProps(title)}
        onClick={onClick}
      >
        {content}
      </button>
    );
  }
  return <div className={`tile${primary ? " tile-primary" : ""}${alert ? " tile-alert" : ""}`} {...tipProps(title)}>{content}</div>;
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

/* ----------------------------------------------------------- Histogram ---
 * One series over evenly spaced, dense buckets (spend per hour). Two quiet,
 * directly labelled gridlines make magnitudes comparable without turning the
 * compact plot into graph paper. A fixed slot colour (a single series never
 * cycles) and a value in every bucket's tooltip carry the series. Empty
 * buckets render as a gap ON the baseline rather than
 * as a hairline — the series is dense precisely so a quiet night cannot be
 * re-spaced into a busy one, and a stub bar would read as a small charge.
 */

export interface HistogramBar {
  key: string;
  /** the bucket's own name — appears in its tooltip and on the axis */
  label: string;
  value: number;
}

export function Histogram({
  bars,
  formatValue = (v: number) => usd(v),
  emptyText = "nothing recorded in this window",
  varName = "--series-1",
  unit = "",
}: {
  bars: HistogramBar[];
  formatValue?: (v: number) => string;
  emptyText?: string;
  varName?: string;
  /** appended to the bucket tooltip's subject, e.g. "spend" */
  unit?: string;
}) {
  const max = bars.reduce((a, b) => Math.max(a, b.value), 0);
  // An all-zero series has no shape to show; collapse to the empty line rather
  // than drawing a flat baseline that looks like a broken chart.
  if (!(max > 0)) return <div className="empty empty-inline">{emptyText}</div>;

  const total = bars.reduce((a, b) => a + b.value, 0);
  const peak = bars.reduce((best, b) => (b.value > best.value ? b : best), bars[0]!);
  const subject = unit ? `${unit} ` : "";

  return (
    <div className="hist">
      <div
        className="hist-plot"
        role="img"
        aria-label={`${subject}across ${bars.length} buckets from ${bars[0]!.label} to ${bars[bars.length - 1]!.label}: ${formatValue(total)} in total, peaking at ${formatValue(peak.value)} in ${peak.label}`}
      >
        <div className="hist-grid" aria-hidden="true">
          {[1 / 3, 2 / 3].map((ratio) => (
            <span
              className="hist-gridline"
              key={ratio}
              style={{ bottom: `${ratio * 100}%` }}
            >
              <span className="hist-grid-label num">{formatValue(max * ratio)}</span>
            </span>
          ))}
        </div>
        {bars.map((bar) => (
          <div
            key={bar.key}
            className="hist-col"
            title={`${bar.label} · ${formatValue(bar.value)}`}
            {...tipProps(`${bar.label} · ${formatValue(bar.value)} spent`)}
          >
            {bar.value > 0 ? (
              <div
                className="hist-bar"
                style={{
                  height: `${(bar.value / max) * 100}%`,
                  background: `var(${varName})`,
                }}
              />
            ) : null}
          </div>
        ))}
      </div>
      <div className="hist-axis faint">
        <span>{bars[0]!.label}</span>
        <span className="hist-axis-peak num">peak {formatValue(max)}</span>
        <span>{bars[bars.length - 1]!.label}</span>
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
