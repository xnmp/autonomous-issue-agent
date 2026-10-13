"""Dashboard application service and composition façade."""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from types import MappingProxyType
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .resolve_contracts import CommandResult, ResolveCommand, ResolveControl
    from .resolve_contracts import WorkflowFamilies

from ...adapters.dashweb import actions, costchart, ghdata, system, usage, verification
from ...adapters.control_store import SQLiteControlStore
from ...adapters.control_path import control_store_path
from ...adapters.env_file import source_env_file
from ...domain.deployment import canonical_deployment_name
from ...adapters.journal import Journal, default_ledger_path
from ...adapters.operator_effects import build_operator_effects
from ...adapters.store import Store
from ...domain import dashboard as dash
from ...domain.labels import Label
from ...domain.fix import harness_merges_branch

# (target, number, labels added, labels removed) — one applied park/unpark move.
_ParkMove = tuple[str, int, tuple[str, ...], tuple[str, ...]]
from ...domain.events import (
    ActionApplied,
    Correlation,
    TelemetryEvent,
)
from .. import cli, config
from . import snapshots
from .ports import GitHubQueries


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def load_operator_configuration(name: str, *, harness_root: str) -> tuple[config.HarnessConfig, Mapping[str, str]]:
    """Mutation scope comes from one launch snapshot, never per-field overlay fallbacks."""
    if not isinstance(name, str) or not name or name != canonical_deployment_name(name) or name in (".", ".."):
        raise ValueError("operator control requires a canonical deployment name")
    snapshot = os.path.join(harness_root, "config", f".active.{name}.env")
    env = source_env_file(snapshot, os.environ, assigned_only=True)
    env.pop("AGENT_ENV", None)
    if not env.get("REPO") or not env.get("TARGET_DIR"):
        raise ValueError("operator control requires explicit repository and checkout in the active snapshot")
    host = env.get("GH_HOST", "github.com")
    if host != os.environ.get("GH_HOST", "github.com"):
        raise ValueError("operator GitHub host differs from the deployment snapshot")
    cfg = config.from_env(env)
    cfg.validate()
    if not cfg.repo or not cfg.target_dir or not os.path.isabs(cfg.target_dir):
        raise ValueError("operator control requires an explicit repository checkout")
    return cfg, MappingProxyType(env)


_DETAIL_CONFIG_FIELDS = (
    "MAX_BUDGET_USD",
    "ROUTER_BUDGET_USD",
    "REVIEW_BUDGET_USD",
)
_FIX_BUDGET_KINDS = (
    "RESOLVE", "RESUME", "PR_RESUME", "ADDRESS_REVIEW", "FIX_CI", "REBASE",
)

_GH_ENRICHMENT_TTL_S = ghdata.DEFAULT_TTL_S
_GHContext = tuple[dict[int, dict], dict[int, dict], list[str]]


@dataclass
class _DeferredGhEntry:
    value: _GHContext | None
    refreshed_at: float
    future: Future[_GHContext] | None


class _DeferredGhContexts:
    """Non-blocking stale-while-revalidate store for optional GitHub fields.

    Journal folds are the dashboard's first-paint contract. GitHub titles,
    links, reviews and check state are enrichment, and a cold ``gh`` process
    can take tens of seconds. The first read therefore schedules enrichment
    and returns no optional fields; later polls atomically pick up the result.
    Once populated, callers keep the last value while its refresh runs so data
    never flashes back to absent at the TTL boundary.
    """

    def __init__(
        self,
        *,
        ttl_s: float = _GH_ENRICHMENT_TTL_S,
        clock=time.monotonic,
        executor: ThreadPoolExecutor | None = None,
    ) -> None:
        self._ttl_s = ttl_s
        self._clock = clock
        self._executor = executor or ThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="dashboard-gh",
        )
        self._lock = threading.Lock()
        self._entries: dict[tuple, _DeferredGhEntry] = {}

    def get(self, key: tuple, load) -> tuple[_GHContext, bool]:
        """Return ``(context, enriched)`` without waiting on ``load``."""
        now = self._clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._entries[key] = _DeferredGhEntry(
                    value=None,
                    refreshed_at=0.0,
                    future=self._executor.submit(load),
                )
                return ({}, {}, []), False

            if entry.future is not None and entry.future.done():
                try:
                    entry.value = entry.future.result()
                except Exception as exc:  # noqa: BLE001 - enrichment degrades
                    entry.value = (
                        {},
                        {},
                        [f"GitHub enrichment unavailable ({_detail(exc)})"],
                    )
                entry.refreshed_at = now
                entry.future = None

            if entry.value is None:
                return ({}, {}, []), False

            if (
                entry.future is None
                and now - entry.refreshed_at >= self._ttl_s
            ):
                entry.future = self._executor.submit(load)
            return entry.value, True


def _detail_limits(name: str, repo: str) -> dict[str, float]:
    configured = {
        field: value
        for field in _DETAIL_CONFIG_FIELDS
        if (value := cli.deployment_env_field(name, field))
    }
    cfg = config.from_env({"REPO": repo, **configured})
    return {
        **{kind: max(0.0, cfg.fix_budget_usd) for kind in _FIX_BUDGET_KINDS},
        "REVIEW": max(0.0, cfg.review_budget_usd),
        "RE_REVIEW": max(0.0, cfg.review_budget_usd),
    }


_AUTOMERGE_CONFIG_FIELDS = ("AUTO_MERGE",)


def _harness_merges(name: str, repo: str) -> Callable[[str], bool]:
    """Which PR branches the fixer merges itself on this repo.

    The fixer's own per-branch rule (`harness_merges_branch`, which
    `automerge_in_scope` applies) bound to this deployment's knobs, so a
    stranger's branch on an auto-merging repo still reads as the operator's.
    An unreadable configuration merges nothing, which surfaces a merge-ready
    PR to the operator rather than letting it sit in Working on a guess.
    """
    try:
        configured = {
            field: value
            for field in _AUTOMERGE_CONFIG_FIELDS
            if (value := cli.deployment_env_field(name, field))
        }
        cfg = config.from_env({"REPO": repo, **configured})
    except Exception:  # noqa: BLE001 - a display hint never fails the board
        return _merges_nothing
    return partial(
        harness_merges_branch,
        auto_merge=cfg.auto_merge,
    )


def _merges_nothing(_branch: str) -> bool:
    return False


def _detail(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:400]


class Backend:
    """Every collector the API exposes, over one cached `gh` reader."""

    def __init__(
        self,
        harness_root: str | None = None,
        gh: GitHubQueries | None = None,
        now_fn=None,
        stale_after_s: float = dash.STALE_AFTER_S,
        snapshot_ttl_s: float = 3.0,
        detail_config_ttl_s: float = 60.0,
        gh_enrichment_ttl_s: float = _GH_ENRICHMENT_TTL_S,
        gh_enrichment_executor: ThreadPoolExecutor | None = None,
        effects_factory=build_operator_effects,
        control_db_path: str = "",
    ):
        self.harness_root = harness_root or cli.HARNESS_ROOT
        self.gh = gh or ghdata.GhReader()
        self.now_fn = now_fn or _utcnow
        self.stale_after_s = stale_after_s
        # Building one snapshot re-reads three journals AND sources the overlay
        # through bash (the CLI's shell-semantics read). At the UI's 5s poll,
        # across three deployments and two views, that is a lot of forks for
        # data that cannot have changed. A few seconds of staleness is free —
        # the daemons themselves tick every 20s.
        self._snapshots = ghdata.TtlCache(ttl_s=snapshot_ttl_s)
        self._detail_configs = ghdata.TtlCache(ttl_s=detail_config_ttl_s)
        self._gh_enrichment = _DeferredGhContexts(
            ttl_s=gh_enrichment_ttl_s,
            executor=gh_enrichment_executor,
        )
        self._self_repo = system.remote_repo(self.harness_root)
        self.effects_factory = effects_factory
        self.control_db_path = control_db_path

    # ── journal side ──
    def names(self) -> tuple[str, ...]:
        return system.deployment_names(cli.REPOS_DIR)

    def known(self, name: str) -> bool:
        """A deployment the operator actually configured.

        Every route gates on this. `name` reaches a subprocess argv and several
        filesystem paths, so an unvalidated one is both an injection surface and
        an unbounded-work surface (each miss forks bash to source an overlay
        that does not exist, and pins a cache entry)."""
        return name in self.names()

    def snapshot(self, name: str, *, refresh: bool = False) -> dash.Snapshot:
        if not refresh:
            cached = self._snapshots.get(name)
            if cached is not None:
                return cached
        return self._snapshots.put(
            name,
            self._deployment_snapshot(name, self.overlay_field(name, "REPO"),
                                      self.overlay_field(name, "TARGET_DIR"),
                                      tuple(cli.journal_paths(name)), self.control_db_path),
        )

    def _deployment_snapshot(
        self, name: str, repo: str, target_dir: str, journal_paths: tuple[str, ...], control_db_path: str,
    ) -> dash.Snapshot:
        return system.collect_snapshot(
            name=name, repo=repo, target_dir=target_dir, journal_paths=list(journal_paths),
            log_dir=cli.log_dir(), ledger_path=default_ledger_path(), now_fn=self.now_fn,
            stale_after_s=self.stale_after_s, control_db_path=control_db_path,
        )

    def repos(self) -> dict:
        live, scan_ok = system.scan_proc_daemons()
        snaps = [self.snapshot(name) for name in self.names()]
        return snapshots.repos_payload(
            snaps, live, scan_ok, self.now_fn(), self_repo=self._self_repo
        )

    @staticmethod
    def overlay_field(name: str, field: str) -> str:
        return cli.read_repo_env_field(
            os.path.join(cli.REPOS_DIR, f"{name}.env"), field
        )

    def usage(self) -> dict:
        def config_for(name: str) -> tuple[str, float, float]:
            repo = cli.read_repo_env_field(
                os.path.join(cli.REPOS_DIR, f"{name}.env"), "REPO"
            )
            return repo, cli.daily_cap(name), cli.global_daily_cap(name)

        return usage.usage_payload(self.names(), config_for)

    def verification(self) -> dict:
        return verification.verification_payload(
            verification.default_path(self.harness_root)
        )

    def overview(self, name: str, timezone_name: str | None = None) -> dict:
        live, scan_ok = system.scan_proc_daemons()
        snapshot = self.snapshot(name)
        local_tz = _local_timezone(timezone_name)
        first_observed_day = snapshots.first_observed_day(snapshot, local_tz)
        issues, issue_error = self.gh.issues(snapshot.repo)
        recurrence_events = [
            event
            for row in snapshot.deployment.issues
            for event in row.events
            if event.get("event") == "recurrence"
        ]
        payload = snapshots.overview_payload(
            snapshot,
            live,
            scan_ok,
            issue_cap_usd=cli._env_cap(name, "ISSUE_SPEND_CAP_USD", 0.0) or None,
            daily_cap_usd=cli.daily_cap(name) or None,
        )
        payload["health_trend"] = snapshots.health_trend(
            issues,
            recurrence_events,
            first_observed_day=first_observed_day,
            local_tz=local_tz,
        )
        payload["first_observed_day"] = first_observed_day
        payload["spend_per_hour"] = snapshots.spend_per_hour(
            snapshot.deployment, self.now_fn()
        )
        if issue_error:
            payload["warnings"].append(issue_error)
        elif isinstance(issues, list) and len(issues) >= ghdata.ISSUE_HISTORY_LIMIT:
            payload["warnings"].append(
                f"GitHub issue history may be incomplete (limited to "
                f"{ghdata.ISSUE_HISTORY_LIMIT} records)"
            )
        return payload

    # ── gh-augmented ──
    def _gh_context(self, repo: str) -> tuple[dict, dict, list[str]]:
        # These are independent network reads. On the measured cold path they
        # took 15s and 9s respectively; running them side by side keeps the
        # eventual enrichment latency at the slower call rather than their sum.
        with ThreadPoolExecutor(max_workers=2) as executor:
            issues_future = executor.submit(self.gh.issues, repo)
            prs_future = executor.submit(self.gh.prs, repo)
            issues_payload, issues_error = issues_future.result()
            prs_payload, prs_error = prs_future.result()
        warnings = [m for m in (issues_error, prs_error) if m]
        return (
            ghdata.index_by_number(issues_payload),
            ghdata.index_by_number(prs_payload),
            warnings,
        )

    def _gh_issue_context(
        self,
        repo: str,
        prs: tuple[int, ...],
    ) -> _GHContext:
        gh_issues, gh_prs, warnings = self._gh_context(repo)
        if not prs:
            return gh_issues, gh_prs, warnings
        with ThreadPoolExecutor(max_workers=min(4, len(prs))) as executor:
            details = {
                number: executor.submit(self.gh.pr, repo, number)
                for number in prs
            }
            for number, future in details.items():
                payload, error = future.result()
                if isinstance(payload, dict):
                    gh_prs[number] = {**gh_prs.get(number, {}), **payload}
                if error:
                    warnings.append(f"PR #{number} change details: {error}")
        return gh_issues, gh_prs, warnings

    def _deferred_gh_context(
        self,
        key: tuple,
        load,
    ) -> tuple[_GHContext, bool]:
        (issues, prs, warnings), enriched = self._gh_enrichment.get(key, load)
        # Response-specific diagnostics are composed below. Never let a poll
        # append into the cached enrichment value shared by every request.
        return (issues, prs, list(warnings)), enriched

    def _cached_detail_limits(
        self, name: str, repo: str
    ) -> dict[str, float]:
        key = (name, repo)
        return self._detail_configs.get_or_load(
            key, lambda: _detail_limits(name, repo)
        )

    def _cached_harness_merges(self, name: str, repo: str) -> Callable[[str], bool]:
        return self._detail_configs.get_or_load(
            ("automerge", name, repo), lambda: _harness_merges(name, repo)
        )

    def issues(self, name: str) -> dict:
        snap = self.snapshot(name)
        now = self.now_fn()
        (gh_issues, gh_prs, warnings), enriched = self._deferred_gh_context(
            ("issues", snap.repo),
            lambda: self._gh_context(snap.repo),
        )
        rows = snap.deployment.issues
        projected_by_model = snapshots.projected_cost_by_model(rows)
        verification_payload = self.verification()
        ledger_rows = verification_payload.get("rows", [])
        issue_status, pr_status = verification.status_indexes(ledger_rows)
        issue_reason, pr_reason = verification.reason_indexes(ledger_rows)
        harness_merges = self._cached_harness_merges(name, snap.repo)

        def card(row):
            model = snapshots.projection_model(row)
            pr = row.prs[-1] if row.prs else None
            return snapshots.issue_card(
                row,
                now,
                gh_issue=gh_issues.get(row.issue),
                gh_prs=gh_prs,
                projected_cost_usd=projected_by_model.get(model) if model else None,
                projected_model=model,
                issue_verification=issue_status.get(row.issue),
                pr_verification=pr_status.get(pr) if pr is not None else None,
                issue_verification_reason=issue_reason.get(row.issue),
                pr_verification_reason=(
                    pr_reason.get(pr) if pr is not None else None
                ),
                harness_merges=harness_merges,
            )

        row_by_issue = {row.issue: row for row in rows}
        ready_numbers = {
            number
            for number, issue in gh_issues.items()
            if ghdata.is_open(issue)
            and "ai-ready" in snapshots.labels_of(issue)
        }
        in_flight_numbers = {
            row.issue
            for row in rows
            if not row.done and row.issue not in ready_numbers
        }
        pending, backlog = [], []
        for number, issue in sorted(gh_issues.items()):
            if not ghdata.is_open(issue):
                continue
            labels = snapshots.labels_of(issue)
            if "ai-ready" in labels:
                pending.append(
                    snapshots.pending_card(
                        issue,
                        row_by_issue.get(number),
                        gh_prs=gh_prs,
                        harness_merges=harness_merges,
                    )
                )
            elif snapshots.is_backlog(issue):
                backlog.append(
                    snapshots.backlog_card(
                        issue, now, row_by_issue.get(number)
                    )
                )
        return {
            "in_flight": [
                card(row) for row in rows
                if row.issue in in_flight_numbers
            ],
            "pending": pending,
            "done": [card(row) for row in rows if row.done],
            "backlog": backlog,
            # A pending background read is not a GitHub failure. Optional
            # fields are absent until the next poll, while an actual completed
            # failure still drives the existing degraded-state affordance.
            "gh_ok": not warnings,
            "notifications": snapshots.journal_notifications(rows),
            "warnings": warnings,
        }

    def issue(self, name: str, number: int) -> dict | None:
        snap = self.snapshot(name)
        row = snapshots.find_row(snap, number)
        if row is None:
            return None
        (gh_issues, gh_prs, warnings), enriched = self._deferred_gh_context(
            ("issue", snap.repo, row.issue, row.prs),
            lambda: self._gh_issue_context(snap.repo, row.prs),
        )
        budget_caps = self._cached_detail_limits(name, snap.repo)
        detail = snapshots.issue_detail(
            row,
            self.now_fn(),
            gh_issues.get(number),
            gh_prs,
            budget_caps=budget_caps,
        )
        detail["warnings"] = warnings
        return detail

    def step_events(self, name: str, number: int, idx: int) -> dict | None:
        row = snapshots.find_row(self.snapshot(name), number)
        if row is None:
            return None
        return {"events": snapshots.step_window_events(row, idx), "warnings": []}

    def cost_chart(self, session_id: str) -> tuple[str, str]:
        return costchart.render_cost_chart(session_id)

    # ── actions ──
    def start(self, name: str) -> tuple[bool, str]:
        if not self.known(name):
            return False, f"unknown deployment '{name}'"
        live, scan_ok = system.scan_proc_daemons()
        if scan_ok and name in live:
            return False, f"deployment '{name}' already has live daemons"
        log_path = actions.start_log_path(cli.log_dir(), name)
        return actions.launch_detached(
            actions.start_command(self.harness_root, name), log_path
        )
    def promote(self, name: str, number: int) -> tuple[bool, str]:
        if not self.known(name):
            return False, f"unknown deployment '{name}'"
        repo = self.overlay_field(name, "REPO")
        if not repo:
            return False, f"no REPO configured for deployment '{name}'"
        effects = self._operator_effects(name, repo)
        return effects.set_issue_labels(number, add=("ai-ready",))

    def priority(self, name: str, number: int, priority: str | None) -> tuple[bool, str]:
        if not self.known(name):
            return False, f"unknown deployment '{name}'"
        repo = self.overlay_field(name, "REPO")
        if not repo:
            return False, f"no REPO configured for deployment '{name}'"
        effects = self._operator_effects(name, repo)
        if priority:
            ensured, detail = effects.ensure_label(
                priority,
                color=actions.PRIORITY_COLORS[priority],
                description=f"harness priority {priority}",
            )
            if not ensured:
                return False, detail
        add, remove = actions.priority_change(priority)
        return effects.set_issue_labels(
            number,
            add=add,
            remove=remove,
        )

    def resolve_control(self, name: str, number: int, request_id: str | None = None) -> ResolveControl:
        """The resolve workflow was deleted with the fix decision (refactor
        slice 5b); its operator surface stays unavailable until slice 5d
        removes the route and the UI panel."""
        from .resolve_control import unavailable_control

        del name, number, request_id
        return unavailable_control(RuntimeError("resolve control was removed with the fix workflow"))

    def resolve_command(self, name: str, number: int, body: ResolveCommand) -> CommandResult:
        from .resolve_control import unavailable_command

        del name, number
        body.command()
        return unavailable_command(RuntimeError("resolve control was removed with the fix workflow"))

    def workflow_families(self, name: str) -> WorkflowFamilies:
        """The repository's activated families, read exactly as the legacy unblock refusal reads them."""
        from .resolve_contracts import WorkflowFamiliesAvailable, WorkflowFamiliesUnavailable
        from .resolve_control import control_error

        try:
            if not self.known(name):
                raise ValueError(f"unknown deployment '{name}'")
            cfg, _env = load_operator_configuration(name, harness_root=self.harness_root)
            state_dirs = (cfg.state_dir, cfg.review_state_dir)
            path = control_store_path(state_dirs, authority_root=cfg.control_authority_root, temporary_on_missing=False)
            if self.control_db_path and os.path.realpath(self.control_db_path) != os.path.realpath(path):
                raise ValueError("workflow family authority differs from the configured deployment")
            active = SQLiteControlStore(path).active_workflow_families(cfg.repo)
            return WorkflowFamiliesAvailable(status="available", repository=cfg.repo,
                                             active_families=[family.value for family in active])
        except Exception as exc:  # noqa: BLE001 - an unread activation never reads as none active
            return WorkflowFamiliesUnavailable(status="unavailable", reason=control_error(exc))

    def unblock(
        self, name: str, number: int, channel: str = "api",
    ) -> tuple[bool, str]:
        if not self.known(name):
            return False, f"unknown deployment '{name}'"
        try:
            cfg, env = load_operator_configuration(name, harness_root=self.harness_root)
            repo = cfg.repo
            state_dirs = (cfg.state_dir, cfg.review_state_dir)
            path = control_store_path(state_dirs, authority_root=cfg.control_authority_root, temporary_on_missing=False)
            if self.control_db_path and os.path.realpath(self.control_db_path) != os.path.realpath(path):
                return False, "unblock control authority differs from the configured deployment"
            control = SQLiteControlStore(path)
            with control.legacy_resolve_authority(repo):
                return self._legacy_unblock(name, number, channel, control=control, cfg=cfg, env=env)
        except Exception as exc:  # noqa: BLE001 - failure cannot authorize legacy resets
            return False, f"unblock unavailable ({type(exc).__name__}: {exc})"

    def _legacy_unblock(
        self,
        name: str,
        number: int,
        channel: str = "api",
        *, control: SQLiteControlStore, cfg: config.HarnessConfig,
        env: Mapping[str, str],
    ) -> tuple[bool, str]:
        """Release unmigrated holds only while every workflow family is inactive.

        Review rejection holds require activated SQLite control. Refusing them
        before any reset preserves their migration evidence and authorization.
        """
        if not self.known(name):
            return False, f"unknown deployment '{name}'"
        repo = cfg.repo
        state_dirs = (cfg.state_dir, cfg.review_state_dir)
        journal_paths = tuple(os.path.join(path, "journal.jsonl") for path in state_dirs)
        # Admission provenance must share mutation scope and bypass the UI cache.
        snapshot = self._deployment_snapshot(name, repo, cfg.target_dir, journal_paths, control.path)
        row = snapshots.find_row(snapshot, number)
        prs = tuple(row.prs) if row is not None else ()
        held, pr_held, unreadable = self._attention_labels(repo, number, prs)
        if unreadable:
            return False, unreadable
        if not held and not pr_held:
            return False, f"nothing to unblock on #{number}"
        issue_add, issue_remove = actions.issue_unblock_change(held)
        review_trigger = Label.AI_PR_OPEN.value
        pr_changes: dict[int, tuple[tuple[str, ...], tuple[str, ...]]] = {}
        candidate_pr = 0
        try:
            for pr, labels in pr_held.items():
                candidate_pr = pr
                hold_role = self._pr_hold_role(row, pr)
                if Label.NEEDS_HUMAN_REVIEW.value in labels and hold_role != "fix":
                    reason = ("review_hold_unsupported; review control was deleted in slice 5c, so remove "
                              "needs-human-review on GitHub to release this hold"
                              if hold_role == "review" else "hold source is unknown; cannot authorize a legacy release")
                    return False, f"pr #{pr}: {reason}"
                pr_changes[pr] = actions.pr_unblock_change(
                    labels,
                    review_trigger,
                    hold_role,
                )
        except ValueError as exc:
            # Resolve every admission decision before counters or labels move:
            # an ambiguous custom-trigger re-arm is safer left held than half
            # admitted to a role that cannot advance it.
            return False, f"pr #{candidate_pr}: {exc}"
        if self._legacy_retry_grant_required(state_dirs, number, prs):
            return False, "fix recovery migration required; retained legacy retry evidence cannot authorize a grant"
        effects = self._operator_effects(name, repo, journalled=False, control=control, state_dirs=state_dirs)
        journal = self._operator_journal(name, repo, legacy_path=journal_paths[0])
        cleared = 0
        # Label-only legacy effects report their own verified changes below.
        issue_applied, issue_detail = True, "ok"
        issue_actual_add: tuple[str, ...] = ()
        issue_actual_remove: tuple[str, ...] = ()
        if held:
            issue_effect = effects.set_issue_labels_effect(
                number,
                add=issue_add,
                remove=issue_remove,
                add_unless_present=actions.SELECTABLE_ISSUE_LABELS,
            )
            issue_applied, issue_detail = (
                issue_effect.applied,
                issue_effect.detail,
            )
            issue_actual_add = issue_effect.added
            issue_actual_remove = issue_effect.removed
        # PR-level escalations hold the PR, not the issue (keifu #173/#177,
        # 2026-08-02: unblock returned ok while the PR kept
        # needs-human-review and the reviewer stayed withheld). Remove only
        # the labels each PR actually carries — an unfiltered DELETE 404s
        # and settles unknown_outcome.
        pr_added: dict[int, tuple[str, ...]] = {}
        pr_removed: dict[int, tuple[str, ...]] = {}
        pr_requested_added: dict[int, tuple[str, ...]] = {}
        pr_applied: dict[int, bool] = {}
        failures = [] if issue_applied else [f"issue #{number}: {issue_detail}"]
        for pr, labels in pr_held.items():
            add, remove = pr_changes[pr]
            pr_requested_added[pr] = add
            pr_effect = effects.set_issue_labels_effect(
                pr, add=add, remove=remove
            )
            target_applied, pr_detail = pr_effect.applied, pr_effect.detail
            pr_added[pr] = pr_effect.added
            pr_removed[pr] = pr_effect.removed
            pr_applied[pr] = target_applied
            if not target_applied:
                failures.append(f"pr #{pr}: {pr_detail}")
        applied = issue_applied and all(pr_applied.values())
        detail = "; ".join(failures) if failures else "ok"
        self._journal_unblock(
            name, repo, number, held, cleared, applied,
            issue_added=issue_actual_add,
            issue_removed=issue_actual_remove,
            issue_requested_added=issue_add,
            issue_applied=issue_applied,
            pr_held=pr_held,
            pr_added=pr_added,
            pr_removed=pr_removed,
            pr_requested_added=pr_requested_added,
            pr_applied=pr_applied,
            channel=channel,
            journal=journal,
        )
        return applied, detail

    def park(
        self, name: str, number: int, channel: str = "api"
    ) -> tuple[bool, str]:
        """Hand this issue's open PR(s) to a human WITHOUT starving the queue.

        Removing `ai-pr-open` is the whole mechanism: the fixer's world only
        contains ai-pr-open PRs, so the parked PR's paths leave the overlap
        serializer next tick and held pending issues dispatch. `ai-parked`
        records the decision on issue and PRs, and the dashboard shows it as a
        parked pending row. NOTE: unpark is a
        RE-ARM, not a label-for-label reversal — escalation labels park
        stripped (ai-blocked / needs-*-review) stay gone, so the pipeline
        resumes review/CI legs on the PR (adversarial P2-5).

        Refused when nothing carries ai-pr-open: parking an issue with no
        harness PR would just hide it (keifu #34 / PR #179, 2026-08-02 —
        approved, macOS-broken, cap-blocked, and holding five issues hostage).
        """
        ok, repo, gh_prs, prs, issue_labels, detail = self._park_context(
            name, number
        )
        if not ok:
            return False, detail
        # OPEN only: a merged PR still wearing ai-pr-open is mid merge-cleanup;
        # stripping the label here would remove the key cleanup_actions drains
        # on (adversarial P2-7). A PR already wearing ai-parked stays parkable
        # so a park whose PR write landed but issue write failed can be
        # retried instead of dead-ending on "no open harness PR" (P2-6).
        def _pr_labels(pr: int) -> set[str]:
            return set(snapshots.labels_of(gh_prs.get(pr) or {}))

        # needs-human-review counts: an ESCALATED PR already swapped
        # ai-pr-open away, and "blocked with an open PR" is the exact state
        # park exists for — refusing it forced label surgery by hand
        # (keifu PRs #182/#183, 2026-08-03).
        parkable = [
            pr for pr in prs
            if str((gh_prs.get(pr) or {}).get("state", "")).upper() == "OPEN"
            and _pr_labels(pr) & {
                "ai-pr-open", "needs-human-review", actions.PARKED_LABEL
            }
        ]
        if not parkable:
            return False, f"nothing to park on #{number}: no open harness PR"
        effects = self._operator_effects(name, repo, journalled=False)
        ensured, ensure_detail = effects.ensure_label(
            actions.PARKED_LABEL,
            color=actions.PARKED_COLOR,
            description="operator parked: harness hands-off",
        )
        if not ensured:
            return False, ensure_detail
        applied, detail = True, "ok"
        moves: list[_ParkMove] = []
        for pr in parkable:
            remove = tuple(x for x in actions.PARK_REMOVES if x in _pr_labels(pr))
            pr_ok, pr_detail = effects.set_issue_labels(
                pr, add=(actions.PARKED_LABEL,), remove=remove
            )
            if pr_ok:
                moves.append(("pr", pr, (actions.PARKED_LABEL,), remove))
            else:
                applied, detail = False, f"pr #{pr}: {pr_detail}"
        remove = tuple(x for x in actions.PARK_REMOVES if x in issue_labels)
        issue_ok, issue_detail = effects.set_issue_labels(
            number, add=(actions.PARKED_LABEL,), remove=remove
        )
        if issue_ok:
            moves.append(("issue", number, (actions.PARKED_LABEL,), remove))
        else:
            applied, detail = False, issue_detail
        self._journal_park(
            name, repo, number, "operator_park", moves, applied, channel
        )
        return applied, detail

    def unpark(
        self, name: str, number: int, channel: str = "api"
    ) -> tuple[bool, str]:
        """Re-arm a parked issue: restore ai-pr-open on the parked PR(s) so
        they rejoin review/merge/overlap, and drop ai-parked everywhere.
        Escalation labels park removed are NOT restored — unparking means
        "the pipeline may work this again", not "recreate the blocked state"."""
        ok, repo, gh_prs, prs, issue_labels, detail = self._park_context(
            name, number
        )
        if not ok:
            return False, detail
        parked = [
            pr for pr in prs
            if actions.PARKED_LABEL
            in set(snapshots.labels_of(gh_prs.get(pr) or {}))
        ]
        if not parked and actions.PARKED_LABEL not in issue_labels:
            return False, f"#{number} is not parked"
        effects = self._operator_effects(name, repo, journalled=False)
        applied, detail = True, "ok"
        moves: list[_ParkMove] = []
        for pr in parked:
            pr_ok, pr_detail = effects.set_issue_labels(
                pr, add=("ai-pr-open",), remove=(actions.PARKED_LABEL,)
            )
            if pr_ok:
                moves.append(("pr", pr, ("ai-pr-open",), (actions.PARKED_LABEL,)))
            else:
                applied, detail = False, f"pr #{pr}: {pr_detail}"
        if actions.PARKED_LABEL in issue_labels:
            add = ("ai-pr-open",) if parked else ()
            issue_ok, issue_detail = effects.set_issue_labels(
                number, add=add, remove=(actions.PARKED_LABEL,)
            )
            if issue_ok:
                moves.append(("issue", number, add, (actions.PARKED_LABEL,)))
            else:
                applied, detail = False, issue_detail
        self._journal_park(
            name, repo, number, "operator_unpark", moves, applied, channel
        )
        return applied, detail

    def _park_context(
        self, name: str, number: int
    ) -> tuple[bool, str, dict, list[int], set[str], str]:
        """(ok, repo, gh_prs, issue's PRs, issue's labels, refusal detail)."""
        if not self.known(name):
            return False, "", {}, [], set(), f"unknown deployment '{name}'"
        repo = self.overlay_field(name, "REPO")
        if not repo:
            return False, "", {}, [], set(), f"no REPO configured for '{name}'"
        try:
            gh_issues, gh_prs, warnings = self._gh_context(repo)
        except Exception as exc:  # noqa: BLE001 - operator-facing result
            return False, repo, {}, [], set(), f"could not read #{number}: {exc}"
        issue = gh_issues.get(number)
        if issue is None:
            return False, repo, {}, [], set(), (
                warnings[0] if warnings
                else f"issue #{number} is not open on {repo}"
            )
        return (
            True,
            repo,
            gh_prs,
            self._issue_prs(name, number),
            set(snapshots.labels_of(issue)),
            "",
        )

    def _journal_park(
        self,
        name: str,
        repo: str,
        number: int,
        kind: str,
        moves: list[_ParkMove],
        applied: bool,
        channel: str,
    ) -> None:
        journal = self._operator_journal(name, repo)
        if journal is None:
            return
        verb = "park" if kind == "operator_park" else "unpark"
        try:
            for target, target_number, added, removed in moves or [
                ("issue", number, (), ())
            ]:
                journal.emit(
                    TelemetryEvent(
                        ActionApplied(
                            f"operator-{verb}:{uuid.uuid4().hex}",
                            kind,
                            target,
                            f"operator {verb} {target} #{target_number}: "
                            + (" ".join(f"-{x}" for x in removed) or "(none)"),
                            target_number,
                            {
                                # The moves that actually reached GitHub —
                                # never reconstructed: a fabricated
                                # +ai-pr-open here wiped a live PR-level
                                # awaiting-reply stamp downstream
                                # (adversarial review F5, 2026-08-03).
                                "add": list(added),
                                "remove": list(removed),
                                "operator": f"{repo}:dashboard",
                                "channel": channel,
                                "labels_applied": applied,
                            },
                        ),
                        Correlation(
                            issue=number,
                            pr=target_number if target == "pr" else None,
                        ),
                    )
                )
        except Exception:  # noqa: BLE001 - telemetry never fails the action
            pass

    def _attention_labels(
        self, repo: str, number: int, prs: tuple[int, ...],
    ) -> tuple[
        tuple[str, ...], dict[int, tuple[str, ...]], str
    ]:
        """The labels currently holding this issue (or its PRs) on a human, and
        a detail when they cannot be read at all.

        A failed read is refused rather than assumed empty: the mutation below
        would fail on the same unreachable GitHub anyway, and guessing "not
        held" here is the one guess that costs breaker state.
        """
        try:
            gh_issues, gh_prs, warnings = self._gh_context(repo)
        except Exception as exc:  # noqa: BLE001 - operator-facing result
            return (), {}, f"could not read labels for #{number}: {exc}"
        issue = gh_issues.get(number)
        if issue is None:
            return (), {}, (
                warnings[0] if warnings
                else f"issue #{number} is not open on {repo}"
            )
        issue_labels = tuple(snapshots.labels_of(issue))
        issue_held = tuple(
            label
            for label in actions.UNBLOCK_LABELS
            if label in set(issue_labels)
        )
        pr_held: dict[int, tuple[str, ...]] = {}
        for pr in prs:
            labels = set(snapshots.labels_of(gh_prs.get(pr) or {}))
            held = tuple(
                label for label in actions.UNBLOCK_LABELS if label in labels
            )
            if held:
                pr_held[pr] = held
        return issue_held, pr_held, ""

    def _pr_hold_role(self, row: dash.IssueRow | None, pr: int) -> str:
        """Role whose successful relabel placed this PR on human review.

        ``needs-human-review`` is a shared destination but custom review
        deployments have two different source labels.  The typed lifecycle
        event preserves the producer role and the exact relabel, so use that
        durable provenance rather than guessing from the current one-label
        GitHub state.  Missing provenance returns ``""`` and the caller may
        proceed only when both roles share the same trigger.
        """
        if row is None:
            return ""
        source = ""
        for event in row.events:
            action = event.get("action")
            action = action if isinstance(action, dict) else {}
            if action.get("labels_applied") is False:
                continue
            attributes = event.get("attributes")
            attributes = attributes if isinstance(attributes, dict) else {}
            kind = str(action.get("type") or event.get("kind") or "").lower()
            target = str(action.get("target") or event.get("target") or "").upper()
            raw_number = event.get("pr") or action.get("number") or event.get("number")
            try:
                event_pr = int(raw_number)
            except (TypeError, ValueError):
                continue
            raw_add = action.get("add", attributes.get("add"))
            if isinstance(raw_add, str):
                added = {raw_add}
            elif isinstance(raw_add, (list, tuple, set, frozenset)):
                added = {str(label) for label in raw_add}
            else:
                added = set()
            raw_remove = action.get("remove", attributes.get("remove"))
            if isinstance(raw_remove, str):
                removed = {raw_remove}
            elif isinstance(raw_remove, (list, tuple, set, frozenset)):
                removed = {str(label) for label in raw_remove}
            else:
                removed = set()
            if not added and not removed:
                fact = dash.parse_action(str(event.get("desc") or ""))
                if fact is not None and fact.verb == "relabel":
                    kind = fact.verb
                    target = fact.target.upper()
                    event_pr = fact.number
                    added = set(fact.added)
                    removed = set(fact.removed)
            if event_pr != pr or not target.startswith("PR"):
                continue
            if Label.NEEDS_HUMAN_REVIEW.value in removed:
                source = ""
            if kind == "escalate_ci_pending":
                # The fixer's stalled-CI action is a compound comment/relabel
                # operation.  Its typed journal payload intentionally has no
                # generic add/remove fields, but the action contract moves
                # ai-pr-open to needs-human-review.
                producer = event.get("producer")
                producer = producer if isinstance(producer, dict) else {}
                role = str(event.get("role") or producer.get("role") or "")
                source = "fix" if role == "fix" else ""
            elif (
                kind == "relabel"
                and Label.NEEDS_HUMAN_REVIEW.value in added
            ):
                producer = event.get("producer")
                producer = producer if isinstance(producer, dict) else {}
                role = str(event.get("role") or producer.get("role") or "")
                source = role if role in {"fix", "review"} else ""
        return source

    def _journal_unblock(
        self,
        name: str,
        repo: str,
        number: int,
        held: tuple[str, ...],
        cleared: int,
        applied: bool,
        issue_added: tuple[str, ...] = (),
        issue_removed: tuple[str, ...] = (),
        issue_requested_added: tuple[str, ...] = (),
        issue_applied: bool = True,
        pr_held: dict[int, tuple[str, ...]] | None = None,
        pr_added: dict[int, tuple[str, ...]] | None = None,
        pr_removed: dict[int, tuple[str, ...]] | None = None,
        pr_requested_added: dict[int, tuple[str, ...]] | None = None,
        pr_applied: dict[int, bool] | None = None,
        channel: str = "api",
        *, journal: Journal | None,
    ) -> None:
        if journal is None:
            return
        # One desc per target so parse_action sees each clear (the PR's labels
        # are what actually held keifu #173/#177); the issue desc leads.
        issue_intent = " ".join(
            [*(f"+{label}" for label in issue_requested_added),
             *(f"-{label}" for label in held)]
        )
        issue_effect = " ".join(
            [*(f"+{label}" for label in issue_added),
             *(f"-{label}" for label in issue_removed)]
        )
        issue_moves = (
            issue_effect if issue_applied
            else "label edit failed: " + issue_intent
        )
        try:
            journal.emit(
                TelemetryEvent(
                    ActionApplied(
                        f"operator-unblock:{uuid.uuid4().hex}",
                        "operator_unblock",
                        "issue",
                        f"operator unblock issue #{number}: "
                        + (issue_moves or "(none)"),
                        number,
                        {
                            "add": list(issue_added) if issue_applied else [],
                            "remove": list(issue_removed) if issue_applied else [],
                            "requested_add": list(issue_requested_added),
                            "requested_remove": list(held),
                            "operator": f"{repo}:dashboard",
                            "channel": channel,
                            "retries_cleared": cleared,
                            "labels_applied": issue_applied,
                        },
                    ),
                    Correlation(issue=number),
                )
            )
            for pr, labels in (pr_held or {}).items():
                added = (pr_added or {}).get(pr, ())
                removed = (pr_removed or {}).get(pr, ())
                requested_added = (pr_requested_added or {}).get(pr, ())
                target_applied = (pr_applied or {}).get(pr, applied)
                intent = " ".join(
                    [*(f"+{label}" for label in requested_added),
                     *(f"-{label}" for label in labels)]
                )
                effect = " ".join(
                    [*(f"+{label}" for label in added),
                     *(f"-{label}" for label in removed)]
                )
                moves = effect if target_applied else "label edit failed: " + intent
                journal.emit(
                    TelemetryEvent(
                        ActionApplied(
                            f"operator-unblock:{uuid.uuid4().hex}",
                            "operator_unblock",
                            "pr",
                            f"operator unblock pr #{pr}: " + moves,
                            pr,
                            {
                                "add": list(added) if target_applied else [],
                                "remove": list(removed) if target_applied else [],
                                "requested_add": list(requested_added),
                                "requested_remove": list(labels),
                                "operator": f"{repo}:dashboard",
                                "channel": channel,
                                "labels_applied": target_applied,
                            },
                        ),
                        Correlation(issue=number, pr=pr),
                    )
                )
        except Exception:  # noqa: BLE001 - telemetry never fails the action
            pass

    def _legacy_retry_grant_required(self, state_dirs: tuple[str, ...], number: int, prs: tuple[int, ...]) -> bool:
        """A legacy label edit cannot mint a selected recovery allowance."""
        keys = [number, *(-pr for pr in prs if pr > 0)]
        for role, state_dir in zip(dash.ROLE_NAMES, state_dirs):
            if not state_dir or role == "review":
                continue
            store = Store(state_dir)
            for key in keys:
                for suffix in ("ci-retries", "review-retries", "rebase-retries"):
                    if os.path.lexists(store.issue_path(key, suffix)):
                        return True
        return False

    def _issue_prs(self, name: str, number: int) -> list[int]:
        try:
            row = snapshots.find_row(self.snapshot(name), number)
        except OSError:
            return []
        return list(row.prs) if row is not None else []

    def _operator_effects(
        self,
        name: str,
        repo: str,
        *,
        journalled: bool = True,
        control: SQLiteControlStore | None = None,
        state_dirs: tuple[str, ...] | None = None,
    ):
        # Derived from journal_paths like _clear_retries, NOT read raw from the
        # overlay: the STATE_DIR keys are optional and standard deployments
        # never set them, so the raw read produced () and control_store_path
        # refused — every journaled operator action (promote/priority/unblock)
        # 500'd on a normal deployment (keifu 2026-08-02). journal_paths
        # resolves snapshot → overlay → the daemon's own default per role.
        if state_dirs is None:
            state_dirs = tuple(os.path.dirname(path) for path in cli.journal_paths(name))
        journal = self._operator_journal(name, repo) if journalled else None
        established = {"control": control} if control is not None else {}
        return self.effects_factory(
            repo,
            state_dirs,
            identity=f"{repo}:dashboard",
            journal=journal,
            **established,
        )

    def _operator_journal(self, name: str, repo: str, *, legacy_path: str | None = None) -> Journal | None:
        """The fix role's journal, where an operator intervention becomes
        visible to the board; the dashboard holds no other record of it."""
        if not repo:
            return None
        return Journal(cli.journal_paths(name)[0] if legacy_path is None else legacy_path)


def _local_timezone(name: str | None):
    if name and len(name) <= 100:
        try:
            return ZoneInfo(name)
        except (ValueError, ZoneInfoNotFoundError):
            pass
    return datetime.now().astimezone().tzinfo or timezone.utc
