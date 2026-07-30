"""Assemble the three role daemons from config + adapters.

Each `build_*_daemon` binds:
  * observe  → gateway.build_world (the GitHub slice the role needs),
  * decide   → the bound pure decision function (decide_fix / decide_review /
               decide_steward-triage),
  * executor → applies the leaf Actions; its `run_agent` drives the (async)
               AgentDispatcher for RunAgent actions.

The dispatcher is async (SDK query is a coroutine). Each paid fix leg owns a
bounded background worker and event loop; observation and lifecycle writes
remain on the daemon thread. Review and steward keep synchronous dispatch.
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Awaitable, Callable
from dataclasses import replace
from functools import partial

from ..adapters.agent_runner import AgentRunner
from ..adapters.github import GitHubGateway, Identity
from ..adapters.journal import (
    Journal,
    JournalingRuntime,
    default_ledger_path,
    ci_flake_keys,
    ci_pending_keys,
    duplicate_pr_keys,
    read_events_tail,
    recurrence_keys,
    unsettled_starts,
)
from ..adapters.store import Store
from ..adapters.wake import WakeSignal, wake_path
from ..adapters.worktree import Worktrees
from ..domain.fix import (
    FixConfig,
    FixState,
    InFlightFix,
    breaker_key,
    ci_flakes,
    ci_pending_stalls,
    decide_fix,
    duplicate_pr_guards,
    issue_holds,
)
from ..domain.labels import Label
from ..domain.lifecycle import retry_breaker
from ..domain.review import (
    DEFAULT_MAX_UNPRODUCTIVE_ROUNDS,
    IdentityMode,
    ReviewConfig,
    ReviewState,
    decide_review,
)
from .planners.tasks import FixTaskConfig, GroomTaskConfig, JiraTaskConfig, ReviewTaskConfig, TriageTaskConfig
from ..domain.triage import (
    TriageConfig,
    TriageState,
    parse_codeowners_default,
    grooming_candidates,
    triage_candidates,
)
from ..domain.jira import jira_phase, jira_sync_needed
from ..domain.actions import AgentKind, Relabel, RunAgent, Target
from ..domain.agent_task import AgentRuntime
from .config import HarnessConfig
from .dispatch import AgentDispatcher, DispatchConfig
from .execute import Executor
from .loop import Daemon
from .mcp import mcp_servers_from_file
from .spend_gate import build_daily_spend_gate


class CancellableAsyncDispatch:
    """Run one dispatch coroutine per worker thread with cooperative shutdown.

    Each worker owns its event loop, while the daemon main thread retains a
    thread-safe cancellation handle. Cancelling the root task propagates into
    the async agent runner, whose subprocess cleanup terminates its process
    group before the worker exits.
    """

    def __init__(
        self,
        dispatch: Callable[[RunAgent], Awaitable[None]],
        finished: Callable[[RunAgent], None] | None = None,
    ) -> None:
        self._dispatch = dispatch
        self._finished = finished
        self._lock = threading.Lock()
        self._active: dict[
            int,
            tuple[
                asyncio.AbstractEventLoop,
                asyncio.Task[None],
                threading.Event,
            ],
        ] = {}
        # Sticky: cancellation may win the race before a submitted pool worker
        # reaches __call__. Every later registration observes this edge too.
        self._shutdown = threading.Event()

    def _new_cancel_event(self) -> threading.Event:
        return threading.Event()

    async def _run(
        self, action: RunAgent, cancel_requested: threading.Event
    ) -> None:
        task = asyncio.create_task(self._dispatch(action))
        try:
            while not task.done():
                if cancel_requested.is_set():
                    task.cancel()
                # call_soon_threadsafe is the normal shutdown path. The bounded
                # wait is a portability fallback for event loops whose self-pipe
                # wake is unavailable during teardown.
                await asyncio.wait((task,), timeout=1.0)
            await task
        except asyncio.CancelledError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            raise

    def __call__(self, action: RunAgent) -> None:
        loop = asyncio.new_event_loop()
        cancel_requested = self._new_cancel_event()
        task = loop.create_task(self._run(action, cancel_requested))
        token = id(task)
        with self._lock:
            self._active[token] = (loop, task, cancel_requested)
            if self._shutdown.is_set():
                cancel_requested.set()
                task.cancel()
        try:
            loop.run_until_complete(task)
        finally:
            with self._lock:
                self._active.pop(token, None)
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
            if self._finished is not None:
                self._finished(action)

    def cancel_all(self) -> None:
        with self._lock:
            self._shutdown.set()
            active = tuple(self._active.values())
        for loop, task, cancel_requested in active:
            cancel_requested.set()
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                # The worker may have completed and closed its loop after the
                # snapshot. Its future is already settling; continue cancelling
                # the remaining workers.
                continue


class DispatchPathReservations:
    """Atomic issue/path leases shared by admission and running routers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._paths: dict[int, frozenset[str]] = {}
        self._actions: dict[RunAgent, int] = {}

    def _conflicts(self, issue: int, paths: frozenset[str]) -> bool:
        return any(
            other_issue != issue
            and (
                not paths
                or not other_paths
                or bool(paths & other_paths)
            )
            for other_issue, other_paths in self._paths.items()
        )

    def reserve(
        self, action: RunAgent, issue: int, paths: frozenset[str]
    ) -> bool:
        with self._lock:
            if issue in self._paths or self._conflicts(issue, paths):
                return False
            self._paths[issue] = paths
            self._actions[action] = issue
            return True

    def expand(self, issue: int, paths: frozenset[str]) -> bool:
        """Atomically add router predictions; one colliding router loses."""
        with self._lock:
            expanded = self._paths.get(issue, frozenset()) | paths
            if self._conflicts(issue, expanded):
                return False
            self._paths[issue] = expanded
            return True

    def finish(self, action: RunAgent) -> None:
        with self._lock:
            issue = self._actions.pop(action, None)
            if issue is not None:
                self._paths.pop(issue, None)

    def issue_for(self, action: RunAgent) -> int | None:
        with self._lock:
            return self._actions.get(action)

    def snapshot(self) -> tuple[InFlightFix, ...]:
        with self._lock:
            return tuple(
                InFlightFix(issue, paths, exclusive=not paths)
                for issue, paths in self._paths.items()
            )


def _log(name: str):
    import sys

    def log(msg: str) -> None:
        sys.stderr.write(msg + "\n")
        sys.stderr.flush()  # unbuffered: a crash right after must not eat the line

    return log


def _onoff(enabled: bool) -> str:
    return "on" if enabled else "off"


def fix_capability_line(*, auto_merge: bool, auto_rebase: bool,
                        escalate_on_evidence: bool, arm_on_open: bool) -> str:
    """One boot line saying what the fixer can DO in this configuration."""
    return (
        "fix: capabilities: resolve=on "
        f"auto_merge={_onoff(auto_merge)} auto_rebase={_onoff(auto_rebase)} "
        f"escalate_on_evidence={_onoff(escalate_on_evidence)} "
        f"arm_on_open={_onoff(arm_on_open)}"
    )


def review_capability_line(*, can_approve: bool, adopt_unlabeled: bool) -> str:
    """One boot line for the reviewer. `approve=off` is solo identity mode: the
    reviewer still reviews and comments, but the verdict never becomes a GitHub
    approval, so nothing it does can merge anything."""
    return (
        "review: capabilities: review=on "
        f"approve={_onoff(can_approve)} adopt_unlabeled={_onoff(adopt_unlabeled)}"
    )


def steward_capability_line(*, triage: bool, promote: bool, jira_sync: bool,
                            recurrence: bool, groom: bool = False) -> str:
    """One boot line for the steward — the role that can be configured into
    doing literally nothing (#46).

    An operator watching three healthy daemons reasonably concludes three things
    are happening. With STEWARD_TRIAGE=0, STEWARD_PROMOTE=0 and JIRA_SYNC=0 the
    steward polls forever with no work it can act on, and in `ps`, in the log
    directory and in `issue-agent status` it is indistinguishable from the two
    daemons that are working. So say it, in the log, at boot.

    Recurrence reporting is reported separately from the verdict because it is
    passive: it journals reopened `ai-done` issues, it never dispatches an agent,
    and it is structurally inert without a FIX_BOT to attribute merges to. It is
    therefore not "work the daemon can do" in the sense the NONE verdict means.
    """
    recurrence_note = (
        "recurrence=watch" if recurrence else "recurrence=inert (FIX_BOT unset)"
    )
    if triage or groom or promote or jira_sync:
        return (
            "steward: capabilities: "
            f"triage={_onoff(triage)} groom={_onoff(groom)} promote={_onoff(promote)} "
            f"jira_sync={_onoff(jira_sync)} {recurrence_note}"
        )
    return (
        "steward: capabilities: NONE — this daemon will poll but has no work it "
        "can do (STEWARD_TRIAGE=0 STEWARD_GROOM=0 STEWARD_PROMOTE=0 JIRA_SYNC=0)"
        f" — {recurrence_note}"
    )


def _journal(store: Store) -> Journal:
    """Each role journals beside its own state, because each role already has an
    isolated STATE_DIR — so three daemons never contend on one file. The GLOBAL
    ledger is the deliberate exception: cost is the one quantity that must be
    summed across every role and repo, and POSIX append atomicity makes sharing
    it safe without a lock."""
    return Journal(os.path.join(store.state_dir, "journal.jsonl"))


def _wake_check(cfg: HarnessConfig, store: Store):
    """The role's push wake, or None when WEBHOOK_WAKE is off (poll only).

    Watched beside the role's own state, like the journal: the forwarder touches
    one wake file per role so a delivery wakes all three independently.

    Interaction with the world probe: a webhook-shortened tick lands inside the
    probe's TTL, and check_suite/status deliveries move NO issue/PR updated_at —
    change_signature reads unchanged and the tick observes nothing. That is fine
    for review/steward (neither reads checks), and the FIXER only stays correct
    because `fix_probe_safe` refuses to gate whenever an ai-pr-open PR exists —
    exactly when a checks event matters. Weaken that predicate and CI-completion
    wakes silently become no-ops until the TTL expires."""
    if not cfg.webhook_wake:
        return None
    return WakeSignal(wake_path(store.state_dir)).pending


def _spend_cfg(cfg: HarnessConfig, role: str, ledger: str) -> dict:
    """The spend-breaker slice of DispatchConfig, identical for all three roles."""
    return {
        "role": role,
        "repo": cfg.repo,
        "spend_ledger_path": ledger,
        "issue_spend_cap_usd": cfg.issue_spend_cap_usd,
        "daily_spend_cap_usd": cfg.daily_spend_cap_usd,
        "global_daily_spend_cap_usd": cfg.global_daily_spend_cap_usd,
        "alert_cmd": cfg.alert_cmd,
    }


def _spend_gate(cfg: HarnessConfig, ledger: str):
    """The daily caps as a `Daemon.tick_gate` — checked before observe, so a
    capped daemon costs no GraphQL quota and writes no labels (#27). Every role
    that can dispatch a paid run gets one; all three share the DispatchConfig
    caps, so all three are gated."""
    return build_daily_spend_gate(
        ledger,
        repo=cfg.repo,
        repo_cap=cfg.daily_spend_cap_usd,
        global_cap=cfg.global_daily_spend_cap_usd,
    )


def _build_codex_runner(
    cfg: HarnessConfig,
    *,
    default_model: str | None,
    sandbox: str,
    inject_system_prompt: bool,
    resume_on_retry: bool,
    leg_role: str,
    log,
):
    """Build either role's Codex adapter from the single runtime config.

    Fix and review deliberately differ only in their capability boundary: fixes
    need the git-capable sandbox plus a worktree-installed governor, while a
    review is read-only. Keeping every transport, rollout-pricing, and budget
    option here prevents a new Codex accounting control reaching one paid role
    while silently skipping the other.

    `leg_role` states which side of that boundary the leg is on, in the leg's own
    process environment. The governor needs it because read-only-ness is declared
    by a generated hook script at the reviewer's cwd, and a fixer worktree nested
    under that cwd resolves to the same script (#113). The dispatching runner is
    the only place that knows the role for certain, so it says so here.
    """
    from ..adapters.codex_runner import CodexProvider, CodexRunner

    runtime = cfg.codex_runtime
    return CodexRunner(
        codex_bin=runtime.binary,
        provider=CodexProvider(
            provider_id=runtime.provider_id,
            name=runtime.provider_name,
            base_url=runtime.base_url,
            env_key=runtime.env_key,
            wire_api=runtime.wire_api,
        ),
        sandbox=sandbox,
        resume_on_retry=resume_on_retry,
        env={"ISSUE_AGENT_LEG_ROLE": leg_role},
        reasoning_effort=runtime.reasoning_effort,
        inject_system_prompt=inject_system_prompt,
        log=log,
        debug=os.environ.get("ISSUE_AGENT_DEBUG", "0") not in ("", "0", "false", "False"),
        idle_timeout=(runtime.idle_timeout if runtime.idle_timeout > 0 else None),
        max_runtime=(runtime.max_runtime if runtime.max_runtime > 0 else None),
        transport_retries=runtime.transport_retries,
        backoff_base=runtime.backoff_base,
        rate_limit_backoff_base=runtime.rate_limit_backoff_base,
        max_rate_limit_wait=runtime.max_rate_limit_wait,
        max_rate_limit_retries=runtime.max_rate_limit_retries,
        cost_per_mtok_in=runtime.cost_per_mtok_in,
        cost_per_mtok_out=runtime.cost_per_mtok_out,
        cost_per_mtok=runtime.cost_per_mtok,
        enforce_budget=runtime.enforce_budget,
        **({"default_model": default_model} if default_model else {}),
    )


def _process_deaths(store: Store) -> dict[int, int]:
    """Paid runs this role's journal shows starting and never settling, keyed
    the way the `infra-deaths` counter is (review P0R-4).

    A session the DAEMON did not outlive — SIGKILL, OOM, `systemctl restart`
    mid-run — reaches none of the bump sites, because every one of them is code
    that runs after the death. The `agent_run_start` line written before the
    await is the only durable trace, so the decide side reconstructs the count
    from it. Read-side only: nothing new is written on the hot path, and the
    scan is a bounded tail of a file this role already owns."""
    return unsettled_starts(read_events_tail(_journal(store).path))


def _inject_predicted_paths(store: Store, world):
    """Attach persisted router predictions to this tick's GitHub issue views.

    GitHub is the source of truth for labels and PR diffs, while predictions are
    local, advisory routing state. Joining them here keeps the domain decision
    pure and makes any unreadable state indistinguishable from no prediction.
    """
    get_predictions = getattr(store, "get_predicted_paths", None)
    if get_predictions is None:
        return world
    issues = []
    changed = False
    for issue in world.issues:
        try:
            predicted = get_predictions(issue.number)
        except Exception:  # noqa: BLE001 — a hint must never stop a fix tick
            predicted = frozenset()
        combined = issue.predicted_paths | frozenset(predicted or ())
        changed = changed or combined != issue.predicted_paths
        issues.append(replace(issue, predicted_paths=combined))
    return replace(world, issues=tuple(issues)) if changed else world


def _prediction_outcome_reporter(journal: Journal | None, repo: str, store: Store, log):
    """Journal each terminal PR's predicted-versus-actual files exactly once.

    This runs before cleanup clears the per-issue prediction state. The event is
    intentionally raw sets plus their intersection: operators can calculate hit
    rate and inspect false positives without another GitHub read.
    """
    seen: set[str] = set()

    def report(world) -> None:
        get_predictions = getattr(store, "get_predicted_paths", None)
        if get_predictions is None:
            return
        prior = read_events_tail(journal.path) if journal is not None else ()
        prior_keys = {
            event.get("block_key") for event in prior
            if event.get("event") == "path_prediction"
        }
        for pr in world.prs:
            if pr.state not in ("MERGED", "CLOSED") or pr.closing_issue is None:
                continue
            try:
                predicted = frozenset(get_predictions(pr.closing_issue) or ())
            except Exception:  # noqa: BLE001 — observability is fail-open
                continue
            if not predicted:
                continue
            key = f"path-prediction:{repo}:{pr.closing_issue}:{pr.number}"
            if key in seen or key in prior_keys:
                continue
            seen.add(key)
            matched = predicted & pr.changed_files
            log(
                f"PATH PREDICTION: issue #{pr.closing_issue} / PR #{pr.number} — "
                f"{len(matched)}/{len(predicted)} predicted files changed"
            )
            if journal is not None:
                journal.emit(
                    "path_prediction", issue=pr.closing_issue, pr=pr.number,
                    predicted_paths=sorted(predicted),
                    changed_files=sorted(pr.changed_files),
                    matched_paths=sorted(matched), block_key=key,
                )

    return report


def _fix_state(
    store: Store,
    world,
    inflight: tuple[InFlightFix, ...] = (),
) -> FixState:
    """Read the per-issue/PR orchestration state the fix decision needs."""
    sessions = frozenset(i.number for i in world.issues if store.has_session(i.number))
    resumed = {i.number: store.get_resumed_at(i.number) for i in world.issues}
    pr_resumed = {p.number: store.get_pr_resumed_at(p.number) for p in world.prs}
    automerge = frozenset(p.number for p in world.prs if store.automerge_enabled(p.number))
    # Retry counters are keyed by breaker_key(pr): the linked issue number, or the
    # NEGATED PR number when a PR has no resolvable issue (Jira-keyed branch, no
    # closing-ref). Must match the workflow's increment key exactly, else the
    # breaker reads 0 forever and the retry loop never escalates (was PR #112).
    keys = {breaker_key(p) for p in world.prs}
    ci_retries = {k: store.get_ci_retries(k) for k in keys}
    # Full (review_at, count) tuple: the domain compares the stored timestamp
    # against the CURRENT review's submitted_at so a superseding review reads
    # as count 0 (see FixState.review_retries).
    review_retries = {k: store.get_review_retry(k) for k in keys}
    rebase_retries = {k: store.get_rebase_retries(k) for k in keys}
    resolve_retries = {i.number: store.get_resolve_retries(i.number) for i in world.issues}
    # Infra deaths (review C3) span BOTH key spaces: the issue legs (resolve /
    # resume) escalate on the issue number, the PR legs on breaker_key(pr) —
    # and the dispatcher writes under whichever one the dying leg used. Read the
    # union, or a leg escalates on a counter nobody reads.
    #
    # The store counter only ever sees deaths the PROCESS SURVIVED. Deaths that
    # took the daemon with them (SIGKILL/OOM/restart) left an unsettled
    # `agent_run_start` in the journal and nothing else, so they are added here
    # — the one place where both halves can be summed without either write side
    # knowing about the other (review P0R-4). A raise-path death is in the store
    # counter AND has a settled `transport_death` line, so `unsettled_starts`
    # nets it to zero and it is counted exactly once.
    #
    # A PR's entry SUMS both key spaces (review P1R-1, matching `_review_state`).
    # When the dispatcher's pre-handler `issue_for_pr` lookup fails — and the
    # network flakiness that drops a session is exactly the flakiness that fails
    # that lookup — the death lands under `-pr` while the decide side asks for
    # `breaker_key(pr)`, splitting one loop across two counters so the ceiling
    # never trips. Summing folds the orphaned half back into the key the domain
    # actually reads. `alt` is skipped when it IS the breaker key (an issueless
    # PR, where breaker_key already == -pr), so nothing is counted twice.
    deaths = dict(_process_deaths(store))
    # Observation now runs while these dispatches are alive. Their durable
    # start receipts are intentionally unsettled until the leg finishes, but
    # they are not process deaths. Remove one reconstructed death per active
    # dispatch key; historical unsettled rounds under the same key remain.
    for active in inflight:
        if deaths.get(active.issue, 0) > 0:
            deaths[active.issue] -= 1

    def _infra_for_pr(p) -> int:
        k = breaker_key(p)
        alt = -p.number
        total = store.get_infra_deaths(k) + deaths.get(k, 0)
        if alt != k:
            total += store.get_infra_deaths(alt) + deaths.get(alt, 0)
        return total

    infra_deaths = {breaker_key(p): _infra_for_pr(p) for p in world.prs}
    for i in world.issues:
        infra_deaths.setdefault(
            i.number, store.get_infra_deaths(i.number) + deaths.get(i.number, 0)
        )
    # #38: the head SHA each PR's ONE free CI re-run was spent on. PR-keyed (not
    # breaker-keyed) because a re-run is an act on a PR's check runs, and a PR
    # with no resolvable issue has check runs like any other.
    ci_rerun_head = {p.number: store.get_ci_rerun_head(p.number) for p in world.prs}
    get_ci_pending_head = getattr(
        store, "get_ci_pending_escalated_head", lambda _pr: ""
    )
    ci_pending_escalated = frozenset(
        (p.number, head)
        for p in world.prs
        if (head := get_ci_pending_head(p.number))
    )
    return FixState(
        sessions=sessions, resumed_at=resumed, pr_resumed_at=pr_resumed,
        automerge_enabled=automerge,
        ci_retries=ci_retries, review_retries=review_retries,
        rebase_retries=rebase_retries, resolve_retries=resolve_retries,
        infra_deaths=infra_deaths, ci_rerun_head=ci_rerun_head,
        ci_pending_escalated=ci_pending_escalated,
        inflight=inflight,
    )


def _hold_reporter(
    journal: Journal | None,
    repo: str,
    log,
    issue_is_blocked: Callable[[int], bool] | None = None,
):
    """Report defer begin/clear transitions without steady-state tick noise.

    `pick_ready_issue` skips an ai-ready issue whose files overlap an in-flight
    PR, or that has an open GitHub dependency — silently, which is
    indistinguishable from a fixer that is simply broken (harness #6: a hold on
    #522 cost ~30 minutes of live investigation before anyone could tell which
    it was).

    The journal is the restart-safe edge memory. A fresh daemon reconstructs
    active deferrals before comparing the current world, so it neither repeats
    a begin nor loses the eventual clear.
    """
    active: dict[tuple[int, str, int | None, int | None], dict] | None = None

    def key_for(hold) -> tuple[int, str, int | None, int | None]:
        return (hold.issue, hold.reason, hold.blocker_pr, hold.blocker_issue)

    def fields_for(key):
        issue, reason, blocker_pr, blocker_issue = key
        if reason == "human":
            return {
                "issue": issue,
                "reason": reason,
                "blocker": "human",
            }
        return {
            "issue": issue,
            "reason": reason,
            "blocker_pr": blocker_pr,
            "blocker_issue": blocker_issue,
        }

    def waiting_label(key) -> str:
        issue, reason, blocker_pr, blocker_issue = key
        if reason == "human":
            return f"issue #{issue} waiting on human input"
        if blocker_pr is not None:
            return f"issue #{issue} waiting on PR #{blocker_pr}"
        if blocker_issue is not None:
            return f"issue #{issue} waiting on #{blocker_issue}"
        return f"issue #{issue} waiting on an open dependency"

    def event_key(event) -> tuple[int, str, int | None, int | None] | None:
        try:
            issue = int(event.get("issue") or 0)
        except (TypeError, ValueError):
            return None
        if issue <= 0:
            return None
        blocker_pr = event.get("blocker_pr")
        blocker_issue = event.get("blocker_issue")
        legacy_blocker = str(event.get("blocker") or "")
        if blocker_pr is None and legacy_blocker.lower().startswith("pr#"):
            try:
                blocker_pr = int(legacy_blocker[3:])
            except ValueError:
                blocker_pr = None
        return (issue, str(event.get("reason") or ""), blocker_pr, blocker_issue)

    def report(world, inflight=()) -> None:
        nonlocal active
        if active is None:
            active = {}
        if journal is not None:
            # Executor-owned human edges can land between polls. Replaying the
            # durable edge log keeps this reporter synchronized across both
            # daemon restarts and in-process writers.
            active = {}
            for event in read_events_tail(journal.path):
                if event.get("event") not in {"deferred", "hold"}:
                    continue
                key = event_key(event)
                if key is None:
                    continue
                if event.get("state") == "cleared":
                    active.pop(key, None)
                elif event.get("state") == "waiting" or event.get("event") == "hold":
                    active[key] = event

        current = {
            key_for(hold): hold for hold in issue_holds(world, tuple(inflight))
        }
        current.update({
            (issue.number, "human", None, None): issue
            for issue in world.open_issues()
            if issue.has(Label.AI_BLOCKED)
        })
        observed_issue_numbers = {issue.number for issue in world.issues}
        for key, event in active.items():
            if key[1] != "human" or key[0] in observed_issue_numbers:
                continue
            if issue_is_blocked is None:
                current[key] = event
                continue
            try:
                still_blocked = issue_is_blocked(key[0])
            except Exception as exc:  # noqa: BLE001 - unread is not an unblock
                log(f"issue #{key[0]} human-wait probe failed: {exc}")
                current[key] = event
            else:
                if still_blocked:
                    current[key] = event
        for key in current.keys() - active.keys():
            message = waiting_label(key)
            log(f"DEFERRED: {message}; dispatches once that clears")
            if journal is not None:
                journal.emit("deferred", state="waiting", **fields_for(key))
            active[key] = fields_for(key)

        for key in active.keys() - current.keys():
            message = waiting_label(key)
            log(
                f"{message.replace(' waiting on ', ' no longer waiting on ')}; "
                "eligible for dispatch"
            )
            if journal is not None:
                journal.emit("deferred", state="cleared", **fields_for(key))
            active.pop(key, None)

    return report


def _ci_stall_reporter(journal: Journal | None, repo: str, fix_cfg: FixConfig, log):
    """Build the per-poll reporter for a CI pending-deadline breach (#30).

    The escalation itself is `decide_fix`'s (a comment + needs-human-review);
    this is the machine-readable half. Without it a wedged check leaves NO
    trace: a pending PR dispatches no leg, so no retry counter moves and no
    `agent_run` is written, and "the PR simply never moves and nothing anywhere
    says why" was the whole complaint.

    Same two-channel shape as `_hold_reporter`, and reads the SAME pure
    derivation `decide_fix` escalates on, so the journal cannot disagree with
    the action. The LOG line fires every poll it is derivable; the JOURNAL line
    fires once per (PR, head SHA) — a re-push that wedges again is a new stall,
    the same stuck run is not. In practice the escalation relabel takes the PR
    out of the fixer's set on the first breach, so this dedup is the backstop
    for the poll where that relabel fails, and for the daemon restarts a hot
    reload produces.
    """
    seen: set[str] = set()

    def report(world) -> None:
        for stall in ci_pending_stalls(world, fix_cfg):
            checks = ", ".join(stall.checks) or "unnamed"
            log(
                f"CI STALL: PR #{stall.pr} has been pending {stall.seconds}s "
                f"({checks}) — pending deadline breached"
            )
            key = f"ci-pending:{repo}:{stall.pr}:{stall.head}"
            if key in seen:
                continue
            seen.add(key)
            if journal is None:
                continue
            if key in ci_pending_keys(read_events_tail(journal.path)):
                continue
            journal.emit(
                "ci_pending_deadline", pr=stall.pr, head=stall.head,
                seconds=stall.seconds, checks=list(stall.checks), block_key=key,
            )

    return report


def _duplicate_pr_reporter(journal: Journal | None, repo: str, fix_cfg: FixConfig, log):
    """Build the per-poll reporter for the duplicate-PR guard (#39).

    The refusal itself is `decide_fix`'s (a comment + ai-ready → ai-blocked);
    this is the machine-readable half, and it reads the SAME pure derivation the
    decision does, so the journal cannot disagree with the action.

    The metric #39 moves is "wasted duplicate resolves" — one in five issues on
    the night it was found — and these events ARE the count of resolves not
    bought. Two channels, as everywhere else here: the LOG line every poll the
    condition holds (normally exactly one, since the relabel clears it), the
    JOURNAL line once per (issue, PR).
    """
    seen: set[str] = set()

    def report(world) -> None:
        for dup in duplicate_pr_guards(world, fix_cfg):
            log(
                f"DUPLICATE PR: issue #{dup.issue} is ai-ready but open PR "
                f"#{dup.pr} already closes it — skipping the resolve and "
                "moving the issue to ai-blocked"
            )
            key = f"duplicate-pr:{repo}:{dup.issue}:{dup.pr}"
            if key in seen:
                continue
            seen.add(key)
            if journal is None:
                continue
            if key in duplicate_pr_keys(read_events_tail(journal.path)):
                continue
            journal.emit(
                "duplicate_pr_guard", issue=dup.issue, pr=dup.pr,
                author=dup.author, block_key=key,
            )

    return report


def _ci_flake_reporter(journal: Journal | None, repo: str, fix_cfg: FixConfig, log):
    """Build the per-poll reporter for a CONFIRMED flake (#38).

    `ci_rerun` (written by the Executor) says a free re-run fired; this says it
    WORKED — the checks that would have bought a $13–29 FIX_CI leg went green on
    a re-run of the identical commit. That is only observable one CI cycle after
    the fact, from world state, which is why it is a reporter over
    `domain.fix.ci_flakes` rather than an event some code path emits inline.

    Counting these against `ci_rerun` gives the flake rate the issue's metric
    needs (repair-leg share of spend), so the JOURNAL line is deduped per
    (PR, head) — the derivation stays true for every poll until the PR merges.
    """
    seen: set[str] = set()

    def report(world, state) -> None:
        for flake in ci_flakes(world, fix_cfg, state):
            log(
                f"CI FLAKE: PR #{flake.pr} went green on a re-run of the same "
                f"commit ({flake.head[:7]}) — no CI-fix leg was dispatched"
            )
            key = f"ci-flake:{repo}:{flake.pr}:{flake.head}"
            if key in seen:
                continue
            seen.add(key)
            if journal is None:
                continue
            if key in ci_flake_keys(read_events_tail(journal.path)):
                continue
            journal.emit(
                "ci_flake", pr=flake.pr, head=flake.head, block_key=key,
            )

    return report


def _recurrence_reporter(journal: Journal | None, gateway, repo: str, fix_bot: str, log):
    """Build the per-poll reporter for LITERAL reopens (harness #10, kind=reopen).

    A merged PR is not ground truth that the problem is fixed. The truest
    "losing power" signal is a resolved issue that comes back — and it is
    invisible to review, which is why it gets recorded here rather than inferred
    from the existing counters.

    **Why `stateReason`, not "open again".** The harness merges to a non-default
    base branch, so GitHub never auto-closes the referenced issue; the harness
    closes it itself, and only when `close_issue_on_merge` is set. An ai-done
    issue that is open therefore means either "a human reopened it" or "the close
    has not happened / is not configured" — a distinction worth one gh call,
    because the second case is every ai-done issue on a default deployment and
    would journal a recurrence for each of them, forever.

    Same two-channel shape as `_hold_reporter`: the LOG line fires every pass
    (free, and answers "why is this labelled recurrence?" from any single poll),
    the JOURNAL line once per issue — a reopen is a LEVEL that persists until
    someone closes the issue again, and the recurrence RATE is computed by
    counting these events, so duplicates would corrupt the metric rather than
    merely add noise.

    Cost: ZERO extra gh calls in the steady state. `issue_state_reason` is asked
    only about issues that are BOTH open and ai-done — a set that is empty on a
    healthy repo. A gateway without the method (a fake, a legacy adapter) yields
    an inert reporter rather than an AttributeError out of the decide closure.
    """
    state_reason = getattr(gateway, "issue_state_reason", None)
    recent_merges = getattr(gateway, "recently_merged_issues", None)
    if state_reason is None:
        return lambda _world: None
    seen: set[str] = set()

    def report(world) -> None:
        reopened = [
            i.number for i in world.issues
            if i.state == "open" and i.has(Label.AI_DONE)
            and state_reason(i.number) == "reopened"
        ]
        if not reopened:
            return
        # Only now is the merged list worth a call: it exists solely to name the
        # PR that shipped the thing that came back.
        shipped: dict[int, int] = {}
        if recent_merges is not None and fix_bot:
            try:
                shipped = {m.issue: m.pr for m in (recent_merges(fix_bot) or ())}
            except Exception as exc:  # noqa: BLE001 — enrichment, never fatal
                log(f"recurrence: could not read merged PRs ({exc})")
        for number in reopened:
            log(
                f"RECURRENCE: issue #{number} was resolved and is open again "
                "(reopened)"
            )
            key = f"recurrence:{repo}:{number}:reopen"
            if key in seen:
                continue
            seen.add(key)
            if journal is None:
                continue
            if key in recurrence_keys(read_events_tail(journal.path)):
                continue
            # of_issue == issue: a reopen is an issue recurring as ITSELF, which
            # keeps one event shape for both kinds so the metric reads them
            # together. No label and no comment here — the reopened issue is
            # already the visible artifact, and the harness must never write to
            # an issue a human just deliberately reopened.
            journal.emit(
                "recurrence", kind="reopen", issue=number, of_issue=number,
                pr=shipped.get(number, 0), block_key=key,
            )

    return report


def build_fix_daemon(
    cfg: HarnessConfig,
    gateway: GitHubGateway,
    store: Store,
    worktrees: Worktrees,
    runner: AgentRunner,
    fixer_busy=lambda: False,
) -> Daemon:
    repo_cfg = cfg.repository
    fix_runtime = cfg.fix_runtime
    codex_runtime = cfg.codex_runtime
    fix_cfg = FixConfig(
        fix_bot=fix_runtime.bot,
        identity_mode=repo_cfg.identity_mode,
        auto_merge=cfg.auto_merge,
        merge_method=cfg.merge_method,
        ci_ignore_checks=cfg.ci_ignore_checks,
        ci_pending_max_seconds=cfg.ci_pending_max_seconds,
        duplicate_pr_guard=cfg.duplicate_pr_guard,
        ci_free_rerun=cfg.ci_free_rerun,
        close_issue_on_merge=cfg.close_issue_on_merge,
        max_ci_retries=fix_runtime.max_ci_retries,
        max_review_retries=fix_runtime.max_review_retries,
        auto_rebase_conflicts=fix_runtime.auto_rebase_conflicts,
        max_rebase_retries=fix_runtime.max_rebase_retries,
        max_resolve_retries=fix_runtime.max_resolve_retries,
        max_infra_deaths=fix_runtime.max_infra_deaths,
        arm_on_open=cfg.arm_on_open_active,
    )
    journal = _journal(store)
    ledger = default_ledger_path()
    dcfg = DispatchConfig(
        base_branch=repo_cfg.base_branch,
        fix=FixTaskConfig(
            repo_cfg.repo, repo_cfg.base_branch, cfg.agent_md,
            fix_runtime.budget_usd, fix_runtime.model, fix_runtime.effort,
        ),
        max_review_rejections=cfg.max_review_rejections,
        author_tests_split=fix_runtime.author_tests_split,
        max_contract_rounds=fix_runtime.max_contract_rounds,
        fix_mcp_config=fix_runtime.mcp_config,
        harness_root=cfg.runtime_paths.harness_root,
        log=_log("fix"),
        fix_bot=fix_runtime.bot,
        fixer_runtime=cfg.fixer_runtime,
        escalate_on_evidence=cfg.escalate_on_evidence,
        # The same ceiling `fix_cfg` escalates on, so a deterministic death can
        # saturate the counter rather than pay for cap-1 identical retries (#13).
        max_infra_deaths=fix_runtime.max_infra_deaths,
        **_spend_cfg(cfg, "fix", ledger),
    )
    # Fixer runtime: Codex (GPT-5.5) swaps the runner AND installs the governor as
    # a Codex hook in each worktree (the Claude runner gets its governor via SDK
    # hooks=, so prepare_cwd stays None there). Reviewer/steward always use Claude.
    fix_runner = runner
    prepare_cwd = None
    if cfg.fixer_runtime == "codex":
        from ..adapters.codex_runner import (
            codex_budget_not_enforced_warning,
            codex_cost_blindspot_warning,
        )
        from ..adapters.codex_hook import write_hook_config

        fix_runner = _build_codex_runner(
            cfg,
            default_model=codex_runtime.model or None,
            sandbox=codex_runtime.sandbox,
            # AGENT.md reaches Codex through the worktree instructions below.
            inject_system_prompt=False,
            resume_on_retry=True,
            leg_role="fix",
            log=_log("fix"),
        )
        # Surface a codex misconfig (missing bin/key) at STARTUP, not on the first
        # dispatch — a clear one-liner instead of an opaque per-issue exit 1.
        _reason = fix_runner.preflight()
        if _reason is not None:
            _log("fix")(f"codex: NOT READY at startup — {_reason}")
        else:
            _log("fix")("codex: runtime ready (bin + key present)")
        # Review M1 / D12: ONE loud line at build time (never per dispatch) when a
        # spend cap is armed but no codex price table is set — those caps cannot see
        # a single codex dollar, and codex has no per-session budget flag either.
        # Structurally unreachable on FIXER_RUNTIME=claude (this whole block is).
        _blindspot = codex_cost_blindspot_warning(
            cost_per_mtok_in=codex_runtime.cost_per_mtok_in,
            cost_per_mtok_out=codex_runtime.cost_per_mtok_out,
            issue_spend_cap_usd=cfg.issue_spend_cap_usd,
            daily_spend_cap_usd=cfg.daily_spend_cap_usd,
            max_runtime=codex_runtime.max_runtime,
        )
        if _blindspot is not None:
            _log("fix")(_blindspot)
        # #29: the OTHER half, which used to be reachable only through the line
        # above — and therefore only when pricing was left unconfigured. It now
        # warns when the budget check cannot bound a run (enforcement off or
        # incomplete pricing); with both in place it emits an informational
        # notice that native codex support is absent but harness enforcement is
        # active. Either way `describe()` prints the matching verdict.
        _unbounded = codex_budget_not_enforced_warning(
            fix_budget_usd=cfg.fix_budget_usd,
            max_runtime=codex_runtime.max_runtime,
            enforced=codex_runtime.enforce_budget,
            priced=codex_runtime.budget_priceable,
        )
        if _unbounded is not None:
            _log("fix")(_unbounded)
        # prepare_cwd installs the governor hook and writes AGENTS.override.md into
        # each worktree, composing the harness + target instructions. It also
        # installs the PostToolUse edit-check when EDIT_CHECK_CMD is set.
        prepare_cwd = partial(
            write_hook_config, agents_md=cfg.agent_md, edit_check_cmd=cfg.edit_check_cmd,
            log=_log("fix"),
        )
    elif cfg.fixer_runtime == "glm":
        # GLM speaks Anthropic's wire protocol, so the SAME Claude runner is used
        # with the profile's env injected (base URL + model aliases + auth token);
        # prepare_cwd stays None because that runner gets its governor via SDK
        # hooks, exactly as on FIXER_RUNTIME=claude.
        from ..adapters.claude_profile import (
            EnvInjectingRuntime,
            claude_profile_env,
            endpoint_host,
        )

        try:
            _glm_env = claude_profile_env(cfg.glm_settings_file)
        except ValueError as exc:
            # Same contract as the codex preflight: a misconfig is ONE loud line at
            # startup, not an opaque failure on the first dispatch. Unwrapped means
            # the fixer runs on plain Claude — visibly wrong beats silently wrong.
            _log("fix")(f"glm: NOT READY at startup — {exc}")
        else:
            fix_runner = EnvInjectingRuntime(runner, env=_glm_env)
            _log("fix")(
                f"glm: runtime ready (endpoint {endpoint_host(_glm_env)}, "
                f"profile {_glm_env['CLAUDE_CONFIG_DIR']})"
            )

    executor = Executor(
        gateway, remove_worktree=worktrees.remove, log=_log("fix"),
        mark_automerge=store.mark_automerge, mark_ci_rerun=store.set_ci_rerun_head,
        mark_ci_pending_escalated=store.set_ci_pending_escalated_head,
        clear_issue_state=store.cleanup_pr,
        journal=journal, alert_cmd=cfg.alert_cmd,
        pending_actions=store,
    )
    # Wrapped LAST, so the decorator sees whichever runtime actually ran (Claude or
    # the Codex swap above) and the ledger is runtime-agnostic.
    fix_runner = JournalingRuntime(
        fix_runner, journal, ledger, cfg.repo, "fix", cfg.fixer_runtime
    )
    reservations = DispatchPathReservations()
    dispatcher = AgentDispatcher(
        dcfg, gateway, store, worktrees, fix_runner, executor,
        prepare_cwd=prepare_cwd, journal=journal,
        reserve_fix_paths=reservations.expand,
    )
    dispatch_runtime = CancellableAsyncDispatch(
        dispatcher.dispatch,
        finished=reservations.finish,
    )
    executor._run_agent = dispatch_runtime  # noqa: SLF001

    report_holds = _hold_reporter(
        journal,
        cfg.repo,
        _log("fix"),
        issue_is_blocked=lambda issue: gateway.get_issue(issue).has(
            Label.AI_BLOCKED
        ),
    )
    report_prediction_outcomes = _prediction_outcome_reporter(
        journal, cfg.repo, store, _log("fix")
    )
    report_ci_stalls = _ci_stall_reporter(journal, cfg.repo, fix_cfg, _log("fix"))
    report_duplicates = _duplicate_pr_reporter(journal, cfg.repo, fix_cfg, _log("fix"))
    report_ci_flakes = _ci_flake_reporter(journal, cfg.repo, fix_cfg, _log("fix"))
    daemon_ref: dict[str, Daemon] = {}

    issue_kinds = frozenset({AgentKind.RESOLVE, AgentKind.RESUME})

    def dispatch_key(action: RunAgent):
        # `decide` reserves every action from the observed world before tick()
        # submits it. Reuse that exact identity; a second live GitHub lookup can
        # disagree with a world-probe snapshot and defeat issue deduplication.
        issue = reservations.issue_for(action)
        if issue is None:
            return (action.kind, action.number)
        return (
            ("issue", issue)
            if issue > 0
            else ("pr", action.number)
        )

    def action_reservation(
        action: RunAgent, world
    ) -> tuple[int, frozenset[str]]:
        issues = {issue.number: issue for issue in world.issues}
        prs = {pr.number: pr for pr in world.prs}
        if action.kind in issue_kinds:
            issue = action.number
            view = issues.get(issue)
            paths = (
                view.declared_paths | view.predicted_paths
                if view is not None
                else frozenset()
            )
            return issue, paths
        pr = prs.get(action.number)
        issue = breaker_key(pr) if pr is not None else -action.number
        view = issues.get(issue)
        paths = (pr.changed_files if pr is not None else frozenset()) | (
            view.declared_paths | view.predicted_paths
            if view is not None
            else frozenset()
        )
        return issue, paths

    def decide(world):
        # Report BEFORE deciding: if decide_fix raises, the tick still leaves a
        # record of what it was holding, which is the state hardest to diagnose.
        # ONE FixState per tick, shared with the flake reporter — it is the same
        # state decide_fix decides on, so the two can never describe different
        # worlds (and the store reads happen once, not twice).
        world = _inject_predicted_paths(store, world)
        inflight = reservations.snapshot()
        state = _fix_state(store, world, inflight)
        report_holds(world, inflight)
        report_prediction_outcomes(world)
        report_ci_stalls(world)
        report_duplicates(world)
        report_ci_flakes(world, state)
        daemon = daemon_ref.get("daemon")
        externally_busy = fixer_busy()
        slots = (
            daemon.available_dispatch_slots
            if daemon is not None and not externally_busy
            else 0
        )
        decided = decide_fix(
            world,
            fix_cfg,
            state,
            fixer_busy=bool(inflight) or externally_busy,
            dispatch_slots=slots,
        )
        rejected: list[RunAgent] = []
        rejected_issues: set[int] = set()
        for action in decided:
            if not isinstance(action, RunAgent):
                continue
            issue, paths = action_reservation(action, world)
            if not reservations.reserve(action, issue, paths):
                rejected.append(action)
                rejected_issues.add(issue)
                _log("fix")(
                    f"dispatch {action.kind.name} #{action.number}: deferred — "
                    "path reservation changed during decision"
                )
        if not rejected:
            return decided
        return [
            action
            for action in decided
            if action not in rejected
            and not (
                isinstance(action, Relabel)
                and action.target is Target.ISSUE
                and action.number in rejected_issues
                and action.add is Label.AI_WORKING
            )
        ]

    _log("fix")(fix_capability_line(
        auto_merge=fix_cfg.auto_merge,
        auto_rebase=fix_cfg.auto_rebase_conflicts,
        escalate_on_evidence=cfg.escalate_on_evidence,
        arm_on_open=fix_cfg.arm_on_open,
    ))
    daemon = Daemon(
        "fix",
        gateway.build_world,
        decide,
        executor,
        cfg.tick_interval_seconds,
        _log("fix"),
        journal=journal,
        tick_gate=_spend_gate(cfg, ledger),
        wake_check=_wake_check(cfg, store),
        dispatch_capacity=cfg.fix_dispatch_concurrency,
        dispatch_key=dispatch_key,
        cancel_active_dispatches=dispatch_runtime.cancel_all,
        dispatch_rejected=reservations.finish,
    )
    daemon_ref["daemon"] = daemon
    return daemon


def _review_state(store: Store, world) -> ReviewState:
    """Read the per-PR state `decide_review` needs — including the ceiling
    counter that stops the reviewer paying for a PR that never converges.

    `unproductive_rounds` is PR-keyed in the domain but reconciled from TWO
    store key spaces here, because the two write sides disagree by construction
    (review P0R-3):

      * `AgentDispatcher.dispatch` bumps `infra-deaths` under
        `gateway.issue_for_pr(pr)`, falling back to `-pr` when that lookup
        returns None *or raises* — and the flakiness that kills sessions is
        exactly the flakiness that fails the lookup, so a PR's deaths can land
        in BOTH key spaces across a run of polls;
      * `ReviewWorkflow` bumps the same counter under `breaker_key(pr)` (pure,
        from the PRView it already fetched) for a round that ran and persisted
        nothing.

    Summing the union is what makes the ceiling see every paid round regardless
    of which key the writer chose. `breaker_key(pr) == -pr` for an issueless PR,
    and a set literal dedupes it, so nothing is double counted."""
    sessions = frozenset(p.number for p in world.prs if store.get_pr_session(p.number))
    last = {p.number: store.get_last_sha(p.number) for p in world.prs}
    deaths = _process_deaths(store)  # review P0R-4 — see _fix_state
    unproductive = {
        p.number: sum(
            store.get_infra_deaths(k) + deaths.get(k, 0)
            for k in {breaker_key(p), -p.number}
        )
        for p in world.prs
    }
    return ReviewState(
        sessions=sessions, last_sha=last, unproductive_rounds=unproductive
    )


def build_review_daemon(
    cfg: HarnessConfig, gateway: GitHubGateway, store: Store, runner: AgentRuntime
) -> Daemon:
    repo_cfg = cfg.repository
    review_runtime = cfg.review_runtime
    journal = _journal(store)
    ledger = default_ledger_path()
    rcfg = ReviewConfig(
        review_bot=review_runtime.bot, identity_mode=repo_cfg.identity_mode,
        trigger_label=Label(review_runtime.trigger_label),
        adopt_unlabeled=review_runtime.adopt_unlabeled,
        # getattr, not a plain attribute read: the env knob
        # (MAX_UNPRODUCTIVE_REVIEW_ROUNDS) is wired into ReviewRuntimeConfig
        # separately from this ceiling. Until it lands the domain default
        # applies — a missing knob must never mean "no ceiling", which is the
        # unbounded state P0R-3 describes.
        max_unproductive_rounds=getattr(
            review_runtime, "max_unproductive_rounds", DEFAULT_MAX_UNPRODUCTIVE_ROUNDS
        ),
    )
    dcfg = DispatchConfig(
        base_branch=repo_cfg.base_branch,
        review=ReviewTaskConfig(
            repo_cfg.repo, repo_cfg.base_branch, review_runtime.prompt,
            review_runtime.budget_usd, review_runtime.model,
            gate_model=review_runtime.gate_model,
            validation_model=review_runtime.validation_model,
            gate_budget_usd=review_runtime.gate_budget_usd,
            dimension_budget_usd=review_runtime.dimension_budget_usd,
            validation_budget_usd=review_runtime.validation_budget_usd,
            scout_model=review_runtime.scout_model,
        ),
        max_review_rejections=review_runtime.max_rejections,
        review_bot=review_runtime.bot,
        reviewer_runtime=review_runtime.runtime,
        review_trigger_label=review_runtime.trigger_label,
        review_mode=review_runtime.mode,
        log=_log("review"),
        # `rcfg.max_unproductive_rounds` is the ceiling the review decide side
        # reads off this counter — same value, same getattr fallback, so a
        # deterministic death saturates to exactly what escalates (#13).
        max_infra_deaths=rcfg.max_unproductive_rounds,
        **_spend_cfg(cfg, "review", ledger),
    )
    review_runner = runner
    review_execution_cwd = ""
    review_prepare_cwd = None
    if review_runtime.runtime == "codex":
        from ..adapters.codex_runner import (
            codex_budget_not_enforced_warning,
            codex_cost_blindspot_warning,
        )

        # Review is deliberately read-only in the operator's checkout. Codex
        # cannot apply the sandbox flag to `exec resume`, so the workflow makes
        # Codex re-reviews fresh rather than carrying a writable resume session.
        review_runner = _build_codex_runner(
            cfg,
            default_model=review_runtime.model,
            sandbox="read-only",
            inject_system_prompt=True,
            resume_on_retry=False,
            leg_role="review",
            log=_log("review"),
        )
        reason = review_runner.preflight()
        if reason is not None:
            _log("review")(f"codex: NOT READY at startup — {reason}")
        else:
            _log("review")("codex: runtime ready (bin + key present)")
        blindspot = codex_cost_blindspot_warning(
            cost_per_mtok_in=cfg.codex_runtime.cost_per_mtok_in,
            cost_per_mtok_out=cfg.codex_runtime.cost_per_mtok_out,
            issue_spend_cap_usd=cfg.issue_spend_cap_usd,
            daily_spend_cap_usd=cfg.daily_spend_cap_usd,
            max_runtime=cfg.codex_runtime.max_runtime,
            runtime_name="REVIEWER_RUNTIME",
        )
        if blindspot is not None:
            _log("review")(blindspot)
        budget_notice = codex_budget_not_enforced_warning(
            fix_budget_usd=review_runtime.budget_usd,
            max_runtime=cfg.codex_runtime.max_runtime,
            enforced=cfg.codex_runtime.enforce_budget,
            priced=cfg.codex_runtime.budget_priceable,
            budget_name="REVIEW_BUDGET_USD",
            runtime_name="REVIEWER_RUNTIME",
        )
        if budget_notice is not None:
            _log("review")(budget_notice)
        # Codex ignores AgentTask's SDK-only tool policy.  The workflow installs
        # this dedicated read-only Bash allow-list immediately before each leg,
        # in the explicit target checkout only.  Builder-time writes would let a
        # test run corrupt a simultaneously active fixer worktree.
        from ..adapters.codex_hook import write_hook_config

        review_execution_cwd = cfg.target_dir
        if review_execution_cwd:
            review_prepare_cwd = partial(
                write_hook_config, review_only=True, log=_log("review")
            )
        else:
            _log("review")(
                "codex: NOT READY at startup — REVIEWER_RUNTIME=codex requires "
                "TARGET_DIR so its read-only governor has an explicit checkout"
            )

    dcfg = replace(
        dcfg,
        review_execution_cwd=review_execution_cwd,
        review_prepare_cwd=review_prepare_cwd,
    )

    # Reviewer has no worktree/issue side; pass a no-op Worktrees-like None usage.
    executor = Executor(
        gateway,
        log=_log("review"),
        journal=journal,
        alert_cmd=cfg.alert_cmd,
        pending_actions=store,
    )
    dispatcher = AgentDispatcher(
        dcfg, gateway, store, None,
        JournalingRuntime(
            review_runner, journal, ledger, cfg.repo, "review", review_runtime.runtime
        ),
        executor, journal=journal,
    )
    executor._run_agent = lambda a: asyncio.run(dispatcher.dispatch(a))  # noqa: SLF001

    def decide(world):
        return decide_review(world, rcfg, _review_state(store, world))

    _log("review")(review_capability_line(
        can_approve=rcfg.identity_mode == IdentityMode.THREE,
        adopt_unlabeled=rcfg.adopt_unlabeled,
    ))

    return Daemon("review", gateway.build_review_world, decide, executor,
                  cfg.tick_interval_seconds, _log("review"), journal=journal,
                  tick_gate=_spend_gate(cfg, ledger),
                  wake_check=_wake_check(cfg, store))


_CODEOWNERS_PATHS = ("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS")


def _resolve_owner_handle(cfg: HarnessConfig, log) -> str:
    """Owner @-mention for triage's technical/architectural questions. Order:
    the target repo's CODEOWNERS `*` default owners → STEWARD_OWNER env → none.
    Never fails silently: logs which source won (or that none was found)."""
    base = cfg.target_dir or os.getcwd()
    for rel in _CODEOWNERS_PATHS:
        path = os.path.join(base, rel)
        try:
            with open(path, encoding="utf-8") as fh:
                owners = parse_codeowners_default(fh.read())
        except OSError:
            continue
        if owners:
            log(f"triage owner: {owners} (from {rel})")
            return owners
    if cfg.steward_owner:
        log(f"triage owner: {cfg.steward_owner} (from STEWARD_OWNER)")
        return cfg.steward_owner
    log(
        "triage owner: NONE — no CODEOWNERS default rule and STEWARD_OWNER unset; "
        "owner-level questions will be tagged generically ('a maintainer'). Set "
        "STEWARD_OWNER or add a CODEOWNERS '*' rule to route them."
    )
    return ""


def build_steward_daemon(
    cfg: HarnessConfig, gateway: GitHubGateway, store: Store, runner: AgentRunner
) -> Daemon:
    steward_log = _log("steward")
    repo_cfg = cfg.repository
    journal = _journal(store)
    ledger = default_ledger_path()
    steward_runtime = cfg.steward_runtime
    jira_runtime = cfg.jira_runtime
    tcfg_dom = TriageConfig(
        steward_bot=steward_runtime.bot, enabled=steward_runtime.triage_enabled,
        promote=steward_runtime.promote, triage_label=Label.AI_TRIAGE,
        owner_handle=_resolve_owner_handle(cfg, steward_log),
    )
    # Jira mirror: enabled by default, but INERT without a project key (so it's safe
    # out of the box). The Atlassian MCP spec is loaded from cfg.jira_mcp_config and
    # attached only to the steward's jira-sync task.
    jira_on = jira_runtime.sync and bool(jira_runtime.project_key)
    if jira_runtime.sync and not jira_runtime.project_key:
        steward_log("JIRA_SYNC is on but JIRA_PROJECT_KEY is unset — Jira sync inert")
    jira_cfg = None
    if jira_on:
        mcp = mcp_servers_from_file(
            jira_runtime.mcp_config, jira_runtime.mcp_tools,
            cfg.runtime_paths.harness_root, log=steward_log,
        )
        if not mcp:
            steward_log(
                f"JIRA_SYNC on but no MCP server loaded from {jira_runtime.mcp_config} — "
                "Jira sync inert (is the Atlassian MCP spec present?)"
            )
            jira_on = False
        else:
            jira_cfg = JiraTaskConfig(
                project_key=jira_runtime.project_key, cloud_id=jira_runtime.cloud_id,
                repo=repo_cfg.repo, mcp_servers=mcp, max_budget_usd=jira_runtime.budget_usd,
                model=steward_runtime.triage_model,
    )
    dcfg = DispatchConfig(
        base_branch=repo_cfg.base_branch,
        triage=TriageTaskConfig(
            steward_runtime.bot, steward_runtime.triage_prompt,
            steward_runtime.triage_budget_usd, steward_runtime.triage_model,
        ),
        groom=GroomTaskConfig(
            steward_runtime.bot, steward_runtime.triage_budget_usd,
            steward_runtime.triage_model,
        ) if cfg.steward_groom else None,
        triage_cfg=tcfg_dom,
        jira=jira_cfg,
        log=steward_log,
        # Not for dispatching anything — the steward's recurrence check needs to
        # know whose merged PRs count as "shipped by this harness".
        fix_bot=cfg.fix_runtime.bot,
        **_spend_cfg(cfg, "steward", ledger),
    )
    executor = Executor(
        gateway,
        log=steward_log,
        journal=journal,
        alert_cmd=cfg.alert_cmd,
        pending_actions=store,
    )
    dispatcher = AgentDispatcher(
        dcfg, gateway, store, None,
        JournalingRuntime(runner, journal, ledger, cfg.repo, "steward", "claude"),
        executor, journal=journal,
    )
    executor._run_agent = lambda a: asyncio.run(dispatcher.dispatch(a))  # noqa: SLF001

    # Same ceiling shape as the reviewer's (review P0R-3), for the same reason:
    # the steward's "already handled" markers — `triage-N.at` and the jira phase
    # — are written ONLY on success, so a session that dies leaves the issue in
    # the candidate set and the next poll buys it again. Unlike the other two
    # roles the steward escalates NOTHING: an untriaged issue is not work anyone
    # promised to finish, and moving it to needs-human-review would be the
    # steward inventing lifecycle state for an issue it never triaged. It stops
    # spending and says so, once per change in the suppressed set (not once per
    # poll — a line repeated 4,300 times a day is a line nobody reads).
    max_deaths = getattr(
        steward_runtime, "max_infra_deaths", DEFAULT_MAX_UNPRODUCTIVE_ROUNDS
    )
    suppressed_last: set[int] = set()
    report_recurrences = _recurrence_reporter(
        journal, gateway, cfg.repo, cfg.fix_runtime.bot, steward_log
    )
    # What this daemon can actually DO here — not what it is configured with
    # (#46). The two differ: JIRA_SYNC=1 with no project key is `jira_sync=off`,
    # and recurrence watching needs a FIX_BOT to attribute merged PRs to.
    steward_log(steward_capability_line(
        triage=tcfg_dom.enabled,
        groom=cfg.steward_groom,
        promote=tcfg_dom.promote,
        jira_sync=jira_on,
        recurrence=bool(cfg.fix_runtime.bot)
        and getattr(gateway, "issue_state_reason", None) is not None,
    ))
    if cfg.steward_groom_edit:
        steward_log("STEWARD_GROOM_EDIT=1 is inert: grooming currently comments only; issue bodies are never edited")

    def decide(world):
        # Before the pure decide, as in the fixer: if the decision raises, the
        # tick has still recorded what came back. The steward is probe-gated, so
        # this only runs on a genuinely fresh world.
        report_recurrences(world)
        state = TriageState(seen_at={i.number: store.get_triage_at(i.number) for i in world.issues})
        deaths = _process_deaths(store)

        def exhausted(n: int) -> bool:
            spent = store.get_infra_deaths(n) + deaths.get(n, 0)
            return retry_breaker(spent, max_deaths).tripped

        actions = [
            RunAgent(AgentKind.TRIAGE, n)
            for n in triage_candidates(world, tcfg_dom, state)
            if not exhausted(n)
        ]
        actions.extend(
            RunAgent(AgentKind.GROOM, n)
            for n in grooming_candidates(
                world,
                {i.number: store.get_groom_at(i.number) for i in world.issues},
                cfg.steward_groom,
                {i.number: store.get_groom_content(i.number) for i in world.issues},
            )
            if not exhausted(n)
        )
        if jira_on:
            for i in world.issues:
                target = jira_phase(i.labels)
                if target is not None and not exhausted(i.number) and jira_sync_needed(
                    target, store.get_jira_key(i.number), store.get_jira_phase(i.number)
                ):
                    actions.append(RunAgent(AgentKind.JIRA_SYNC, i.number))
        suppressed = {i.number for i in world.issues if exhausted(i.number)}
        if suppressed != suppressed_last:
            if suppressed:
                steward_log(
                    f"steward: no longer dispatching for issue(s) "
                    f"{sorted(suppressed)} — {max_deaths} sessions died before "
                    "returning a result. Check the runtime/daemon; clear the "
                    "issue-N.infra-deaths marker in the steward STATE_DIR to retry."
                )
            suppressed_last.clear()
            suppressed_last.update(suppressed)
        return actions

    # Jira needs closed ai-done issues too (for the final Done transition); triage
    # alone needs just open issues.
    observe = gateway.build_steward_world if jira_on else gateway.build_triage_world
    return Daemon("steward", observe, decide, executor, cfg.tick_interval_seconds,
                  steward_log, journal=journal, tick_gate=_spend_gate(cfg, ledger),
                  wake_check=_wake_check(cfg, store))
