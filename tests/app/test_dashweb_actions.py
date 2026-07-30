"""The three POST actions — asserted as the argv they build, never executed.

An action test that shelled out would either need a live repo or a mock deep
enough to prove nothing. What matters is the command: `start` must go through
`make point-at` (not a reimplemented launch), and a priority swap must be ONE
edit so a half-applied change cannot leave an issue with no priority at all.
"""

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

    def factory(repo, state_dirs, *, identity):
        factory_calls.append((repo, state_dirs, identity))
        return Effects()

    monkeypatch.setattr(service.system, "remote_repo", lambda _root: "harness/repo")
    backend = service.Backend(effects_factory=factory)
    monkeypatch.setattr(backend, "known", lambda _name: True)
    values = {
        "REPO": "o/r",
        "STATE_DIR": "/state/fix",
        "REVIEW_STATE_DIR": "/state/review",
        "STEWARD_STATE_DIR": "/state/steward",
    }
    monkeypatch.setattr(
        backend,
        "overlay_field",
        lambda _name, field: values[field],
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
