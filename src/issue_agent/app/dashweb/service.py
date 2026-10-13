"""Dashboard application service and composition façade."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ...adapters.dashweb import actions, costchart, ghdata, system, usage, verification
from ...adapters.journal import Journal, default_ledger_path
from ...adapters.operator_effects import build_operator_effects
from ...domain import dashboard as dash
from ...domain.fix import harness_merges_branch

from .. import cli, config
from . import snapshots
from .ports import GitHubQueries


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


_DETAIL_CONFIG_FIELDS = (
    "MAX_BUDGET_USD",
    "ROUTER_BUDGET_USD",
    "REVIEW_BUDGET_USD",
)
_FIX_BUDGET_KINDS = (
    "RESOLVE", "ADDRESS_REVIEW", "FIX_CI", "REBASE",
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

    def park(self, name: str, number: int, channel: str = "api") -> tuple[bool, str]:
        """Hand an issue to a human: `needs-human` on the issue and on each open
        harness PR for it (refactor decision Q19). The fix policy waits on
        that label until it is removed or a human replies to the stop."""
        return self._move_hold(name, number, channel, park=True)

    def unblock(self, name: str, number: int, channel: str = "api") -> tuple[bool, str]:
        """Release a held issue: remove `needs-human` from the issue and its open
        harness PRs, and nothing else."""
        return self._move_hold(name, number, channel, park=False)

    def _move_hold(self, name: str, number: int, channel: str, *, park: bool) -> tuple[bool, str]:
        if not self.known(name):
            return False, f"unknown deployment '{name}'"
        repo = self.overlay_field(name, "REPO")
        if not repo:
            return False, f"no REPO configured for deployment '{name}'"
        return self._operator_effects(name, repo).move_hold(number, park=park, channel=channel)

    def _operator_effects(self, name: str, repo: str):
        journal = self._operator_journal(name, repo)
        return self.effects_factory(repo, identity=f"{repo}:dashboard", journal=journal)

    def _operator_journal(self, name: str, repo: str) -> Journal | None:
        """The fix role's journal, where an operator intervention becomes
        visible to the board; the dashboard holds no other record of it."""
        if not repo:
            return None
        return Journal(cli.journal_paths(name)[0])


def _local_timezone(name: str | None):
    if name and len(name) <= 100:
        try:
            return ZoneInfo(name)
        except (ValueError, ZoneInfoNotFoundError):
            pass
    return datetime.now().astimezone().tzinfo or timezone.utc
