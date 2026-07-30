"""The daily spend breakers, evaluated BEFORE a tick observes anything (#27).

Both daily caps are LEVELS that heal by themselves at 00:00 UTC, and both are
answerable from a local file (`adapters.journal.sum_today`) with no network call
at all. Checking them at DISPATCH time — after the world build, after decide,
after the `Relabel` that claims the issue — was therefore pure waste: the claim
landed, the dispatch was refused, no session started, the reaper requeued the
orphan on the next tick, and the orphaned `ai-working` label also made
`domain.world.fix_probe_safe` false, so the cheap change-probe was skipped and a
full ~14-point GraphQL world build ran every 20s while the daemon was, by
definition, doing nothing (#27: ~2,520 points/hour against a 5,000-point pool,
plus 360 label writes/hour, per capped daemon).

Two caps, two scopes (#32):

* `DAILY_SPEND_CAP_USD` lives in the per-repo overlay and now means what its
  location says — today's spend by THIS deployment's repo.
* `GLOBAL_DAILY_SPEND_CAP_USD` lives in `config/shared.env` and bounds the whole
  box: every repo, every role, one ledger.

Ledger entries with no `repo` field (legacy lines, or a truncated write) count
toward the global sum only: spend that cannot be attributed to a deployment must
not be charged to an arbitrary one, but it is still real money off the box.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

_TAIL_CHUNK_BYTES = 64 * 1024
_MAX_LEDGER_LINE_BYTES = 64 * 1024

class AccountingState(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    CORRUPT = "corrupt"


@dataclass(frozen=True)
class AccountingBlock:
    state: AccountingState
    detail: str
    scope: str = "accounting"
    spent: float = 0.0
    cap: float = 0.0

    @property
    def message(self) -> str:
        return (
            f"ACCOUNTING {self.state.value.upper()} — blocking new paid "
            f"admission while cleanup and reconciliation continue ({self.detail})"
        )

    def block_key(self, day: str) -> str:
        return f"accounting:{day}:{self.state.value}:{self.detail}"


@dataclass(frozen=True)
class DailyCapBreach:
    """Which daily cap tripped, and the numbers that tripped it."""

    scope: str  # "daily-global" | "daily-repo"
    spent: float
    cap: float
    repo: str = ""

    @property
    def message(self) -> str:
        if self.scope == "daily-global":
            what = (
                f"TODAY'S SPEND ACROSS ALL REPOS ${self.spent:.2f} is at or over "
                f"the ${self.cap:.2f} GLOBAL_DAILY_SPEND_CAP_USD"
            )
        else:
            what = (
                f"TODAY'S SPEND ON {self.repo} ${self.spent:.2f} is at or over "
                f"the ${self.cap:.2f} DAILY_SPEND_CAP_USD"
            )
        return (
            f"{what} — all dispatch paused until 00:00 UTC "
            "(no labels changed; work resumes by itself)"
        )

    def block_key(self, day: str) -> str:
        """Dedup identity for `_spend_block`. Carries the scope and the cap so
        that raising either knob, or the other scope tripping afterwards, is a
        new operator-visible fact that alerts once more."""
        return f"{self.scope}:{day}:cap:{self.cap:g}"


def daily_cap_breach(
    *,
    repo: str,
    repo_spent: float,
    repo_cap: float,
    global_spent: float,
    global_cap: float,
) -> DailyCapBreach | None:
    """The tripped cap, or None. Pure — both the tick gate and the dispatch-time
    backstop phrase the same block identically.

    The GLOBAL cap is reported first when both are tripped: it is the wider halt
    and the one that raising this deployment's overlay knob will not clear, so
    naming the repo cap there would send an operator to the wrong file. A cap of
    0 is disabled, per the documented convention.
    """
    if global_cap > 0 and global_spent >= global_cap:
        return DailyCapBreach("daily-global", global_spent, global_cap)
    if repo_cap > 0 and repo_spent >= repo_cap:
        return DailyCapBreach("daily-repo", repo_spent, repo_cap, repo)
    return None


def read_daily_cap_breach(
    ledger_path: str,
    *,
    repo: str,
    repo_cap: float,
    global_cap: float,
    now_fn=None,
) -> DailyCapBreach | AccountingBlock | None:
    """`daily_cap_breach` over the ledger. Each sum is read only if its cap is
    armed, so the common one-cap deployment pays one file scan, not two."""
    if not ledger_path:
        return None
    now = now_fn() if now_fn is not None else datetime.now(timezone.utc)
    status, totals, detail = _read_day_totals(ledger_path, now)
    if status is not AccountingState.AVAILABLE:
        return AccountingBlock(status, detail)
    return daily_cap_breach(
        repo=repo,
        repo_spent=totals.get(repo, 0.0) if repo_cap > 0 else 0.0,
        repo_cap=repo_cap,
        global_spent=sum(totals.values()) if global_cap > 0 else 0.0,
        global_cap=global_cap,
    )


def _read_day_totals(
    ledger_path: str, now: datetime
) -> tuple[AccountingState, dict[str, float], str]:
    """Strict, day-bounded accounting read.

    The initialized marker distinguishes a genuinely empty account from a
    missing/unreadable ledger.  Parsing walks backward and stops at the first
    prior-day record, so the polling hot path is bounded by today's activity.
    """

    marker = f"{ledger_path}.initialized"
    try:
        parent = os.path.dirname(ledger_path)
        if parent:
            os.makedirs(parent, mode=0o700, exist_ok=True)
        if not os.path.exists(marker):
            fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        fd = os.open(ledger_path, os.O_RDONLY | os.O_CREAT, 0o600)
    except FileExistsError:
        try:
            fd = os.open(ledger_path, os.O_RDONLY | os.O_CREAT, 0o600)
        except OSError as exc:
            return AccountingState.UNAVAILABLE, {}, str(exc)
    except OSError as exc:
        return AccountingState.UNAVAILABLE, {}, str(exc)

    today = now.astimezone(timezone.utc).date().isoformat()
    totals: dict[str, float] = {}
    try:
        for encoded in _reverse_lines(fd):
            if not encoded.strip():
                continue
            event = json.loads(encoded)
            if not isinstance(event, dict):
                raise ValueError("ledger row is not an object")
            stamp = str(event["ts"])
            row_day = stamp[:10]
            if row_day < today:
                break
            if row_day > today:
                continue
            if event.get("event") != "spend":
                raise ValueError("ledger row is not a spend event")
            value = event.get("billed_cost_usd", event.get("cost_usd"))
            amount = float(value)
            if amount < 0:
                raise ValueError("negative spend")
            repository = str(event.get("repo") or "<unattributed>")
            totals[repository] = totals.get(repository, 0.0) + amount
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return AccountingState.CORRUPT, {}, f"malformed ledger row: {exc}"
    except OSError as exc:
        return AccountingState.UNAVAILABLE, {}, str(exc)
    finally:
        os.close(fd)
    return AccountingState.AVAILABLE, totals, ""


def _reverse_lines(fd: int):
    """Yield newline-delimited records from an fd without loading old months."""

    position = os.lseek(fd, 0, os.SEEK_END)
    carry = b""
    while position:
        size = min(_TAIL_CHUNK_BYTES, position)
        position -= size
        os.lseek(fd, position, os.SEEK_SET)
        block = os.read(fd, size)
        if len(block) != size:
            raise OSError(f"short ledger read: {len(block)}/{size}")
        parts = (block + carry).split(b"\n")
        carry = parts[0]
        for line in reversed(parts[1:]):
            if len(line) > _MAX_LEDGER_LINE_BYTES:
                raise ValueError("ledger row exceeds line-size limit")
            yield line
        if len(carry) > _MAX_LEDGER_LINE_BYTES:
            raise ValueError("ledger row exceeds line-size limit")
    if carry:
        yield carry


def build_daily_spend_gate(
    ledger_path: str,
    *,
    repo: str,
    repo_cap: float,
    global_cap: float,
    now_fn=None,
) -> Callable[[], str | None] | None:
    """A `Daemon.tick_gate`: returns the block message while a daily cap is
    tripped, None otherwise. None instead of a gate when neither cap is armed —
    an unarmed daemon must not pay a ledger read per poll.
    """
    if not ledger_path or (repo_cap <= 0 and global_cap <= 0):
        return None

    def gate() -> str | None:
        breach = read_daily_cap_breach(
            ledger_path,
            repo=repo,
            repo_cap=repo_cap,
            global_cap=global_cap,
            now_fn=now_fn,
        )
        return breach.message if breach is not None else None

    return gate
