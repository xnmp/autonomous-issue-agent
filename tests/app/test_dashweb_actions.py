"""The three POST actions — asserted as the argv they build, never executed.

An action test that shelled out would either need a live repo or a mock deep
enough to prove nothing. What matters is the command: `start` must go through
`make point-at` (not a reimplemented launch), and a priority swap must be ONE
edit so a half-applied change cannot leave an issue with no priority at all.
"""

import pytest

from issue_agent.adapters.dashweb import actions, costchart


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


# ── promote ───────────────────────────────────────────────────────────────────
def test_promote_adds_ai_ready_to_the_target_repos_issue():
    assert actions.promote_command("o/r", 9) == [
        "gh", "issue", "edit", "9", "-R", "o/r", "--add-label", "ai-ready"
    ]


# ── priority ──────────────────────────────────────────────────────────────────
def test_setting_a_priority_creates_the_label_then_swaps_in_one_edit():
    create, edit = actions.priority_commands("o/r", 9, "P2")
    assert create[:4] == ["gh", "label", "create", "P2"]
    assert edit[:6] == ["gh", "issue", "edit", "9", "-R", "o/r"]
    assert edit[6:] == ["--remove-label", "P1", "--remove-label", "P3", "--add-label", "P2"]


def test_clearing_a_priority_removes_all_three_and_creates_nothing():
    commands = actions.priority_commands("o/r", 9, None)
    assert len(commands) == 1
    assert commands[0][6:] == [
        "--remove-label", "P1", "--remove-label", "P2", "--remove-label", "P3"
    ]


def test_an_unknown_priority_is_rejected_before_any_command_is_built():
    with pytest.raises(ValueError):
        actions.priority_commands("o/r", 9, "P9")


def test_an_existing_label_error_is_not_fatal_but_the_edit_failing_is():
    calls = []

    def run(argv):
        calls.append(argv)
        return (1, "label already exists") if argv[1] == "label" else (0, "")

    ok, _detail = actions.run_label_commands(actions.priority_commands("o/r", 9, "P1"), run)
    assert ok and len(calls) == 2


def test_the_final_commands_failure_is_reported_to_the_operator():
    ok, detail = actions.run_label_commands([["gh", "issue", "edit"]],
                                            lambda argv: (1, "no such issue"))
    assert ok is False and detail == "no such issue"


def test_running_a_missing_binary_reports_it_rather_than_raising():
    code, message = actions.run_command(["/nonexistent/binary"])
    assert code == 127 and "installed" in message


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
