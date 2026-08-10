import type { JournalEvent } from "../api/types";
import { clockTime, localTime, truncate } from "../lib/format";
import { statusTone, type Tone } from "../lib/semantics";
import { Chip } from "./ui/Chip";
import { Collapsible, JsonBlock } from "./ui/Json";
import { EmptyState } from "./ui/Feedback";

/** Fields worth surfacing inline; everything else lives in the raw JSON. */
const KEY_FIELDS = [
  "status",
  "exit_reason",
  "model",
  "cost_usd",
  "duration_s",
  "turns",
  "pr",
  "number",
  "issue",
  "reason",
  "detail",
  "message",
  "label",
  "session_id",
  "desc",
];

const NOISE = new Set(["ts", "kind", "event", "role"]);

function eventKind(e: JournalEvent): string {
  return String(e.kind ?? e.event ?? "event");
}

function toneFor(e: JournalEvent): Tone {
  const k = eventKind(e).toLowerCase();
  if (/(fail|error|block|escalat|dead|abort)/.test(k)) return "fail";
  if (/(start|running|tick)/.test(k)) return "running";
  if (/(done|merged|approve|settled|complete)/.test(k)) return "ok";
  if (/(recurrence|triage|jira|steward)/.test(k)) return "meta";
  const st = typeof e.status === "string" ? e.status : null;
  return st ? statusTone(st) : "info";
}

function summarize(e: JournalEvent): { k: string; v: string }[] {
  const out: { k: string; v: string }[] = [];
  for (const key of KEY_FIELDS) {
    const v = e[key];
    if (v === undefined || v === null || v === "") continue;
    if (typeof v === "object") continue;
    out.push({ k: key, v: truncate(String(v), 80) });
  }
  if (out.length === 0) {
    for (const [k, v] of Object.entries(e)) {
      if (NOISE.has(k) || v === null || typeof v === "object") continue;
      out.push({ k, v: truncate(String(v), 80) });
      if (out.length >= 4) break;
    }
  }
  return out;
}

/** Observation heartbeats: individually meaningless, collectively noise. */
function isObservation(e: JournalEvent): boolean {
  return /world\.observed/.test(eventKind(e));
}

type TimelineEntry =
  | { grouped: false; event: JournalEvent }
  | { grouped: true; events: JournalEvent[] };

type FactorizedTimelineEntry =
  | TimelineEntry
  | {
      cycleKinds: string[];
      events: JournalEvent[];
      repetitions: number;
    };

/**
 * Fold consecutive world-observed heartbeats into one entry. A lone
 * observation stays a plain item; only runs collapse, so an observation that
 * sits between two real transitions still reads in place.
 */
export function foldObservations(events: JournalEvent[]): TimelineEntry[] {
  const out: TimelineEntry[] = [];
  for (const e of events) {
    const last = out[out.length - 1];
    if (isObservation(e)) {
      if (last?.grouped) last.events.push(e);
      else out.push({ grouped: true, events: [e] });
    } else {
      out.push({ grouped: false, event: e });
    }
  }
  return out.map((entry) => {
    if (!entry.grouped || entry.events.length !== 1) return entry;
    const only = entry.events[0];
    return only ? { grouped: false as const, event: only } : entry;
  });
}

/**
 * Collapse adjacent runs whose event-kind period is one or two. Greedy
 * left-to-right: a same-kind run is taken before an overlapping pair cycle
 * is considered (A A B A B groups "A ×2" and leaves B A B alone), so the
 * result is not globally maximal — acceptable for a readability fold.
 */
function foldRepeatedCycles(entries: TimelineEntry[]): FactorizedTimelineEntry[] {
  const out: FactorizedTimelineEntry[] = [];
  let i = 0;
  while (i < entries.length) {
    const first = entries[i];
    if (!first || first.grouped) {
      if (first) out.push(first);
      i += 1;
      continue;
    }

    const firstKind = eventKind(first.event);
    let sameKindCount = 1;
    while (i + sameKindCount < entries.length) {
      const next = entries[i + sameKindCount];
      if (!next || next.grouped || eventKind(next.event) !== firstKind) break;
      sameKindCount += 1;
    }
    if (sameKindCount >= 2) {
      const run = entries.slice(i, i + sameKindCount) as { grouped: false; event: JournalEvent }[];
      out.push({
        cycleKinds: [firstKind],
        events: run.map((entry) => entry.event),
        repetitions: sameKindCount,
      });
      i += sameKindCount;
      continue;
    }

    const second = entries[i + 1];
    if (!second || second.grouped) {
      out.push(first);
      i += 1;
      continue;
    }
    const secondKind = eventKind(second.event);
    let repetitions = 1;
    while (i + repetitions * 2 + 1 < entries.length) {
      const nextFirst = entries[i + repetitions * 2];
      const nextSecond = entries[i + repetitions * 2 + 1];
      if (
        !nextFirst ||
        nextFirst.grouped ||
        !nextSecond ||
        nextSecond.grouped ||
        eventKind(nextFirst.event) !== firstKind ||
        eventKind(nextSecond.event) !== secondKind
      ) {
        break;
      }
      repetitions += 1;
    }
    if (firstKind !== secondKind && repetitions >= 2) {
      const length = repetitions * 2;
      const run = entries.slice(i, i + length) as { grouped: false; event: JournalEvent }[];
      out.push({
        cycleKinds: [firstKind, secondKind],
        events: run.map((entry) => entry.event),
        repetitions,
      });
      i += length;
      continue;
    }

    out.push(first);
    i += 1;
  }
  return out;
}

function ObservationGroup({ events }: { events: JournalEvent[] }) {
  const first = events[0];
  const last = events[events.length - 1] ?? first;
  if (!first || !last) return null;
  return (
    <li className="tl-item tl-item-grouped">
      <span className="tl-time mono faint" title={localTime(first.ts)}>
        {clockTime(first.ts)}
      </span>
      <span className="tl-rail" aria-hidden="true" />
      <div className="tl-body">
        <div className="row-wrap">
          <Chip tone="meta" square>
            {eventKind(first)} ×{events.length}
          </Chip>
          <span className="faint">
            {clockTime(first.ts)} – {clockTime(last.ts)}
          </span>
        </div>
        <Collapsible title={<span className="faint">raw ({events.length})</span>}>
          <JsonBlock value={events} />
        </Collapsible>
      </div>
    </li>
  );
}

function RepeatedCycleGroup({
  cycleKinds,
  events,
  repetitions,
}: {
  cycleKinds: string[];
  events: JournalEvent[];
  repetitions: number;
}) {
  const first = events[0];
  const last = events[events.length - 1] ?? first;
  if (!first || !last) return null;
  const cycle = cycleKinds.length === 1 ? cycleKinds[0] : `(${cycleKinds.join(" / ")})`;
  return (
    <li className="tl-item tl-item-grouped">
      <span className="tl-time mono faint" title={localTime(first.ts)}>
        {clockTime(first.ts)}
      </span>
      <span className="tl-rail" aria-hidden="true" />
      <div className="tl-body">
        <div className="row-wrap">
          <Chip tone={toneFor(first)} square>
            {cycle} ×{repetitions}
          </Chip>
          <span className="faint">
            {clockTime(first.ts)} – {clockTime(last.ts)}
          </span>
        </div>
        <Collapsible title={<span className="faint">raw ({events.length})</span>}>
          <JsonBlock value={events} />
        </Collapsible>
      </div>
    </li>
  );
}

/**
 * Journal events as a readable timeline: timestamp, kind chip, the handful of
 * fields that carry meaning, and the raw record one click away. The raw JSON is
 * always reachable — the summariser is a convenience, never a filter that hides
 * something the operator needs during an incident.
 */
export function EventTimeline({
  events,
  emptyText = "no events in this window",
  dense,
}: {
  events: JournalEvent[];
  emptyText?: string;
  dense?: boolean;
}) {
  if (events.length === 0) return <EmptyState>{emptyText}</EmptyState>;
  return (
    <ol className={`timeline${dense ? " dense" : ""}`}>
      {foldRepeatedCycles(foldObservations(events)).map((entry, i) => {
        if ("cycleKinds" in entry) {
          return (
            <RepeatedCycleGroup
              key={i}
              cycleKinds={entry.cycleKinds}
              events={entry.events}
              repetitions={entry.repetitions}
            />
          );
        }
        if (entry.grouped) return <ObservationGroup key={i} events={entry.events} />;
        const e = entry.event;
        return (
        <li key={i} className="tl-item">
          <span className="tl-time mono faint" title={localTime(e.ts)}>
            {clockTime(e.ts)}
          </span>
          <span className="tl-rail" aria-hidden="true" />
          <div className="tl-body">
            <div className="row-wrap">
              <Chip tone={toneFor(e)} square>
                {eventKind(e)}
              </Chip>
              {e.role ? (
                <span className="faint">
                  by <strong>{String(e.role)}</strong>
                </span>
              ) : null}
              {summarize(e).map((f) => (
                <span key={f.k} className="kv">
                  <span className="kv-k faint">{f.k}</span>
                  <span className="kv-v mono">{f.v}</span>
                </span>
              ))}
            </div>
            <Collapsible title={<span className="faint">raw</span>}>
              <JsonBlock value={e} />
            </Collapsible>
          </div>
        </li>
        );
      })}
    </ol>
  );
}
