"""Dashboard application service and composition façade."""

from __future__ import annotations

import os
import subprocess
import threading
import time
import uuid
from collections.abc import Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .resolve_contracts import CommandResult, ResolveCommand, ResolveControl

from ...adapters.dashweb import actions, costchart, ghdata, system, usage, verification
from ...adapters.control_store import SQLiteControlStore
from ...adapters.control_path import control_store_path
from ...adapters.event_log import EventLog
from ...adapters.github import parse_pr
from ...adapters.journal import default_ledger_path
from ...adapters.operator_effects import build_operator_effects
from ...adapters.store import Store
from ...adapters.typed_journal import TypedJournal
from ...domain import dashboard as dash
from ...domain.fix import breaker_key
from ...domain.labels import Label

# (target, number, labels added, labels removed) — one applied park/unpark move.
_ParkMove = tuple[str, int, tuple[str, ...], tuple[str, ...]]
from ...domain import review as review_dom
from ...domain.events import (
    ActionApplied,
    Correlation,
    Producer,
    TelemetryEvent,
)
from .. import cli, config
from ..review_identity import review_identity
from ..resolve_control import load_operator_configuration, open_resolve_operator
from . import snapshots
from .guidance import GuidanceError, parse_guidance
from .ports import GitHubQueries


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _positive_int(raw: str, default: int) -> int:
    """A configured ceiling, or the domain default when the knob is absent or
    malformed. A configured 0 is returned as 0 and means the breaker is OFF —
    the harness-wide convention every other ceiling uses. Guessing a number
    there would invent a breaker the daemons do not have."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(value, 0)


_DETAIL_CONFIG_FIELDS = (
    "MAX_BUDGET_USD",
    "ROUTER_BUDGET_USD",
    "REVIEW_BUDGET_USD",
    "STEWARD_TRIAGE_BUDGET_USD",
    "JIRA_SYNC_BUDGET_USD",
    "MAX_UNPRODUCTIVE_REVIEW_ROUNDS",
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


def _detail_limits(name: str, repo: str) -> tuple[dict[str, float], int]:
    configured = {
        field: value
        for field in _DETAIL_CONFIG_FIELDS
        if (value := cli.deployment_env_field(name, field))
    }
    cfg = config.from_env({"REPO": repo, **configured})
    caps = {
        **{kind: max(0.0, cfg.fix_budget_usd) for kind in _FIX_BUDGET_KINDS},
        "ROUTE": max(0.0, cfg.router_budget_usd),
        "REVIEW": max(0.0, cfg.review_budget_usd),
        "RE_REVIEW": max(0.0, cfg.review_budget_usd),
        "TRIAGE": max(0.0, cfg.triage_budget_usd),
        "GROOM": max(0.0, cfg.triage_budget_usd),
        "JIRA_SYNC": max(0.0, cfg.jira_budget_usd),
    }
    return (
        caps,
        max(0, cfg.max_unproductive_review_rounds),
    )


def _detail(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:400]


def _identity_run(identity):
    """A subprocess runner that keeps a role identity in the child env."""

    def run(argv, **kwargs):
        configured = kwargs.pop("env", None)
        env = dict(os.environ if configured is None else configured)
        token = identity.env_token()
        if not token:
            return subprocess.run(argv, env=env, **kwargs)
        env["GH_TOKEN"] = token
        env["GITHUB_TOKEN"] = token
        return subprocess.run(argv, env=env, **kwargs)

    return run


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
        resolve_factory=open_resolve_operator,
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
        # Behind the TTL, because the TTL only bounds how often we ask: on
        # expiry the old collector re-read and re-folded all three feeds from
        # byte zero, so a single polling browser tab held a core at 100% and
        # the cost grew with the deployment's history (keifu, 2026-08-02). The
        # collector answers an unchanged deployment from its own fingerprint
        # and parses only appended bytes otherwise.
        self._collector = system.SnapshotCollector()
        self._self_repo = system.remote_repo(self.harness_root)
        self.effects_factory = effects_factory
        self.control_db_path = control_db_path
        self._resolve_factory = resolve_factory
        self._journals: dict[tuple[str, str, str], TypedJournal | None] = {}

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
        return self._collector.collect(
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
        issues, issue_error = (
            ([], None)
            if snapshot.deployment.schema_version == 2
            else self.gh.issues(snapshot.repo)
        )
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
    ) -> tuple[dict[str, float], int]:
        key = (name, repo)
        return self._detail_configs.get_or_load(
            key, lambda: _detail_limits(name, repo)
        )

    def _review_unproductive_rounds(
        self,
        name: str,
        snapshot: dash.Snapshot,
        row: dash.IssueRow,
        gh_prs: Mapping[int, Mapping],
    ) -> tuple[dict[int, int | None], list[str]]:
        """Read the same two durable sources as ``roles._review_state``.

        File counters cover failures the process survived. Abandoned dispatches
        in the control database cover deaths it did not. Both key namespaces
        are summed because a transient closing-issue lookup failure can write a
        death under ``-PR`` while the next successful poll reads the issue key.
        """
        if not row.prs:
            return {}, []
        counts: dict[int, int | None] = {number: None for number in row.prs}
        warnings: list[str] = []
        review_path = next(
            (
                path
                for role, path in zip(dash.ROLE_NAMES, cli.journal_paths(name))
                if role == "review"
            ),
            "",
        )
        state_dir = os.path.dirname(review_path)
        if not state_dir:
            return counts, ["review breaker state directory is unavailable"]

        process_deaths: dict[int, int] = {}
        if self.control_db_path:
            owner_id = config.owner_identity_for_target(
                snapshot.repo, snapshot.target_dir, "review"
            )
            try:
                process_deaths = SQLiteControlStore(
                    self.control_db_path
                ).abandoned_dispatch_counts(owner_id)
            except Exception as exc:  # noqa: BLE001 - detail degrades honestly
                return counts, [
                    f"review abandoned-dispatch counts unavailable ({_detail(exc)})"
                ]

        store = Store(state_dir)
        for number in row.prs:
            raw = gh_prs.get(number)
            if not isinstance(raw, Mapping) or not {
                "headRefName", "closingIssuesReferences"
            }.issubset(raw):
                warnings.append(
                    f"PR #{number} breaker key unavailable from GitHub details"
                )
                continue
            try:
                pr = parse_pr({**raw, "number": number})
            except (AttributeError, TypeError, ValueError):
                warnings.append(f"PR #{number} breaker key is malformed")
                continue
            counts[number] = sum(
                store.get_infra_deaths(key) + process_deaths.get(key, 0)
                for key in {breaker_key(pr), -number}
            )
        return counts, warnings

    def issues(self, name: str) -> dict:
        snap = self.snapshot(name)
        now = self.now_fn()
        (gh_issues, gh_prs, warnings), enriched = self._deferred_gh_context(
            ("issues", snap.repo),
            lambda: self._gh_context(snap.repo),
        )
        if snap.deployment.schema_version == 2:
            gh_issues = {
                number: _github_issue_enrichment(issue)
                for number, issue in gh_issues.items()
            }
            gh_prs = {
                number: _github_enrichment(pr)
                for number, pr in gh_prs.items()
            }
        rows = snap.deployment.issues
        projected_by_model = snapshots.projected_cost_by_model(rows)
        projected_eta = snapshots.mean_wall_clock_s(rows)
        verification_payload = self.verification()
        ledger_rows = verification_payload.get("rows", [])
        issue_status, pr_status = verification.status_indexes(ledger_rows)
        issue_reason, pr_reason = verification.reason_indexes(ledger_rows)

        def card(row):
            model = snapshots.projection_model(row)
            pr = row.prs[-1] if row.prs else None
            return snapshots.issue_card(
                row,
                now,
                gh_issue=gh_issues.get(row.issue),
                gh_prs=gh_prs,
                projected_cost_usd=projected_by_model.get(model) if model else None,
                projected_eta_s=projected_eta,
                projected_model=model,
                issue_verification=issue_status.get(row.issue),
                pr_verification=pr_status.get(pr) if pr is not None else None,
                issue_verification_reason=issue_reason.get(row.issue),
                pr_verification_reason=(
                    pr_reason.get(pr) if pr is not None else None
                ),
            )

        row_by_issue = {row.issue: row for row in rows}
        ready_numbers = (
            {
                row.issue
                for row in rows
                if row.lane == "pending" and not row.done
            }
            if snap.deployment.schema_version == 2
            else {
                number
                for number, issue in gh_issues.items()
                if ghdata.is_open(issue)
                and "ai-ready" in snapshots.labels_of(issue)
            }
        )
        backlog_numbers = (
            {
                row.issue
                for row in rows
                if row.lane == "backlog" and not row.done
            }
            if snap.deployment.schema_version == 2
            else set()
        )
        in_flight_numbers = {
            row.issue
            for row in rows
            if (
                not row.done
                and row.issue not in ready_numbers
                and row.issue not in backlog_numbers
            )
        }
        pending, backlog = [], []
        if snap.deployment.schema_version == 2:
            for number in sorted(ready_numbers):
                issue = _projected_issue(
                    row_by_issue[number],
                    gh_issues.get(number, {}),
                )
                pending.append(
                    snapshots.pending_card(
                        issue,
                        row_by_issue.get(number),
                        gh_prs=gh_prs,
                    )
                )
            for number in sorted(backlog_numbers):
                issue = _projected_issue(
                    row_by_issue[number],
                    gh_issues.get(number, {}),
                )
                backlog.append(
                    snapshots.backlog_card(
                        issue,
                        now,
                        row_by_issue.get(number),
                    )
                )
        else:
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
        if snap.deployment.schema_version == 2:
            gh_issues = {
                key: _github_issue_enrichment(value)
                for key, value in gh_issues.items()
            }
            gh_prs = {
                key: _github_enrichment(value)
                for key, value in gh_prs.items()
            }
        budget_caps, unproductive_cap = self._cached_detail_limits(
            name, snap.repo
        )
        if enriched:
            unproductive_rounds, breaker_warnings = self._review_unproductive_rounds(
                name, snap, row, gh_prs
            )
        else:
            unproductive_rounds = {pr: None for pr in row.prs}
            breaker_warnings = []
        warnings.extend(breaker_warnings)
        detail = snapshots.issue_detail(
            row,
            self.now_fn(),
            gh_issues.get(number),
            gh_prs,
            budget_caps=budget_caps,
            unproductive_round_cap=unproductive_cap,
            unproductive_round_counts=unproductive_rounds,
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

    def _resolve_operator(self, name: str):
        if not self.known(name):
            raise ValueError(f"unknown deployment '{name}'")
        return self._resolve_factory(name, harness_root=self.harness_root,
                                     control_db_path=self.control_db_path, now=self.now_fn)

    def resolve_control(self, name: str, number: int, request_id: str | None = None) -> ResolveControl:
        from .resolve_control import read_control

        return read_control(self._resolve_operator(name), number, request_id)

    def resolve_command(self, name: str, number: int, body: ResolveCommand) -> CommandResult:
        from .resolve_control import submit_control

        # Reject malformed direct callers before opening configured authority.
        body.command()
        return submit_control(self._resolve_operator(name), number, body)

    def unblock(
        self, name: str, number: int, channel: str = "api", guidance: str | None = None,
    ) -> tuple[bool, str]:
        if not self.known(name):
            return False, f"unknown deployment '{name}'"
        try:
            guidance = parse_guidance(guidance)
            cfg, env = load_operator_configuration(name, harness_root=self.harness_root)
            repo = cfg.repo
            state_dirs = (cfg.state_dir, cfg.review_state_dir, cfg.steward_state_dir)
            path = control_store_path(state_dirs, authority_root=cfg.control_authority_root, temporary_on_missing=False)
            if self.control_db_path and os.path.realpath(self.control_db_path) != os.path.realpath(path):
                return False, "unblock control authority differs from the configured deployment"
            control = SQLiteControlStore(path)
            with control.legacy_resolve_authority(repo):
                return self._legacy_unblock(name, number, channel, guidance, control=control, cfg=cfg, env=env)
        except Exception as exc:  # noqa: BLE001 - failure cannot authorize legacy resets
            return False, f"unblock unavailable ({type(exc).__name__}: {exc})"

    def _legacy_unblock(
        self,
        name: str,
        number: int,
        channel: str = "api",
        guidance: str | None = None,
        *, control: SQLiteControlStore, cfg: config.HarnessConfig,
        env: Mapping[str, str],
    ) -> tuple[bool, str]:
        """Undo a hold a human is expected to clear: dismiss the reviewer's
        standing change requests, drop the labels that stop re-dispatch, and
        hand back the retry budget the wedge consumed. A lifecycle hold is
        atomically replaced with the role's selectable trigger; an advisory
        budget label on an otherwise healthy target is only removed.

        The dismissal is what makes this a RE-ARM rather than a cosmetic clear.
        The rejection ceiling is computed from GitHub review state, not from a
        counter, so labels-and-counters alone left it tripped: the next review
        round was immediately at the ceiling again, and because the escalation
        is edge-triggered on the label it had just removed, nothing was
        journalled, relabelled or dispatched (keifu PR #173 / issue #25,
        2026-08-02 — an invisible dead state with both clocks frozen).

        Refused unless the issue is actually held. Resetting breakers is not a
        neutral act: on a healthy issue it would silently buy retries nobody
        asked for, so "nothing to unblock" must cost nothing.

        `guidance` is the optional scope note the operator attaches to the
        re-arm. Without it the re-armed loop can only repeat the round that
        wedged it — the reviewer's prompt has no channel a human can speak
        through (keifu 2026-08-08: a reviewer demanded evidence the environment
        cannot produce, rode its rejection ceiling, and every unblock bought
        the same four rounds again). It is stored per held PR in the review
        role's state dir and injected into that PR's later review prompts.
        Tri-state: `None` leaves any stored note alone, `""` clears it, text
        sets it — a note must be retractable by the operator who wrote it, and
        blank-as-keep would make it permanent until the PR merged.

        This is an API-only lever for now (the dashboard SPA sends no body), so
        every existing caller keeps working unchanged.
        """
        if not self.known(name):
            return False, f"unknown deployment '{name}'"
        try:
            # Defense in depth: the HTTP boundary already parsed this, but the
            # service is callable directly and a note reaches an agent prompt.
            guidance = parse_guidance(guidance)
        except GuidanceError as exc:
            return False, str(exc)
        repo = cfg.repo
        state_dirs = (cfg.state_dir, cfg.review_state_dir, cfg.steward_state_dir)
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
        raw_trigger = (
            env.get("REVIEW_TRIGGER_LABEL")
            or Label.AI_PR_OPEN.value
        )
        try:
            review_trigger = Label(raw_trigger).value
        except ValueError:
            # Match ReviewWorkflow._trigger_label: an invalid configured label
            # falls back to ai-pr-open rather than creating an unreadable arm.
            review_trigger = Label.AI_PR_OPEN.value
        pr_changes: dict[int, tuple[tuple[str, ...], tuple[str, ...]]] = {}
        pr_hold_roles: dict[int, str] = {}
        candidate_pr = 0
        try:
            for pr, labels in pr_held.items():
                candidate_pr = pr
                hold_role = self._pr_hold_role(row, pr)
                pr_hold_roles[pr] = hold_role
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
        # Counters first: a cleared counter with the labels still on holds the
        # issue exactly as before, while the reverse order would re-admit it
        # against a breaker that is still tripped. The guidance lands in the
        # same window and for the same reason: a PR re-armed before its scope
        # note is durable would run one more unscoped round.
        review_dir = next((path for role, path in zip(dash.ROLE_NAMES, state_dirs) if role == "review"), "")
        effects = self._operator_effects(name, repo, journalled=False, control=control, state_dirs=state_dirs, env=env)
        review_effects = self._operator_effects(name, repo, journalled=False, credential_role="review", control=control, state_dirs=state_dirs, env=env)
        journal = self._operator_journal(name, repo, legacy_path=journal_paths[0])
        cleared = self._clear_retry_counters(state_dirs, number, prs)
        guidance_action, guided_prs, guidance_failures = (
            self._apply_operator_guidance(review_dir, tuple(pr_held), guidance)
        )
        # Unjournalled effects: this flow writes its own single event below,
        # because the counter reset happened whatever GitHub then did with the
        # labels — including the no-op path, which emits nothing of its own.
        # Before the labels, and never allowed to wedge them: the counters and
        # labels are still worth clearing when a dismissal cannot happen. But a
        # re-arm that failed must NOT read as a clean unblock — the whole
        # incident was a green result over a dead PR — so its notes ride back
        # in the operator-facing detail as well as the journal.
        rearm_failures = self._rearm_rejections(
            name,
            repo,
            number,
            review_effects,
            prs=prs, review_dir=review_dir, journal=journal, env=env,
            reviewer_held_prs=frozenset(
                pr
                for pr, labels in pr_held.items()
                if Label.NEEDS_HUMAN_REVIEW.value in labels
                and pr_hold_roles.get(pr) == "review"
            ),
        )
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
            guidance_action=guidance_action,
            guided_prs=guided_prs,
            journal=journal,
        )
        # A guidance write that failed leaves the re-arm itself intact, so it
        # reads like a failed dismissal: the action still happened, and the
        # operator is told exactly what did not, rather than being handed a
        # green "ok" over a review that will run unscoped.
        notes = [*rearm_failures, *guidance_failures]
        if applied and notes:
            return True, "ok — but " + "; ".join(notes)
        if notes:
            return applied, "; ".join([detail, *notes])
        return applied, detail

    def park(
        self, name: str, number: int, channel: str = "api"
    ) -> tuple[bool, str]:
        """Hand this issue's open PR(s) to a human WITHOUT starving the queue.

        Removing `ai-pr-open` is the whole mechanism: the fixer's world only
        contains ai-pr-open PRs, so the parked PR's paths leave the overlap
        serializer next tick and held pending issues dispatch. `ai-parked`
        records the decision on issue and PRs — the steward will not re-triage
        it, the dashboard shows it as a parked pending row. NOTE: unpark is a
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

    def _rearm_rejections(
        self,
        name: str,
        repo: str,
        number: int,
        effects,
        *,
        prs: tuple[int, ...], review_dir: str, journal: TypedJournal | None,
        env: Mapping[str, str],
        reviewer_held_prs: frozenset[int] = frozenset(),
    ) -> list[str]:
        """Dismiss the review bot's standing change requests on this issue's
        open PRs, but ONLY where the rejection ceiling is what is holding them.

        Bounded four ways: only this issue's projected PRs, only PRs whose next
        rejection could no longer bounce back to the fixer, only the reviews
        `stale_rejections` says count toward that ceiling, and never more of
        them than the ceiling itself. Every outcome — including "the review bot
        is not configured" and "GitHub would not say" — is journalled and
        returned; none of them may fail the unblock, because the labels and
        counters are still worth clearing when the dismissal cannot happen.

        The below-ceiling exclusion is not caution, it is correctness: a live
        change request is the fixer's next instruction (`domain.fix` dispatches
        ADDRESS_REVIEW off exactly that state, and reads a DISMISSED review as
        nothing to do). Retracting one because an unrelated breaker — a rebase
        or CI ceiling — held the same issue would destroy the feedback and
        strand the PR in a NEW dead state of the same family as #173.

        Returns one note per outcome the operator must know about — an
        empty list means the ceiling is not what is holding anything here, or
        it was fully re-armed.
        """
        review_bot = env.get("REVIEW_BOT", "")
        ceiling = _positive_int(
            env.get("REVIEW_MAX_REJECTIONS", ""),
            review_dom.DEFAULT_MAX_REVIEW_REJECTIONS,
        )
        # A disabled ceiling (REVIEW_MAX_REJECTIONS=0, the harness-wide
        # "breaker off" convention) can never be what holds a PR, so there is
        # never anything here to re-arm.
        if not prs or ceiling <= 0:
            return []
        message = (
            f"Dismissed by an operator re-arm of issue #{number} from the "
            "harness dashboard: the rejection ceiling is recomputed from "
            "GitHub review state, so this review had to be retracted for the "
            "review loop to resume."
        )
        failures: list[str] = []
        closed = self._closed_prs(repo)
        for pr in prs:
            # A merged or closed PR has no live review loop to re-arm, and
            # GitHub refuses the dismissal anyway. A PR whose state could not be
            # read is NOT assumed closed — the dismissal attempt reports the
            # real error instead of silently skipping.
            if pr <= 0 or pr in closed:
                continue
            try:
                reviews = effects.pr_reviews(pr)
            except Exception as exc:  # noqa: BLE001 - operator-facing result
                note = f"pr #{pr} reviews unreadable: {_detail(exc)}"
                failures.append(note)
                self._journal_rearm_failure(repo, number, pr, note, journal=journal)
                continue
            # A PR carrying no change request at all has no ceiling to re-arm:
            # say nothing rather than journal a "could not dismiss" line on
            # every unblock of a perfectly healthy issue. But if the review bot
            # HAS reviewed and every one of its verdicts is retracted — the
            # state a previous unblock's dismissals leave behind — the re-arm
            # must also reset the review baseline: the copy shortcut may have
            # bound `last-sha` to the current head, so with no live verdict and
            # no head movement neither daemon has any signal left and the PR is
            # stranded in a labeled-open dead state (tauri #634, 2026-08-09).
            bot_live = [
                r
                for r in reviews
                if review_bot
                and r.author == review_bot
                and r.state
                in (
                    review_dom.REVIEW_CHANGES_REQUESTED,
                    review_dom.REVIEW_APPROVED,
                )
            ]
            bot_reviewed = review_bot and any(
                r.author == review_bot for r in reviews
            )
            if not any(
                r.state == review_dom.REVIEW_CHANGES_REQUESTED for r in reviews
            ):
                if bot_reviewed and not bot_live:
                    failures.extend(
                        self._reset_review_baseline(review_dir, repo, number, pr, journal=journal)
                    )
                continue
            if not review_bot:
                note = (
                    f"pr #{pr}: no REVIEW_BOT configured — standing change "
                    "requests were left in place, so the rejection ceiling is "
                    "still armed"
                )
                failures.append(note)
                self._journal_rearm_failure(repo, number, pr, note, journal=journal)
                continue
            counted = review_dom.stale_rejections(
                reviews,
                review_bot,
                limit=ceiling,
                trailing_approvals_reset=pr not in reviewer_held_prs,
            )
            # The same arithmetic `review_submission` applies: this many
            # rejections stand, so the NEXT one escalates instead of bouncing
            # back to the fixer. Below that the loop still has a round to give
            # and the feedback must survive.
            if len(counted) + 1 < ceiling:
                continue
            dismissed_ids: set[str] = set()
            for review in counted:
                ok, detail = effects.dismiss_review(pr, review, message)
                if ok:
                    dismissed_ids.add(review.review_id)
                else:
                    failures.append(
                        f"pr #{pr} review {review.review_id} not dismissed: "
                        f"{detail}"
                    )
                self._journal_dismissal(
                    repo, number, pr, review, ok, detail, journal=journal,
                )
            # If the dismissals just retracted the bot's LAST live verdict, the
            # fixer has nothing to address and the reviewer thinks this head is
            # already reviewed — reset the baseline so a fresh round dispatches
            # (same dead state as above, reached in one unblock instead of two).
            if dismissed_ids and not any(
                r.review_id not in dismissed_ids for r in bot_live
            ):
                failures.extend(
                    self._reset_review_baseline(review_dir, repo, number, pr, journal=journal)
                )
        return failures

    def _reset_review_baseline(
        self, state_dir: str, repo: str, number: int, pr: int, *, journal: TypedJournal | None,
    ) -> list[str]:
        """Clear the review role's `last-sha` for `pr` so `needs_re_review`
        reads the current head as unreviewed and dispatches a fresh round.

        Part of the unblock re-arm contract: dismissing the standing verdict
        removes the fixer's signal, so the re-arm must hand the move back to
        the reviewer. Without this, a verdict already bound to the current head
        (e.g. by the re-review copy shortcut) left `last-sha == head` after the
        dismissal and NOTHING could ever fire again — tauri #634 (2026-08-09)
        sat labeled-open with operator guidance parked until hand surgery.

        Returns operator-facing notes on failure; an empty list means the
        baseline is cleared (or there was no state dir to clear it in, which is
        itself a failure worth surfacing)."""
        if not state_dir:
            note = (
                f"pr #{pr}: review state dir unresolvable — baseline not "
                "reset, a fresh review round may not dispatch"
            )
            self._journal_rearm_failure(repo, number, pr, note, journal=journal)
            return [note]
        try:
            Store(state_dir).set_last_sha(pr, "")
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            note = (
                f"pr #{pr}: review baseline not reset "
                f"({type(exc).__name__}: {exc}) — a fresh review round may "
                "not dispatch"
            )
            self._journal_rearm_failure(repo, number, pr, note, journal=journal)
            return [note]
        if journal is not None:
            try:
                journal.emit(
                    TelemetryEvent(
                        ActionApplied(
                            f"operator-rearm-baseline:{uuid.uuid4().hex}",
                            "operator_rearm_baseline",
                            "pr",
                            f"operator re-arm pr #{pr}: review baseline "
                            "reset — no live reviewer verdict remains, next "
                            "poll dispatches a fresh review round",
                            pr,
                            {
                                "add": [],
                                "remove": [],
                                "operator": f"{repo}:dashboard",
                            },
                        ),
                        Correlation(issue=number, pr=pr),
                    )
                )
            except Exception:  # noqa: BLE001 - telemetry never fails the action
                pass
        return []

    def _closed_prs(self, repo: str) -> frozenset[int]:
        """PRs GitHub says are NOT open. An absent or unreadable state is
        omitted deliberately: "closed" here suppresses a dismissal, and
        suppressing one on a guess is how a re-arm silently does nothing."""
        try:
            payload, _error = self.gh.prs(repo)
        except Exception:  # noqa: BLE001 - unknown state is not "closed"
            return frozenset()
        return frozenset(
            number
            for number, pr in ghdata.index_by_number(payload).items()
            if str(pr.get("state") or "").strip() and not ghdata.is_open(pr)
        )

    def _journal_dismissal(
        self,
        repo: str,
        number: int,
        pr: int,
        review,
        ok: bool,
        detail: str,
        *, journal: TypedJournal | None,
    ) -> None:
        if journal is None:
            return
        try:
            journal.emit(
                TelemetryEvent(
                    ActionApplied(
                        f"operator-dismiss-review:{uuid.uuid4().hex}",
                        "operator_dismiss_review",
                        "pr",
                        f"operator re-arm pr #{pr}: "
                        + (
                            f"dismissed review {review.review_id}"
                            if ok
                            else f"review {review.review_id} NOT dismissed "
                            f"({detail})"
                        ),
                        pr,
                        {
                            "add": [],
                            "remove": [],
                            "operator": f"{repo}:dashboard",
                            "review_id": review.review_id,
                            "review_state": review.state,
                            "dismissed": ok,
                            "detail": detail,
                        },
                    ),
                    Correlation(issue=number, pr=pr),
                )
            )
        except Exception:  # noqa: BLE001 - telemetry never fails the action
            pass

    def _journal_rearm_failure(
        self, repo: str, number: int, pr: int, detail: str, *, journal: TypedJournal | None,
    ) -> None:
        if journal is None:
            return
        try:
            journal.emit(
                TelemetryEvent(
                    ActionApplied(
                        f"operator-dismiss-review:{uuid.uuid4().hex}",
                        "operator_dismiss_review",
                        "pr" if pr else "issue",
                        f"operator re-arm of #{number} could not dismiss "
                        f"change requests: {detail}",
                        pr or number,
                        {
                            "add": [],
                            "remove": [],
                            "operator": f"{repo}:dashboard",
                            "dismissed": False,
                            "detail": detail,
                        },
                    ),
                    Correlation(issue=number, pr=pr or None),
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
        guidance_action: str = "",
        guided_prs: tuple[int, ...] = (),
        *, journal: TypedJournal | None,
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
                            # "set"/"cleared", absent when the re-arm left the
                            # stored note alone — so a repeated unblock is
                            # tellable from a rescoped one in history.
                            **({"guidance": guidance_action} if guided_prs else {}),
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
                                **(
                                    {"guidance": guidance_action}
                                    if pr in guided_prs
                                    else {}
                                ),
                            },
                        ),
                        Correlation(issue=number, pr=pr),
                    )
                )
        except Exception:  # noqa: BLE001 - telemetry never fails the action
            pass

    def _apply_operator_guidance(
        self, state_dir: str, prs: tuple[int, ...], guidance: str | None
    ) -> tuple[str, tuple[int, ...], list[str]]:
        """Set or clear the operator's scope note for each held PR, in the
        review role's state dir (review and repairs share this authority).

        `guidance` is already normalized: None = no change, "" = clear, text =
        set (overwriting — the operator's latest word on a PR is the one the
        next round obeys).

        Returns (action, PRs touched, failures) where action is "set",
        "cleared" or "" — what HAPPENED, not what was asked for: an issue-only
        hold has no PR to carry a note, and the state dir may be unresolvable.

        A write can fail (full disk, permissions). It must not raise: the retry
        counters are already reset by the time this runs, so an escaping
        exception would leave a half-applied unblock reported to the operator as
        a generic refusal. Failures ride back in the detail like the re-arm's
        dismissal failures do.
        """
        if guidance is None or not prs:
            return "", (), []
        if not state_dir:
            return "", (), ["review state dir unresolvable: guidance not stored"]
        store = Store(state_dir)
        action = "set" if guidance else "cleared"
        touched: list[int] = []
        failures: list[str] = []
        for pr in prs:
            try:
                if guidance:
                    store.set_operator_guidance(pr, guidance)
                else:
                    store.clear_operator_guidance(pr)
            except Exception as exc:  # noqa: BLE001 - reported, never raised
                failures.append(
                    f"pr #{pr}: guidance not {action} "
                    f"({type(exc).__name__}: {exc})"
                )
            else:
                touched.append(pr)
        if not touched:
            return "", (), failures
        return action, tuple(touched), failures

    def _clear_retry_counters(self, state_dirs: tuple[str, ...], number: int, prs: tuple[int, ...]) -> int:
        """Reset the per-attempt ceilings in every role's state dir.

        The operator cannot be asked which role wedged: ci-/rebase-retries are
        the fix role's, review-retries the review role's. The review role's
        infra-death ceiling is cleared too — a dead-session loop is precisely
        what an unblock answers — for the issue and for the negated PR keys
        (`issue--<pr>.infra-deaths`) `domain.fix.breaker_key` uses when a PR has
        no resolvable issue. `Store.clear_retries` itself keeps infra-deaths,
        since no automatic path may reset that ceiling.

        The resolve episode is not touched: it is SQLite authority (ADR 0015),
        renewed only by a typed re-arm after explicit family activation. The
        retired resolve files are offline import evidence and are never reset
        here, so an unblock cannot grant a fresh resolve allowance.

        Returns how many counters were actually retired, so the journalled
        event reports what happened rather than what was attempted.
        """
        keys = [number, *(-pr for pr in prs if pr > 0)]
        cleared = 0
        for role, state_dir in zip(dash.ROLE_NAMES, state_dirs):
            if not state_dir:
                continue
            store = Store(state_dir)
            for key in keys:
                cleared += store.clear_retries(key)
                if role == "review":
                    cleared += os.path.exists(store.issue_path(key, "infra-deaths"))
                    store.clear_infra_deaths(key)
        return cleared

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
        credential_role: str = "",
        control: SQLiteControlStore | None = None,
        state_dirs: tuple[str, ...] | None = None,
        env: Mapping[str, str] | None = None,
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
        if credential_role == "review":
            identity_env = {
                field: cli.deployment_env_field(name, field) if env is None else env.get(field, "")
                for field in (
                    "GH_APP_ID",
                    "GH_APP_INSTALL_ID",
                    "GH_APP_KEY_FILE",
                    "GH_APP_TOKEN_CACHE",
                    "REVIEW_BOT_TOKEN",
                )
            }
            identity = review_identity(identity_env)
            source = (
                "github_app"
                if identity.token_provider is not None
                else "review_bot_token"
                if identity.static_token
                else "ambient"
            )
            if identity.token_provider is not None or identity.static_token:
                return self.effects_factory(
                    repo,
                    state_dirs,
                    identity=f"{repo}:dashboard:review:{source}",
                    journal=journal,
                    run=_identity_run(identity),
                    **established,
                )
            return self.effects_factory(
                repo,
                state_dirs,
                identity=f"{repo}:dashboard:review:ambient",
                journal=journal,
                **established,
            )
        return self.effects_factory(
            repo,
            state_dirs,
            identity=f"{repo}:dashboard",
            journal=journal,
            **established,
        )

    def _operator_journal(self, name: str, repo: str, *, legacy_path: str | None = None) -> TypedJournal | None:
        """Where an operator intervention becomes visible to the fold.

        It is written to the fix role's canonical feed because the dashboard
        reads only the three role feeds — an event anywhere else does not
        exist. The producer identity is the dashboard's own and never a
        daemon's: role liveness and the world projection are per-feed, and an
        operator click must not stand in for either.

        Cached for the process's lifetime: opening an `EventLog` re-reads the
        whole feed to restore its projection state, under the writer lock the
        daemons append through, so building one per request would stall a live
        fix loop on a mature deployment. The log is append-only, so a cached
        one can never go stale.
        """
        if not repo:
            return None
        legacy = cli.journal_paths(name)[0] if legacy_path is None else legacy_path
        key = name, repo, os.path.realpath(legacy)
        if key in self._journals:
            return self._journals[key]
        journal: TypedJournal | None = None
        try:
            events = EventLog(
                os.path.join(os.path.dirname(legacy), "events-v2.jsonl"),
                Producer(
                    f"dashboard:{name}",
                    repo,
                    "dashboard",
                    os.getpid(),
                    uuid.uuid4().hex,
                ),
            )
        except (OSError, ValueError):
            journal = None  # telemetry never blocks the mutation
        else:
            journal = TypedJournal(legacy, events)
        self._journals[key] = journal
        return journal


# Only fields GitHub may add to an authoritative v2 projection. Every key is
# presentational: the v2 world projection decides which bucket a card sits in,
# and none of these can move it.
#
# A pull request has no v2-projected lifecycle of its own, so its merge/review
# fields come from GitHub — without them a v2 deployment showed a PR chip with
# no state at all. An *issue* does: `state` and `mergedAt` are the projection's
# to own, and letting a GitHub read supply them would put a second, contradicting
# answer in the same dict key the projection already filled.
_PR_ENRICHMENT_FIELDS = (
    "title", "url", "state", "mergedAt", "reviewDecision", "mergeable",
    # The chip's CI colour: ghdata already fetches the rollup, and without it
    # here every v2 card rendered pr_ci_state=None while CI was visibly red.
    "statusCheckRollup",
    # Labels feed `_human_action`'s attention/ai-blocked arms. Without them
    # the journal fallback was the ONLY block signal for PRs — and it is
    # sticky: an operator clearing ai-blocked with a plain gh write (never
    # journalled) left the fold believing a human owned the row forever,
    # freezing both clocks while paid legs ran (keifu #142/#181, 2026-08-03).
    "labels",
    # Fetched only by GhReader.pr for an open issue detail. Keeping them in the
    # v2 enrichment is necessary because `_github_enrichment` deliberately
    # strips list-query fields that cannot affect the projected lifecycle.
    "headRefName", "headRefOid", "closingIssuesReferences", "commits", "files",
)
# `body` and `createdAt` are presentational fields the projection does not
# carry: the backlog drill-down renders the issue text, and its age column
# needs the creation instant. Dropping them here blanked every expanded
# backlog row (2026-08-02).
_ISSUE_ENRICHMENT_FIELDS = ("title", "url", "body", "createdAt")


def _enrichment(value: dict, fields: tuple[str, ...]) -> dict:
    return {key: value[key] for key in fields if value.get(key) is not None}


def _github_enrichment(value: dict) -> dict:
    """PR-side enrichment: everything a PR chip renders."""

    return _enrichment(value, _PR_ENRICHMENT_FIELDS)


def _github_issue_enrichment(value: dict) -> dict:
    """Issue-side enrichment: only what the v2 projection cannot supply."""

    return _enrichment(value, _ISSUE_ENRICHMENT_FIELDS)


def _projected_issue(row: dash.IssueRow, enrichment: dict) -> dict:
    """Presentation fields from the authoritative v2 world projection."""

    facts = row.facts
    labels = facts.get("labels", ())
    projected_labels = (
        [str(label) for label in labels]
        if isinstance(labels, (tuple, list, set, frozenset))
        else []
    )
    return {
        "number": row.issue,
        "state": str(facts.get("state") or ""),
        "title": facts.get("title"),
        "labels": projected_labels,
        "comments": facts.get("comments_total", facts.get("comments", ())),
        **enrichment,
    }


def _local_timezone(name: str | None):
    if name and len(name) <= 100:
        try:
            return ZoneInfo(name)
        except (ValueError, ZoneInfoNotFoundError):
            pass
    return datetime.now().astimezone().tzinfo or timezone.utc
