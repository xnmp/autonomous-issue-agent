/**
 * View preferences the operator sets by clicking, remembered across reloads.
 *
 * Storage is best-effort by design: a browser with storage disabled (or a
 * quota-full one) still gets a fully working dashboard for the session, so
 * every read and write here swallows its own failure rather than propagating.
 */

export function readFlag(key: string, fallback: boolean): boolean {
  try {
    const raw = localStorage.getItem(key);
    return raw === null ? fallback : raw === "true";
  } catch {
    return fallback;
  }
}

/** A stored JSON object, or `{}` when the entry is absent, blank, truncated or not an object. */
export function readRecord<T = unknown>(key: string): Record<string, T> {
  try {
    const parsed: unknown = JSON.parse(localStorage.getItem(key) || "{}");
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? (parsed as Record<string, T>) : {};
  } catch {
    return {};
  }
}

export function writeFlag(key: string, value: boolean): void {
  try {
    localStorage.setItem(key, String(value));
  } catch {
    // The preference stays in memory for this session.
  }
}

/** A stored finite, positive number, or null when absent, blank, non-numeric or unreadable. */
export function readTimestamp(key: string): number | null {
  try {
    const raw = localStorage.getItem(key);
    if (raw === null || raw.trim() === "") return null;
    const n = Number(raw);
    return Number.isFinite(n) && n > 0 ? n : null;
  } catch {
    return null;
  }
}

export function writeTimestamp(key: string, value: number): void {
  try {
    localStorage.setItem(key, String(value));
  } catch {
    // The watermark stays in memory for this session.
  }
}
