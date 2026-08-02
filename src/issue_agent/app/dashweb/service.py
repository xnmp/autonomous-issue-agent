"""Dashboard application service and composition façade."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ...adapters.dashweb import actions, costchart, ghdata, system, usage, verification
from ...adapters.event_log import EventLog
from ...adapters.journal import default_ledger_path
from ...adapters.operator_effects import build_operator_effects
from ...adapters.store import Store
from ...adapters.typed_journal import TypedJournal
from ...domain import dashboard as dash
from ...domain.events import (
    ActionApplied,
    Correlation,
    Producer,
    TelemetryEvent,
)
from .. import cli
from . import snapshots


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Backend:
    """Every collector the API exposes, over one cached `gh` reader."""

    def __init__(
        self,
        harness_root: str | None = None,
        gh: ghdata.GhReader | None = None,
        now_fn=None,
        stale_after_s: float = dash.STALE_AFTER_S,
        snapshot_ttl_s: float = 3.0,
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
        self._self_repo = system.remote_repo(self.harness_root)
        self.effects_factory = effects_factory
        self.control_db_path = control_db_path
        self._journals: dict[str, TypedJournal | None] = {}

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

    def snapshot(self, name: str) -> dash.Snapshot:
        cached = self._snapshots.get(name)
        if cached is not None:
            return cached
        return self._snapshots.put(
            name,
            system.collect_snapshot(
                name=name,
                repo=self.overlay_field(name, "REPO"),
                target_dir=self.overlay_field(name, "TARGET_DIR"),
                journal_paths=cli.journal_paths(name),
                log_dir=cli.log_dir(),
                ledger_path=default_ledger_path(),
                now_fn=self.now_fn,
                stale_after_s=self.stale_after_s,
                control_db_path=self.control_db_path,
            ),
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
        issues_payload, issues_error = self.gh.issues(repo)
        prs_payload, prs_error = self.gh.prs(repo)
        warnings = [m for m in (issues_error, prs_error) if m]
        return (
            ghdata.index_by_number(issues_payload),
            ghdata.index_by_number(prs_payload),
            warnings,
        )

    def issues(self, name: str) -> dict:
        snap = self.snapshot(name)
        now = self.now_fn()
        gh_issues, gh_prs, warnings = self._gh_context(snap.repo)
        if snap.deployment.schema_version == 2:
            gh_issues = {
                number: _github_enrichment(issue)
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
                    snapshots.pending_card(issue, row_by_issue.get(number))
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
                            issue, row_by_issue.get(number)
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
            "gh_ok": not warnings,
            "notifications": snapshots.journal_notifications(rows),
            "warnings": warnings,
        }

    def issue(self, name: str, number: int) -> dict | None:
        snap = self.snapshot(name)
        row = snapshots.find_row(snap, number)
        if row is None:
            return None
        gh_issues, gh_prs, warnings = self._gh_context(snap.repo)
        if snap.deployment.schema_version == 2:
            gh_issues = {
                key: _github_enrichment(value)
                for key, value in gh_issues.items()
            }
            gh_prs = {
                key: _github_enrichment(value)
                for key, value in gh_prs.items()
            }
        detail = snapshots.issue_detail(
            row, self.now_fn(), gh_issues.get(number), gh_prs
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

    def unblock(self, name: str, number: int) -> tuple[bool, str]:
        """Undo a hold a human is expected to clear: drop the labels that stop
        re-dispatch and hand back the retry budget the wedge consumed. Nothing
        is added — `decide()` re-derives the next lifecycle label itself.

        Refused unless the issue is actually held. Resetting breakers is not a
        neutral act: on a healthy issue it would silently buy retries nobody
        asked for, so "nothing to unblock" must cost nothing.
        """
        if not self.known(name):
            return False, f"unknown deployment '{name}'"
        repo = self.overlay_field(name, "REPO")
        if not repo:
            return False, f"no REPO configured for deployment '{name}'"
        held, unreadable = self._attention_labels(name, repo, number)
        if unreadable:
            return False, unreadable
        if not held:
            return False, f"nothing to unblock on #{number}"
        add, remove = actions.unblock_change()
        # Counters first: a cleared counter with the labels still on holds the
        # issue exactly as before, while the reverse order would re-admit it
        # against a breaker that is still tripped.
        cleared = self._clear_retry_counters(name, number)
        # Unjournalled effects: this flow writes its own single event below,
        # because the counter reset happened whatever GitHub then did with the
        # labels — including the no-op path, which emits nothing of its own.
        effects = self._operator_effects(name, repo, journalled=False)
        applied, detail = effects.set_issue_labels(number, add=add, remove=remove)
        self._journal_unblock(name, repo, number, held, cleared, applied)
        return applied, detail

    def _attention_labels(
        self, name: str, repo: str, number: int
    ) -> tuple[tuple[str, ...], str]:
        """The labels currently holding this issue (or its PRs) on a human, and
        a detail when they cannot be read at all.

        A failed read is refused rather than assumed empty: the mutation below
        would fail on the same unreachable GitHub anyway, and guessing "not
        held" here is the one guess that costs breaker state.
        """
        try:
            gh_issues, gh_prs, warnings = self._gh_context(repo)
        except Exception as exc:  # noqa: BLE001 - operator-facing result
            return (), f"could not read labels for #{number}: {exc}"
        issue = gh_issues.get(number)
        if issue is None:
            return (), (
                warnings[0] if warnings
                else f"issue #{number} is not open on {repo}"
            )
        labels = set(snapshots.labels_of(issue))
        for pr in self._issue_prs(name, number):
            labels.update(snapshots.labels_of(gh_prs.get(pr) or {}))
        return tuple(
            label for label in actions.UNBLOCK_LABELS if label in labels
        ), ""

    def _journal_unblock(
        self,
        name: str,
        repo: str,
        number: int,
        held: tuple[str, ...],
        cleared: int,
        applied: bool,
    ) -> None:
        journal = self._operator_journal(name, repo)
        if journal is None:
            return
        try:
            journal.emit(
                TelemetryEvent(
                    ActionApplied(
                        f"operator-unblock:{uuid.uuid4().hex}",
                        "operator_unblock",
                        "issue",
                        f"operator unblock issue #{number}: "
                        + " ".join(f"-{label}" for label in held),
                        number,
                        {
                            "add": [],
                            "remove": list(held),
                            "operator": f"{repo}:dashboard",
                            "retries_cleared": cleared,
                            "labels_applied": applied,
                        },
                    ),
                    Correlation(issue=number),
                )
            )
        except Exception:  # noqa: BLE001 - telemetry never fails the action
            pass

    def _clear_retry_counters(self, name: str, number: int) -> int:
        """Reset the per-attempt ceilings in every role's state dir.

        The operator cannot be asked which role wedged: ci-/rebase-retries are
        the fix role's, review-retries the review role's. The review role's
        infra-death ceiling is cleared too — a dead-session loop is precisely
        what an unblock answers — for the issue and for the negated PR keys
        (`issue--<pr>.infra-deaths`) `domain.fix.breaker_key` uses when a PR has
        no resolvable issue. `Store.clear_retries` itself keeps infra-deaths,
        since no automatic path may reset that ceiling.

        Returns how many counters were actually retired, so the journalled
        event reports what happened rather than what was attempted.
        """
        prs = self._issue_prs(name, number)
        keys = [number, *(-pr for pr in prs if pr > 0)]
        cleared = 0
        for role, path in zip(dash.ROLE_NAMES, cli.journal_paths(name)):
            state_dir = os.path.dirname(path)
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

    def _operator_effects(self, name: str, repo: str, *, journalled: bool = True):
        state_dirs = tuple(
            self.overlay_field(name, field)
            for field in ("STATE_DIR", "REVIEW_STATE_DIR", "STEWARD_STATE_DIR")
        )
        return self.effects_factory(
            repo,
            state_dirs,
            identity=f"{repo}:dashboard",
            journal=self._operator_journal(name, repo) if journalled else None,
        )

    def _operator_journal(self, name: str, repo: str) -> TypedJournal | None:
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
        if name in self._journals:
            return self._journals[name]
        legacy = cli.journal_paths(name)[0]
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
        self._journals[name] = journal
        return journal


def _github_enrichment(value: dict) -> dict:
    """Only fields GitHub may add to an authoritative v2 projection.

    Every key here is presentational: the v2 world projection decides which
    bucket a card sits in, and none of these can move it. The PR merge/review
    fields are what the card chips render — without them a v2 deployment showed
    a PR chip with no state at all.
    """

    return {
        key: value[key]
        for key in ("title", "url", "state", "mergedAt", "reviewDecision", "mergeable")
        if value.get(key) is not None
    }


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
