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

export function writeFlag(key: string, value: boolean): void {
  try {
    localStorage.setItem(key, String(value));
  } catch {
    // The preference stays in memory for this session.
  }
}
