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
      {events.map((e, i) => (
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
      ))}
    </ol>
  );
}
