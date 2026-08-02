"""The dashboard's POST actions — asserted as the intent they build, never
executed.

An action test that shelled out would either need a live repo or a mock deep
enough to prove nothing. What matters is the command: `start` must go through
`make point-at` (not a reimplemented launch), and a priority swap must be ONE
edit so a half-applied change cannot leave an issue with no priority at all.
"""

from datetime import datetime, timezone

import pytest

from issue_agent.adapters.dashweb import actions, costchart
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
            "/state/steward/journal.jsonl",
        ],
    )

    assert backend.promote("demo", 9) == (True, "ok")
    assert backend.priority("demo", 9, "P2") == (True, "ok")

    assert factory_calls == [
        (
            "o/r",
            ("/state/fix", "/state/review", "/state/steward"),
            "o/r:dashboard",
        ),
        (
            "o/r",
            ("/state/fix", "/state/review", "/state/steward"),
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
def test_unblock_removes_every_label_that_holds_an_issue_and_adds_none():
    add, remove = actions.unblock_change()
    assert add == ()
    assert set(remove) == {"needs-human-review", "needs-budget-review", "ai-blocked"}


def _unblock_backend(
    monkeypatch,
    tmp_path,
    prs=(12,),
    issue_labels=("needs-human-review",),
    pr_labels=(),
    gh_ok=True,
    applied=(True, "ok"),
    reviews=(),
    reviews_error=None,
    dismissed=(True, "ok"),
    review_bot="reviewer[bot]",
    pr_state="OPEN",
):
    """A Backend whose three role state dirs are real, with a journalled row
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
        def set_issue_labels(self, issue, **kwargs):
            calls.append((issue, kwargs))
            return applied

        def pr_reviews(self, pr):
            if reviews_error is not None:
                raise RuntimeError(reviews_error)
            return tuple(reviews)

        def dismiss_review(self, pr, review, message):
            calls.append(("dismiss", pr, review.review_id, message))
            return dismissed

    def factory(repo, state_dirs, *, identity, journal=None):
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
    monkeypatch.setattr(
        cli,
        "deployment_env_field",
        lambda _name, field: review_bot if field == "REVIEW_BOT" else "",
    )
    backend = service.Backend(gh=Gh(), effects_factory=factory)
    monkeypatch.setattr(backend, "known", lambda _name: True)
    monkeypatch.setattr(backend, "overlay_field", lambda _name, _field: "o/r")

    events = [
        {"ts": "2026-07-29T09:00:00+00:00", "event": "agent_run", "role": "fix",
         "kind": "FIX_CI", "issue": 7, "number": pr, "cost_usd": 0.0}
        for pr in prs
    ]
    feeds = (("fix", "/j", "/l", events), ("review", "/j", "/l", []),
             ("steward", "/j", "/l", []))
    deployment = dash.build_deployment(
        "demo", "o/r", feeds, (),
        now_fn=lambda: datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        backend,
        "snapshot",
        lambda _name: dash.Snapshot("demo", "o/r", "/t", deployment, ()),
    )
    return backend, dirs, calls


def test_unblock_drops_the_holding_labels_and_names_the_operator_action(
    monkeypatch, tmp_path
):
    backend, dirs, calls = _unblock_backend(monkeypatch, tmp_path)
    (dirs["fix"] / "issue-7.ci-retries").write_text("3")

    assert backend.unblock("demo", 7) == (True, "ok")

    issue, kwargs = calls[1]
    assert issue == 7
    assert kwargs["add"] == ()
    assert set(kwargs["remove"]) == set(actions.UNBLOCK_LABELS)


def _journalled(dirs):
    from issue_agent.adapters.event_log import read_event_log

    return [
        event.payload
        for event in read_event_log(str(dirs["fix"] / "events-v2.jsonl")).events
    ]


def test_an_unblock_journals_one_event_naming_what_it_actually_reset(
    monkeypatch, tmp_path
):
    backend, dirs, _calls = _unblock_backend(monkeypatch, tmp_path)
    (dirs["fix"] / "issue-7.ci-retries").write_text("3")
    (dirs["review"] / "issue-7.review-retries").write_text("at 5")

    backend.unblock("demo", 7)

    (payload,) = _journalled(dirs)
    assert payload.kind == "operator_unblock" and payload.number == 7
    assert payload.attributes["remove"] == ["needs-human-review"]
    assert payload.attributes["retries_cleared"] == 2
    assert payload.attributes["labels_applied"] is True


def test_a_counter_reset_is_journalled_even_when_github_changed_nothing(
    monkeypatch, tmp_path
):
    """The hold lives on the PR (keifu #173/#177): the PR's own labels must be
    removed — an unblock that edits only the issue returns ok while the
    reviewer stays withheld — and the breaker reset still journals."""
    backend, dirs, calls = _unblock_backend(
        monkeypatch, tmp_path, issue_labels=(), pr_labels=("needs-human-review",),
    )
    (dirs["fix"] / "issue-7.ci-retries").write_text("3")

    assert backend.unblock("demo", 7)[0] is True

    label_edits = [c for c in calls if c[0] != "factory"]
    assert label_edits == [
        (12, {"add": (), "remove": ("needs-human-review",)})
    ]
    issue_event, pr_event = _journalled(dirs)
    assert issue_event.attributes["retries_cleared"] == 1
    assert pr_event.target == "pr"
    assert pr_event.attributes["remove"] == ["needs-human-review"]
    assert "pr #12" in pr_event.display


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
    (dirs["fix"] / "issue-7.ci-retries").write_text("3")

    assert backend.unblock("demo", 7) == (False, "network down")

    (payload,) = _journalled(dirs)
    assert payload.attributes["labels_applied"] is False
    assert payload.attributes["retries_cleared"] == 1


def test_unblock_hands_back_the_retry_budget_in_every_role_state_dir(
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

    backend.unblock("demo", 7)

    assert not (dirs["fix"] / "issue-7.ci-retries").exists()
    assert not (dirs["fix"] / "issue-7.rebase-retries").exists()
    assert not (dirs["review"] / "issue-7.review-retries").exists()
    # The review role's dead-session ceiling is what an unblock answers; the
    # resolve ceiling and the fix role's death tally are not.
    assert not (dirs["review"] / "issue-7.infra-deaths").exists()
    assert not (dirs["review"] / "issue--12.infra-deaths").exists()
    assert (dirs["fix"] / "issue-7.resolve-retries").read_text() == "4"
    assert (dirs["fix"] / "issue-7.infra-deaths").read_text() == "5"


def test_an_unblock_that_reset_nothing_does_not_claim_it_did(monkeypatch, tmp_path):
    backend, dirs, _calls = _unblock_backend(monkeypatch, tmp_path)

    backend.unblock("demo", 7)

    (payload,) = _journalled(dirs)
    assert payload.attributes["retries_cleared"] == 0


def test_unblock_on_an_issue_with_no_pr_clears_what_it_can(monkeypatch, tmp_path):
    backend, dirs, _calls = _unblock_backend(monkeypatch, tmp_path, prs=())
    (dirs["fix"] / "issue-7.ci-retries").write_text("3")

    assert backend.unblock("demo", 7) == (True, "ok")
    assert not (dirs["fix"] / "issue-7.ci-retries").exists()


def test_the_operator_journal_is_opened_once_per_deployment(monkeypatch, tmp_path):
    """Opening an EventLog re-reads the whole feed under the writer lock the
    daemons append through; one per request would stall a live fix loop."""
    backend, _dirs, _calls = _unblock_backend(monkeypatch, tmp_path)
    first = backend._operator_journal("demo", "o/r")
    backend.unblock("demo", 7)
    assert backend._operator_journal("demo", "o/r") is first


def test_unblock_of_an_unknown_deployment_never_reaches_github(monkeypatch, tmp_path):
    backend, _dirs, calls = _unblock_backend(monkeypatch, tmp_path)
    monkeypatch.setattr(backend, "known", lambda _name: False)
    ok, detail = backend.unblock("nope", 7)
    assert ok is False and "unknown" in detail and calls == []


# ── unblock is a re-arm (keifu PR #173 / issue #25, 2026-08-02) ───────────────
def _rejection(review_id, state="CHANGES_REQUESTED", author="reviewer[bot]"):
    from issue_agent.domain.review import SubmittedReview

    return SubmittedReview(review_id, state, author, f"2026-08-02T0{review_id}:00:00Z")


def _at_ceiling(*ids):
    """A review history the ceiling (4) is holding: three rejections stand, so
    the next one can only escalate."""
    return tuple(_rejection(i) for i in ("1", "2", *ids))


def test_unblock_dismisses_the_change_requests_that_still_arm_the_ceiling(
    monkeypatch, tmp_path
):
    """The ceiling is recomputed from GitHub review state, so clearing labels
    and counters left it tripped: the very next round was at the ceiling again
    with no attention label to edge-trigger an escalation."""
    backend, dirs, calls = _unblock_backend(
        monkeypatch,
        tmp_path,
        reviews=(
            _rejection("1"),
            _rejection("2", state="APPROVED"),
            _rejection("3"),
            _rejection("4"),
            _rejection("5"),
            _rejection("6", author="a-human"),
            _rejection("7", state="COMMENTED"),
        ),
    )

    assert backend.unblock("demo", 7) == (True, "ok")

    dismissals = [c for c in calls if c[0] == "dismiss"]
    # Only the bot's OWN change requests SINCE its last approve: the pre-approve
    # rejection was satisfied, the human's is not ours to retract, and the
    # COMMENT is the escalation itself.
    assert [(c[1], c[2]) for c in dismissals] == [(12, "3"), (12, "4"), (12, "5")]
    assert "re-arm" in dismissals[0][3]


def test_a_live_change_request_below_the_ceiling_is_never_retracted(
    monkeypatch, tmp_path
):
    """An outstanding rejection is the FIXER's next instruction — `domain.fix`
    dispatches ADDRESS_REVIEW off exactly that state and reads a dismissed
    review as nothing to do. Retracting one because a rebase or CI breaker held
    the same issue would destroy the feedback and wedge the PR for good."""
    backend, dirs, calls = _unblock_backend(
        monkeypatch, tmp_path, reviews=(_rejection("1"),),
    )

    assert backend.unblock("demo", 7) == (True, "ok")

    assert [c for c in calls if c[0] == "dismiss"] == []
    assert [p.kind for p in _journalled(dirs)] == ["operator_unblock"]


def test_a_disabled_rejection_ceiling_has_nothing_to_re_arm(monkeypatch, tmp_path):
    backend, _dirs, calls = _unblock_backend(
        monkeypatch,
        tmp_path,
        reviews=tuple(_rejection(str(n)) for n in range(1, 6)),
    )
    monkeypatch.setattr(
        service.cli,
        "deployment_env_field",
        lambda _name, field: "reviewer[bot]" if field == "REVIEW_BOT" else "0",
    )

    assert backend.unblock("demo", 7) == (True, "ok")
    assert [c for c in calls if c[0] == "dismiss"] == []


def test_each_dismissal_is_journalled_against_the_issue_and_the_pr(
    monkeypatch, tmp_path
):
    backend, dirs, _calls = _unblock_backend(
        monkeypatch, tmp_path, reviews=_at_ceiling("3"),
    )

    backend.unblock("demo", 7)

    _one, _two, dismissal, unblock = _journalled(dirs)
    assert dismissal.kind == "operator_dismiss_review"
    assert dismissal.target == "pr" and dismissal.number == 12
    assert dismissal.attributes["review_id"] == "3"
    assert dismissal.attributes["dismissed"] is True
    assert dismissal.attributes["operator"] == "o/r:dashboard"
    assert unblock.kind == "operator_unblock"


def test_the_dismissal_fan_out_is_bounded_by_the_configured_ceiling(
    monkeypatch, tmp_path
):
    """One operator click may not turn a pathological review history into an
    unbounded run of GitHub mutations (ADR 0012)."""
    backend, _dirs, calls = _unblock_backend(
        monkeypatch,
        tmp_path,
        reviews=tuple(_rejection(str(n)) for n in range(1, 6)),
    )
    monkeypatch.setattr(
        service.cli,
        "deployment_env_field",
        lambda _name, field: "reviewer[bot]" if field == "REVIEW_BOT" else "2",
    )

    backend.unblock("demo", 7)

    assert [c[2] for c in calls if c[0] == "dismiss"] == ["4", "5"]


def test_a_healthy_issue_with_no_change_requests_dismisses_nothing(
    monkeypatch, tmp_path
):
    backend, dirs, calls = _unblock_backend(
        monkeypatch, tmp_path, reviews=(_rejection("1", state="APPROVED"),),
    )

    backend.unblock("demo", 7)

    assert [c for c in calls if c[0] == "dismiss"] == []
    (payload,) = _journalled(dirs)
    assert payload.kind == "operator_unblock"


def test_an_issue_with_no_pr_has_nothing_to_re_arm(monkeypatch, tmp_path):
    backend, dirs, calls = _unblock_backend(
        monkeypatch, tmp_path, prs=(), reviews=(_rejection("3"),),
    )

    assert backend.unblock("demo", 7) == (True, "ok")
    assert [c for c in calls if c[0] == "dismiss"] == []
    assert [p.kind for p in _journalled(dirs)] == ["operator_unblock"]


def test_a_closed_pr_is_not_re_armed(monkeypatch, tmp_path):
    """A merged/closed PR has no review loop left to unwedge, and GitHub
    refuses the dismissal anyway."""
    backend, dirs, calls = _unblock_backend(
        monkeypatch,
        tmp_path,
        pr_state="MERGED",
        reviews=_at_ceiling("3"),
    )

    assert backend.unblock("demo", 7) == (True, "ok")
    assert [c for c in calls if c[0] == "dismiss"] == []


def test_unreadable_reviews_are_journalled_and_never_wedge_the_unblock(
    monkeypatch, tmp_path
):
    backend, dirs, calls = _unblock_backend(
        monkeypatch, tmp_path, reviews_error="gh: 503",
    )
    (dirs["fix"] / "issue-7.ci-retries").write_text("3")

    ok, detail = backend.unblock("demo", 7)

    # The operator is told the re-arm did not happen, not just "ok"…
    assert ok is True and "gh: 503" in detail
    # …the labels and counters still cleared…
    assert not (dirs["fix"] / "issue-7.ci-retries").exists()
    assert [c for c in calls if c[0] != "factory" and c[0] != "dismiss"]
    # …and the operator can see the ceiling was NOT re-armed.
    failure, unblock = _journalled(dirs)
    assert failure.kind == "operator_dismiss_review"
    assert failure.attributes["dismissed"] is False
    assert "gh: 503" in failure.attributes["detail"]
    assert unblock.kind == "operator_unblock"


def test_a_failed_dismissal_is_journalled_and_the_labels_still_clear(
    monkeypatch, tmp_path
):
    backend, dirs, calls = _unblock_backend(
        monkeypatch,
        tmp_path,
        reviews=_at_ceiling("3"),
        dismissed=(False, "OperatorEffectError: 403 forbidden"),
    )
    (dirs["fix"] / "issue-7.ci-retries").write_text("3")

    ok, detail = backend.unblock("demo", 7)

    assert ok is True and "403" in detail
    assert not (dirs["fix"] / "issue-7.ci-retries").exists()
    assert [c[2] for c in calls if c[0] == "dismiss"] == ["1", "2", "3"]
    failure = _journalled(dirs)[0]
    assert failure.attributes["dismissed"] is False
    assert "403" in failure.attributes["detail"]


def test_an_unconfigured_review_bot_says_so_rather_than_dismissing_blindly(
    monkeypatch, tmp_path
):
    """Whose change requests count is the review bot's login; without it the
    only safe set is the empty one — and an operator must hear that the ceiling
    is still armed."""
    backend, dirs, calls = _unblock_backend(
        monkeypatch, tmp_path, review_bot="", reviews=_at_ceiling("3"),
    )

    ok, detail = backend.unblock("demo", 7)

    assert ok is True and "REVIEW_BOT" in detail
    assert [c for c in calls if c[0] == "dismiss"] == []
    failure, _unblock = _journalled(dirs)
    assert failure.attributes["dismissed"] is False
    assert "REVIEW_BOT" in failure.attributes["detail"]
