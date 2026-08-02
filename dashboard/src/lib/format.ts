/** Formatting helpers. Every one is null-tolerant — the backend degrades to
 * null on any collector failure, and "—" is a better cell than "NaN". */

export const DASH = "—";

export function usd(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return DASH;
  return `$${v.toFixed(digits)}`;
}

/** Compact money for stat tiles: $1.2K past four figures. */
export function usdCompact(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return DASH;
  if (Math.abs(v) >= 1000) return `$${(v / 1000).toFixed(1)}K`;
  return `$${v.toFixed(2)}`;
}

export function pct(v: number | null | undefined, digits = 0): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return DASH;
  return `${v.toFixed(digits)}%`;
}

/** A 0..1 rate rendered as a percentage. */
export function rate(v: number | null | undefined, digits = 0): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return DASH;
  return `${(v * 100).toFixed(digits)}%`;
}

export function num(v: number | null | undefined, digits = 0): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return DASH;
  return v.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function tokens(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return DASH;
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`;
  if (v >= 1_000) return `${(v / 1_000).toFixed(1)}K`;
  return String(v);
}

/** Seconds → `1:04:12` / `4:12`; minute precision omits seconds entirely. */
export function duration(
  s: number | null | undefined,
  showSeconds = true,
): string {
  if (s === null || s === undefined || !Number.isFinite(s) || s < 0) return DASH;
  const total = Math.floor(s);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const sec = total % 60;
  if (!showSeconds) {
    if (h > 0) return `${h}:${String(m).padStart(2, "0")}`;
    return m > 0 ? `${m}m` : "<1m";
  }
  if (h > 0) {
    return `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
  }
  return `${m}:${String(sec).padStart(2, "0")}`;
}

export function parseTs(ts: string | null | undefined): number | null {
  if (!ts) return null;
  // The backend emits ISO-8601 UTC; tolerate a missing zone designator.
  const withZone = /[zZ]|[+-]\d{2}:?\d{2}$/.test(ts) ? ts : `${ts}Z`;
  const ms = Date.parse(withZone);
  return Number.isFinite(ms) ? ms : null;
}

export function ago(ts: string | null | undefined, nowMs = Date.now()): string {
  const ms = parseTs(ts);
  if (ms === null) return DASH;
  const s = Math.max(0, (nowMs - ms) / 1000);
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

/** Compact age with calendar-scale units before clock-scale units. */
export function ageCompact(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds < 0) {
    return DASH;
  }
  if (seconds >= 30 * 86400) return `${Math.floor(seconds / (30 * 86400))}mo`;
  if (seconds >= 7 * 86400) return `${Math.floor(seconds / (7 * 86400))}w`;
  if (seconds >= 86400) return `${Math.floor(seconds / 86400)}d`;
  if (seconds >= 3600) return `${Math.floor(seconds / 3600)}h`;
  if (seconds >= 60) return `${Math.floor(seconds / 60)}m`;
  return `${Math.floor(seconds)}s`;
}

export function untilLabel(ts: string | null | undefined, nowMs = Date.now()): string {
  const ms = parseTs(ts);
  if (ms === null) return DASH;
  const s = (ms - nowMs) / 1000;
  if (s <= 0) return "now";
  if (s < 3600) return `${Math.ceil(s / 60)}m`;
  if (s < 86400) return `${(s / 3600).toFixed(1)}h`;
  return `${(s / 86400).toFixed(1)}d`;
}

export function localTime(ts: string | null | undefined): string {
  const ms = parseTs(ts);
  if (ms === null) return DASH;
  return new Date(ms).toLocaleString(undefined, {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

export function localDate(ts: string | null | undefined): string {
  const ms = parseTs(ts);
  if (ms === null) return DASH;
  return new Date(ms).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
  });
}

/** An hour bucket in the reader's own timezone: `Aug 02, 14:00`. */
export function localHour(ts: string | null | undefined): string {
  const ms = parseTs(ts);
  if (ms === null) return DASH;
  const at = new Date(ms);
  const day = at.toLocaleDateString(undefined, { month: "short", day: "2-digit" });
  const hour = String(at.getHours()).padStart(2, "0");
  return `${day}, ${hour}:00`;
}

export function clockTime(ts: string | null | undefined): string {
  const ms = parseTs(ts);
  if (ms === null) return DASH;
  return new Date(ms).toLocaleTimeString(undefined, { hour12: false });
}

export function truncate(s: string, max: number): string {
  return s.length <= max ? s : `${s.slice(0, max - 1)}…`;
}
