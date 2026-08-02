"""Fix-watcher decision logic — pure port of watcher.sh:poll_once + helpers.

Structure mirrors the bash: small lesson-bearing cores (pick_ready_issue,
issues_needing_resume, should_automerge, reap_actions, cleanup_actions) plus a
`decide_fix` that composes them in the same priority order poll_once uses.

Orchestration state that lives in STATE_DIR files (sessions, resumed-at marks,
automerge markers) is passed in explicitly as `FixState`, and the live-fixer
run-lock as `fixer_busy` — so the decision stays a pure function of its inputs.

Concurrency is explicit input: the application supplies free dispatch slots and
the active issues/paths. The pure decision fills only disjoint slots; non-fixer
actions (auto-merge, cleanup) remain independent of pool occupancy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .actions import (
    Action,
    AgentKind,
    CancelWorkflowRuns,
    ClearIssueState,
    CloseIssue,
    EnableAutoMerge,
    EscalateCiPending,
    UpdateBranch,
    PostComment,
    Relabel,
    RemoveWorktree,
    RerunFailedChecks,
    RunAgent,
    Target,
)
from .ci import (
    ci_state,
    failed_checks,
    latest_other_review,
    pending_age_seconds,
    parse_timestamp,
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
    # Historical name for the boot-verified server merge gate. True only when
    # branch protection requires review, status checks, and an up-to-date head.
    # Informational: should_automerge's exact-head review+CI preconditions are
    # the whole client-side gate either way. Where the server gate holds it
    # independently re-checks them at merge time and closes the base-advance
    # race an expected-head merge cannot bind; where it cannot hold (an
    # unprotected base, or a private free-plan repo where the protection API
    # itself 403s) that race is the accepted residual — requiring this flag
    # unconditionally silently disabled every merge on such repos (observed
    # 2026-08-01; see residuals.md).
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
    # Give a red PR ONE free `gh run rerun --failed` per head SHA before paying
    # for a FIX_CI leg (#38). A flake costs a CI cycle to disprove and $13–29
    # (plus the review tail its push triggers) to "fix". Off = every FAILURE
    # dispatches a leg immediately, as before.
    ci_free_rerun: bool = True


@dataclass(frozen=True)
class FixState:
    """Persistent orchestration state (STATE_DIR files), passed in purely."""

    sessions: frozenset[int] = frozenset()        # issues with a saved session
    resumed_at: dict[int, str] = field(default_factory=dict)  # issue → last reply ts resumed on
    # PR → last PR-thread reply timestamp consumed by a PR-stage resume. This
    # must not share the issue marker: one issue can have a different issue
    # thread and PR thread, whose timestamps are unrelated.
    pr_resumed_at: dict[int, str] = field(default_factory=dict)
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
    # PR number → the head SHA whose ONE free CI re-run has been spent (#38).
    # Keyed on the SHA, not a boolean, so a push resets the allowance for free:
    # a new head is a genuinely new failure and deserves its own flake test,
    # while a second FAILURE on the SAME head is evidence the diff is broken and
    # goes straight to a paid FIX_CI leg. Missing/"" = never re-run.
    ci_rerun_head: dict[int, str] = field(default_factory=dict)
    # PR → head a branch-refresh was last dispatched for (the advisory-UNSTABLE
    # arm of should_update_branch). Head-keyed like ci_rerun_head: a successful
    # update creates a NEW head (fresh key), an already-up-to-date update
    # changes nothing (marker matches, loop stops).
    update_branch_head: dict[int, str] = field(default_factory=dict)
    # PR → the head SHA whose wedged workflow runs have already been sent a
    # cancel. Head-keyed for the same reason `ci_rerun_head` is, and separate
    # from it because cancel and re-run are two ticks of ONE remediation: the
    # cancel latch stops the harness re-cancelling every poll while GitHub takes
    # the run down, and the re-run marker is what actually spends the budget.
    ci_cancel_head: dict[int, str] = field(default_factory=dict)
    # Paid work currently executing in the application dispatch pool. This is
    # observation state, not persisted orchestration state: it prevents a second
    # leg for the same issue and extends the path-overlap serializer to work that
    # has not opened a PR yet.
    inflight: tuple["InFlightFix", ...] = ()
    # (PR, head SHA) pairs already escalated by the pending-CI deadline. Unlike
    # the label-based one-shot used by older breakers, this survives an
    # operator deliberately restoring ai-pr-open while the same check runs.
    ci_pending_escalated: frozenset[tuple[int, str]] = frozenset()


# ── holds (why a ready issue was NOT picked) ─────────────────────────────────
HOLD_BLOCKER = "blocker"
HOLD_OVERLAP = "overlap"


@dataclass(frozen=True)
class InFlightFix:
    issue: int
    paths: frozenset[str] = frozenset()
    # A leg without a concrete declared/router-predicted path cannot yet be
    # proven disjoint from other work. Keep it exclusive until the router
    # persists a usable prediction and a later observation refreshes this value.
    exclusive: bool = False


@dataclass(frozen=True)
class Hold:
    """One reason ONE ai-ready issue was not dispatched this poll.

    A deliberate hold and an idle bug look identical from outside (harness #6: a
    silent path-overlap hold on #522 cost ~30 minutes of live investigation), so
    every skip `pick_ready_issue` makes silently is also reportable as a value.

    The blocker stays structured all the way to the journal. Presentation can
    therefore say ``waiting on PR #123`` or ``waiting on #42`` without parsing
    prose written by an older process.
    """

    issue: int
    reason: str
    blocker_pr: int | None = None
    blocker_issue: int | None = None


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


def _holds(
    issue: IssueView,
    world: WorldState,
    inflight: tuple[InFlightFix, ...] = (),
) -> tuple[Hold, ...]:
    """Every reason THIS issue is held back, ignoring its label/state (the
    caller decides who is eligible to be held). Empty ⇒ dispatchable.

    THE single definition of "held": `pick_ready_issue` filters on
    `not _holds(...)` and `issue_holds` reports exactly what it returns, so the
    journal can never disagree with the decision it explains."""
    holds: list[Hold] = []
    if issue.blocked_by_open:
        holds.append(
            Hold(
                issue.number,
                HOLD_BLOCKER,
                blocker_issue=issue.open_blocker_numbers[0]
                if issue.open_blocker_numbers
                else None,
            )
        )
    overlap = _overlapping_open_pr(
        issue.declared_paths | issue.predicted_paths, world.prs
    )
    if overlap is not None:
        holds.append(Hold(issue.number, HOLD_OVERLAP, blocker_pr=overlap.number))
    paths = issue.declared_paths | issue.predicted_paths
    for active in inflight:
        if active.issue == issue.number:
            holds.append(
                Hold(issue.number, HOLD_OVERLAP, blocker_issue=active.issue)
            )
            break
        if (
            active.exclusive
            or not paths
            or (active.paths and paths & active.paths)
        ):
            holds.append(
                Hold(issue.number, HOLD_OVERLAP, blocker_issue=active.issue)
            )
            break
    return tuple(holds)


def issue_holds(
    world: WorldState, inflight: tuple[InFlightFix, ...] = ()
) -> tuple[Hold, ...]:
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
        for hold in _holds(issue, world, inflight)
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
def pick_ready_issue(
    world: WorldState, inflight: tuple[InFlightFix, ...] = ()
) -> int | None:
    """Highest-priority ai-ready issue number, or None. Port of watcher.sh §1
    (P1<P2<P3<none; ties broken by issue number for determinism — the bash relied
    on gh's list order, we make it explicit).

    An issue with an OPEN GitHub-native "blocked by" dependency is SKIPPED — its
    prerequisite isn't on the base branch yet, so dispatching it makes the fixer
    build against a missing layer (observed with the P3–P6 series depending on the
    P2 route layer). The blocker resolves via GitHub's dependency graph; when the
    last blocker closes, the issue becomes pickable automatically next poll.

    Also SKIPPED: an issue whose declared or router-predicted file paths overlap
    the changed files of an OPEN PR. Two issues editing the same files in parallel produce a
    guaranteed merge conflict when the second PR tries to land (observed: #59 vs
    #60 both rewired the ConversationService composition root). Holding the second
    issue until the first PR merges serialises the collision away. Fail-open: an
    issue with no usable paths remains fail-open (the gate only fires on a
    concrete, known overlap).

    Both skips are delegated to `_holds`, which `issue_holds` also reads, so the
    hold journal explains THIS decision rather than a copy of it."""
    ready = [
        i for i in world.open_issues()
        if issue_lifecycle(i).is_ready and not _holds(i, world, inflight)
    ]
    if not ready:
        return None
    ready.sort(key=lambda i: (priority_rank(i.labels), i.number))
    return ready[0].number


def last_human_reply(issue: IssueView | PRView, fix_bot: str) -> Comment | None:
    """Latest answer posted after the harness's unblock prompt, or None.

    Public because BOTH sides of the resume dedup must select the SAME comment:
    the decide-side read (issues_needing_resume, comparing created_at against
    the resumed-at mark) and the workflow's write side (set_resumed_at after
    dispatch). When the two sides used different filters the write recorded a
    different comment than the read compared, and one unanswered reply re-fired
    a paid RESUME session every poll (V1/#13).

    A deployment commonly uses the operator's ambient login as FIX_BOT. Author
    filtering therefore made that operator unable to answer their own daemon;
    filtering `[bot]` accounts also excluded the App identity they could switch
    to (#55). The protocol boundary is temporal, not identity-based: the
    harness prompt contains `_Reply to unblock`, and anything posted after the
    latest such prompt is an answer. The prompt itself can never self-trigger.

    The fallback preserves pre-marker blocked threads created by older harness
    versions: choose the latest ordinary human comment under the old rule.
    """
    comments = list(issue.comments)
    prompt_prefixes = (
        "**Agent needs input:**",
        "**Agent still needs input:**",
        "**Agent needs clarification on review:**",
        "**Author needs input:**",
    )
    prompt_index = next(
        (
            index
            for index in range(len(comments) - 1, -1, -1)
            if login_eq(comments[index].author, fix_bot)
            and comments[index].body.startswith(prompt_prefixes)
            and "_Reply to unblock" in comments[index].body
        ),
        None,
    )
    if prompt_index is not None:
        replies = [
            comment
            for comment in comments[prompt_index + 1 :]
            if login_eq(comment.author, fix_bot)
            or not comment.is_bot
            or comment.body.lstrip().startswith("/unblock")
        ]
        return replies[-1] if replies else None
    humans = [
        c
        for c in comments
        if not login_eq(c.author, fix_bot) and not c.is_bot
    ]
    return humans[-1] if humans else None


def _last_human_reply_at(issue: IssueView | PRView, fix_bot: str) -> str | None:
    reply = last_human_reply(issue, fix_bot)
    if reply is None:
        return None
    return reply.created_at or None


def _issues_needing_resume(
    world: WorldState,
    state: FixState,
    cfg: FixConfig,
    require_session: bool,
) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for issue in world.issues:
        lifecycle = issue_lifecycle(issue)
        has_session = issue.number in state.sessions
        if not lifecycle.is_blocked or has_session != require_session:
            continue
        last_at = _last_human_reply_at(issue, cfg.fix_bot)
        if last_at is None:
            continue
        if last_at != state.resumed_at.get(issue.number, ""):
            out.append((issue.number, last_at))
    return out


def issues_needing_resume(
    world: WorldState, state: FixState, cfg: FixConfig
) -> list[tuple[int, str]]:
    """Blocked issues with a session and an unconsumed human reply.

    Kept as the saved-session selector for callers that specifically need a
    runtime continuation. The daemon combines it with sessionless recovery.
    """
    return _issues_needing_resume(world, state, cfg, require_session=True)


def issues_needing_any_resume(
    world: WorldState, state: FixState, cfg: FixConfig
) -> list[tuple[int, str]]:
    """Merge saved and fresh continuations without changing issue priority."""
    by_issue = dict(issues_needing_resume(world, state, cfg))
    by_issue.update(
        _issues_needing_resume(world, state, cfg, require_session=False)
    )
    return [
        (issue.number, by_issue[issue.number])
        for issue in world.issues
        if issue.number in by_issue
    ]


def prs_needing_resume(
    world: WorldState, state: FixState, cfg: FixConfig
) -> list[tuple[int, int, str]]:
    """Blocked fixer PRs with a linked issue session and an unconsumed reply.

    PR-stage legs put their clarification prompt and `ai-blocked` label on the
    PR, not its issue. The reply marker is therefore PR-keyed too: an issue and
    its PR have independent threads, and their GitHub timestamps cannot safely
    share a dedupe namespace.
    """
    out: list[tuple[int, int, str]] = []
    for pr in fixer_prs(world, cfg):
        # `needs-human-review` is also lifecycle-blocked, but it is terminal:
        # an operator reply must never silently reopen an escalation that
        # deliberately removed `ai-blocked` at the retry/infra ceiling.
        if not pr.has(Label.AI_BLOCKED):
            continue
        issue = _issue_for_pr(pr)
        if issue is None or issue not in state.sessions:
            continue
        last_at = _last_human_reply_at(pr, cfg.fix_bot)
        if last_at is not None and last_at != state.pr_resumed_at.get(pr.number, ""):
            out.append((pr.number, issue, last_at))
    return out


def should_automerge(pr: PRView, state: FixState, cfg: FixConfig) -> bool:
    """Port of watcher-lib.sh:maybe_automerge gating. Requires: AUTO_MERGE on, a
    branch the harness itself authored, three-identity mode, an exact-head
    approval by a distinct actor, and exact-head CI ok|none.

    The boot-verified strict server gate (arm_on_open) is deliberately NOT a
    precondition: it is defense-in-depth that additionally closes the
    base-advance race where the base can carry protection. Requiring it
    unconditionally (2026-07-31 hardening) disabled merging entirely on repos
    whose base cannot carry verifiable protection — a private free-plan repo
    403s on the protection API itself — zeroing merges-per-deployment on
    every current target (observed 2026-08-01). The exact-head preconditions
    below are the whole client-side gate either way; the executor merges only
    the observed head (`--match-head-commit`) and never arms persistent
    GitHub auto-merge (its approval would survive later pushes without
    last-push protection). Residual on unprotected bases: the base may
    advance between observation and merge (see residuals.md)."""
    if not cfg.auto_merge:
        return False
    if not pr.head_ref_oid:
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
    if pr.merge_state.upper() != "CLEAN":
        return False
    # arm_on_open: branch protection (verified at boot) already refuses the
    # merge until a review approves and the required checks pass, so waiting
    # for them HERE only adds daemon latency — a busy fixer (one synchronous
    # session per tick) left #539 APPROVED+green and unmerged for the length
    # of an unrelated resolve (2026-07-27). That early-arm optimization is
    # retired: the executor deliberately performs exact-head merges and never
    # arms persistent GitHub auto-merge, so firing before approval would 405
    # against a protected base every poll (#534-class noise). The review/CI
    # preconditions below are the whole client-side gate; server protection,
    # where present, independently re-checks them at merge time.
    review = latest_other_review(pr.reviews, cfg.fix_bot)
    if (
        review is None
        or review.state != REVIEW_APPROVED
        or not review.commit_id
        or review.commit_id != pr.head_ref_oid
    ):
        return False
    # known=False → UNKNOWN, which is deliberately NOT in the allowed pair: an
    # unread checks fetch must never arm a merge (#48). Honest NONE (a repo with
    # no CI, successfully read) stays mergeable.
    return ci_state(
        pr.checks, cfg.ci_ignore_checks, known=pr.checks_known
    ) in (CIState.OK, CIState.NONE)


def update_branch_marker(pr: PRView, base_head: str) -> str:
    """The once-per-attempt key for the advisory-UNSTABLE refresh: one attempt
    per (head, base tip) pair. Head alone is not enough — an attempt made
    BEFORE the base gained the fix for the advisory check would permanently
    disarm the refresh for a PR whose head never moves again (keifu #166's
    exact shape). A no-op update changes neither part, so the loop still
    terminates; a base advance changes the key, re-arming exactly when the
    event this arm exists for occurs. Callers gate on a non-empty base_head
    (an unobserved base disables the arm — see should_update_branch)."""
    return f"{pr.head_ref_oid}:{base_head}"


def should_update_branch(
    pr: PRView, state: FixState, cfg: FixConfig, base_head: str = ""
) -> bool:
    """A merge-ready PR (distinct APPROVED review + CI ok/none) that is BEHIND its
    base needs its branch brought up to date so an armed auto-merge can land it.

    Deliberately INDEPENDENT of the automerge-enabled marker (unlike
    should_automerge): a PR armed while CLEAN that LATER goes BEHIND (its base
    advanced) — or one armed on a poll BEFORE this update-branch logic existed —
    would otherwise stay stuck forever, armed-but-behind. Only MERGEABLE+BEHIND:
    a CONFLICTING branch takes the REBASE agent path (§3a). Self-limiting: after
    an update the branch is no longer BEHIND until the base advances again, so this
    does not re-fire every poll.

    The second arm covers advisory-UNSTABLE (keifu #166, 2026-08-01): a failing
    check WINS the mergeStateStatus enum, so a PR whose only failures are
    operator-ignored/advisory checks reports UNSTABLE, never BEHIND — and when
    the base advances with the fix for that very check (quick-xml audit fixed
    by #168), nothing re-runs the stale checks: the exact-head merge gate
    refuses UNSTABLE forever while decide sees CI as ok. Refreshing the branch
    re-runs the checks against the fixed base. Head-keyed marker: a successful
    update makes a new head (checks pending block the tail until they settle),
    an already-up-to-date one changes nothing and the marker stops the loop —
    this cannot push-loop."""
    if not cfg.auto_merge or cfg.identity_mode != IdentityMode.THREE:
        return False
    if not pr.head_ref_oid:
        return False
    # Same containment as should_automerge: this PUSHES to the PR's branch, which
    # is not something to do to a PR the harness did not author.
    if not is_governed_branch(pr.head_ref_name):
        return False
    # Requires an OBSERVED base tip: the arm exists to react to base advances,
    # and without base visibility the marker key would flap between "H:" and
    # "H:B" on every fetch hiccup, re-firing a no-op update per transition.
    # No base_head → only the BEHIND arm (which needs no marker) applies.
    advisory_unstable_refresh = (
        bool(base_head)
        and pr.merge_state.upper() == "UNSTABLE"
        and not pr.is_conflicting()
        and state.update_branch_head.get(pr.number, "")
        != update_branch_marker(pr, base_head)
    )
    if not pr.is_behind() and not advisory_unstable_refresh:
        return False
    review = latest_other_review(pr.reviews, cfg.fix_bot)
    if (
        review is None
        or review.state != REVIEW_APPROVED
        or not review.commit_id
        or review.commit_id != pr.head_ref_oid
    ):
        return False
    return ci_state(
        pr.checks, cfg.ci_ignore_checks, known=pr.checks_known
    ) in (CIState.OK, CIState.NONE)


def reap_actions(
    world: WorldState, active_issues: frozenset[int] = frozenset()
) -> list[Action]:
    """An ai-working issue with no live leg is orphaned → requeue it.

    Active issue identities are excluded individually. A continuously occupied
    pool must not suppress recovery of an unrelated orphan, while reaping the
    issue a healthy worker owns would double-dispatch it.

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
    # expected_revision comes from the OBSERVED issue, not a live read at
    # command creation (which is a self-fulfilling CAS): a leg completing
    # between this decision and the write performs its own relabel, bumping
    # updated_at, so the stale repair is discarded instead of clobbering the
    # fresh label (keifu #18/#25 dual-label race, 2026-08-02). An issue
    # observed without updated_at carries "" and fails closed by design.
    return [
        Relabel(
            Target.ISSUE, issue.number, Label.AI_WORKING,
            Label.AI_PR_OPEN if issue.number in with_open_pr else Label.AI_READY,
            expected_revision=issue.updated_at,
        )
        for issue in world.issues_with(Label.AI_WORKING)
        if issue.number not in active_issues
    ]


def cleanup_actions(
    pr: PRView, issue_number: int | None, cfg: FixConfig,
    issue_observed_pr_open: bool = True,
    issue_observed_closed: bool = False,
    issue_observed_reopened: bool = False,
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
    genuinely still is ai-pr-open) and matches every existing direct caller.

    `issue_observed_closed` suppresses only the CloseIssue: since the world now
    also carries CLOSED ai-pr-open issues (#28 — that is how a post-merge issue
    becomes observable at all), the ordinary merge cleanup runs against an issue
    GitHub already closed from the mandatory `Fixes #n`. Re-closing it is at best
    a wasted call and at worst a duplicate "Resolved by #N" comment; the harness
    decides that here rather than relying on `gh issue close` treating an
    already-closed issue as a no-op. Default False keeps direct callers on the
    ordinary open-issue path.

    `issue_observed_reopened` suppresses the CloseIssue for the opposite reason:
    the issue is OPEN because a HUMAN put it back (GitHub `stateReason` =
    reopened), which is the #10 recurrence signal — the truest "losing power"
    evidence the harness has, and the one thing review cannot see. Re-closing it
    within one poll would erase it. The ai-done relabel still runs: the steward's
    recurrence reporter selects exactly `open + ai-done + reopened`, so ai-done on
    a reopened issue is what MAKES the signal readable."""
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
            if (
                cfg.close_issue_on_merge
                and not issue_observed_closed
                and not issue_observed_reopened
            ):
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


def orphaned_cleanup_actions(
    world: WorldState, active_issues: frozenset[int] = frozenset()
) -> list[Action]:
    """The ISSUE-driven half of merge cleanup (#28): repair an `ai-pr-open` issue
    whose PR is no longer observable anywhere in the world.

    Cleanup is otherwise driven entirely by the PR — and the PR-side
    `Relabel(PR, -ai-pr-open)` inside `cleanup_actions` is UNCONDITIONAL and lands
    even when the issue-side arm was skipped or its writes failed. One tick later
    the PR carries no label, so `list_pr_states("ai-pr-open", …)` stops covering it
    (its `--state all` results linger in GitHub's stale search index for about a
    day, but `_safe_get_pr` re-reads the PR's REAL labels, so `needs_cleanup` is
    False) and the issue is unreachable from the PR side FOREVER. Verified live on
    the flagship target: 23 closed `ai-pr-open` issues, every one of their merged
    PRs carrying no labels at all. Without this arm the widened closed-issue slice
    would observe those 23 forever without ever draining them — a permanent
    per-poll cost, and no repair.

    It is also the retry loop the pipeline never had. The executor applies each
    action in its own try/except, so a transient `gh` failure on the issue relabel
    used to be unrecoverable (the PR relabel in the same tick removed the only
    handle). Now the next poll sees issue-still-ai-pr-open + PR-not-covering and
    repairs it. One-shot by construction: the repair's own relabel REMOVES
    ai-pr-open, which drops the issue out of both queries that feed this arm.

    **Which issues it will touch, and why not the others.** `covered` is every
    issue linked to a PR that genuinely still carries ai-pr-open — an in-flight PR,
    or a merged one whose cleanup has not run yet. Those belong to the PR-driven
    arm and are left alone. Of the rest:

    * CLOSED → repair to ai-done. Whether GitHub auto-closed it from the mandatory
      `Fixes #n` or a human closed it, the lifecycle is over and the worktree and
      issue state should go.
    * OPEN + `stateReason` "reopened" → repair to ai-done, and (via
      `cleanup_actions`' sibling guard) never re-close. GitHub itself testifies the
      issue was closed and deliberately put back; ai-done is precisely what makes
      it legible to the steward's recurrence reporter.
    * OPEN + never closed → DELIBERATELY NOT TOUCHED. This shape is ambiguous
      between a closed-unmerged requeue whose relabel failed (wants ai-ready) and a
      merged PR on a non-default base whose close failed (wants ai-done), and there
      is no field that separates them: `closedByPullRequestsReferences` carries no
      PR state and is empty for an open issue. Worse, it is INDISTINGUISHABLE from
      a perfectly healthy in-flight issue whose PR was dropped from this one world
      by a transient `_safe_get_pr` failure — and acting there would delete a live
      session's worktree and requeue a paid resolve, the exact P1R-15 harm the
      gate exists to prevent. Guessing wrong costs either a silently abandoned bug
      or a duplicate $13–20 resolve; doing nothing costs one visible stuck label,
      and the widened OPEN ai-pr-open query means an operator can now actually see
      it. Not-acting is the only choice whose worst case is visible."""
    covered = {
        n for p in world.prs
        if p.has(Label.AI_PR_OPEN) and (n := _issue_for_pr(p)) is not None
    }
    actions: list[Action] = []
    for issue in world.issues_with(Label.AI_PR_OPEN):
        if issue.number in covered or issue.number in active_issues:
            continue
        if issue.state == "open" and issue.state_reason != "reopened":
            continue
        actions.append(RemoveWorktree(issue.number))
        actions.append(
            Relabel(Target.ISSUE, issue.number, Label.AI_PR_OPEN, Label.AI_DONE)
        )
        # pr=None: the PR is exactly what we cannot see. Only the issue's half of
        # the state is retirable — a `pr-N.automerge` marker for a PR that merged
        # days ago is inert anyway.
        actions.append(ClearIssueState(None, issue.number, True))
    return actions


def _observed_issue(world: WorldState, issue_number: int | None) -> IssueView | None:
    """The linked issue AS THIS SNAPSHOT SAW IT, or None when the world does not
    carry it (unlinked PR, or an issue outside every slice the builder fetched)."""
    if issue_number is None:
        return None
    return next((i for i in world.issues if i.number == issue_number), None)


def _issue_carries_ai_pr_open(world: WorldState, issue_number: int | None) -> bool:
    """Whether the OBSERVED issue — in THIS world snapshot — still shows
    ai-pr-open. The gate `cleanup_actions` uses (review P1R-15) to tell a
    genuinely-awaiting-cleanup issue from one a fresh dispatch already moved
    off ai-pr-open (or that never carried it, or no longer exists) within the
    same poll.

    Answerable only because the fix world fetches ai-pr-open issues in BOTH
    states (#28): the post-merge issue this gate is really about is closed."""
    observed = _observed_issue(world, issue_number)
    return observed is not None and observed.has(Label.AI_PR_OPEN)


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


# ── wedged-run remediation (keifu PR #177) ───────────────────────────────────
def stalled_run_ids(pr: PRView, cfg: FixConfig) -> tuple[int, ...]:
    """The workflow runs pinning this PR at PENDING, or () when unsafe to act.

    The mirror of `ci_rerun_run_ids` for the pending side. A pending EXTERNAL
    status (or any check whose details URL carried no `/actions/runs/<id>`) has
    no immutable run identity, so it cannot be cancelled by SHA-verified id and
    the whole PR falls through to the human escalation that predates this path —
    cancelling a subset would leave the real pin in place while looking handled.
    """

    pending = pending_checks(pr.checks, cfg.ci_ignore_checks)
    if not pending or any(check.run_id is None for check in pending):
        return ()
    return tuple(sorted({c.run_id for c in pending if c.run_id is not None}))


CANCEL_MARKER_SEP = "@"


def parse_cancel_marker(raw: str) -> tuple[str, str]:
    """`(head, decided_at)` from a ci-cancel marker.

    The marker dates the cancel because the wait for GitHub to act on it cannot
    be bounded by the stall clock: cancelling a wedged run is precisely what
    lets follow-on jobs start, and one fresh pending check resets
    `pending_age_seconds` (adversarial review S2 - a PR sat in await_cancel for
    11 simulated days). Markers written before the timestamp existed are bare
    SHAs and parse as `(head, "")`, which falls back to the weaker stall-clock
    bound rather than escalating a PR mid-upgrade.
    """

    head, sep, decided_at = (raw or "").partition(CANCEL_MARKER_SEP)
    return head, decided_at if sep else ""


def cancel_grace_breached(cfg: FixConfig, decided_at: str, now: str) -> bool:
    """Whether a cancel already issued for this head has had long enough.

    The ADR 0012 ceiling on the await-cancel wait: the cancel gets ONE further
    full pending deadline to take GitHub's run down, measured from WHEN THE
    CANCEL WAS DECIDED. Derived from the stall deadline rather than configured
    separately so an operator who tunes one tunes both.
    """

    if cfg.ci_pending_max_seconds <= 0:
        return False
    since = parse_timestamp(decided_at)
    at = parse_timestamp(now)
    if since is None or at is None:
        # An UNDATED marker cannot be waited on: the only other clock available
        # is the stall clock, and that is precisely the one a cancel resets by
        # letting follow-on jobs start — the S2 hole, kept alive in a fallback
        # (adversarial review N2). So an undated marker means the grace is
        # already gone: escalate rather than wait on a bound that may never
        # arrive. Only pre-upgrade markers are undated; a cancel decided by this
        # code always carries the poll clock, because a stall cannot be detected
        # at all unless `world.now` parses.
        return True
    return (at - since).total_seconds() >= cfg.ci_pending_max_seconds


class CiStallStep(str, Enum):
    """What the harness does about ONE stalled CI head this poll."""

    # Take the wedged runs down so their failed jobs become rerunnable.
    CANCEL = "cancel"
    # Cancel already issued for this head; GitHub has not completed the run yet.
    # Deliberately no action: cancellation completing is the next tick's
    # observation, not something to block a daemon poll on.
    AWAIT_CANCEL = "await_cancel"
    # Budget or ceiling spent, or the runs cannot be safely identified — hand to
    # a human, exactly as the pending deadline did before remediation existed.
    ESCALATE = "escalate"


@dataclass(frozen=True)
class CiStallRemedy:
    """One stalled head plus the step the harness takes about it.

    THE single definition of the remediation, for the same reason
    `ci_pending_stalls` is the single definition of "stalled": `decide_fix`
    emits exactly this and the wiring journals exactly this, so the journal can
    never disagree with the decision it explains.
    """

    stall: CiPendingStall
    step: CiStallStep
    issue: int = 0
    run_ids: tuple[int, ...] = ()
    detail: str = ""
    # The poll clock, carried into the cancel so the marker can date it.
    decided_at: str = ""

    @property
    def pr(self) -> int:
        return self.stall.pr

    @property
    def head(self) -> str:
        return self.stall.head

    @property
    def escalation_body(self) -> str:
        if not self.stall.checks and self.stall.seconds <= 0:
            # A synthesised stall (the post-cancel green illusion) has no
            # pending clock and no pinning checks, so `CiPendingStall.reason`
            # would report a nonexistent "pending for 0 minutes".
            return self.detail
        return f"{self.stall.reason} {self.detail}".strip()

    def action(self) -> Action | None:
        """The remote effect this step needs, or None while waiting."""
        if self.step is CiStallStep.CANCEL:
            return CancelWorkflowRuns(
                self.pr, self.head, self.run_ids, self.decided_at
            )
        if self.step is CiStallStep.ESCALATE:
            return EscalateCiPending(self.pr, self.head, self.escalation_body)
        return None


def ci_stall_remedies(
    world: WorldState, cfg: FixConfig, state: FixState
) -> tuple[CiStallRemedy, ...]:
    """The remediation step for every stalled head this poll.

    A wedged run is not a failure the fixer can act on and not a re-run GitHub
    will accept: `rerun-failed-jobs` on an `in_progress` run is refused with
    HTTP 403 "already running", which made the harness's re-run capability dead
    on arrival for exactly the runs that needed it (keifu PR #177 — a job pinned
    `in_progress` for 60+ minutes; the operator cancelled, waited for the run to
    complete, re-ran the failed jobs, and CI went green).

    So the deadline now spends its ONE free re-run instead of a human's time:
    cancel → (next ticks) await completion → the cancelled run reads as a
    settled FAILURE, and the pre-existing `free_ci_rerun` arm re-runs it. That
    is why cancellation takes no budget of its own — the re-run it unblocks is
    the budget, and a head that has already spent it escalates on sight.
    """

    prs = {pr.number: pr for pr in fixer_prs(world, cfg)}
    remedies: list[CiStallRemedy] = []
    for stall in ci_pending_stalls(world, cfg):
        if (stall.pr, stall.head) in state.ci_pending_escalated:
            continue
        pr = prs.get(stall.pr)
        issue = (_issue_for_pr(pr) or 0) if pr is not None else 0
        run_ids = stalled_run_ids(pr, cfg) if pr is not None else ()
        cancelled_head, cancelled_at = parse_cancel_marker(
            state.ci_cancel_head.get(stall.pr, "")
        )

        if not stall.head:
            # No SHA means no way to prove a cancel targets THIS commit's runs
            # rather than a push that landed since, and no way to key either
            # marker. Fail to the human path, never to an unguarded cancel.
            step = CiStallStep.ESCALATE
            detail = (
                "The harness could not resolve the PR head SHA, so it could not "
                "safely cancel the stuck run (a cancel is only issued for runs "
                "whose head matches the commit under test)."
            )
        elif not cfg.ci_free_rerun:
            # Cancelling is only worth doing because the free re-run picks the
            # cancelled run back up. With re-runs disabled the cancel would
            # destroy the run and buy a PAID repair leg pointed at a failure
            # nobody produced, so the operator's switch turns this off too.
            step = CiStallStep.ESCALATE
            detail = (
                "Free CI re-runs are disabled, so the harness will not cancel "
                "the stuck run it could not then re-run."
            )
        elif state.ci_rerun_head.get(stall.pr, "") == stall.head:
            step = CiStallStep.ESCALATE
            detail = (
                "This commit's one free CI re-run was already spent and CI has "
                "wedged again, so the harness stops rather than re-running in a "
                "loop."
            )
        elif cancelled_head == stall.head:
            if cancel_grace_breached(cfg, cancelled_at, world.now):
                step = CiStallStep.ESCALATE
                detail = (
                    "The harness cancelled the stuck workflow run, but it was "
                    "still not completed a full deadline later \u2014 GitHub never "
                    "took it down, so the re-run could not be issued."
                )
            else:
                step = CiStallStep.AWAIT_CANCEL
                detail = "cancel issued; awaiting run completion before the re-run"
        elif not run_ids:
            step = CiStallStep.ESCALATE
            detail = (
                "The pending checks carry no GitHub Actions run identity (an "
                "external status, or an unrecognised details URL), so the "
                "harness cannot cancel and re-run them."
            )
        else:
            step = CiStallStep.CANCEL
            detail = (
                "cancelling the wedged workflow run so its failed jobs become "
                "rerunnable"
            )
        remedies.append(
            CiStallRemedy(stall, step, issue, run_ids, detail, world.now)
        )
    return tuple(remedies)


def ci_cancel_green_illusions(
    world: WorldState, cfg: FixConfig, state: FixState
) -> tuple[CiStallRemedy, ...]:
    """Heads the harness cancelled that came back GREEN BY OMISSION.

    The remediation assumes a cancelled run observes as a settled FAILURE, which
    is what the re-run then acts on. That holds for a job cancelled mid-flight,
    but GitHub reports jobs that had NOT STARTED when the run was cancelled as
    `skipped`, and `skipped` buckets as PASS. A run stalled in `queued` — the
    starved-runner case — can therefore come back with nothing red at all.

    Left alone, `ci_state` would flip PENDING → OK and an already-approved PR
    would be auto-merged on the strength of CI the harness itself cancelled
    (adversarial review S3). So a head with a live cancel marker and no spent
    re-run is NOT allowed to read as green: it goes to a human, once, with the
    reason. The re-run spending its budget is what clears the marker's authority
    — after that the CI result is GitHub's again, not the harness's artefact.

    Deliberately gated on the MARKER alone, not on `ci_pending_max_seconds` or
    `ci_free_rerun` (adversarial review N3): those switches decide whether a
    cancel may be ISSUED, and turning one off afterwards does not un-cancel a
    run already taken down. A live marker means this harness cancelled this
    head, and that stays true — and stays disqualifying — whatever the config
    says next.
    """

    illusions: list[CiStallRemedy] = []
    for pr in fixer_prs(world, cfg):
        head = pr.head_ref_oid
        cancelled_head, _at = parse_cancel_marker(
            state.ci_cancel_head.get(pr.number, "")
        )
        if (
            not head
            or cancelled_head != head
            or state.ci_rerun_head.get(pr.number, "") == head
            or (pr.number, head) in state.ci_pending_escalated
            or pr_lifecycle(pr).is_blocked
        ):
            continue
        if ci_state(
            pr.checks, cfg.ci_ignore_checks, known=pr.checks_known
        ) not in (CIState.OK, CIState.NONE):
            continue
        illusions.append(
            CiStallRemedy(
                CiPendingStall(pr.number, head, 0, ()),
                CiStallStep.ESCALATE,
                _issue_for_pr(pr) or 0,
                (),
                # Says ONLY what is known. The harness cannot tell a run whose
                # jobs were skipped by the cancellation from one that finished
                # green on its own (a cancel that never landed — the mint failed
                # — leaves the same marker), so it must not assert the first:
                # an escalation that explains itself wrongly sends the human
                # looking in the wrong place (adversarial review N1).
                "The harness requested cancellation of this commit's stuck "
                "workflow run, and CI now reports nothing red. It cannot tell "
                "whether those checks actually ran or were skipped by the "
                "cancellation, so this green is unverified — re-run CI before "
                "merging.",
                world.now,
            )
        )
    return tuple(illusions)


def ci_remediations(
    world: WorldState, cfg: FixConfig, state: FixState
) -> tuple[CiStallRemedy, ...]:
    """Every CI-remediation step this poll: wedged heads, then cancel aftermath.

    THE single derivation both halves read. `decide_fix` emits exactly this and
    the wiring journals exactly this, so an escalation can never happen without
    a record explaining it (adversarial review N5 — the aftermath arm escalated
    outside the reporter's view, which is the failure
    `test_the_journal_agrees_with_the_escalation_it_explains` exists to forbid).
    """

    return (
        *ci_stall_remedies(world, cfg, state),
        *ci_cancel_green_illusions(world, cfg, state),
    )


# ── flake-aware CI repair (#38) ──────────────────────────────────────────────
def free_ci_rerun(pr: PRView, state: FixState, cfg: FixConfig) -> bool:
    """Whether this PR's ONE free CI re-run is still unspent for its CURRENT
    head — i.e. a FAILURE here should be re-run rather than repaired.

    An UNKNOWN head ("" — the adapter couldn't resolve headRefOid) returns
    False: the marker keys on the SHA, so with no SHA the executor could not
    record that the allowance was spent, and every poll would re-run the same
    checks forever. Falling through to the paid leg is the old behaviour, which
    is bounded by the CI retry breaker; an unbounded free re-run loop is not
    bounded by anything."""
    if (
        not cfg.ci_free_rerun
        or not pr.head_ref_oid
        or not ci_rerun_run_ids(pr, cfg)
    ):
        return False
    return state.ci_rerun_head.get(pr.number, "") != pr.head_ref_oid


def ci_rerun_run_ids(pr: PRView, cfg: FixConfig) -> tuple[int, ...]:
    """Exact workflow runs covering every failure, or empty when unsafe.

    A failed external status or malformed Actions observation has no immutable
    workflow-run identity. Mixing it with identified runs must fall through to
    the bounded repair leg; rerunning only a subset could hide the real gate.
    """

    failures = failed_checks(pr.checks, cfg.ci_ignore_checks)
    if not failures or any(check.run_id is None for check in failures):
        return ()
    return tuple(sorted({check.run_id for check in failures if check.run_id is not None}))


@dataclass(frozen=True)
class CiFlake:
    """A PR whose head spent its free re-run and is now GREEN — i.e. the failure
    that would have bought a FIX_CI leg was a flake, and the leg never happened.

    The confirmation half of #38: the re-run itself is journalled when it fires
    (`ci_rerun`), but "was it actually a flake?" is only knowable one CI cycle
    later, from the world. That makes it a derivation over world+state rather
    than an event any code path can emit at the moment it happens — the same
    reason `CiPendingStall` exists."""

    pr: int
    head: str


def ci_flakes(
    world: WorldState, cfg: FixConfig, state: FixState
) -> tuple[CiFlake, ...]:
    """Every fixer PR that used its free re-run on its current head and is now
    settled OK. The measurement behind the metric #38 moves (repair-leg share of
    spend): each one is a FIX_CI leg — $13–29 plus its review tail — that was
    not bought. Pure; the wiring journals exactly this, deduped per (PR, head)."""
    return tuple(
        CiFlake(pr=pr.number, head=pr.head_ref_oid)
        for pr in fixer_prs(world, cfg)
        if pr.head_ref_oid
        and state.ci_rerun_head.get(pr.number, "") == pr.head_ref_oid
        and ci_state(pr.checks, cfg.ci_ignore_checks, known=pr.checks_known)
        == CIState.OK
    )


def _escalate_pr(
    pr: int, reason: str, from_label: Label = Label.AI_PR_OPEN
) -> list[Action]:
    """Stop dispatching a stuck PR leg: comment why, then swap ai-pr-open for
    needs-human-review so neither the fixer nor the reviewer re-acts on it."""
    return pr_escalation(pr, reason, from_label).actions()


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
    dispatch_slots: int | None = None,
) -> list[Action]:
    """One fix-watcher poll. Emits, in poll_once order:

      reap (when idle) → CI pending-deadline escalation (lock-free) →
      duplicate-PR guard (lock-free) → bounded fixer-consuming dispatches
      (PR repairs > issue resume > new resolve) →
      auto-merge (lock-free) → cleanup of merged/closed PRs (lock-free).

    A red PR's FIRST failure on a head emits `RerunFailedChecks` INSTEAD of the
    FIX_CI dispatch (#38) — a free GitHub-side action, so it consumes no fixer
    either.

    `dispatch_slots=None` preserves the legacy single-fixer contract: idle means
    one slot, busy means none. The fix daemon passes its pool's current free
    slots explicitly. A legacy external busy lock still gates the whole reaper;
    pool-backed state identifies active issues, so unrelated orphans can recover
    while healthy live legs remain protected.

    Concurrency is enforced twice: this pure decision reserves issue identities
    and known paths while filling the supplied slots, and the application pool
    independently bounds workers and rejects duplicate issue keys. There is
    deliberately no PR-side `ai-working` marker — active dispatch state is the
    lease, including PR legs mapped back to their linked issue.
    """
    actions: list[Action] = []

    active_issues = frozenset(active.issue for active in state.inflight)
    # A legacy external lock exposes only a boolean, so it cannot identify the
    # issue to protect and retains global suppression. Pool state carries exact
    # identities and can safely reap every other orphan.
    if not fixer_busy or state.inflight:
        actions.extend(reap_actions(world, active_issues))

    remaining = (
        (0 if fixer_busy else 1)
        if dispatch_slots is None
        else max(0, dispatch_slots)
    )
    inflight = list(state.inflight)
    issues_by_number = {issue.number: issue for issue in world.issues}

    def dispatchable(issue: int, paths: frozenset[str]) -> bool:
        exclusive = not paths
        return all(
            active.issue != issue
            and not exclusive
            and not active.exclusive
            and not (active.paths and paths & active.paths)
            for active in inflight
        )

    def claim(issue: int, paths: frozenset[str]) -> None:
        nonlocal remaining
        inflight.append(InFlightFix(issue, paths, exclusive=not paths))
        remaining -= 1

    def issue_paths(issue: int) -> frozenset[str]:
        view = issues_by_number.get(issue)
        if view is None:
            return frozenset()
        return view.declared_paths | view.predicted_paths

    # Only the fixer's OWN PRs are actionable here — see `fixer_prs`.
    open_prs = list(fixer_prs(world, cfg))
    pr_resume_replies = {
        pr: (issue, reply_at)
        for pr, issue, reply_at in prs_needing_resume(world, state, cfg)
    }

    # 0) CI pending deadline (#30). Lock-free: it dispatches no agent, so unlike
    # every breaker below it does not read the dispatch budget, and for a given world it
    # emits the same actions whether the fixer is busy or idle. It runs FIRST and
    # REMOVES the stalled PR from `open_prs` so no later pass can act on a PR this
    # poll is handing to a human — escalating and dispatching the same PR in one
    # tick would be incoherent, and a PR whose CI is wedged is exactly the one a
    # rebase/address-review leg cannot help. That filtering imports no lock
    # dependence (the reason the auto-merge pass below deliberately does NOT
    # filter this poll's other escalations) precisely because this arm is
    # lock-free itself.
    #
    # Since keifu PR #177 the deadline no longer goes straight to a human: a
    # wedged run is first cancelled so its failed jobs become rerunnable, and the
    # existing free re-run (#38) picks it up once GitHub reports the run
    # complete. Cancel, wait and escalate all remove the PR from `open_prs` —
    # while the harness is unwedging CI, no repair leg may be dispatched at it.
    for remedy in ci_remediations(world, cfg, state):
        action = remedy.action()
        if action is not None:
            actions.append(action)
        open_prs = [p for p in open_prs if p.number != remedy.pr]

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

    # 1) Open PR repairs: finish nearly-landed work before starting a fresh,
    # expensive resolve. Under continuous ready-issue load, the old resolve-first
    # order starved ADDRESS_REVIEW/FIX_CI/REBASE for 88 minutes (#56).
    for pr in open_prs:
        lifecycle = pr_lifecycle(pr)
        if lifecycle.is_blocked:
            # PR-stage blockers are answered on the PR, while the saved agent
            # transcript belongs to its linked issue. A qualifying reply gets a
            # dedicated PR-keyed dispatch; all other blocked PRs remain quiet.
            if remaining and pr.number in pr_resume_replies:
                bkey = breaker_key(pr)
                issue, _reply_at = pr_resume_replies[pr.number]
                paths = pr.changed_files | issue_paths(issue)
                if not dispatchable(issue, paths):
                    continue
                if infra_ceiling_hit(state, cfg, bkey):
                    actions.extend(_escalate_pr(
                        pr.number, infra_escalation_reason(cfg.max_infra_deaths),
                        Label.AI_BLOCKED,
                    ))
                    continue
                actions.append(RunAgent(AgentKind.PR_RESUME, pr.number))
                claim(issue, paths)
            continue
        if remaining:
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
                    or ci_state(pr.checks, cfg.ci_ignore_checks, known=pr.checks_known)
                    == CIState.FAILURE
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
                paths = pr.changed_files | issue_paths(issue_n)
                if not dispatchable(issue_n, paths):
                    continue
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
                claim(issue_n, paths)
                continue
            review = latest_other_review(pr.reviews, cfg.fix_bot)
            if (
                review is not None
                and review.state == REVIEW_CHANGES_REQUESTED
                and not review_superseded(review, pr)
            ):
                paths = pr.changed_files | issue_paths(issue_n)
                if not dispatchable(issue_n, paths):
                    continue
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
                claim(issue_n, paths)
                continue
            # A superseded CHANGES_REQUESTED review (head moved past it) falls
            # through: the fixer's push is awaiting re-review, but a CI failure
            # on the NEW head is still the fixer's to fix.
            # (The `not lifecycle.is_working` conjunct that used to guard this
            # was deleted with the rest of the phantom PR busy-marker — review
            # M22/D9: nothing ever adds ai-working to a PR, so it was
            # permanently true. See decide_fix's docstring for the live lease.)
            if (
                ci_state(pr.checks, cfg.ci_ignore_checks, known=pr.checks_known)
                == CIState.FAILURE
            ):
                paths = pr.changed_files | issue_paths(issue_n)
                if not dispatchable(issue_n, paths):
                    continue
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
                # Flake first (#38): one free `gh run rerun --failed` per head
                # SHA before any paid repair. Two FIX_CI legs of one deployment
                # night ($13.15 + $28.88, plus a $8 review tail on the second)
                # both "fixed" the same pre-existing flaky test that neither
                # PR's diff had touched — ≈$80 of a $188 batch. A re-run costs
                # one CI cycle and no tokens.
                #
                # AFTER the breakers on purpose: a PR that has already burnt its
                # CI-fix budget belongs to a human, and delaying that handover by
                # a CI cycle would move the escalation-latency the wrong way. In
                # practice the two never meet — the free re-run fires on the
                # FIRST failure of a head, when the counter is 0.
                #
                # Emits no RunAgent, so the dispatch budget is untouched: this costs no
                # fixer and another PR may still dispatch a real leg this poll.
                # The re-run flips the checks to PENDING, and the #49
                # pending-outranks-failure rule then makes the fixer wait for
                # them without any special casing here — while #30's pending
                # deadline stops that wait from becoming the new deadlock.
                if free_ci_rerun(pr, state, cfg):
                    actions.append(
                        RerunFailedChecks(
                            pr.number,
                            pr.head_ref_oid,
                            ci_rerun_run_ids(pr, cfg),
                        )
                    )
                    continue
                actions.append(RunAgent(AgentKind.FIX_CI, pr.number))
                claim(issue_n, paths)
                continue

    # 2) Blocked issues with NEW answers, up to the remaining disjoint slots.
    if remaining:
        needing = issues_needing_any_resume(world, state, cfg)
        for n, _at in needing:
            if not remaining:
                break
            paths = issue_paths(n)
            if not dispatchable(n, paths):
                continue
            if infra_ceiling_hit(state, cfg, n):
                actions.extend(issue_escalation(
                    n, infra_escalation_reason(cfg.max_infra_deaths), Label.AI_BLOCKED,
                ).actions())
            else:
                issue = issues_by_number[n]
                claim_action = Relabel(
                    Target.ISSUE,
                    n,
                    Label.AI_BLOCKED,
                    Label.AI_WORKING,
                    issue.updated_at,
                )
                actions.append(claim_action)
                actions.append(RunAgent(AgentKind.RESUME, n, claim_action))
                claim(n, paths)

    # 3) New ai-ready issues, only after repair work and only while slots remain.
    while remaining:
        n = pick_ready_issue(world, tuple(inflight))
        if n is not None and n in guarded:
            # The duplicate guard above relabels this issue out of the ready set;
            # let the next poll pick the next candidate.
            break
        elif n is not None:
            if breaker_tripped(
                state.resolve_retries.get(n, 0), cfg.max_resolve_retries
            ):
                actions.extend(issue_escalation(
                    n,
                    f"resolve failed {cfg.max_resolve_retries}× without "
                    "opening a PR — this issue needs a human look.",
                    Label.AI_READY,
                ).actions())
                break
            elif infra_ceiling_hit(state, cfg, n):
                actions.extend(issue_escalation(
                    n, infra_escalation_reason(cfg.max_infra_deaths), Label.AI_READY,
                ).actions())
                break
            else:
                issue = issues_by_number[n]
                claim_action = Relabel(
                    Target.ISSUE,
                    n,
                    Label.AI_READY,
                    Label.AI_WORKING,
                    issue.updated_at,
                )
                actions.append(claim_action)
                actions.append(RunAgent(AgentKind.RESOLVE, n, claim_action))
                claim(n, issue_paths(n))
        else:
            break

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
    # marker) — never dispatch budget / pool occupancy — which makes "independent"
    # checkable: for a given world these two arms emit the same actions in both
    # lock states. (A PR escalated on an EARLIER poll carries needs-human-review,
    # so is_blocked keeps it out; a PR escalated on THIS poll is not filtered,
    # because reading this poll's escalations would re-import pool dependence —
    # escalation itself only happens when a dispatch slot is available.)
    for pr in open_prs:
        if pr_lifecycle(pr).is_blocked:
            continue  # a human owns the next move — don't arm anything
        if should_automerge(pr, state, cfg):
            actions.append(
                EnableAutoMerge(
                    pr.number, cfg.merge_method, pr.head_ref_oid
                )
            )
        if should_update_branch(pr, state, cfg, world.base_head):
            actions.append(
                UpdateBranch(pr.number, pr.head_ref_oid, world.base_head)
            )

    # 4) Merged/closed PRs → cleanup (lock-free).
    for pr in world.prs:
        if pr_lifecycle(pr).needs_cleanup:
            issue_n = _issue_for_pr(pr)
            if issue_n in active_issues:
                # A terminal worker can still emit lifecycle follow-ups. Defer
                # the whole cleanup transaction so it cannot resurrect a closed
                # issue or lose a terminal relabel after this poll publishes it.
                continue
            observed = _observed_issue(world, issue_n)
            actions.extend(cleanup_actions(
                pr, issue_n, cfg,
                issue_observed_pr_open=_issue_carries_ai_pr_open(world, issue_n),
                issue_observed_closed=observed is not None and observed.state != "open",
                issue_observed_reopened=(
                    observed is not None and observed.state_reason == "reopened"
                ),
            ))

    # 5) Issue-driven cleanup (#28): the mirror of (4) for an issue whose PR the
    # world can no longer see at all. Lock-free, and deliberately AFTER (4) so
    # `covered` reflects the same snapshot the PR-driven arm just consumed — the
    # two are disjoint by construction, never both acting on one issue.
    actions.extend(orphaned_cleanup_actions(world, active_issues))

    return actions
