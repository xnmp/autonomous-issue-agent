"""Ceiling on durable-effect re-execution (ADR 0012).

A durable lifecycle command whose remote postcondition cannot be verified is
receipted `retryable_failure` and re-executed by the next tick's drain. That
loop had no bound. The keifu deployment (2026-08-01) proved why it needs one:
the steward's review identity held a PAT without Issues:Write, so every
`gh issue edit --add-label` exited 403. A bare 403 is deliberately NOT an infra
failure (`adapters/github.py:classify_gh_failure`) — "Resource not accessible"
is a permanent permission ANSWER — so the write returned "", readback said "not
applied", and the same denied write was re-attempted on every tick, forever,
with nothing telling the operator which write was stuck or why.

The bound counts verified REFUSALS, not wall time and not attempts. A permanent
denial fails identically however long it is left, so time is the wrong unit —
but `lifecycle_commands.attempts` is the wrong counter too: `begin_command`
increments it for every execution including the ones a GitHub outage
interrupted, and `retry_reconciled_command` moves an ambiguous command back to
`retryable_failure` without resetting it. Counting attempts therefore let a
ten-tick degradation pre-burn the ceiling so that the first genuine 403 retired
the command. Control-store v18 adds a `refusals` column that only a
`retryable_failure` receipt — a readback that PROVED the requested state absent
— increments; that is what this ceiling reads.

An infra failure (`GhUnavailable` — rate limit, DNS, 5xx, timeout) escapes
`apply_and_verify` as an exception, is receipted `unknown_outcome`, and is
reconciled rather than retired: a GitHub outage must never burn a command's
ceiling and turn a transient condition into a permanent one.
"""

from __future__ import annotations

from .contracts import EffectStatus

# Ten denials is far past any plausible transient verification miss (readback
# lag on a label write is one tick, not ten) while still cheap: a denied write
# costs one gh call per tick, so the ceiling is reached in minutes, not days.
DEFAULT_EFFECT_RETRY_CEILING = 10


def effect_ceiling_reached(state: str, refusals: int, ceiling: int) -> bool:
    """Whether this command has spent its retry budget and must be retired.

    `refusals` is the verified-refusal count, NOT `attempts` — see the module
    docstring. ``ceiling <= 0`` disables the bound entirely (the pre-ceiling
    behaviour), which is why config validation rejects it as operator input.
    """
    if ceiling <= 0:
        return False
    if state != EffectStatus.RETRYABLE_FAILURE.value:
        return False
    return refusals >= ceiling


def exhausted_effect_reason(
    kind: str, target: str, refusals: int, ceiling: int
) -> str:
    """The receipted error for a retired command — says what stopped, not why
    the individual attempt failed (the last attempt's own error is already in
    the journal's ActionFailed events)."""
    return (
        f"{kind} on {target} was refused by GitHub {refusals} times "
        f"(retry ceiling {ceiling}); retired as permanent_failure"
    )


def exhausted_effect_advice(kind: str, target: str) -> str:
    """Operator-facing line for a retired command. Names the write that never
    landed and the likeliest cause, because the symptom (nothing happens on
    GitHub) points at the agent rather than at the token."""
    return (
        f"lifecycle write permanently failed: {kind} on {target} — GitHub kept "
        "refusing it. The usual cause is the acting identity's token: a "
        "fine-grained PAT missing Issues: Read+Write / Pull requests: "
        "Read+Write, or a bot whose collaborator invite was never accepted. "
        "Run issue-agent doctor to probe the identities, then re-mint the token; "
        "the command and its payload stay in the control store."
    )
