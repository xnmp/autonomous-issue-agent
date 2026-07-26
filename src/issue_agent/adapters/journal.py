"""JSONL event journal + spend ledger — the harness's flight recorder.

Two append-only JSONL files, one line per event:

  * the per-role **journal** (`<STATE_DIR>/journal.jsonl`) — every agent run,
    every applied action, every escalation, every spend block, every skipped
    tick. This is what makes "what did the harness actually DO last night?"
    answerable without re-reading a rotated stderr log.
  * the global **spend ledger** (`$XDG_STATE_HOME/issue-agent/spend.jsonl`) —
    one `spend` line per costed run, shared by EVERY repo and role on the box.
    Cost is the one resource that is genuinely global: three daemons across four
    repos draw on one billing account, so a per-repo cap cannot see the sum.

Design rules, both hard-won:

1. **Observability must never add a failure mode.** `Journal.emit` catches
   everything and degrades to a single stderr line. A daemon that dies because
   its log directory went read-only has traded a working harness for a diary.
2. **Readers tolerate garbage.** A process killed mid-`write()` leaves a
   truncated final line; a reader that raises on it turns one crash into a
   permanently unreadable ledger. Malformed lines are skipped in silence.

Append atomicity is POSIX's: a single `write()` of a short line to a file opened
`O_APPEND` is not interleaved with other appenders, so three daemons may share
one ledger without locking. Hence `emit` builds the whole line first and writes
it exactly once — never `write(line)` then `write("\\n")`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..domain.agent_task import AgentTask
from ..domain.outcome import AgentResult, ExitReason

# The four SDK usage counters worth keeping. `cost_usd` is the gateway's dollar
# figure, which is 0.00 on a subscription plan and absent on Codex — tokens are
# the substrate that always exists, so they are captured verbatim.
_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(now_fn=_now) -> str:
    """UTC ISO-8601 at SECONDS resolution. Sub-second precision buys nothing for
    a poll loop that ticks every 20s, and a fixed-width stamp keeps the file
    greppable by date prefix (`grep '"ts": "2026-07-25'`)."""
    return now_fn().replace(microsecond=0).isoformat()


def event_line(event: str, now_fn=_now, **fields) -> str:
    """Serialise one event to its JSONL line (no trailing newline).

    Shared by `Journal.emit` and the alert hook so an alert payload is byte-for-
    byte the line that landed in the journal. `default=str` means a stray enum or
    Path degrades to its repr instead of raising; `sort_keys` keeps diffs of two
    journals meaningful.
    """
    return json.dumps(
        {"ts": _stamp(now_fn), "event": event, **fields}, default=str, sort_keys=True
    )


@dataclass(frozen=True)
class Journal:
    """Append-only JSONL sink at `path`. Frozen: a journal is its path."""

    path: str

    def emit(self, event: str, **fields) -> None:
        """Append one event line. NEVER raises — see rule 1 in the module
        docstring. A failure costs one stderr line and the event, nothing more."""
        try:
            line = event_line(event, **fields)
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")  # ONE write: POSIX append atomicity
        except Exception as exc:  # noqa: BLE001 — observability is never fatal
            sys.stderr.write(f"journal: dropped {event!r} event ({exc})\n")


def default_ledger_path() -> str:
    """The GLOBAL spend ledger every role on this machine appends to. Global by
    construction: one billing account, one file, so a daily cap can see the true
    sum instead of each repo's slice of it."""
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "issue-agent", "spend.jsonl")


# ── alert hook ────────────────────────────────────────────────────────────────
def alert(cmd: str, event_json: str) -> None:
    """Pipe `event_json` to `cmd` via `sh -c` (blank cmd → no-op).

    Deliberately a shell string, not an executable + argv: the operator's alert
    is invariably a pipeline (`jq -r .event | mail -s harness me@…`), and making
    them wrap it in a script file is friction that ends with alerts unconfigured.
    Bounded at 10s and swallows every failure — an unreachable pager must not
    wedge the poll loop, and the event is already durably journalled.

    The payload is newline-TERMINATED: every plausible alert sink is line
    oriented (`tee -a alerts.jsonl`, `while read`, `jq`), and two unterminated
    events appended to one file concatenate into a line nothing can parse.
    """
    if not (cmd or "").strip():
        return
    try:
        subprocess.run(
            ["/bin/sh", "-c", cmd],
            input=event_json if event_json.endswith("\n") else event_json + "\n",
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 — an alert that fails stays failed
        sys.stderr.write(f"journal: alert command failed ({exc})\n")


# ── journaling runtime decorator ──────────────────────────────────────────────
@dataclass(frozen=True)
class RunContext:
    """Which dispatch a run belongs to. Set once per dispatch; a multi-leg review
    (gate → dimensions → validation) emits one `agent_run` per leg all carrying
    the SAME context, which is per-leg cost attribution for free.

    `dispatch_id` is what makes those legs identifiable AS one dispatch. Minted
    here rather than passed in, so no call site has to thread it: the dispatcher
    already calls `set_context` exactly once per dispatch, which is the same
    boundary. Without it, "how many dispatches never settled?" is unanswerable —
    a daemon killed mid-fan-out leaves six unsettled starts for ONE dead review,
    and a ceiling of five would hand the PR to a human after a single
    `systemctl restart` (see `unsettled_starts`).
    """

    role: str = ""
    kind: str = ""
    number: int = 0
    issue: int | None = None
    dispatch_id: str = ""


class JournalingRuntime:
    """An `AgentRuntime` that wraps another and records what each run cost.

    A decorator rather than a change to `AgentRunner`/`CodexRunner` because both
    runtimes need it identically and neither should learn about journals: the
    port stays `run(task) -> AgentResult`, and the cost ledger becomes a
    composition concern that `roles.py` wires in one place.
    """

    def __init__(
        self,
        inner,
        journal: Journal,
        ledger_path: str = "",
        repo: str = "",
        role: str = "",
    ):
        self.inner = inner
        self.journal = journal
        self.ledger = Journal(ledger_path) if ledger_path else None
        self.repo = repo
        self.role = role
        self.context = RunContext(role=role)

    def set_context(self, role: str, kind: str, number: int, issue: int | None) -> None:
        """Called by the dispatcher immediately before it hands off to a handler.
        Mutable state on purpose: threading a context through every workflow
        signature would touch a dozen call sites to carry a field only the
        journal reads.

        Each call mints a fresh `dispatch_id`; every leg the handler runs
        inherits it. Random rather than sequential because the counter's whole
        purpose is surviving a process death, and a per-process sequence
        restarts at 0 with the daemon."""
        self.context = RunContext(
            role=role, kind=kind, number=number, issue=issue,
            dispatch_id=uuid.uuid4().hex[:12],
        )

    def __getattr__(self, name: str):
        """Delegate anything this decorator does not define to the wrapped
        runtime. Without it (review S7) a decorated runtime silently loses the
        inner surface callers use — e.g. `roles.py`'s startup
        `fix_runner.preflight()` codex readiness check would become an
        AttributeError (or, behind a `getattr(..., None)` probe, a silent
        no-op) the moment the wrap order changed.

        Dunders are NOT delegated: implicit special-method lookup bypasses
        `__getattr__` anyway, so delegating them only produces half-working
        protocols (`__await__`, copy/pickle) that lie about what this object is.
        `inner` is read off `__dict__` directly so a half-initialised instance
        raises AttributeError instead of recursing forever.
        """
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        try:
            inner = object.__getattribute__(self, "inner")
        except AttributeError:
            raise AttributeError(name) from None
        return getattr(inner, name)

    async def run(self, task: AgentTask) -> AgentResult:
        """Journal the dispatch BEFORE the await and the settlement after it —
        including when the await raises.

        Review C3: `AgentRunner` deliberately *raises* on a pre-result transport
        death (`agent_runner.py:522-531`), which used to skip every
        attempt-recording write — no journal line, no ledger line — while
        gh-side reaping requeued the issue and re-dispatched it next poll. One
        full paid session every two polls, forever, with `$0.00` on the ledger
        and nothing on `issue-agent status`. So: `agent_run_start` first (it is
        the only durable trace if this process is killed mid-run), and the raise
        path gets its own settled line before the exception continues upward
        UNCHANGED — the crash isolation the raise buys (`Daemon.tick` catches,
        issue keeps `ai-working`, reaper requeues) must not change.
        """
        self._record_start(task)
        t0 = time.monotonic()
        try:
            result = await self.inner.run(task)
        except BaseException as exc:  # noqa: BLE001 — recorded, never handled
            # BaseException, not Exception: a cancellation or Ctrl-C mid-run
            # leaves exactly the same requeue-and-re-dispatch signature, so it
            # deserves the same receipt. The bare `raise` preserves the
            # exception object and its traceback verbatim.
            self._record(
                task,
                AgentResult(
                    is_error=True,
                    status="raised",
                    cost_usd=0.0,
                    exit_reason=ExitReason.TRANSPORT_DEATH,
                ),
                time.monotonic() - t0,
                error=f"{type(exc).__name__}: {str(exc)[:200]}",
            )
            raise
        self._record(task, result, time.monotonic() - t0)
        return result

    def _record_start(self, task: AgentTask) -> None:
        """The pre-await half of the pair. Carries the same
        role/kind/number/issue as the settled line so the two correlate."""
        try:
            ctx = self.context
            self.journal.emit(
                "agent_run_start",
                role=ctx.role,
                kind=ctx.kind,
                number=ctx.number,
                issue=ctx.issue,
                dispatch=ctx.dispatch_id,
                model=task.model or "",
            )
        except Exception as exc:  # noqa: BLE001 — never fail a run over its receipt
            sys.stderr.write(f"journal: could not record agent_run_start ({exc})\n")

    def _record(
        self, task: AgentTask, result: AgentResult, duration_s: float, **extra
    ) -> None:
        try:
            ctx = self.context
            cost = float(getattr(result, "cost_usd", 0.0) or 0.0)
            usage = _usage(result.usage)
            # An ABORTED run (wall cap, idle, dropped result) never receives the
            # ResultMessage that carries dollars, so `cost` is 0.0 however long it
            # ran. Observed on the first real deployment: 906s and four commits
            # ledgered as $0.00 — meaning repeated timeouts accumulate real spend
            # that BOTH caps are structurally blind to. Price the tokens instead,
            # at a rate learned from this journal's own completed runs.
            #
            # The estimate lands in its own field and NEVER in `cost_usd`, so it
            # cannot become evidence for the next estimate (see
            # `observed_cost_per_token`). The ledger — what the caps actually read
            # — takes the true cost when there is one, else the estimate.
            estimated = 0.0
            if cost <= 0 and total_tokens(usage) > 0:
                estimated = estimate_cost_usd(
                    usage, observed_cost_per_token(read_events_tail(self.journal.path))
                )
            self.journal.emit(
                "agent_run",
                role=ctx.role,
                kind=ctx.kind,
                number=ctx.number,
                issue=ctx.issue,
                dispatch=ctx.dispatch_id,
                model=task.model or "",
                duration_s=round(duration_s, 1),
                cost_usd=cost,
                cost_estimated_usd=(estimated or None),
                status=result.status,
                # The envelope's own kind, alongside the agent's claimed `status`.
                # Without it, an errored run that self-declared "success" (#501)
                # leaves nothing in the journal to explain WHY it errored.
                envelope_subtype=(getattr(result, "envelope_subtype", "") or None),
                is_error=result.is_error,
                exit_reason=(result.exit_reason.value if result.exit_reason else None),
                session_id=result.session_id,
                usage=usage,
                **extra,
            )
            # Only costed runs reach the ledger: a $0.00 line is pure noise in the
            # file the spend caps scan on every dispatch. An estimated cost is not
            # noise — it is the only figure an aborted run will ever have — so it
            # counts, flagged so an auditor can tell measured from inferred.
            billed = cost if cost > 0 else estimated
            if self.ledger is not None and billed > 0:
                self.ledger.emit(
                    "spend", repo=self.repo, issue=ctx.issue, kind=ctx.kind,
                    cost_usd=billed, estimated=(cost <= 0) or None, usage=usage,
                )
        except Exception as exc:  # noqa: BLE001 — never fail a run over its receipt
            sys.stderr.write(f"journal: could not record agent_run ({exc})\n")


def _usage(raw) -> dict | None:
    """Normalise the SDK usage block to the four counters we keep. A non-dict
    (Codex reports none) → None, so 'no usage' and 'zero tokens' stay distinct."""
    if not isinstance(raw, dict):
        return None
    return {k: raw.get(k, 0) for k in _USAGE_KEYS}


# ── readers (pure over the file's bytes; tolerate a truncated tail) ───────────
def _parse_lines(lines) -> list[dict]:
    events: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue  # truncated/garbled line — skip, never poison the read
        if isinstance(obj, dict):
            events.append(obj)
    return events


def read_events(path: str) -> list[dict]:
    """Every parseable JSON object in `path`, oldest first. A missing file is an
    empty history, not an error — the first status check runs before any daemon
    has written a line."""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    return _parse_lines(lines)


# How much journal tail a per-poll reader looks at. 1 MiB is ~3-4k events (a
# settled `agent_run` line is ~300 bytes) — days of a busy daemon — and it is
# read on EVERY poll, so the cost of the scan must not grow with the age of the
# install. Bytes rather than lines because a byte offset is one `seek`, whereas
# "the last N lines" of a growing file is a full read.
_TAIL_BYTES = 1 << 20


def read_events_tail(path: str, max_bytes: int = _TAIL_BYTES) -> list[dict]:
    """`read_events` over at most the LAST `max_bytes` of the file.

    For the readers that run on the poll loop rather than on an operator's
    `status` command. The first line of the window is dropped whenever the
    window did not start at byte 0, because a mid-line seek would otherwise hand
    the parser a truncated object — the same tolerance rule as a killed writer's
    torn tail, applied to the read side.
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            start = max(0, fh.tell() - max(0, max_bytes))
            fh.seek(start)
            blob = fh.read()
    except OSError:
        return []
    text = blob.decode("utf-8", errors="replace")
    if start > 0:
        _, _, text = text.partition("\n")  # discard the partial leading line
    return _parse_lines(text.splitlines())


def death_key(event: dict) -> int | None:
    """The `infra-deaths` counter key an event's dispatch would have been
    charged to, or None if the event names no usable target.

    Mirrors `AgentDispatcher.dispatch`'s `key = issue if issue is not None else
    -action.number` exactly — including `domain.fix.breaker_key`'s negated-PR
    convention for a PR with no resolvable issue — because the whole point of
    `unsettled_starts` is to feed the SAME per-key ceiling the surviving-process
    bump feeds. Duplicated as a convention (a one-line formula) rather than
    imported: adapters must not depend on app.
    """
    issue = _int(event.get("issue"))
    if issue is not None:
        return issue
    number = _int(event.get("number"))
    return None if number is None else -number


def unsettled_starts(
    events, window_s: float = 86400.0, now_fn=_now
) -> dict[int, int]:
    """Paid agent runs that never reached ANY settlement, per `death_key`.

    Review P0R-4: every place that counts a dead session requires the daemon to
    survive that session. A SIGKILL, an OOM kill or a `systemctl restart`
    mid-`await` leaves an `agent_run_start` and nothing else — no settled line,
    no ledger line, no counter — while systemd's `Restart=always` brings the
    daemon back to re-dispatch the very same work. One full paid session per
    (RestartSec + session length), forever, invisible to every breaker. This is
    the read that makes those deaths countable WITHOUT adding a write on the
    death path (there is no such write to add: the process is already gone).

    **Matching rule.** Group by `dispatch` — the id `set_context` mints once per
    dispatch and every leg inherits — and count a group as ONE death when it has
    more starts than settled runs. Then, for pre-`dispatch` journals, fall back
    to grouping by `(kind, number)` and counting `starts - settled`, floored at
    zero.

    Two decisions, both of which cost real money to get wrong:

      * **A dispatch, not a leg.** A `REVIEW_MODE=multi` dispatch fans 8+ legs
        out under one context. Counting legs would charge one `systemctl
        restart` mid-review as six deaths — a ceiling of five, tuned for "the
        session keeps dying", tripped by a single routine restart. FAILURE-MODES
        #58 is explicit that a machine under memory pressure must not walk
        healthy work to a human.
      * **Counting, not positional pairing.** Those legs settle in arbitrary
        order, so "a start with no settled event before the next start" reports
        7 phantom deaths for every healthy multi-agent review. Counting is
        order-free and answers the only question that matters: is this dispatch
        still owed a settlement?

    Consequences worth stating, all of which the tests pin:
      * the RAISE path (a pre-result transport death the process SURVIVED) emits
        a settled `transport_death` line, so it nets to zero here and is counted
        exactly once — by the dispatcher's store bump, not twice;
      * a window edge that cuts a start off from its settlement can only make
        the count NEGATIVE, and negatives floor at zero, so the bound can
        under-report but never invent a death.

    `window_s<=0` scans every event given. The default 24h matches the horizon
    an operator debugs over; older deaths are already spent money and their
    counter file, if the process lived to write one, persists anyway.
    """
    scoped = events_within(events, window_s, now_fn) if window_s > 0 else list(events)
    starts: dict[tuple, list[dict]] = {}
    settled: dict[tuple, int] = {}
    for e in scoped:
        event = e.get("event")
        if event not in ("agent_run_start", "agent_run"):
            continue
        dispatch = str(e.get("dispatch") or "")
        key = (
            ("dispatch", dispatch) if dispatch
            else ("context", str(e.get("kind", "")), _int(e.get("number")))
        )
        if event == "agent_run_start":
            starts.setdefault(key, []).append(e)
        else:
            settled[key] = settled.get(key, 0) + 1
    out: dict[int, int] = {}
    for key, start_events in starts.items():
        if key[0] == "dispatch":
            missing = 1 if len(start_events) > settled.get(key, 0) else 0
        else:
            missing = len(start_events) - settled.get(key, 0)
        if missing <= 0:
            continue
        # Attribute to the MOST RECENT unsettled starts: when a key's context
        # changed (a PR whose closing-ref appeared between runs), the newest
        # events describe the dispatch the next poll will repeat.
        for e in start_events[-missing:]:
            k = death_key(e)
            if k is not None:
                out[k] = out.get(k, 0) + 1
    return out


def parse_ts(value) -> datetime | None:
    """`ts` → aware UTC datetime, or None if it is missing/unparseable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def sum_today(path: str, now_fn=_now) -> float:
    """Total `spend` today (UTC). The daily cap's window resets at midnight UTC
    rather than local midnight so the same number is read by every daemon on the
    box regardless of the timezone its shell was launched with."""
    today = _stamp(now_fn)[:10]
    return sum(
        _cost(e) for e in read_events(path)
        if e.get("event") == "spend" and str(e.get("ts", ""))[:10] == today
    )


def sum_issue(path: str, repo: str, issue: int) -> float:
    """LIFETIME spend on one issue (all roles, all legs, all days). Lifetime, not
    windowed: a per-issue cap exists to stop a single issue grinding forever, and
    a windowed one would forgive exactly that by ticking over at midnight."""
    return sum(
        _cost(e) for e in read_events(path)
        if e.get("event") == "spend"
        and e.get("repo") == repo
        and _int(e.get("issue")) == issue
    )


def recent_runs(path: str, n: int) -> list[dict]:
    """The last `n` `agent_run` events, oldest first."""
    runs = [e for e in read_events(path) if e.get("event") == "agent_run"]
    return runs[-n:] if n > 0 else []


def events_within(events, seconds: float, now_fn=_now) -> list[dict]:
    """Events stamped within the last `seconds`. Unparseable/absent `ts` → dropped
    (an event we cannot place in time cannot answer a "recently?" question)."""
    cutoff = now_fn() - timedelta(seconds=seconds)
    out = []
    for e in events:
        ts = parse_ts(e.get("ts"))
        if ts is not None and ts >= cutoff:
            out.append(e)
    return out


def repeated_dispatch_warning(events, window_s: float, threshold: int) -> str:
    """Warn when the same (kind, number) was DISPATCHED `threshold`+ times inside
    any `window_s` span. Returns "" when nothing is looping.

    This is the cheap detector for the failure shape that costs the most money:
    a leg that never converges (agent errors, requeue, re-dispatch) burns a full
    paid session per poll and looks, in the log, exactly like healthy activity.
    Pure over `events` and self-referenced — the window is measured between the
    events themselves, not against wall-clock now — so it is deterministic to
    test and does not need a clock injected.

    Counts STARTS as well as settled runs (review C3): the loop that costs the
    most is the one where the session dies before any result, which used to emit
    no `agent_run` at all and so was invisible here — the detector reported
    health while the harness burned a session every other poll.

    **A dispatch, not a leg** (review P1R-4). Every leg of one dispatch carries
    the `dispatch` id `set_context` mints, so legs collapse to ONE timestamp
    (the earliest of the group). Counting legs made a single healthy
    `REVIEW_MODE=multi` review — a gate, five dimension specialists and N
    validators, 8+ start/settled pairs under one (kind, number) — report
    "dispatched 8x … likely a non-converging retry loop" on EVERY review. A
    warning that is always on is a warning that is off, and this is the only
    operator-visible signal for the process deaths of P0R-4.

    Events with no `dispatch` field (journals written before the id existed)
    keep the old rule: a target's count is max(starts, settleds), never their
    sum, since a healthy run emits both halves and only the missing half falls
    back to the other. The two rules add, so a journal that spans the upgrade
    counts its old and new events once each.
    """
    if threshold <= 1:
        return ""
    dispatched: dict[tuple, dict[str, datetime]] = {}
    starts: dict[tuple, list[datetime]] = {}
    settled: dict[tuple, list[datetime]] = {}
    for e in events:
        bucket = {"agent_run_start": starts, "agent_run": settled}.get(e.get("event"))
        if bucket is None:
            continue
        ts = parse_ts(e.get("ts"))
        if ts is None:
            continue
        key = (e.get("kind", ""), _int(e.get("number")))
        dispatch = str(e.get("dispatch") or "")
        if not dispatch:
            bucket.setdefault(key, []).append(ts)
            continue
        seen = dispatched.setdefault(key, {})
        first = seen.get(dispatch)
        if first is None or ts < first:
            seen[dispatch] = ts  # a dispatch is stamped when it STARTED
    groups: dict[tuple, list[datetime]] = {
        key: [
            *dispatched.get(key, {}).values(),
            *max(starts.get(key, []), settled.get(key, []), key=len),
        ]
        for key in {**dispatched, **starts, **settled}
    }
    worst = ""
    for (kind, number), stamps in sorted(groups.items(), key=lambda kv: str(kv[0])):
        stamps.sort()
        for i in range(len(stamps) - threshold + 1):
            span = (stamps[i + threshold - 1] - stamps[i]).total_seconds()
            if span <= window_s:
                worst = worst or (
                    f"{kind or 'agent'} #{number} dispatched {len(stamps)}x "
                    f"within {int(window_s)}s — likely a non-converging retry loop"
                )
                break
    return worst


def spend_blocked_keys(events) -> set[str]:
    """The `block_key`s a `spend_block` event has ALREADY been emitted for.

    The read half of the spend cap's edge trigger (review P1R-2). A cap is a
    LEVEL — once an issue is over its lifetime cap it stays over — so the naive
    "emit on every blocked dispatch" wrote one journal event and ran one
    `ALERT_CMD` subprocess per poll, ~4,300 a day at POLL_SECONDS=20, which is
    how a pager gets muted. Deduping through the journal rather than in memory
    is deliberate: the shape that generates the flood is a wedged daemon under
    `Restart=always`, and an in-process set is empty again after every restart.

    Events with no `block_key` (written before the field existed) are ignored
    rather than guessed at — the cost of that is at most one extra alert per
    key after an upgrade.
    """
    return {
        str(e["block_key"])
        for e in events
        if e.get("event") == "spend_block" and e.get("block_key")
    }


# How many settled runs must agree before the ledger is declared blind. Small
# enough to fire within one poll cycle of a fresh daemon, large enough that a
# single genuinely-free run (a session that died before billing) is not it.
COST_BLINDSPOT_MIN_RUNS = 5


def cost_blindspot_warning(
    runs,
    *,
    issue_spend_cap_usd: float,
    daily_spend_cap_usd: float,
    min_runs: int = COST_BLINDSPOT_MIN_RUNS,
) -> str | None:
    """The ONE loud line for a spend cap that cannot see a single dollar
    (review P1R-5). `runs` is the settled `agent_run` events; returns None
    (silence) when no cap is armed, when there is not enough evidence yet, or
    when any run DID record a cost — there is then nothing to mislead about.

    `JournalingRuntime._record` writes a ledger line only when `cost > 0`, and
    that is the right call (a $0.00 line is noise in the file both caps scan on
    every dispatch). The consequence is what needed saying out loud: a
    subscription-plan Claude gateway reports $0.00 for every run and an
    un-priced Codex reports nothing at all, so the ledger stays EMPTY, both caps
    read $0.00 forever, and neither can fire for any loop shape. The D2
    acceptance ("the wall cap and the infra ceiling bound it") does not cover
    it: the wall cap bounds one session's LENGTH, not the number of sessions.

    Sibling of `codex_cost_blindspot_warning`, which announces the same blind
    spot for the one cause it can detect at startup (missing codex prices); this
    one is evidence-based and catches every cause, including the plan.
    """
    if issue_spend_cap_usd <= 0 and daily_spend_cap_usd <= 0:
        return None
    runs = list(runs)
    if len(runs) < max(1, min_runs):
        return None
    if any(_cost(r) > 0 for r in runs):
        return None
    caps = ", ".join(
        part
        for part in (
            f"issue=${issue_spend_cap_usd:.2f}" if issue_spend_cap_usd > 0 else "",
            f"daily=${daily_spend_cap_usd:.2f}" if daily_spend_cap_usd > 0 else "",
        )
        if part
    )
    return (
        f"spend: WARNING — spend caps are armed ({caps}) but the last "
        f"{len(runs)} agent runs all recorded $0.00, so NOTHING reaches the "
        "spend ledger and NEITHER cap can ever fire. A subscription-plan Claude "
        "gateway reports no per-run cost, and codex reports none unless "
        "CODEX_COST_PER_MTOK_IN/OUT are set. Until a run records a cost, the "
        "only bounds on this harness's spend are the wall cap (one session's "
        "length) and the retry/infra ceilings (a loop's length) — not dollars."
    )


def total_tokens(usage) -> int:
    """Every token counter in an SDK usage block, summed. Input/output/cache are
    all billed, so the total is the right cost substrate; the per-counter split
    only matters if you are pricing them separately, which `observed_cost_per_token`
    deliberately does not."""
    if not isinstance(usage, dict):
        return 0
    return sum(
        int(v) for v in usage.values()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    )


def observed_cost_per_token(events) -> float:
    """Blended $/token LEARNED from completed runs, or 0.0 with no evidence.

    An aborted run reports tokens but no dollars — cost only ever arrives on the
    final ResultMessage. Rather than hardcode a price table (which goes stale
    silently, and is wrong for every gateway/plan that is not the list price),
    read the rate off runs that reported BOTH. It self-calibrates to whatever the
    operator is actually billed at, and returns 0.0 — "no estimate" — until there
    is evidence, so a fresh install never invents a number.

    A single blended rate rather than per-counter prices: cache-read dominates
    real runs, so the counters move together. Measured on the first deployment,
    a resolve ($1.383e-6/tok) and a review ($1.395e-6/tok) agreed to within 1%.

    Only TRUE costs feed this. Estimated runs record their figure in
    `cost_estimated_usd`, never in `cost_usd`, precisely so an estimate can never
    become evidence for the next estimate.
    """
    cost = 0.0
    tokens = 0
    for e in events:
        if e.get("event") != "agent_run":
            continue
        c = _cost(e)
        t = total_tokens(e.get("usage"))
        if c > 0 and t > 0:
            cost += c
            tokens += t
    return cost / tokens if tokens > 0 else 0.0


def estimate_cost_usd(usage, rate: float) -> float:
    """Price a run's tokens at a learned blended rate. 0.0 when either is absent
    — an unknown cost must read as unknown, never as a fabricated number."""
    if rate <= 0:
        return 0.0
    tokens = total_tokens(usage)
    return round(tokens * rate, 6) if tokens > 0 else 0.0


def _cost(event: dict) -> float:
    try:
        return float(event.get("cost_usd", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "COST_BLINDSPOT_MIN_RUNS",
    "Journal",
    "JournalingRuntime",
    "RunContext",
    "alert",
    "cost_blindspot_warning",
    "death_key",
    "default_ledger_path",
    "estimate_cost_usd",
    "event_line",
    "events_within",
    "observed_cost_per_token",
    "parse_ts",
    "read_events",
    "read_events_tail",
    "recent_runs",
    "repeated_dispatch_warning",
    "spend_blocked_keys",
    "sum_issue",
    "sum_today",
    "total_tokens",
    "unsettled_starts",
]
