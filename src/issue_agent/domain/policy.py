"""Map derived item states to Actions. Pure.

`needs-human` has one writer: this module. A stop adds the label first and
then one marked comment keyed for dedupe. That order matters: a crash between
the two leaves the item labelled (stopped, visible) rather than leaving a stop
comment on an unlabelled item, which derive would read as already released.
"""

from __future__ import annotations

from collections.abc import Mapping

from .actions import Action, AgentKind, MergePullRequest, PostComment, Relabel, RunAgent, Target, UpdateBranch
from .labels import Label
from .state import (
    Backoff, Busy, DerivedState, Dispatch, Harness, ItemKey, Merge, NeedsHuman, Refresh, Release,
    StopReason, Wait, stop_tag, ATTEMPT_CAP, REVIEW_ROUND_CAP,
)

_WHY = {
    StopReason.WORKFLOW_PATH: "this PR changes files under `.github/`, which a human must judge.",
    StopReason.LIFETIME_CAP: "this issue has spent its lifetime budget; raising the cap is a grant.",
    StopReason.ATTEMPTS: f"{{act}} ran {ATTEMPT_CAP} times without progress since the last human activity.",
    StopReason.REVIEW_ROUNDS: f"the reviewer requested changes {REVIEW_ROUND_CAP} times; the dispute needs a human.",
}
_HOW = {
    StopReason.LIFETIME_CAP: "Raise the per-issue budget, then reply here or remove the `needs-human` label.",
}
_DEFAULT_HOW = "Reply here or remove the `needs-human` label to release it."


def actions(states: Mapping[ItemKey, DerivedState], harness: Harness) -> tuple[Action, ...]:
    """The actions for every item, ordered issues first then PRs, by number."""
    out: list[Action] = []
    for key in sorted(states, key=lambda k: k.sort_key):
        out.extend(_for(key, states[key], harness))
    return tuple(out)


def needs_human_key(key: ItemKey, stop: NeedsHuman) -> str:
    act = f"-{stop.act.name.lower()}" if stop.act is not None else ""
    return f"needs-human:{key.target.name.lower()}:{key.number}:{stop.reason.value}{act}:{stop.anchor}"


def _for(key: ItemKey, state: DerivedState, harness: Harness) -> tuple[Action, ...]:
    if isinstance(state, Dispatch):
        if (state.act is AgentKind.RESOLVE) != (key.target is Target.ISSUE):
            raise ValueError("RESOLVE dispatches exactly on issues")
        return (RunAgent(state.act, key.number, head=state.head),)
    if isinstance(state, Merge):
        return (MergePullRequest(key.number, state.head),)
    if isinstance(state, Refresh):
        return (UpdateBranch(key.number, state.head),)
    if isinstance(state, Release):
        return (Relabel(key.target, key.number, remove=Label.NEEDS_HUMAN, add=None, harness_signal=True),)
    if isinstance(state, NeedsHuman):
        why = _WHY[state.reason].format(act=state.act.name if state.act else "")
        body = (f"{harness.marker}\n{stop_tag(state.reason)}\n"
                f"**needs-human**: {why}\n\n{_HOW.get(state.reason, _DEFAULT_HOW)}")
        return (
            Relabel(key.target, key.number, remove=None, add=Label.NEEDS_HUMAN, harness_signal=True),
            PostComment(key.target, key.number, body, harness_signal=True, key=needs_human_key(key, state)),
        )
    if isinstance(state, (Wait, Busy, Backoff)):
        return ()
    raise TypeError(f"unknown derived state {state!r}")


__all__ = ["actions", "needs_human_key"]
