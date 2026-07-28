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

from collections.abc import Callable
from dataclasses import dataclass

from ..adapters.journal import sum_today


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
) -> DailyCapBreach | None:
    """`daily_cap_breach` over the ledger. Each sum is read only if its cap is
    armed, so the common one-cap deployment pays one file scan, not two."""
    if not ledger_path:
        return None
    kwargs = {"now_fn": now_fn} if now_fn is not None else {}
    return daily_cap_breach(
        repo=repo,
        repo_spent=(
            sum_today(ledger_path, repo=repo, **kwargs) if repo_cap > 0 else 0.0
        ),
        repo_cap=repo_cap,
        global_spent=sum_today(ledger_path, **kwargs) if global_cap > 0 else 0.0,
        global_cap=global_cap,
    )


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
