"""Dashboard application service and composition façade."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ...adapters.dashweb import actions, costchart, ghdata, system, usage, verification
from ...adapters.journal import default_ledger_path
from ...domain import dashboard as dash
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
            first_observed_day=snapshots.first_observed_day(snapshot, local_tz),
            local_tz=local_tz,
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
        rows = snap.deployment.issues
        metrics = snap.deployment.metrics
        projected_cost = metrics.cost_per_done_usd if metrics.done_issues else None
        projected_eta = snapshots.mean_wall_clock_s(rows)

        def card(row):
            return snapshots.issue_card(
                row,
                now,
                gh_issue=gh_issues.get(row.issue),
                gh_prs=gh_prs,
                projected_cost_usd=projected_cost,
                projected_eta_s=projected_eta,
            )

        row_by_issue = {row.issue: row for row in rows}
        in_flight_numbers = {row.issue for row in rows if not row.done}
        pending, backlog = [], []
        for number, issue in sorted(gh_issues.items()):
            if not ghdata.is_open(issue):
                continue
            labels = snapshots.labels_of(issue)
            if "ai-ready" in labels and number not in in_flight_numbers:
                pending.append(snapshots.pending_card(issue, row_by_issue.get(number)))
            elif snapshots.is_backlog(issue):
                backlog.append(
                    snapshots.backlog_card(issue, now, row_by_issue.get(number))
                )
        return {
            "in_flight": [card(row) for row in rows if not row.done],
            "pending": pending,
            "done": [card(row) for row in rows if row.done],
            "backlog": backlog,
            "gh_ok": not warnings,
            "warnings": warnings,
        }

    def issue(self, name: str, number: int) -> dict | None:
        snap = self.snapshot(name)
        row = snapshots.find_row(snap, number)
        if row is None:
            return None
        gh_issues, gh_prs, warnings = self._gh_context(snap.repo)
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
        return actions.run_label_commands([actions.promote_command(repo, number)])

    def priority(self, name: str, number: int, priority: str | None) -> tuple[bool, str]:
        if not self.known(name):
            return False, f"unknown deployment '{name}'"
        repo = self.overlay_field(name, "REPO")
        if not repo:
            return False, f"no REPO configured for deployment '{name}'"
        return actions.run_label_commands(
            actions.priority_commands(repo, number, priority)
        )


def _local_timezone(name: str | None):
    if name and len(name) <= 100:
        try:
            return ZoneInfo(name)
        except (ValueError, ZoneInfoNotFoundError):
            pass
    return datetime.now().astimezone().tzinfo or timezone.utc
