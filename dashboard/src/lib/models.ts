/** Keep model chips narrow; the full identifier remains in the title. */
export function shortModel(model: string): string {
  return model
    .replace(/^(claude|openai|anthropic)[-/]/, "")
    .replace(/\[1m\]$/, "");
}

/**
 * The reasoning-effort ladder, weakest → strongest.
 *
 * `domain/effort.py` mints exactly low/medium/high, but the wire words are
 * whatever the runtime settled with, and operator knobs are free text — codex
 * has emitted `minimal` and `xhigh`, and receipts predate the enum. Ordering is
 * written down once here so no component re-derives it, and lexical comparison
 * (which orders "high" < "low" < "medium", exactly backwards) never happens.
 */
const EFFORT_LADDER: readonly string[] = [
  "minimal",
  "low",
  "medium",
  "high",
  "xhigh",
  "max",
  "ultra",
];

const HIGH_EFFORT_RANK = EFFORT_LADDER.indexOf("high");

/** Position on the ladder, or null for anything not on it. */
export function effortRank(effort: string | null | undefined): number | null {
  if (typeof effort !== "string") return null;
  const rank = EFFORT_LADDER.indexOf(effort.trim().toLowerCase());
  return rank < 0 ? null : rank;
}

/**
 * Is this leg thinking at `high` or above? An unknown word is deliberately NOT
 * elevated: a typo in `FIX_EFFORT` must not paint the board with alarm colour.
 */
export function isElevatedEffort(effort: string | null | undefined): boolean {
  const rank = effortRank(effort);
  return rank !== null && rank >= HIGH_EFFORT_RANK;
}
