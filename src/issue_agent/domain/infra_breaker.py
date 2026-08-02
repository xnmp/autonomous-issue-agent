"""Deployment-wide circuit breaker for infra-class leg deaths.

Per-issue ceilings are the wrong granularity for a repo-wide fault. keifu,
2026-08-02 04:53-04:58Z: systemd's minimal PATH resolved a `codex` 0.142.3 the
backend answered with an instant 400, so EVERY leg spawn died stillborn. Four
issues (#34/#130/#132/#142) independently walked their own per-issue
`infra-deaths` ladder — a dozen-plus deaths in five minutes — and each produced
its own human escalation. Nothing in the harness could say the one true thing:
*the runtime is broken deployment-wide, stop buying legs.*

The distinction this module draws, and the reason it does not replace the
per-issue ceiling:

* ONE issue dying repeatedly is plausibly about that issue (a huge diff that
  OOMs, a prompt that wedges the gateway). The per-issue ceiling owns it, and
  escalating one issue is the right blast radius.
* Deaths spread across TWO OR MORE distinct issues inside a short window cannot
  be about any one issue. The only shared variable is the deployment, so the
  correct response is deployment-wide: pause PAID admission, escalate ONCE.

Following ADR 0013's spirit inside today's representation: the trip carries a
machine-checkable CAUSE (predicate name plus its evidence) and the REMEDIATION
already attempted before it opened, rather than prose. There is no gate row to
write yet, so the cause and remediation ride the journal event and the operator
log line; when the gates table lands, `BreakerTrip` is what opens the row.

Pure: every input is supplied by the caller, including the clock. `at`/`now` are
caller-supplied seconds from a MONOTONIC source — window arithmetic across a
suspend/resume or an NTP step must not be able to go backwards.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "BreakerPolicy",
    "BreakerTrip",
    "CLEAR_COOLDOWN_ELAPSED",
    "CLEAR_PROBE_SUCCEEDED",
    "DEFAULT_BREAKER_COOLDOWN_SECONDS",
    "DEFAULT_BREAKER_DEATHS",
    "DEFAULT_BREAKER_WINDOW_SECONDS",
    "InfraDeath",
    "MIN_DISTINCT_ISSUES",
    "clear_reason",
    "gate_reason",
    "trip_for",
    "within_window",
]

# Four deaths, not two: a single flaky gateway drop on each of two issues is
# ordinary weather, and pausing the whole deployment for it costs more than it
# saves. The observed incident produced twelve in five minutes.
DEFAULT_BREAKER_DEATHS = 4
DEFAULT_BREAKER_WINDOW_SECONDS = 600
# How long a trip holds before re-arming on its own. It has to exist: while the
# breaker holds, no paid leg dispatches, so no leg can succeed and clear it —
# the cooldown is what lets the deployment retry after an operator (or a
# systemd restart) fixes the runtime without anyone touching the harness. A
# still-broken runtime re-trips within one window, at the cost of one storm.
DEFAULT_BREAKER_COOLDOWN_SECONDS = 900
# Two distinct issues is the whole predicate: it is what makes the evidence say
# "the deployment" instead of "this issue".
MIN_DISTINCT_ISSUES = 2

CLEAR_PROBE_SUCCEEDED = "probe_succeeded"
CLEAR_COOLDOWN_ELAPSED = "cooldown_elapsed"


@dataclass(frozen=True)
class InfraDeath:
    """One infra-class leg death, as observed.

    `issue` is the issue the leg was bought for; PR-keyed legs pass their
    underlying issue so a PR storm and an issue storm count as one deployment.
    `at` is monotonic seconds. `leg`/`reason` are evidence for the cause string
    (`outcome.infra_class_reason` supplies the latter) and nothing branches on
    them.
    """

    issue: int
    at: float
    leg: str = ""
    reason: str = ""


def _positive_int(value: object, default: int) -> int:
    """Coerce an operator knob, substituting the default for anything unusable.

    Absent, malformed, non-numeric, boolean, or non-positive all mean "the
    operator did not express a usable intent", and a breaker that silently
    disables itself because a knob was `"4 "` is worse than one that ignores the
    typo. The config layer already reports parse errors at startup; this is the
    defensive floor for every other construction path.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
        return default
    coerced = int(value)
    return coerced if coerced > 0 else default


@dataclass(frozen=True)
class BreakerPolicy:
    """Operator knobs, already coerced. Build with `sane` from raw config."""

    deaths: int = DEFAULT_BREAKER_DEATHS
    window_seconds: int = DEFAULT_BREAKER_WINDOW_SECONDS
    cooldown_seconds: int = DEFAULT_BREAKER_COOLDOWN_SECONDS

    @classmethod
    def sane(
        cls,
        deaths: object = None,
        window_seconds: object = None,
        cooldown_seconds: object = None,
    ) -> "BreakerPolicy":
        return cls(
            deaths=_positive_int(deaths, DEFAULT_BREAKER_DEATHS),
            window_seconds=_positive_int(
                window_seconds, DEFAULT_BREAKER_WINDOW_SECONDS
            ),
            cooldown_seconds=_positive_int(
                cooldown_seconds, DEFAULT_BREAKER_COOLDOWN_SECONDS
            ),
        )


@dataclass(frozen=True)
class BreakerTrip:
    """Why paid admission is paused, in terms a machine can re-check.

    ADR 0013 shape: `cause` is a predicate name plus its evidence, never prose;
    `remediation` names what the harness already tried automatically before it
    gave up on its own. An escalation that cannot say either is the class of
    escalation that ADR is written against.
    """

    deaths: int
    issues: tuple[int, ...]
    window_seconds: int
    legs: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    @property
    def cause(self) -> str:
        return (
            "infra_deaths_across_issues("
            f"deaths={self.deaths}, "
            f"issues={list(self.issues)}, "
            f"window_s={self.window_seconds}, "
            f"reasons={list(self.reasons)})"
        )

    @property
    def remediation(self) -> str:
        """What ran before the breaker opened.

        Every death counted here was already charged to its issue's own
        infra-death ceiling and, for a resolve-family leg, already skipped the
        quality counters (`outcome.infra_class_failure`). That IS the automatic
        remediation for a single-issue fault, and its failure across issues is
        the evidence this breaker exists to act on.
        """
        return (
            "per_issue_infra_ceilings_charged("
            f"issues={list(self.issues)}, legs={list(self.legs)})"
        )


def within_window(
    deaths: tuple[InfraDeath, ...] | list[InfraDeath],
    *,
    now: float,
    window_seconds: int,
) -> tuple[InfraDeath, ...]:
    """The deaths still inside the sliding window at `now`.

    A death exactly `window_seconds` old has expired: the window is half-open
    so a test that advances by exactly the window observes expiry rather than a
    boundary coin-flip. Deaths timestamped in the FUTURE relative to `now` are
    kept — a caller with a jittered clock must not silently lose evidence.
    """
    return tuple(
        death for death in deaths if now - death.at < window_seconds
    )


def trip_for(
    deaths: tuple[InfraDeath, ...] | list[InfraDeath],
    *,
    now: float,
    policy: BreakerPolicy,
) -> BreakerTrip | None:
    """The trip these observations justify, or None.

    Both conditions must hold over the SAME window: at least `policy.deaths`
    infra-class deaths, spread over at least `MIN_DISTINCT_ISSUES` issues. One
    issue's repeated deaths deliberately return None here and stay the per-issue
    ceiling's business.
    """
    recent = within_window(deaths, now=now, window_seconds=policy.window_seconds)
    if len(recent) < policy.deaths:
        return None
    issues = tuple(sorted({death.issue for death in recent}))
    if len(issues) < MIN_DISTINCT_ISSUES:
        return None
    return BreakerTrip(
        deaths=len(recent),
        issues=issues,
        window_seconds=policy.window_seconds,
        legs=tuple(sorted({death.leg for death in recent if death.leg})),
        reasons=tuple(sorted({death.reason for death in recent if death.reason})),
    )


def clear_reason(
    *,
    tripped_at: float,
    now: float,
    policy: BreakerPolicy,
    probe_succeeded: bool = False,
) -> str:
    """Why the breaker may re-arm now, or "" while it must keep holding.

    Two arms only, and both are bounded: a leg that actually completed is direct
    evidence the runtime is alive, and the cooldown bounds the hold itself so a
    trip can never become a permanent stop. `now < tripped_at` (a clock that
    went backwards) reads as "not elapsed", which holds rather than re-arms.
    """
    if probe_succeeded:
        return CLEAR_PROBE_SUCCEEDED
    if now - tripped_at >= policy.cooldown_seconds:
        return CLEAR_COOLDOWN_ELAPSED
    return ""


def gate_reason(trip: BreakerTrip, *, policy: BreakerPolicy) -> str:
    """The operator-facing line, held STABLE for the life of one trip.

    `Daemon._gate_blocks_this_tick` logs edge-triggered on the reason string, so
    anything that varies per tick (elapsed seconds, a countdown) would turn the
    one loud escalation this module exists to produce back into a line every
    poll — the noise the incident already had four copies of.
    """
    return (
        "INFRA BREAKER — paid dispatch paused deployment-wide: "
        f"{trip.cause}; remediation attempted: {trip.remediation}; "
        f"clears on the next successful leg or after {policy.cooldown_seconds}s"
    )
