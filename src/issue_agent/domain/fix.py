"""Fix-watcher decision logic — pure port of watcher.sh:poll_once + helpers.

Structure mirrors the bash: small lesson-bearing cores (pick_ready_issue,
issues_needing_resume, should_automerge, reap_actions, cleanup_actions) plus a
`decide_fix` that composes them in the same priority order poll_once uses.

Orchestration state that lives in STATE_DIR files (sessions, resumed-at marks,
automerge markers) is passed in explicitly as `FixState`, and the live-fixer
run-lock as `fixer_busy` — so the decision stays a pure function of its inputs.

Concurrency model (preserved): ONE fixer, sequential dispatch. Within a single
poll only ONE fixer-consuming action is emitted; non-fixer actions (auto-merge,
cleanup) are independent of the lock.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .actions import (
    Action,
    AgentKind,
    ClearIssueState,
    CloseIssue,
    EnableAutoMerge,
    UpdateBranch,
    PostComment,
    Relabel,
    RemoveWorktree,
    RunAgent,
    Target,
)
from .ci import (
    ci_state,
    latest_other_review,
    pending_age_seconds,
    pending_checks,
    pending_deadline_breached,
)
from .identity import login_eq
from .labels import Label, priority_rank
from .lifecycle import (
    issue_escalation,
    issue_lifecycle,
    pr_escalation,
    pr_lifecycle,
    retry_breaker,
)
from .refs import is_governed_branch, issue_from_branch
from .review import IdentityMode
from .world import (
    CIState,
    ClosingRef,
    Comment,
    IssueView,
    PRView,
    Review,
    WorldState,
    REVIEW_APPROVED,
    REVIEW_CHANGES_REQUESTED,
    paths_overlap_open_prs,
)


@dataclass(frozen=True)
class FixConfig:
    fix_bot: str
    # A plain "three"/"solo" string still compares equal (str-Enum), so a
    # hand-built config keeps working; the config boundary hands over a member.
    identity_mode: IdentityMode = IdentityMode.THREE
    auto_merge: bool = True
    merge_method: str = "squash"
    ci_ignore_checks: str = ""
    close_issue_on_merge: bool = True
    max_ci_retries: int = 3
    max_review_retries: int = 3
    auto_rebase_conflicts: bool = True
    max_rebase_retries: int = 3
    max_resolve_retries: int = 3
    # Arm auto-merge at PR CREATION instead of after APPROVE+CI. Only ever set
    # True after the adapter has POSITIVELY verified that the base branch's
    # protection requires ≥1 approving review AND ≥1 status check (see
    # GitHubGateway.merge_gate_protection) — with the gate held server-side,
    # arming early just delegates the same ordering to GitHub, which then merges
    # the instant review+checks land, with no daemon on the critical path. On an
    # unprotected base this flag would mean merging unreviewed work, which is
    # why no env knob can force it True without the probe.
    arm_on_open: bool = False
    # SEPARATE, HIGHER ceiling for sessions that die BEFORE returning a result
    # (review C3/D2c). "Infra deaths are not attempts" stays true for every
    # retry breaker above (FAILURE-MODES #58) — but a leg whose session keeps
    # dying pre-result never records an attempt at all, so nothing bounded it:
    # one full paid session every two polls, forever. 0 disables.
    max_infra_deaths: int = 5
    # How long CI may sit PENDING before the PR is handed to a human (#30),
    # measured from the newest pending check's start. PENDING outranks FAILURE
    # (the #49 anti-loop rule) and nothing bounded that, so one job stuck
    # IN_PROGRESS pinned a PR forever — silently, since a pending PR dispatches
    # no leg and therefore writes no counter, no journal event and no log line.
    # 0 disables, like every other breaker here. The default is deliberately
    # generous: a real pipeline can legitimately run for well over half an hour
    # (webkit suites ~23m on the flagship target), and escalating a healthy long
    # run would move the escalation-rate guardrail the wrong way.
    ci_pending_max_seconds: int = 3600
    # Refuse to RESOLVE an issue an open PR already closes (#39). The most
    # expensive leg there is, spent rediscovering a fix that is already written:
    # the operator's own PR #558 closed tauri-explorer #554, labelling the issue
    # ai-ready anyway bought a duplicate PR for ≈$32, and the reviewer
    # independently converged on the approach #558 already had. Off = dispatch
    # regardless (the pre-#39 behaviour).
    duplicate_pr_guard: bool = True


@dataclass(frozen=True)
class FixState:
    """Persistent orchestration state (STATE_DIR files), passed in purely."""

    sessions: frozenset[int] = frozenset()        # issues with a saved session
    resumed_at: dict[int, str] = field(default_factory=dict)  # issue → last reply ts resumed on
    automerge_enabled: frozenset[int] = frozenset()  # PRs already auto-merge-enabled (marker)
    # Per-issue retry counters (issue → count). Drive the circuit breakers: an
    # unbounded fix-CI / address-review loop burns budget forever, so at the cap
    # we escalate to a human instead of re-dispatching. Default 0 (no attempts).
    ci_retries: dict[int, int] = field(default_factory=dict)
    # review_retries values are (review_submitted_at, count): the counter is
    # KEYED to the specific CHANGES_REQUESTED review it accumulated against. A
    # NEW review (different submitted_at) reads as count 0 — supersession is the
    # ONLY thing that resets it. The V2 lesson: the old clear-on-push-success
    # reset the counter every round, so the breaker could never trip and a downed
    # review daemon meant an infinite full-session-cost loop.
    review_retries: dict[int, tuple[str, int]] = field(default_factory=dict)
    rebase_retries: dict[int, int] = field(default_factory=dict)
    # Per-ISSUE resolve requeue counter (T3 finding): an always-failing resolve
    # requeues ai-ready each round; without a breaker that is a full paid
    # session per poll, forever — #39's shape on the most expensive leg.
    resolve_retries: dict[int, int] = field(default_factory=dict)
    # Pre-result session deaths (review C3), keyed the same way the retry
    # counters are: issue number for the issue legs, `breaker_key(pr)` for the
    # PR legs. Written by the dispatcher (on a raised handler) and by the fix
    # workflow (on a RETURNED infra-death result) — the two are mutually
    # exclusive, so a single death counts once.
    infra_deaths: dict[int, int] = field(default_factory=dict)


# ── holds (why a ready issue was NOT picked) ─────────────────────────────────
HOLD_BLOCKER = "blocker"
HOLD_OVERLAP = "overlap"


@dataclass(frozen=True)
class Hold:
    """One reason ONE ai-ready issue was not dispatched this poll.

    A deliberate hold and an idle bug look identical from outside (harness #6: a
    silent path-overlap hold on #522 cost ~30 minutes of live investigation), so
    every skip `pick_ready_issue` makes silently is also reportable as a value.

    `blocker` NAMES the thing being waited on — `"pr#123"` for an overlap — so
    the operator can go look at it. It is `""` for a GitHub-native dependency
    hold: `IssueView.blocked_by_open` is a settled boolean (the adapter resolves
    the dependency list and the domain only ever sees "at least one is open"),
    so there is no number to name. If that view ever carries the blocking issue
    numbers, this is where they go.
    """

    issue: int
    reason: str          # HOLD_BLOCKER | HOLD_OVERLAP
    blocker: str = ""    # "pr#123" | "" (unknown/unnameable)


def _overlapping_open_pr(
    declared: frozenset[str], prs: tuple[PRView, ...]
) -> PRView | None:
    """The first OPEN PR whose changed files collide with `declared`, or None.

    The WITNESS form of `world.paths_overlap_open_prs` — and it is written by
    applying that same predicate to one PR at a time rather than by re-stating
    the matching rule, so the two can never drift: `_overlapping_open_pr(...) is
    not None` is `paths_overlap_open_prs(...)` by construction, including the
    fail-open on empty `declared` and the OPEN-only filter."""
    for pr in prs:
        if paths_overlap_open_prs(declared, (pr,)):
            return pr
    return None


def _holds(issue: IssueView, world: WorldState) -> tuple[Hold, ...]:
    """Every reason THIS issue is held back, ignoring its label/state (the
    caller decides who is eligible to be held). Empty ⇒ dispatchable.

    THE single definition of "held": `pick_ready_issue` filters on
    `not _holds(...)` and `issue_holds` reports exactly what it returns, so the
    journal can never disagree with the decision it explains."""
    holds: list[Hold] = []
    if issue.blocked_by_open:
        holds.append(Hold(issue.number, HOLD_BLOCKER))
    overlap = _overlapping_open_pr(issue.declared_paths, world.prs)
    if overlap is not None:
        holds.append(Hold(issue.number, HOLD_OVERLAP, f"pr#{overlap.number}"))
    return tuple(holds)


def issue_holds(world: WorldState) -> tuple[Hold, ...]:
    """Every hold in force this poll: for each OPEN, ai-ready issue that
    `pick_ready_issue` will skip, why. Pure; ordered by the world's issue order,
    then blocker-before-overlap for a doubly-held issue.

    Reported, not acted on — the wiring journals these (deduped per
    issue/reason/blocker) so a harness that looks idle can be told apart from a
    harness that is deliberately waiting."""
    return tuple(
        hold
        for issue in world.open_issues()
        if issue_lifecycle(issue).is_ready
        for hold in _holds(issue, world)
    )


# ── duplicate-PR guard (#39) ─────────────────────────────────────────────────
@dataclass(frozen=True)
class DuplicatePR:
    """One ai-ready issue an OPEN, NOT-ours PR already closes.

    NOT a `Hold`. A hold is a silent one-poll defer on a condition that clears
    by itself (the blocking PR merges, the dependency closes) — this is a
    terminal refusal of work the operator explicitly asked for, so it says so
    out loud and hands the decision back: comment naming the PR, then
    ai-ready → ai-blocked. The label swap is what makes it one-shot, exactly as
    `_escalate_pr` relies on: the issue leaves `pick_ready_issue`'s set, so the
    guard cannot re-fire, and the operator re-arms it by closing the stale PR
    and re-labelling (or removes ai-ready to leave the fix to the human).

    ai-blocked rather than needs-human-review: nothing is broken and no breaker
    tripped — the work is simply already done somewhere else. ai-blocked is the
    harness's existing "waiting on a person" state and keeps the issue in the
    fixer's world (a human reply on a blocked issue with a session resumes it),
    where needs-human-review would retire it from the pipeline entirely."""

    issue: int
    pr: int
    author: str = ""

    @property
    def reason(self) -> str:
        by = f" (opened by @{self.author})" if self.author else ""
        return (
            f"**Resolve skipped:** open PR #{self.pr}{by} already closes this "
            "issue — close it or remove `ai-ready` to hand the issue to the "
            "harness. Dispatching would open a second PR for work that is "
            "already written."
        )

    def actions(self) -> list[Action]:
        return [
            PostComment(Target.ISSUE, self.issue, self.reason),
            Relabel(Target.ISSUE, self.issue, Label.AI_READY, Label.AI_BLOCKED),
        ]


def _is_fixers(ref: ClosingRef, cfg: FixConfig) -> bool:
    """Whether the fix bot opened this closing PR — the NORMAL in-flight state
    (its issue is ai-pr-open and its PR is the harness's own work), which the
    guard must never fire on.

    Same fail-safe direction, and the same explicit both-sides-non-empty guard,
    as `fixer_prs`: an unresolvable PR author ("" — a deleted account, a
    GraphQL null) or an unset FIX_BOT reads as NOT-ours. The asymmetry is
    deliberate and points the way the money does: treating an unknown author as
    ours would dispatch a duplicate resolve (the ≈$32 mistake #39 is about),
    while treating it as theirs at worst posts a comment on an issue the
    operator can un-block in one click."""
    return bool(
        ref.author and cfg.fix_bot and login_eq(ref.author, cfg.fix_bot)
    )


def duplicate_pr_guards(
    world: WorldState, cfg: FixConfig
) -> tuple[DuplicatePR, ...]:
    """Every ai-ready issue that an OPEN, non-fix-bot PR already closes.

    THE single definition, in the `issue_holds` / `ci_pending_stalls` shape:
    `decide_fix` refuses exactly what this returns and the wiring journals
    exactly what this returns, so the journal cannot disagree with the action.

    Reads `world.closing_refs` (a repo-wide open-PR list the fix world builder
    fills), never `world.prs` — the fix world's `prs` are the harness's OWN
    label-scoped PRs, so a stranger's PR is structurally absent from it, which
    is precisely why #39 was invisible. An unfilled `closing_refs` yields no
    guards: the guard fails OPEN (see `WorldState.closing_refs`).

    Base branch is NOT part of the test — see `ClosingRef`."""
    if not cfg.duplicate_pr_guard:
        return ()
    ready = {
        i.number for i in world.open_issues() if issue_lifecycle(i).is_ready
    }
    seen: set[int] = set()
    out: list[DuplicatePR] = []
    for ref in world.closing_refs:
        if ref.issue not in ready or ref.issue in seen or _is_fixers(ref, cfg):
            continue
        seen.add(ref.issue)
        out.append(DuplicatePR(issue=ref.issue, pr=ref.pr, author=ref.author))
    return tuple(out)


# ── cores ────────────────────────────────────────────────────────────────────
def pick_ready_issue(world: WorldState) -> int | None:
    """Highest-priority ai-ready issue number, or None. Port of watcher.sh §1
    (P1<P2<P3<none; ties broken by issue number for determinism — the bash relied
    on gh's list order, we make it explicit).

    An issue with an OPEN GitHub-native "blocked by" dependency is SKIPPED — its
    prerequisite isn't on the base branch yet, so dispatching it makes the fixer
    build against a missing layer (observed with the P3–P6 series depending on the
    P2 route layer). The blocker resolves via GitHub's dependency graph; when the
    last blocker closes, the issue becomes pickable automatically next poll.

    Also SKIPPED: an issue whose body-declared file paths overlap the changed
    files of an OPEN PR. Two issues editing the same files in parallel produce a
    guaranteed merge conflict when the second PR tries to land (observed: #59 vs
    #60 both rewired the ConversationService composition root). Holding the second
    issue until the first PR merges serialises the collision away. Fail-open: an
    issue that declares no paths can't be predicted, so it is NOT held (the gate
    only fires on a concrete, known overlap).

    Both skips are delegated to `_holds`, which `issue_holds` also reads, so the
    hold journal explains THIS decision rather than a copy of it."""
    ready = [
        i for i in world.open_issues()
        if issue_lifecycle(i).is_ready and not _holds(i, world)
    ]
    if not ready:
        return None
    ready.sort(key=lambda i: (priority_rank(i.labels), i.number))
    return ready[0].number


def last_human_reply(issue: IssueView, fix_bot: str) -> Comment | None:
    """Latest comment by a real human (not the fixer, not any [bot]), or None.
    Port of the jq in watcher.sh §2 / resume-issue.sh.

    Public because BOTH sides of the resume dedup must select the SAME comment:
    the decide-side read (issues_needing_resume, comparing created_at against
    the resumed-at mark) and the workflow's write side (set_resumed_at after
    dispatch). When the two sides used different filters the write recorded a
    different comment than the read compared, and one unanswered reply re-fired
    a paid RESUME session every poll (V1/#13)."""
    humans = [
        c for c in issue.comments
        if not login_eq(c.author, fix_bot) and not c.is_bot
    ]
    return humans[-1] if humans else None


def _last_human_reply_at(issue: IssueView, fix_bot: str) -> str | None:
    reply = last_human_reply(issue, fix_bot)
    if reply is None:
        return None
    return reply.created_at or None


def issues_needing_resume(
    world: WorldState, state: FixState, cfg: FixConfig
) -> list[tuple[int, str]]:
    """ai-blocked issues with a session AND a human reply we have NOT yet resumed
    on. Returns (issue_number, reply_at). The resumed-at dedupe is the anti-loop
    lesson: a single reply must not re-trigger resume every poll."""
    out: list[tuple[int, str]] = []
    for issue in world.issues:
        lifecycle = issue_lifecycle(issue)
        if not lifecycle.is_blocked or issue.number not in state.sessions:
            continue
        last_at = _last_human_reply_at(issue, cfg.fix_bot)
        if last_at is None:
            continue
        if last_at != state.resumed_at.get(issue.number, ""):
            out.append((issue.number, last_at))
    return out


def should_automerge(pr: PRView, state: FixState, cfg: FixConfig) -> bool:
    """Port of watcher-lib.sh:maybe_automerge gating. Requires: AUTO_MERGE on, a
    branch the harness itself authored, no existing marker, three-identity mode
    (solo has no independent approver → 422 on self-merge), and then EITHER
    arm_on_open (verified branch protection holds the review+CI gate server-side)
    OR an APPROVED review by a DISTINCT actor with CI ok|none."""
    if not cfg.auto_merge:
        return False
    # Merge ONLY our own work. The reviewer deliberately adopts unlabelled PRs
    # (REVIEW_ADOPT_UNLABELED, on by default) so one that bypassed the pipeline is
    # still seen — reviewing a stranger's PR is useful, MERGING it is not what
    # anybody asked for. Composed with AUTO_MERGE those two safe-sounding defaults
    # meant "approve and merge any open PR into the base branch", and on the first
    # real deployment the reviewer promptly adopted a dependabot PR (#486) on a
    # repo with five of them queued. Adoption widens what we REVIEW; this keeps it
    # from widening what we MERGE.
    #
    # Keyed on the BRANCH, not the linked issue: `_issue_for_pr` prefers GitHub's
    # closing-reference graph, which a stranger's PR can populate just by writing
    # "Fixes #N" in its body. The branch name is the thing only the fixer creates.
    if not is_governed_branch(pr.head_ref_name):
        return False
    if pr.number in state.automerge_enabled:
        return False
    if cfg.identity_mode != IdentityMode.THREE:
        return False
    # A CONFLICTING PR cannot merge no matter what gh is told — arming it just
    # burns a gh call + verification probe per poll and logs a refusal every
    # 20s until the REBASE leg lands (observed on #534, 2026-07-26, after the
    # arm-verification fix stopped the failed attempt from latching its
    # marker). The rebase path owns conflicted PRs; automerge re-fires the
    # first poll after the branch is mergeable again.
    if pr.is_conflicting():
        return False
    # arm_on_open: branch protection (verified at boot) already refuses the
    # merge until a review approves and the required checks pass, so waiting
    # for them HERE only adds daemon latency — a busy fixer (one synchronous
    # session per tick) left #539 APPROVED+green and unmerged for the length
    # of an unrelated resolve (2026-07-27). Arm immediately; GitHub holds the
    # gate. The review/CI preconditions below remain the whole gate when the
    # base is unprotected.
    if cfg.arm_on_open:
        return True
    review = latest_other_review(pr.reviews, cfg.fix_bot)
    if review is None or review.state != REVIEW_APPROVED:
        return False
    return ci_state(pr.checks, cfg.ci_ignore_checks) in (CIState.OK, CIState.NONE)


def should_update_branch(pr: PRView, cfg: FixConfig) -> bool:
    """A merge-ready PR (distinct APPROVED review + CI ok/none) that is BEHIND its
    base needs its branch brought up to date so an armed auto-merge can land it.

    Deliberately INDEPENDENT of the automerge-enabled marker (unlike
    should_automerge): a PR armed while CLEAN that LATER goes BEHIND (its base
    advanced) — or one armed on a poll BEFORE this update-branch logic existed —
    would otherwise stay stuck forever, armed-but-behind. Only MERGEABLE+BEHIND:
    a CONFLICTING branch takes the REBASE agent path (§3a). Self-limiting: after
    an update the branch is no longer BEHIND until the base advances again, so this
    does not re-fire every poll."""
    if not cfg.auto_merge or cfg.identity_mode != IdentityMode.THREE:
        return False
    # Same containment as should_automerge: this PUSHES to the PR's branch, which
    # is not something to do to a PR the harness did not author.
    if not is_governed_branch(pr.head_ref_name):
        return False
    if not pr.is_behind():
        return False
    review = latest_other_review(pr.reviews, cfg.fix_bot)
    if review is None or review.state != REVIEW_APPROVED:
        return False
    return ci_state(pr.checks, cfg.ci_ignore_checks) in (CIState.OK, CIState.NONE)


def reap_actions(world: WorldState) -> list[Action]:
    """When no fixer is alive, an ISSUE still ai-working is orphaned (the run
    died before resetting the label) → requeue it. Port of watcher-lib.sh:reap.

    Where it requeues TO depends on ground truth, not on how the run died
    (re-review P0R-5): an issue that ALREADY HAS AN OPEN PR is repaired to
    ai-pr-open, never to ai-ready. Requeueing such an issue sends it back
    through RESOLVE, which opens a SECOND PR for work that is already up — and
    because the resolve leg has no counter on its success path, nothing bounds
    the repeat. Reproduced: with the follow-up `ai-working → ai-pr-open` relabel
    failing (a renamed label, a 403, or a crash between the PR opening and the
    relabel — and since review S7 that failure is swallowed per-action), the
    reaper requeued every other poll and the harness paid for a full RESOLVE
    session each time, with `resolve_retries` and `infra_deaths` both reading 0.
    Repairing instead of requeueing removes the loop at its source: the label is
    re-derived from the world, so the same tick that observes the PR fixes the
    label, and the PR legs (address-review / fix-ci / rebase) take over.

    The PR arm is GONE (review M22/D9): the SDK port never adds ai-working to a
    PR, so `prs_with(AI_WORKING)` was permanently empty — a reaper arm that
    looked like protection for a busy-marker that does not exist. If a PR-side
    busy marker is ever reintroduced (P2 parallel dispatch), restore this arm
    WITH the code that adds the label, not before."""
    # Issues with an OPEN PR pointing at them. Deliberately NOT filtered by
    # author: whoever opened it, a second PR for the same issue is the harm.
    with_open_pr = {
        n for pr in world.prs if pr.state == "OPEN"
        and (n := _issue_for_pr(pr)) is not None
    }
    return [
        Relabel(
            Target.ISSUE, issue.number, Label.AI_WORKING,
            Label.AI_PR_OPEN if issue.number in with_open_pr else Label.AI_READY,
        )
        for issue in world.issues_with(Label.AI_WORKING)
    ]


def cleanup_actions(
    pr: PRView, issue_number: int | None, cfg: FixConfig,
    issue_observed_pr_open: bool = True,
) -> list[Action]:
    """Merged/closed PR cleanup. Port of cleanup.sh. MERGED → issue ai-pr-open→
    ai-done (+ optional close, since GitHub only auto-closes on default-branch
    merges); CLOSED-unmerged → issue ai-pr-open→ai-ready (re-queue). Always drop
    ai-pr-open from the PR and remove the worktree.

    Also emits `ClearIssueState` (review M4/D8): `clear_issue`/`clear_pr_review`
    had zero production call sites, so a closed-unmerged requeue left
    issue-N.session/.phase alive and the next lifecycle resumed a dead PR's
    session with a preloaded breaker. MERGED clears everything (incl.
    infra-deaths + the pr-N.automerge marker); CLOSED-unmerged only clears the
    resume-blocking markers, keeping resolve-retries/infra-deaths (handover
    §5.7's deliberate conservatism — fewer retries after a human closed the PR).

    `issue_observed_pr_open` gates the whole ISSUE-side arm — RemoveWorktree,
    the issue Relabel, CloseIssue, and the issue half of ClearIssueState
    (review P1R-15). `decide_fix` passes whether the OBSERVED issue, in the
    SAME world snapshot, still carries ai-pr-open. Without this gate: an
    ai-ready issue and its own stale CLOSED/MERGED PR (not yet cleaned up) can
    appear together in one snapshot — decide_fix dispatches a FRESH RunAgent
    on the issue (relabelling it ai-working) in the same poll that also walks
    this stale PR to cleanup. Unconditional issue-side cleanup then deletes the
    worktree the dispatch just created, requeues the issue that just got a new
    session, and wipes the session ClearIssueState had just written — a second
    PR next tick, reachable with NO failure (close a PR, then
    `issue-agent ready N`). The PR-side arm (dropping ai-pr-open from the PR,
    and its half of ClearIssueState) stays UNCONDITIONAL: the PR really is
    closed/merged regardless of what its issue currently shows, and that part
    of the world is never stale. Default True is the ordinary case (the issue
    genuinely still is ai-pr-open) and matches every existing direct caller."""
    actions: list[Action] = []
    merged = pr.state == "MERGED"
    issue_arm = issue_number is not None and issue_observed_pr_open
    if issue_arm:
        actions.append(RemoveWorktree(issue_number))
    if merged:
        if issue_arm:
            actions.append(
                Relabel(Target.ISSUE, issue_number, Label.AI_PR_OPEN, Label.AI_DONE)
            )
            if cfg.close_issue_on_merge:
                actions.append(
                    CloseIssue(
                        issue_number,
                        comment=f"Resolved by #{pr.number} (merged).",
                    )
                )
    else:  # CLOSED unmerged → re-queue the issue
        if issue_arm:
            actions.append(
                Relabel(Target.ISSUE, issue_number, Label.AI_PR_OPEN, Label.AI_READY)
            )
    actions.append(Relabel(Target.PR, pr.number, Label.AI_PR_OPEN, None))
    actions.append(ClearIssueState(pr.number, issue_number if issue_arm else None, merged))
    return actions


def _issue_carries_ai_pr_open(world: WorldState, issue_number: int | None) -> bool:
    """Whether the OBSERVED issue — in THIS world snapshot — still shows
    ai-pr-open. The gate `cleanup_actions` uses (review P1R-15) to tell a
    genuinely-awaiting-cleanup issue from one a fresh dispatch already moved
    off ai-pr-open (or that never carried it, or no longer exists) within the
    same poll."""
    if issue_number is None:
        return False
    return any(i.number == issue_number for i in world.issues_with(Label.AI_PR_OPEN))


def _issue_for_pr(pr: PRView) -> int | None:
    """Linked issue: prefer GitHub's closing-reference, fall back to the branch
    number. Port of issue_for_pr || issue_from_branch."""
    if pr.closing_issue is not None:
        return pr.closing_issue
    return issue_from_branch(pr.head_ref_name)


def breaker_key(pr: PRView) -> int:
    """Stable retry-counter key for a PR. Prefer the linked issue number; when a
    PR has NO resolvable issue (no closing-ref AND a branch without a numeric issue
    — e.g. a Jira-keyed `feat/ACME-2468-…` branch), fall back to the NEGATED PR
    number so the counter still accumulates per-PR. The old `issue_n or -1` sentinel
    collapsed EVERY issue-less PR onto the single dead key -1: it was never written
    (the workflow incremented under the PR's own None→? key) so the breaker never
    tripped → infinite REBASE/CI loop (observed: PR #112). Negated so it can't
    collide with a real issue N. The workflow increments under the SAME key."""
    n = _issue_for_pr(pr)
    return n if n is not None else -pr.number


def review_superseded(review: Review, pr: PRView) -> bool:
    """A CHANGES_REQUESTED review is superseded when the PR head has moved past
    the commit it judged: the fixer already pushed a response, and the next move
    belongs to the REVIEWER (a re-review of the new head), not the fixer.

    Without this gate the fixer re-dispatched ADDRESS_REVIEW on the same stale
    review every poll until a new review arrived — and if the review daemon was
    down, forever, at full session cost (V2/#64). Unknown commit_id or head
    ("") fails open to dispatch: the attempt-counting breaker is the backstop."""
    return bool(
        review.commit_id
        and pr.head_ref_oid
        and review.commit_id != pr.head_ref_oid
    )


# ── circuit breakers ──────────────────────────────────────────────────────────
def breaker_tripped(count: int, cap: int) -> bool:
    """A retry loop has hit its ceiling. cap<=0 disables the breaker (unbounded).
    `count` is attempts already made; tripped once it reaches the cap."""
    return retry_breaker(count, cap).tripped


@dataclass(frozen=True)
class CiPendingStall:
    """One PR whose CI has been PENDING past `CI_PENDING_MAX_SECONDS` (#30).

    The witness form of `pending_deadline_breached`, carrying what an operator
    needs to act: which PR, which head it is stuck on, how long, and the names
    of the checks doing the pinning. `head` is what makes the report one-shot
    per commit — a push produces new check runs, so a stall on a new head is a
    genuinely new fact and is reported again."""

    pr: int
    head: str
    seconds: int
    checks: tuple[str, ...]

    @property
    def reason(self) -> str:
        names = ", ".join(self.checks) or "an unnamed check"
        return (
            f"CI has been pending for {self.seconds // 60} minutes without "
            f"settling ({names}) — a stuck check pins this PR indefinitely, "
            "since a pending check never becomes a failure the fixer can act on."
        )


def fixer_prs(world: WorldState, cfg: FixConfig) -> tuple[PRView, ...]:
    """The OPEN ai-pr-open PRs the fixer may act on: its OWN.

    The reviewer "adopts" rogue PRs (labels any unlabelled OPEN PR ai-pr-open so
    it gets reviewed) — including PRs opened by humans. But the fixer has a
    worktree + session ONLY for its own issue-branch PRs, so dispatching
    ADDRESS_REVIEW/FIX_CI/REBASE on someone else's PR does no work (nothing to
    push) yet still burns a fixer leg and mislabels the PR ai-blocked (observed:
    PR #351, opened by a human, adopted for review, then the fixer commented +
    blocked it). Gate on authorship: a PR whose author is not FIX_BOT belongs to
    a human — leave it for them. An unknown author (`author == ""`, e.g. a PR
    whose author field didn't resolve) or an unset FIX_BOT is treated as
    NOT-ours EXPLICITLY, by the `p.author and cfg.fix_bot` guard below. That
    guard predates the comparison being safe: `login_eq("","")` used to return
    True, so with both blank a stranger's PR with an unresolvable author read as
    the fixer's own — the exact inversion of the fail-safe this comment claimed.
    `login_eq` now returns False when either side is unknown (review P1R-7), so
    the two are belt and braces; the explicit guard stays because a filter this
    consequential should not depend on a helper's edge case being read correctly
    by the next person.

    The match goes through `login_eq`, never `==`: a FIX_BOT written with the
    `[bot]` suffix would otherwise match NO PR, silently disabling every fixer
    PR leg (address-review / fix-ci / rebase / auto-merge) forever (review C1).
    """
    return tuple(
        p for p in world.prs
        if p.has(Label.AI_PR_OPEN) and p.state == "OPEN"
        and p.author and cfg.fix_bot
        and login_eq(p.author, cfg.fix_bot)
    )


def ci_pending_stalls(world: WorldState, cfg: FixConfig) -> tuple[CiPendingStall, ...]:
    """Every fixer PR whose CI has been pending past the deadline this poll.

    THE single definition of "stalled": `decide_fix` escalates exactly what this
    returns and the wiring journals exactly what this returns, so the journal can
    never disagree with the decision it explains (the `issue_holds` pattern).

    Already-escalated PRs are excluded twice over — `is_blocked` covers the poll
    the label lands on nothing else, and the escalation itself swaps ai-pr-open
    for needs-human-review, which takes the PR out of `fixer_prs` for good."""
    return tuple(
        CiPendingStall(
            pr=pr.number,
            head=pr.head_ref_oid,
            seconds=int(age or 0),
            checks=tuple(
                c.name for c in pending_checks(pr.checks, cfg.ci_ignore_checks) if c.name
            ),
        )
        for pr in fixer_prs(world, cfg)
        if not pr_lifecycle(pr).is_blocked
        and pending_deadline_breached(
            pr.checks, world.now, cfg.ci_pending_max_seconds, cfg.ci_ignore_checks
        )
        and (age := pending_age_seconds(pr.checks, world.now, cfg.ci_ignore_checks))
        is not None
    )


def _escalate_pr(pr: int, reason: str) -> list[Action]:
    """Stop dispatching a stuck PR leg: comment why, then swap ai-pr-open for
    needs-human-review so neither the fixer nor the reviewer re-acts on it."""
    return pr_escalation(pr, reason).actions()


def infra_ceiling_hit(state: FixState, cfg: FixConfig, key: int) -> bool:
    """The infra-death breaker (review C3/D2c). A session that dies before
    returning a result records NO attempt — deliberately, so a machine under
    memory pressure can't walk a healthy issue to needs-human-review
    (FAILURE-MODES #58). The cost of that exemption was that nothing bounded
    dying itself: the reaper requeued and the next poll paid for another full
    session. This is the separate, HIGHER ceiling that stops it. cap<=0
    disables, like every other breaker here."""
    return breaker_tripped(state.infra_deaths.get(key, 0), cfg.max_infra_deaths)


def infra_escalation_reason(cap: int) -> str:
    """Human-facing text for an infra-death escalation. Says what the harness
    saw (deaths before any result) so the human looks at the runtime/daemon,
    not at the code the agent never got to touch."""
    return (
        f"the agent session died {cap}× before returning a result — a transport "
        "drop, an external kill, or a daemon restart mid-run, so no attempt was "
        "ever recorded. The harness/runtime needs a human look."
    )


# ── composition ────────────────────────────────────────────────────────────
def decide_fix(
    world: WorldState,
    cfg: FixConfig,
    state: FixState,
    fixer_busy: bool,
) -> list[Action]:
    """One fix-watcher poll. Emits, in poll_once order:

      reap (when idle) → CI pending-deadline escalation (lock-free) →
      duplicate-PR guard (lock-free) → at most ONE fixer-consuming dispatch
      (resolve > resume > address-review > fix-ci, in that priority) →
      auto-merge (lock-free) → cleanup of merged/closed PRs (lock-free).


    Only ONE fixer-consuming action is ever returned per poll (the single-fixer
    invariant). `fixer_busy` reflects the live run-lock and gates the reap step.

    **Single-fixer is enforced by process TOPOLOGY, not by labels** (review
    M22/D9). One daemon per repo (`single_instance` flock) dispatching
    synchronously is the whole mechanism: the poll cannot run while a dispatch
    is in flight, so `consumed` only has to bound ONE poll's emissions. There is
    deliberately no PR-side `ai-working` busy marker — the SDK port never added
    one, and the removes/guards that pretended otherwise were deleted rather
    than left to read as protection a future parallel dispatcher could trust.
    Reintroducing parallelism (P2) means designing a real lease, not restoring
    those label writes.
    """
    actions: list[Action] = []

    # reap runs only when idle (bash: reap() returns early if fixer_busy).
    if not fixer_busy:
        actions.extend(reap_actions(world))

    consumed = fixer_busy
    # Only the fixer's OWN PRs are actionable here — see `fixer_prs`.
    open_prs = list(fixer_prs(world, cfg))

    # 0) CI pending deadline (#30). Lock-free: it dispatches no agent, so unlike
    # every breaker below it does not read `consumed`, and for a given world it
    # emits the same actions whether the fixer is busy or idle. It runs FIRST and
    # REMOVES the stalled PR from `open_prs` so no later pass can act on a PR this
    # poll is handing to a human — escalating and dispatching the same PR in one
    # tick would be incoherent, and a PR whose CI is wedged is exactly the one a
    # rebase/address-review leg cannot help. That filtering imports no lock
    # dependence (the reason the auto-merge pass below deliberately does NOT
    # filter this poll's other escalations) precisely because this arm is
    # lock-free itself.
    for stall in ci_pending_stalls(world, cfg):
        actions.extend(_escalate_pr(stall.pr, stall.reason))
        open_prs = [p for p in open_prs if p.number != stall.pr]

    # 0b) Duplicate-PR guard (#39). Lock-free for the same reason the pass above
    # is: it dispatches no agent and emits the same actions for a given world
    # whether the fixer is busy or idle. It runs BEFORE the resolve pass and
    # names the issues that pass must not dispatch — refusing an issue and
    # resolving it in the same tick would be incoherent, and the whole point is
    # that the resolve never happens.
    guarded: set[int] = set()
    for dup in duplicate_pr_guards(world, cfg):
        actions.extend(dup.actions())
        guarded.add(dup.issue)

    # 1) New ai-ready issue (highest priority).
    if not consumed:
        n = pick_ready_issue(world)
        if n is not None and n in guarded:
            # Refused above (comment + ai-ready→ai-blocked). No fixer is
            # consumed and no other issue is picked this poll: the relabel takes
            # this one out of the ready set, so the next-highest-priority issue
            # is picked next tick — the same one-shot shape the resolve breaker
            # relies on.
            pass
        elif n is not None:
            # Resolve breaker (T3 finding): every other paid leg has a retry
            # ceiling; resolve — the most expensive — had none, so an
            # always-erroring resolve requeued ai-ready and re-dispatched a
            # full session every poll forever. At the cap, hand it to a human.
            # The escalation relabel takes the issue out of the pickable set,
            # so this is one-shot; other ready issues are picked next tick.
            if breaker_tripped(state.resolve_retries.get(n, 0),
                               cfg.max_resolve_retries):
                actions.extend(issue_escalation(
                    n,
                    f"resolve failed {cfg.max_resolve_retries}× without "
                    "opening a PR — this issue needs a human look.",
                    Label.AI_READY,
                ).actions())
            elif infra_ceiling_hit(state, cfg, n):
                # The resolve counter above never sees these (an infra death is
                # not an attempt), so without this the requeue→re-dispatch cycle
                # is unbounded — the C3 loop.
                actions.extend(issue_escalation(
                    n, infra_escalation_reason(cfg.max_infra_deaths), Label.AI_READY,
                ).actions())
            else:
                actions.append(
                    Relabel(Target.ISSUE, n, Label.AI_READY, Label.AI_WORKING)
                )
                actions.append(RunAgent(AgentKind.RESOLVE, n))
                consumed = True

    # 2) Blocked issue with a NEW human reply (first one only).
    if not consumed:
        needing = issues_needing_resume(world, state, cfg)
        if needing:
            n, _at = needing[0]
            if infra_ceiling_hit(state, cfg, n):
                # A resume whose session keeps dying pre-result never consumes
                # the reply (resume_actions returns NOTHING on an infra death),
                # so the dedup mark can't stop it either — only this ceiling can
                # (review C3). ai-blocked is the label the issue carries here.
                actions.extend(issue_escalation(
                    n, infra_escalation_reason(cfg.max_infra_deaths), Label.AI_BLOCKED,
                ).actions())
            else:
                # Flip ai-blocked → ai-working for the duration of the resume run, so an
                # actively-resuming issue reads as working (not still-blocked) and a
                # second poll can't treat it as awaiting-human. Mirrors the RESOLVE
                # dispatch relabel. resume_actions transitions FROM ai-working at the
                # tail (pr-open / back-to-blocked / etc.), so this closes the loop.
                actions.append(
                    Relabel(Target.ISSUE, n, Label.AI_BLOCKED, Label.AI_WORKING)
                )
                actions.append(RunAgent(AgentKind.RESUME, n))
                consumed = True

    # 3) Open PRs: review responses + CI (one fixer-consuming action total).
    for pr in open_prs:
        lifecycle = pr_lifecycle(pr)
        if lifecycle.is_blocked:
            continue  # awaiting human clarification — leave alone
        if not consumed:
            issue_n = _issue_for_pr(pr)
            bkey = breaker_key(pr)  # retry-counter key (issue N, or -PR when issueless)
            # 3·0) A PR the fixer CANNOT resolve to an issue (no closing-ref AND a
            # non-numeric branch — e.g. a Jira-keyed `feat/ACME-2468-…` PR opened
            # outside the harness) has no issue-keyed worktree/session, so every
            # fixer agent (rebase/address-review/fix-ci) early-returns without doing
            # work — re-dispatching forever (observed: PR #112 rebase loop). The
            # fixer can't drive it; hand it to a human ONCE and stop re-acting. Only
            # when it actually needs the fixer (conflict / changes-requested / CI
            # fail); a clean issueless PR is left for the reviewer + auto-merge.
            if issue_n is None:
                _rev = latest_other_review(pr.reviews, cfg.fix_bot)
                needs_fix = (
                    (cfg.auto_rebase_conflicts and pr.is_conflicting())
                    or (_rev is not None and _rev.state == REVIEW_CHANGES_REQUESTED)
                    or ci_state(pr.checks, cfg.ci_ignore_checks) == CIState.FAILURE
                )
                if needs_fix:
                    actions.extend(_escalate_pr(
                        pr.number,
                        "this PR has no linked GitHub issue (no `Closes #n` and a "
                        "branch without an issue number), so the issue-scoped fixer "
                        "can't drive it — a human needs to rebase/fix or link an issue.",
                    ))
                continue
            # 3a) CONFLICTING branch → wake the fixer to rebase onto base. The
            # backup plan for a conflict that slipped past the pre-dispatch
            # file-overlap gate (two in-flight PRs colliding, or an issue that
            # declared no paths). The agent already has the worktree + session —
            # it just needs re-triggering (its session ended at pr_opened, before
            # the conflict existed). Bounded by a breaker → needs-human-review, so
            # a conflict it genuinely can't resolve escalates instead of looping.
            if cfg.auto_rebase_conflicts and pr.is_conflicting():
                if infra_ceiling_hit(state, cfg, bkey):
                    actions.extend(_escalate_pr(
                        pr.number, infra_escalation_reason(cfg.max_infra_deaths)))
                    continue
                if breaker_tripped(state.rebase_retries.get(bkey, 0),
                                   cfg.max_rebase_retries):
                    actions.extend(_escalate_pr(
                        pr.number,
                        f"rebase failed {cfg.max_rebase_retries}× without a clean "
                        "merge — the conflict needs a human.",
                    ))
                    continue
                actions.append(RunAgent(AgentKind.REBASE, pr.number))
                consumed = True
                continue
            review = latest_other_review(pr.reviews, cfg.fix_bot)
            if (
                review is not None
                and review.state == REVIEW_CHANGES_REQUESTED
                and not review_superseded(review, pr)
            ):
                # Circuit breaker: repeated address-review ATTEMPTS on the SAME
                # review escalate to a human rather than looping forever. The
                # counter is keyed to the review's submitted_at — a stored count
                # from an OLDER review reads as 0 (superseded = reset), and
                # nothing else resets it (see FixState.review_retries).
                if infra_ceiling_hit(state, cfg, bkey):
                    actions.extend(_escalate_pr(
                        pr.number, infra_escalation_reason(cfg.max_infra_deaths)))
                    continue
                at, n = state.review_retries.get(bkey, ("", 0))
                count = n if at == review.submitted_at else 0
                if breaker_tripped(count, cfg.max_review_retries):
                    actions.extend(_escalate_pr(
                        pr.number,
                        f"address-review failed {cfg.max_review_retries}× without resolution.",
                    ))
                    continue
                actions.append(RunAgent(AgentKind.ADDRESS_REVIEW, pr.number))
                consumed = True
                continue
            # A superseded CHANGES_REQUESTED review (head moved past it) falls
            # through: the fixer's push is awaiting re-review, but a CI failure
            # on the NEW head is still the fixer's to fix.
            # (The `not lifecycle.is_working` conjunct that used to guard this
            # was deleted with the rest of the phantom PR busy-marker — review
            # M22/D9: nothing ever adds ai-working to a PR, so it was
            # permanently true. See decide_fix's docstring for what actually
            # enforces single-fixer.)
            if ci_state(pr.checks, cfg.ci_ignore_checks) == CIState.FAILURE:
                if infra_ceiling_hit(state, cfg, bkey):
                    actions.extend(_escalate_pr(
                        pr.number, infra_escalation_reason(cfg.max_infra_deaths)))
                    continue
                if breaker_tripped(state.ci_retries.get(bkey, 0),
                                   cfg.max_ci_retries):
                    actions.extend(_escalate_pr(
                        pr.number,
                        f"CI fix failed {cfg.max_ci_retries}× without going green.",
                    ))
                    continue
                actions.append(RunAgent(AgentKind.FIX_CI, pr.number))
                consumed = True
                continue

    # 3c) auto-merge + branch-update: lock-free and INDEPENDENT of the fixer, so
    # they get their OWN pass over the same PRs (review M14). They used to sit at
    # the tail of the loop above, where every `continue` in it — the issueless
    # arm, each breaker escalation, each dispatch — skipped them: an approved,
    # green, ISSUELESS PR armed auto-merge only on the polls where the fixer
    # happened to be BUSY (busy ⇒ the consuming block is skipped ⇒ the `continue`
    # never runs). Same world, opposite outcome depending on an unrelated lock.
    #   arm auto-merge once (marker-guarded), AND
    #   update a merge-ready-but-BEHIND branch so the armed auto-merge lands —
    #   even if it was armed on an earlier poll (CLEAN then base advanced) or
    #   before this logic existed. should_update_branch is marker-independent by
    #   design; both are self-limiting so neither re-fires needlessly.
    # Every predicate here reads the WORLD only (labels, reviews, checks, the
    # marker) — never `consumed`/`fixer_busy` — which is what makes "independent"
    # checkable: for a given world these two arms emit the same actions in both
    # lock states. (A PR escalated on an EARLIER poll carries needs-human-review,
    # so is_blocked keeps it out; a PR escalated on THIS poll is not filtered,
    # because reading this poll's escalations would re-import the lock dependence
    # — escalation itself only happens when the fixer is idle.)
    for pr in open_prs:
        if pr_lifecycle(pr).is_blocked:
            continue  # a human owns the next move — don't arm anything
        if should_automerge(pr, state, cfg):
            actions.append(EnableAutoMerge(pr.number, cfg.merge_method))
        if should_update_branch(pr, cfg):
            actions.append(UpdateBranch(pr.number))

    # 4) Merged/closed PRs → cleanup (lock-free).
    for pr in world.prs:
        if pr_lifecycle(pr).needs_cleanup:
            issue_n = _issue_for_pr(pr)
            actions.extend(cleanup_actions(
                pr, issue_n, cfg,
                issue_observed_pr_open=_issue_carries_ai_pr_open(world, issue_n),
            ))

    return actions
