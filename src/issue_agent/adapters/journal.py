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
    the SAME context, which is per-leg cost attribution for free."""

    role: str = ""
    kind: str = ""
    number: int = 0
    issue: int | None = None


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
        journal reads."""
        self.context = RunContext(role=role, kind=kind, number=number, issue=issue)

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
            self.journal.emit(
                "agent_run",
                role=ctx.role,
                kind=ctx.kind,
                number=ctx.number,
                issue=ctx.issue,
                model=task.model or "",
                duration_s=round(duration_s, 1),
                cost_usd=cost,
                status=result.status,
                is_error=result.is_error,
                exit_reason=(result.exit_reason.value if result.exit_reason else None),
                session_id=result.session_id,
                usage=_usage(result.usage),
                **extra,
            )
            # Only costed runs reach the ledger: a $0.00 line is pure noise in the
            # file the spend caps scan on every dispatch.
            if self.ledger is not None and cost > 0:
                self.ledger.emit(
                    "spend", repo=self.repo, issue=ctx.issue, kind=ctx.kind, cost_usd=cost
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
def read_events(path: str) -> list[dict]:
    """Every parseable JSON object in `path`, oldest first. A missing file is an
    empty history, not an error — the first status check runs before any daemon
    has written a line."""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
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
    """Warn when the same (kind, number) was dispatched `threshold`+ times inside
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
    health while the harness burned a session every other poll. A healthy run
    emits both halves, so a target's dispatch count is max(starts, settleds),
    never their sum; only the half that is missing (a start with no settlement,
    or a pre-`agent_run_start` journal) falls back to the other.
    """
    if threshold <= 1:
        return ""
    starts: dict[tuple, list[datetime]] = {}
    settled: dict[tuple, list[datetime]] = {}
    for e in events:
        bucket = {"agent_run_start": starts, "agent_run": settled}.get(e.get("event"))
        if bucket is None:
            continue
        ts = parse_ts(e.get("ts"))
        if ts is None:
            continue
        bucket.setdefault((e.get("kind", ""), _int(e.get("number"))), []).append(ts)
    groups: dict[tuple, list[datetime]] = {
        key: max(starts.get(key, []), settled.get(key, []), key=len)
        for key in {**starts, **settled}
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
    "Journal",
    "JournalingRuntime",
    "RunContext",
    "alert",
    "default_ledger_path",
    "event_line",
    "events_within",
    "parse_ts",
    "read_events",
    "recent_runs",
    "repeated_dispatch_warning",
    "sum_issue",
    "sum_today",
]
