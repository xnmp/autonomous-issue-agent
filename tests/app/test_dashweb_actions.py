"""The dashboard's POST actions — asserted as the intent they build, never
executed.

An action test that shelled out would either need a live repo or a mock deep
enough to prove nothing. What matters is the command: `start` must go through
`make point-at` (not a reimplemented launch), and a priority swap must be ONE
edit so a half-applied change cannot leave an issue with no priority at all.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import os
import shlex

import pytest

from issue_agent.adapters.dashweb import actions, costchart
from issue_agent.adapters.journal import read_events
from issue_agent.app.dashweb import guidance as guidance_rules
from issue_agent.app.dashweb import service


# ── start ─────────────────────────────────────────────────────────────────────
def test_start_delegates_to_point_at_detached():
    assert actions.start_command("/h/harness", "demo") == [
        "setsid", "/h/harness/bin/point-at.sh", "demo",
    ]


def test_start_never_routes_through_make():
    """make expands a command-line variable into the recipe TEXT, which it hands
    to /bin/sh — so a `"` in the name would escape into a shell command. The
    name is an argv element to a script here, and nothing else."""
    argv = actions.start_command("/h/harness", 'a";touch pwned;"')
    assert "make" not in argv
    assert argv[-1] == 'a";touch pwned;"'  # inert: one argv element


def test_start_log_path_is_per_deployment():
    assert actions.start_log_path("/logs", "demo").endswith("/dashboard-start-demo.log")


def test_launch_detached_creates_the_log_and_returns_its_path(tmp_path):
    log = str(tmp_path / "nested" / "start.log")
    ok, detail = actions.launch_detached(["true"], log)
    assert ok and detail == log


def test_launch_failure_is_reported_not_raised(tmp_path):
    ok, detail = actions.launch_detached(["/nonexistent/binary"], str(tmp_path / "l.log"))
    assert ok is False and "launch" in detail


# ── priority ──────────────────────────────────────────────────────────────────
def test_setting_a_priority_is_one_atomic_label_intent():
    add, remove = actions.priority_change("P2")
    assert add == ("P2",)
    assert remove == ("P1", "P3")


def test_clearing_a_priority_removes_all_three_and_creates_nothing():
    add, remove = actions.priority_change(None)
    assert add == ()
    assert remove == ("P1", "P2", "P3")


def test_an_unknown_priority_is_rejected_before_any_command_is_built():
    with pytest.raises(ValueError):
        actions.priority_change("P9")


def test_dashboard_label_actions_use_the_durable_operator_effect_port(
    monkeypatch,
):
    calls = []

    class Effects:
        def ensure_label(self, label, **kwargs):
            calls.append(("ensure", label, kwargs))
            return True, "ok"

        def set_issue_labels(self, issue, *, add=(), remove=()):
            calls.append(("set", issue, add, remove))
            return True, "ok"

    factory_calls = []

    def factory(repo, state_dirs, *, identity, journal=None):
        factory_calls.append((repo, state_dirs, identity))
        return Effects()

    monkeypatch.setattr(service.system, "remote_repo", lambda _root: "harness/repo")
    backend = service.Backend(effects_factory=factory)
    monkeypatch.setattr(backend, "known", lambda _name: True)
    # Only REPO comes from the overlay. The state dirs must come from the
    # journal_paths derivation: a standard deployment never sets the STATE_DIR
    # overlay keys, and reading them raw broke every journaled operator action
    # live (keifu 2026-08-02) while this test's overlay mock hid the gap.
    values = {"REPO": "o/r"}
    monkeypatch.setattr(
        backend,
        "overlay_field",
        lambda _name, field: values[field],
    )
    monkeypatch.setattr(
        service.cli,
        "journal_paths",
        lambda _name: [
            "/state/fix/journal.jsonl",
            "/state/review/journal.jsonl",
        ],
    )

    assert backend.promote("demo", 9) == (True, "ok")
    assert backend.priority("demo", 9, "P2") == (True, "ok")

    assert factory_calls == [
        (
            "o/r",
            ("/state/fix", "/state/review"),
            "o/r:dashboard",
        ),
        (
            "o/r",
            ("/state/fix", "/state/review"),
            "o/r:dashboard",
        ),
    ]
    assert calls[0] == ("set", 9, ("ai-ready",), ())
    assert calls[1][0:2] == ("ensure", "P2")
    assert calls[2] == ("set", 9, ("P2",), ("P1", "P3"))


# ── cost chart ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("bad", ["", "../../etc/passwd", "a b", "id;rm -rf /", "x/y"])
def test_a_session_id_that_could_escape_a_path_is_rejected(bad):
    assert costchart.valid_session_id(bad) is False


def test_a_normal_session_id_is_accepted():
    assert costchart.valid_session_id("0f3a-99_bC") is True


def test_the_transcript_is_located_by_glob_across_project_dirs(tmp_path):
    project = tmp_path / "-home-chong-repo"
    project.mkdir()
    (project / "sess1.jsonl").write_text("{}")
    assert costchart.transcript_path("sess1", str(tmp_path)) == str(project / "sess1.jsonl")
    assert costchart.transcript_path("missing", str(tmp_path)) == ""


def test_the_cost_script_is_invoked_with_open_disabled_and_a_private_tmpdir(tmp_path):
    env = costchart.cost_chart_env(str(tmp_path), base_env={"PATH": "/usr/bin"})
    assert env["SESSION_COST_OPEN"] == "0"
    assert env["TMPDIR"] == str(tmp_path)
    assert costchart.cost_chart_command("/s/session_cost.py", "/t/a.jsonl")[-2:] == [
        "/s/session_cost.py", "/t/a.jsonl"
    ]


def test_a_missing_cc_cost_install_is_an_inline_error_not_an_exception(monkeypatch, tmp_path):
    monkeypatch.setenv("CC_COST_SCRIPT", str(tmp_path / "absent.py"))
    html, error = costchart.render_cost_chart("sess1")
    assert html == "" and "CC_COST_SCRIPT" in error


def test_a_missing_transcript_is_reported_against_the_session(monkeypatch, tmp_path):
    script = tmp_path / "session_cost.py"
    script.write_text("print('hi')")
    monkeypatch.setenv("CC_COST_SCRIPT", str(script))
    html, error = costchart.render_cost_chart("sess1", projects_root=str(tmp_path))
    assert html == "" and "sess1" in error


def test_a_generated_chart_is_returned_verbatim(monkeypatch, tmp_path):
    script = tmp_path / "session_cost.py"
    script.write_text("")
    monkeypatch.setenv("CC_COST_SCRIPT", str(script))
    project = tmp_path / "proj"
    project.mkdir()
    (project / "sess1.jsonl").write_text("{}")
    html, error = costchart.render_cost_chart(
        "sess1", projects_root=str(tmp_path),
        run=lambda s, t: ("<html>chart</html>", ""),
    )
    assert (html, error) == ("<html>chart</html>", "")


# ── unblock ───────────────────────────────────────────────────────────────────
def test_unblock_rearms_a_lifecycle_held_issue_for_the_fixer():
    add, remove = actions.issue_unblock_change(("needs-human-review",))
    assert add == ("ai-ready",)
    assert set(remove) == {"needs-human-review", "needs-budget-review", "ai-blocked"}


def test_clearing_only_an_advisory_budget_hold_preserves_the_current_stage():
    add, remove = actions.issue_unblock_change(("needs-budget-review",))
    assert add == ()
    assert set(remove) == set(actions.UNBLOCK_LABELS)


def test_pr_unblock_restores_the_trigger_even_when_adoption_is_disabled():
    add, remove = actions.pr_unblock_change(
        ("needs-human-review",), "ai-pr-open", "review"
    )
    assert add == ("ai-pr-open",)
    assert remove == ("needs-human-review",)


def test_pr_budget_unblock_restores_the_fixer_arm_without_duplicate_labels():
    add, remove = actions.pr_unblock_change(
        ("ai-blocked", "needs-budget-review"), "ai-pr-open"
    )
    assert add == ("ai-pr-open",)
    assert remove == ("ai-blocked", "needs-budget-review")


def test_a_terminal_mixed_hold_restores_only_its_durable_source_role():
    add, remove = actions.pr_unblock_change(
        ("ai-blocked", "needs-human-review"), "ai-review", "review"
    )
    assert add == ("ai-review",)
    assert remove == ("ai-blocked", "needs-human-review")


def test_a_custom_trigger_without_hold_provenance_is_refused():
    with pytest.raises(ValueError, match="source is unknown"):
        actions.pr_unblock_change(("needs-human-review",), "ai-review")


def _active_values(tmp_path, **values):
    active = tmp_path / "config" / ".active.demo.env"
    active.parent.mkdir(exist_ok=True)
    with active.open("a") as snapshot:
        snapshot.writelines(f"{key}={shlex.quote(value)}\n" for key, value in values.items())


def _unblock_backend(
    monkeypatch,
    tmp_path,
    prs=(12,),
    issue_labels=("needs-human-review",),
    pr_labels=(),
    gh_ok=True,
    applied=(True, "ok"),
    review_bot="reviewer[bot]",
    pr_state="OPEN",
    hold_role="",
    composite_hold=False,
    prior_unblock=False,
    applied_by_target=None,
    effective_by_target=None,
):
    """A Backend whose role state dirs are real, with a journalled row
    that links issue 7 to `prs` and a GitHub reader that answers with
    `issue_labels`/`pr_labels`."""
    from issue_agent.app import cli
    from issue_agent.domain import dashboard as dash

    dirs = {}
    for role in dash.ROLE_NAMES:
        state = tmp_path / role
        state.mkdir()
        dirs[role] = state
    monkeypatch.setattr(
        cli,
        "journal_paths",
        lambda _name: [str(dirs[role] / "journal.jsonl") for role in dash.ROLE_NAMES],
    )

    calls = []

    class Effects:
        def set_issue_labels_effect(self, issue, **kwargs):
            from issue_agent.adapters.operator_effects import LabelEffect

            calls.append((issue, kwargs))
            ok, detail = (applied_by_target or {}).get(issue, applied)
            if not ok:
                return LabelEffect(False, detail)
            current = set(issue_labels if issue == 7 else pr_labels)
            requested_add = tuple(kwargs.get("add", ()))
            suppressors = set(kwargs.get("add_unless_present", ()))
            resolved_add = (
                ()
                if requested_add and suppressors.intersection(current)
                else tuple(label for label in requested_add if label not in current)
            )
            resolved_remove = tuple(
                label for label in kwargs.get("remove", ()) if label in current
            )
            effective_add, effective_remove = (effective_by_target or {}).get(
                issue, (resolved_add, resolved_remove)
            )
            return LabelEffect(True, detail, effective_add, effective_remove)

    def factory(repo, state_dirs, *, identity, journal=None, run=None, control=None):
        calls.append(("factory", journal is not None))
        return Effects()

    class Gh:
        def issues(self, _repo):
            if not gh_ok:
                return ([], "gh unavailable")
            return ([{
                "number": 7,
                "state": "OPEN",
                "labels": [{"name": name} for name in issue_labels],
            }], "")

        def prs(self, _repo):
            return ([{
                "number": pr,
                "state": pr_state,
                "labels": [{"name": name} for name in pr_labels],
            } for pr in prs], "")

    monkeypatch.setattr(service.system, "remote_repo", lambda _root: "harness/repo")
    from issue_agent.app import config
    from issue_agent.adapters import control_path
    from issue_agent.adapters.control_store import SQLiteControlStore

    authority_root = str(tmp_path / "authority")
    monkeypatch.setattr(control_path, "machine_control_authority_root", lambda: authority_root)
    monkeypatch.setattr(config, "machine_control_authority_root", lambda: authority_root)
    target = tmp_path / "checkout"
    target.mkdir()
    _active_values(
        tmp_path, REPO="o/r", TARGET_DIR=str(target),
        STATE_DIR=str(dirs["fix"]), REVIEW_STATE_DIR=str(dirs["review"]),
        CONTROL_AUTHORITY_ROOT=authority_root,
        FIX_BOT="fixer[bot]", REVIEW_BOT=review_bot,
        GH_HOST=os.environ.get("GH_HOST", "github.com"),
        ROLES="fix review",
    )
    control = SQLiteControlStore(control_path.canonical_control_store_path(authority_root=authority_root))
    control.initialize()
    backend = service.Backend(harness_root=str(tmp_path), gh=Gh(), effects_factory=factory,
                              control_db_path=control.path)
    monkeypatch.setattr(backend, "known", lambda _name: True)
    monkeypatch.setattr(backend, "overlay_field", lambda _name, _field: "o/r")

    role_events = {role: [] for role in dash.ROLE_NAMES}
    role_events["fix"] = [
        {"ts": "2026-07-29T09:00:00+00:00", "event": "agent_run", "role": "fix",
         "kind": "FIX_CI", "issue": 7, "number": pr, "cost_usd": 0.0}
        for pr in prs
    ]
    if hold_role:
        role_events[hold_role].extend(
            {
                "ts": "2026-07-29T09:01:00+00:00",
                "event": "action",
                "role": hold_role,
                "pr": pr,
                "desc": (
                    f"relabel pr #{pr}: -ai-pr-open +needs-human-review"
                ),
                "action": (
                    {
                        "type": "escalate_ci_pending",
                        "target": "PR",
                        "number": pr,
                        "pr": pr,
                        "head": "abc1234",
                    }
                    if composite_hold
                    else {
                        "type": "relabel",
                        "target": "PR",
                        "number": pr,
                        "remove": "ai-pr-open",
                        "add": "needs-human-review",
                    }
                ),
            }
            for pr in prs
        )
    if prior_unblock:
        role_events["fix"].extend(
            {
                "ts": "2026-07-29T09:02:00+00:00",
                "event": "action",
                "role": "dashboard",
                "pr": pr,
                "desc": f"relabel pr #{pr}: -needs-human-review",
                "action": {
                    "type": "operator_unblock",
                    "target": "PR",
                    "number": pr,
                    "add": [],
                    "remove": ["needs-human-review"],
                    "labels_applied": True,
                },
            }
            for pr in prs
        )
    feeds = tuple(
        (role, "/j", "/l", role_events[role]) for role in dash.ROLE_NAMES
    )
    deployment = dash.build_deployment(
        "demo", "o/r", feeds, (),
        now_fn=lambda: datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        backend,
        "_deployment_snapshot",
        lambda *_args, **_kwargs: dash.Snapshot(
            "demo", "o/r", "/t", deployment, ()
        ),
    )
    return backend, dirs, calls


def test_unblock_drops_the_holding_labels_and_names_the_operator_action(
    monkeypatch, tmp_path
):
    backend, dirs, calls = _unblock_backend(monkeypatch, tmp_path)

    assert backend.unblock("demo", 7) == (True, "ok")

    issue, kwargs = next(call for call in calls if isinstance(call[0], int))
    assert issue == 7
    assert kwargs["add"] == ("ai-ready",)
    assert kwargs["add_unless_present"] == actions.SELECTABLE_ISSUE_LABELS
    assert set(kwargs["remove"]) == set(actions.UNBLOCK_LABELS)


@dataclass(frozen=True)
class _Journalled:
    kind: str
    number: int | None
    target: str
    display: str
    attributes: dict


def _journalled(dirs):
    return [
        _Journalled(
            kind=line["action"]["type"],
            number=line["action"].get("number"),
            target=str(line["action"].get("target", "")).lower(),
            display=line["desc"],
            attributes=line["action"],
        )
        for line in read_events(str(dirs["fix"] / "journal.jsonl"))
        if line.get("event") == "action"
    ]


def test_an_unblock_journals_one_event_naming_what_it_actually_reset(
    monkeypatch, tmp_path
):
    backend, dirs, _calls = _unblock_backend(monkeypatch, tmp_path)
    (dirs["review"] / "issue-7.review-retries").write_text("at 5")

    backend.unblock("demo", 7)

    (payload,) = _journalled(dirs)
    assert payload.kind == "operator_unblock" and payload.number == 7
    assert payload.attributes["remove"] == ["needs-human-review"]
    assert payload.attributes["retries_cleared"] == 0
    assert (dirs["review"] / "issue-7.review-retries").read_text() == "at 5"
    assert payload.attributes["labels_applied"] is True


def test_unblock_journals_the_resolved_fallback_not_the_stale_request(
    monkeypatch, tmp_path
):
    backend, dirs, _calls = _unblock_backend(
        monkeypatch,
        tmp_path,
        issue_labels=("needs-human-review", "ai-working"),
    )

    assert backend.unblock("demo", 7) == (True, "ok")

    (payload,) = _journalled(dirs)
    assert payload.attributes["add"] == []
    assert payload.attributes["requested_add"] == ["ai-ready"]
    assert payload.attributes["remove"] == ["needs-human-review"]
    assert "+ai-ready" not in payload.display


def test_a_counter_reset_is_journalled_even_when_github_changed_nothing(
    monkeypatch, tmp_path
):
    """The hold lives on the PR (keifu #173/#177): the PR's own labels must be
    removed — an unblock that edits only the issue returns ok while the
    reviewer stays withheld — and the breaker reset still journals."""
    backend, dirs, calls = _unblock_backend(
        monkeypatch, tmp_path, issue_labels=(), pr_labels=("needs-human-review",),
        hold_role="fix",
    )

    assert backend.unblock("demo", 7)[0] is True

    label_edits = [c for c in calls if c[0] != "factory"]
    assert label_edits == [
        (12, {"add": ("ai-pr-open",), "remove": ("needs-human-review",)})
    ]
    issue_event, pr_event = _journalled(dirs)
    assert issue_event.attributes["retries_cleared"] == 0
    assert pr_event.target == "pr"
    assert pr_event.attributes["add"] == ["ai-pr-open"]
    assert pr_event.attributes["remove"] == ["needs-human-review"]
    assert "pr #12" in pr_event.display


@pytest.mark.parametrize("trigger", ["ai-pr-open", "ai-review"])
def test_pr_review_hold_is_refused_with_the_way_to_release_it(monkeypatch, tmp_path, trigger):
    backend, dirs, calls = _unblock_backend(
        monkeypatch, tmp_path, issue_labels=(), pr_labels=("needs-human-review",),
        hold_role="review",
    )
    _active_values(tmp_path, REVIEW_TRIGGER_LABEL=trigger)
    counter = dirs["fix"] / "issue-7.ci-retries"
    counter.write_text("3")
    evidence = dirs["review"] / "pr-12.last-sha"
    evidence.write_text("a" * 40)
    ok, detail = backend.unblock("demo", 7)
    assert not ok and "review_hold_unsupported" in detail and "remove needs-human-review on GitHub" in detail
    assert counter.read_text() == "3" and evidence.read_text() == "a" * 40
    assert calls == [] and _journalled(dirs) == []


def test_custom_trigger_restores_fixer_source_for_a_fixer_escalation(
    monkeypatch, tmp_path
):
    backend, _dirs, calls = _unblock_backend(
        monkeypatch, tmp_path, issue_labels=(), pr_labels=("needs-human-review",),
        hold_role="fix",
    )
    _active_values(tmp_path, REVIEW_TRIGGER_LABEL="ai-review")

    assert backend.unblock("demo", 7) == (True, "ok")

    label_edits = [c for c in calls if c[0] != "factory"]
    assert label_edits == [
        (12, {"add": ("ai-pr-open",), "remove": ("needs-human-review",)})
    ]


def test_custom_trigger_restores_fixer_after_stalled_ci_escalation(
    monkeypatch, tmp_path
):
    backend, _dirs, calls = _unblock_backend(
        monkeypatch, tmp_path, issue_labels=(), pr_labels=("needs-human-review",),
        hold_role="fix", composite_hold=True,
    )
    _active_values(tmp_path, REVIEW_TRIGGER_LABEL="ai-review")

    assert backend.unblock("demo", 7) == (True, "ok")

    label_edits = [c for c in calls if c[0] != "factory"]
    assert label_edits == [
        (12, {"add": ("ai-pr-open",), "remove": ("needs-human-review",)})
    ]


@pytest.mark.parametrize("trigger", ["ai-pr-open", "ai-review"])
def test_unknown_hold_owner_refuses_before_resetting_counters(
    monkeypatch, tmp_path, trigger
):
    backend, dirs, calls = _unblock_backend(
        monkeypatch, tmp_path, issue_labels=(), pr_labels=("needs-human-review",)
    )
    _active_values(tmp_path, REVIEW_TRIGGER_LABEL=trigger, REVIEW_BOT="")
    counter = dirs["review"] / "issue--12.infra-deaths"
    counter.write_text("5")

    ok, detail = backend.unblock("demo", 7)

    assert ok is False and "source is unknown" in detail
    assert counter.read_text() == "5"
    assert calls == [] and _journalled(dirs) == []


def test_a_prior_hold_episode_cannot_supply_provenance_to_a_new_hold(
    monkeypatch, tmp_path
):
    backend, dirs, calls = _unblock_backend(
        monkeypatch,
        tmp_path,
        issue_labels=(),
        pr_labels=("needs-human-review",),
        hold_role="review",
        prior_unblock=True,
    )
    _active_values(tmp_path, REVIEW_TRIGGER_LABEL="ai-review", REVIEW_BOT="")
    counter = dirs["review"] / "issue--12.infra-deaths"
    counter.write_text("5")

    ok, detail = backend.unblock("demo", 7)

    assert ok is False and "source is unknown" in detail
    assert counter.read_text() == "5"
    assert calls == [] and _journalled(dirs) == []


# ── guidance parse rules (one definition, two enforcement points) ─────────────
def test_parse_guidance_distinguishes_absent_from_cleared_from_set():
    assert guidance_rules.parse_guidance(None) is None      # leave stored alone
    assert guidance_rules.parse_guidance("   ") == ""       # explicit retraction
    assert guidance_rules.parse_guidance("  scope  ") == "scope"


@pytest.mark.parametrize(
    "raw",
    [
        "x" * (guidance_rules.GUIDANCE_MAX_CHARS + 1),
        "a <<< b",
        "\ud800",
        {"not": "a string"},
        42,
    ],
)
def test_parse_guidance_refuses_what_must_never_reach_a_prompt(raw):
    with pytest.raises(guidance_rules.GuidanceError):
        guidance_rules.parse_guidance(raw)


def test_parse_guidance_accepts_the_ceiling_exactly():
    note = "x" * guidance_rules.GUIDANCE_MAX_CHARS
    assert guidance_rules.parse_guidance(note) == note


def test_a_refused_unblock_journals_nothing_and_resets_nothing(
    monkeypatch, tmp_path
):
    backend, dirs, calls = _unblock_backend(monkeypatch, tmp_path, issue_labels=("P1",))
    (dirs["fix"] / "issue-7.ci-retries").write_text("3")

    ok, detail = backend.unblock("demo", 7)

    assert ok is False and "nothing to unblock" in detail
    assert (dirs["fix"] / "issue-7.ci-retries").read_text() == "3"
    assert calls == [] and _journalled(dirs) == []


def test_an_unreadable_github_refuses_rather_than_guessing_not_held(
    monkeypatch, tmp_path
):
    backend, dirs, calls = _unblock_backend(monkeypatch, tmp_path, gh_ok=False)
    (dirs["fix"] / "issue-7.ci-retries").write_text("3")

    ok, detail = backend.unblock("demo", 7)

    assert ok is False and "gh unavailable" in detail
    assert (dirs["fix"] / "issue-7.ci-retries").read_text() == "3"
    assert calls == []


def test_a_failed_label_edit_still_records_the_reset_it_already_made(
    monkeypatch, tmp_path
):
    backend, dirs, _calls = _unblock_backend(
        monkeypatch, tmp_path, applied=(False, "network down"),
    )

    assert backend.unblock("demo", 7) == (False, "issue #7: network down")

    (payload,) = _journalled(dirs)
    assert payload.attributes["labels_applied"] is False
    assert payload.attributes["add"] == []
    assert payload.attributes["remove"] == []
    assert payload.attributes["requested_add"] == ["ai-ready"]
    assert payload.attributes["requested_remove"] == ["needs-human-review"]
    assert payload.attributes["retries_cleared"] == 0


def test_each_target_journals_its_own_label_result(monkeypatch, tmp_path):
    backend, dirs, _calls = _unblock_backend(
        monkeypatch,
        tmp_path,
        prs=(12, 13),
        issue_labels=(),
        pr_labels=("needs-human-review",),
        hold_role="fix",
        applied_by_target={
            12: (False, "first PR failed"),
            13: (True, "ok"),
        },
    )

    ok, detail = backend.unblock("demo", 7)

    assert ok is False and detail == "pr #12: first PR failed"
    issue_event, failed, succeeded = _journalled(dirs)
    assert issue_event.attributes["labels_applied"] is True
    assert failed.number == 12 and failed.attributes["labels_applied"] is False
    assert failed.attributes["add"] == [] and failed.attributes["remove"] == []
    assert failed.attributes["requested_add"] == ["ai-pr-open"]
    assert failed.attributes["requested_remove"] == ["needs-human-review"]
    assert succeeded.number == 13 and succeeded.attributes["labels_applied"] is True
    assert succeeded.attributes["add"] == ["ai-pr-open"]
    assert succeeded.attributes["remove"] == ["needs-human-review"]


def test_legacy_unblock_refuses_unsupported_grant_and_preserves_all_evidence(
    monkeypatch, tmp_path
):
    backend, dirs, _calls = _unblock_backend(monkeypatch, tmp_path)
    (dirs["fix"] / "issue-7.ci-retries").write_text("3")
    (dirs["fix"] / "issue-7.rebase-retries").write_text("2")
    (dirs["fix"] / "issue-7.resolve-retries").write_text("4")
    (dirs["review"] / "issue-7.review-retries").write_text("at 5")
    (dirs["review"] / "issue-7.infra-deaths").write_text("5")
    (dirs["review"] / "issue--12.infra-deaths").write_text("5")
    (dirs["fix"] / "issue-7.infra-deaths").write_text("5")

    ok, detail = backend.unblock("demo", 7)
    assert not ok and "migration required" in detail
    assert _calls == [] and _journalled(dirs) == []

    assert (dirs["fix"] / "issue-7.ci-retries").read_text() == "3"
    assert (dirs["fix"] / "issue-7.rebase-retries").read_text() == "2"
    assert (dirs["review"] / "issue-7.review-retries").read_text() == "at 5"
    assert (dirs["review"] / "issue-7.infra-deaths").read_text() == "5"
    assert (dirs["review"] / "issue--12.infra-deaths").read_text() == "5"
    # ADR 0015: the resolve allowance is SQLite authority renewed only by a
    # typed re-arm; the retired file is offline import evidence and survives.
    assert (dirs["fix"] / "issue-7.resolve-retries").read_text() == "4"
    assert (dirs["fix"] / "issue-7.infra-deaths").read_text() == "5"


def test_an_unblock_that_reset_nothing_does_not_claim_it_did(monkeypatch, tmp_path):
    backend, dirs, _calls = _unblock_backend(monkeypatch, tmp_path)

    backend.unblock("demo", 7)

    (payload,) = _journalled(dirs)
    assert payload.attributes["retries_cleared"] == 0


def test_unblock_on_an_issue_with_no_pr_clears_what_it_can(monkeypatch, tmp_path):
    backend, dirs, _calls = _unblock_backend(monkeypatch, tmp_path, prs=())

    assert backend.unblock("demo", 7) == (True, "ok")
    assert not (dirs["fix"] / "issue-7.ci-retries").exists()


def test_unblock_of_an_unknown_deployment_never_reaches_github(monkeypatch, tmp_path):
    backend, _dirs, calls = _unblock_backend(monkeypatch, tmp_path)
    monkeypatch.setattr(backend, "known", lambda _name: False)
    ok, detail = backend.unblock("nope", 7)
    assert ok is False and "unknown" in detail and calls == []


# ── unblock is a re-arm (keifu PR #173 / issue #25, 2026-08-02) ───────────────


@pytest.mark.parametrize("key,suffix,payload", [(7, "ci-retries", "broken"), (-12, "rebase-retries", "2"),
                                                 (7, "review-retries", "at 3")])
def test_unsupported_legacy_recovery_grant_refuses_before_labels_or_comment(monkeypatch, tmp_path, key, suffix, payload):
    backend, dirs, calls = _unblock_backend(monkeypatch, tmp_path)
    source = dirs["fix"] / f"issue-{key}.{suffix}"
    source.write_text(payload)
    ok, detail = backend.unblock("demo", 7)
    assert not ok and "migration required" in detail
    assert calls == [] and _journalled(dirs) == []
    assert source.read_text() == payload


def test_advisory_ci_context_alone_does_not_require_an_operator_grant(monkeypatch, tmp_path):
    backend, dirs, calls = _unblock_backend(monkeypatch, tmp_path)
    history = dirs["fix"] / "issue-7.ci-history"
    history.write_text("[]")
    assert backend.unblock("demo", 7) == (True, "ok")
    assert calls and history.read_text() == "[]"
    assert _journalled(dirs)[0].attributes["retries_cleared"] == 0
