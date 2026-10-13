import { useEffect, useState } from "react";
import type { DoneCard } from "../../api/types";
import { parseTs } from "../../lib/format";
import { readTimestamp, writeTimestamp } from "../../lib/prefs";

const key = (repo: string) => `done-seen:${repo}`;

/** Done time in ms; null (never new) when the card has no parseable timestamp. */
const doneAt = (card: DoneCard) => parseTs(card.merged_ts);

/** Cards done strictly after the watermark. */
export function newerThan(cards: readonly DoneCard[], watermark: number): DoneCard[] {
  return cards.filter((card) => {
    const at = doneAt(card);
    return at !== null && at > watermark;
  });
}

interface State {
  repo: string;
  /** Read-watermark (ms). Everything done at or before it has been seen. */
  watermark: number;
  /** Issues unread on entry to the Done tab, frozen for the visit; null while away. */
  frozen: ReadonlySet<number> | null;
}

// First ever visit (nothing stored) starts at "now", so nothing is new.
const init = (repo: string): State => ({
  repo,
  watermark: readTimestamp(key(repo)) ?? Date.now(),
  frozen: null,
});

export interface DoneUnread {
  /** Header badge: done items newer than the watermark; 0 while the tab is open. */
  count: number;
  /** Issues to render as unread; non-empty only while the tab is open. */
  unread: ReadonlySet<number>;
}

const NONE: ReadonlySet<number> = new Set();

/**
 * Inbox-style read state for the Done list, per repo and per viewer.
 *
 * Opening the tab freezes the unread set (so the user sees what arrived) and
 * advances the watermark to the newest done time seen. Items that arrive while
 * the tab stays open are seen immediately: they advance the watermark but are
 * not added to the frozen set. On a repo switch the first render may use that
 * repo's cached poll; items the refresh then adds count as arrivals. Counts cover only the cards loaded (page 1 of
 * the server's newest-first list), which is where new items sort anyway.
 */
export function useDoneUnread(
  repo: string | null,
  cards: readonly DoneCard[] | undefined,
  viewing: boolean,
): DoneUnread {
  const [state, setState] = useState<State | null>(() => (repo ? init(repo) : null));
  let cur = state;
  if (repo === null) cur = null;
  else if (cur?.repo !== repo) cur = init(repo);

  // Derived-state transitions happen during render (no effect, no extra paint).
  if (cur && !viewing && cur.frozen !== null) cur = { ...cur, frozen: null };
  if (cur && viewing && cards) {
    // Clamped to now so a future-dated server timestamp cannot park the
    // watermark ahead of real time and hide later arrivals.
    const newest = Math.max(cur.watermark, Math.min(Date.now(), ...cards.map((c) => doneAt(c) ?? 0).sort((x, y) => y - x).slice(0, 1)));
    if (cur.frozen === null) {
      cur = { ...cur, frozen: new Set(newerThan(cards, cur.watermark).map((c) => c.issue)), watermark: newest };
    } else if (newest !== cur.watermark) {
      cur = { ...cur, watermark: newest };
    }
  }
  if (cur !== state) setState(cur);

  const persisted = cur?.watermark;
  useEffect(() => {
    // Never lower a watermark another browser tab has raised.
    if (repo !== null && persisted !== undefined) {
      writeTimestamp(key(repo), Math.max(persisted, readTimestamp(key(repo)) ?? 0));
    }
  }, [repo, persisted]);

  if (!cur) return { count: 0, unread: NONE };
  return {
    count: viewing || !cards ? 0 : newerThan(cards, cur.watermark).length,
    unread: viewing && cur.frozen ? cur.frozen : NONE,
  };
}
