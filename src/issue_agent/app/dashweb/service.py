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
from .. import cli
from . import snapshots


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


def _detail(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:400]


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

    def snapshot(self, name: str, *, refresh: bool = False) -> dash.Snapshot:
        if not refresh:
            cached = self._snapshots.get(name)
            if cached is not None:
                return cached
        return self._snapshots.put(
            name,
            self._collector.collect(
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
                key: _github_issue_enrichment(value)
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

    def unblock(
        self, name: str, number: int, channel: str = "api"
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
        """
        if not self.known(name):
            return False, f"unknown deployment '{name}'"
        repo = self.overlay_field(name, "REPO")
        if not repo:
            return False, f"no REPO configured for deployment '{name}'"
        held, pr_held, unreadable = self._attention_labels(
            name, repo, number
        )
        if unreadable:
            return False, unreadable
        if not held and not pr_held:
            return False, f"nothing to unblock on #{number}"
        issue_add, issue_remove = actions.issue_unblock_change(held)
        raw_trigger = (
            cli.deployment_env_field(name, "REVIEW_TRIGGER_LABEL")
            or Label.AI_PR_OPEN.value
        )
        try:
            review_trigger = Label(raw_trigger).value
        except ValueError:
            # Match ReviewWorkflow._trigger_label: an invalid configured label
            # falls back to ai-pr-open rather than creating an unreadable arm.
            review_trigger = Label.AI_PR_OPEN.value
        pr_changes: dict[int, tuple[tuple[str, ...], tuple[str, ...]]] = {}
        candidate_pr = 0
        try:
            for pr, labels in pr_held.items():
                candidate_pr = pr
                pr_changes[pr] = actions.pr_unblock_change(
                    labels,
                    review_trigger,
                    self._pr_hold_role(name, number, pr),
                )
        except ValueError as exc:
            # Resolve every admission decision before counters or labels move:
            # an ambiguous custom-trigger re-arm is safer left held than half
            # admitted to a role that cannot advance it.
            return False, f"pr #{candidate_pr}: {exc}"
        # Counters first: a cleared counter with the labels still on holds the
        # issue exactly as before, while the reverse order would re-admit it
        # against a breaker that is still tripped.
        cleared = self._clear_retry_counters(name, number)
        # Unjournalled effects: this flow writes its own single event below,
        # because the counter reset happened whatever GitHub then did with the
        # labels — including the no-op path, which emits nothing of its own.
        effects = self._operator_effects(name, repo, journalled=False)
        # Before the labels, and never allowed to wedge them: the counters and
        # labels are still worth clearing when a dismissal cannot happen. But a
        # re-arm that failed must NOT read as a clean unblock — the whole
        # incident was a green result over a dead PR — so its notes ride back
        # in the operator-facing detail as well as the journal.
        rearm_failures = self._rearm_rejections(name, repo, number, effects)
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
        )
        if applied and rearm_failures:
            return True, "ok — but " + "; ".join(rearm_failures)
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
        self, name: str, repo: str, number: int, effects
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
        review_bot = cli.deployment_env_field(name, "REVIEW_BOT")
        ceiling = _positive_int(
            cli.deployment_env_field(name, "REVIEW_MAX_REJECTIONS"),
            review_dom.DEFAULT_MAX_REVIEW_REJECTIONS,
        )
        prs = self._issue_prs(name, number)
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
                self._journal_rearm_failure(name, repo, number, pr, note)
                continue
            # A PR carrying no change request at all has no ceiling to re-arm:
            # say nothing rather than journal a "could not dismiss" line on
            # every unblock of a perfectly healthy issue.
            if not any(
                r.state == review_dom.REVIEW_CHANGES_REQUESTED for r in reviews
            ):
                continue
            if not review_bot:
                note = (
                    f"pr #{pr}: no REVIEW_BOT configured — standing change "
                    "requests were left in place, so the rejection ceiling is "
                    "still armed"
                )
                failures.append(note)
                self._journal_rearm_failure(name, repo, number, pr, note)
                continue
            counted = review_dom.stale_rejections(
                reviews, review_bot, limit=ceiling
            )
            # The same arithmetic `review_submission` applies: this many
            # rejections stand, so the NEXT one escalates instead of bouncing
            # back to the fixer. Below that the loop still has a round to give
            # and the feedback must survive.
            if len(counted) + 1 < ceiling:
                continue
            for review in counted:
                ok, detail = effects.dismiss_review(pr, review, message)
                if not ok:
                    failures.append(
                        f"pr #{pr} review {review.review_id} not dismissed: "
                        f"{detail}"
                    )
                self._journal_dismissal(
                    name, repo, number, pr, review, ok, detail
                )
        return failures

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
        name: str,
        repo: str,
        number: int,
        pr: int,
        review,
        ok: bool,
        detail: str,
    ) -> None:
        journal = self._operator_journal(name, repo)
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
        self, name: str, repo: str, number: int, pr: int, detail: str
    ) -> None:
        journal = self._operator_journal(name, repo)
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
        self, name: str, repo: str, number: int
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
        for pr in self._issue_prs(name, number):
            labels = set(snapshots.labels_of(gh_prs.get(pr) or {}))
            held = tuple(
                label for label in actions.UNBLOCK_LABELS if label in labels
            )
            if held:
                pr_held[pr] = held
        return issue_held, pr_held, ""

    def _pr_hold_role(self, name: str, issue: int, pr: int) -> str:
        """Role whose successful relabel placed this PR on human review.

        ``needs-human-review`` is a shared destination but custom review
        deployments have two different source labels.  The typed lifecycle
        event preserves the producer role and the exact relabel, so use that
        durable provenance rather than guessing from the current one-label
        GitHub state.  Missing provenance returns ``""`` and the caller may
        proceed only when both roles share the same trigger.
        """
        try:
            # Admission safety cannot use the UI's short stale cache: a fresh
            # GitHub hold may arrive just after the prior unblock event was
            # journalled, and reusing the previous episode's source label can
            # admit the wrong role on custom-trigger deployments.
            row = snapshots.find_row(self.snapshot(name, refresh=True), issue)
        except OSError:
            return ""
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
    ) -> None:
        journal = self._operator_journal(name, repo)
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
        # Derived from journal_paths like _clear_retries, NOT read raw from the
        # overlay: the STATE_DIR keys are optional and standard deployments
        # never set them, so the raw read produced () and control_store_path
        # refused — every journaled operator action (promote/priority/unblock)
        # 500'd on a normal deployment (keifu 2026-08-02). journal_paths
        # resolves snapshot → overlay → the daemon's own default per role.
        state_dirs = tuple(
            os.path.dirname(path) for path in cli.journal_paths(name)
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
