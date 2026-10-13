"""The dashboard's POST actions — asserted as the intent they build, never
executed.

An action test that shelled out would either need a live repo or a mock deep
enough to prove nothing. What matters is the command: `start` must go through
`make point-at` (not a reimplemented launch), and park/unblock move only
`needs-human` on GitHub, read fresh (refactor decision Q19).
"""


import json
from types import SimpleNamespace

import pytest

from issue_agent.adapters.dashweb import actions, costchart
from issue_agent.adapters.operator_effects import GitHubOperatorEffects
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



# ── promote / park / unblock: label moves only ───────────────────────────────
class _GitHub:
    """GitHub behind the real operator effects: label state keyed by
    (kind, number), one issue (#7) and `prs` [(number, head branch)] open."""

    def __init__(self, labels, prs, issue_state, read_error):
        self.labels = {key: set(value) for key, value in labels.items()}
        self.prs = prs
        self.issue_state = issue_state
        self.read_error = read_error
        self.refuse = set()
        self.writes = []
        self.events = []

    # GitHubGateway's label surface.
    def has_label(self, kind, number, label):
        return label in self.labels.setdefault((kind, number), set())

    def relabel(self, kind, number, remove, add):
        if (kind, number) in self.refuse:
            raise OSError("gh refused")
        self.writes.append((kind, number, remove, add))
        current = self.labels.setdefault((kind, number), set())
        current.discard(remove)
        if add is not None:
            current.add(add)

    # The `gh` reads the effects make before a hold move.
    def run(self, argv, **_kwargs):
        if self.read_error:
            return SimpleNamespace(returncode=1, stdout="", stderr=self.read_error)
        if argv[:3] == ["gh", "issue", "view"]:
            payload = {"number": int(argv[3]), "state": self.issue_state}
        elif argv[:3] == ["gh", "pr", "list"]:
            payload = [{"number": n, "headRefName": head} for n, head in self.prs]
        else:
            raise AssertionError(f"unexpected command: {argv}")
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    def emit(self, event):
        self.events.append(event.payload)

    def effects(self, repo, *, identity, journal=None):
        assert repo == "o/r" and identity == "o/r:dashboard"
        return GitHubOperatorEffects(repo, self, identity=identity, run=self.run, journal=self)


def _label_backend(monkeypatch, labels, *, prs=(), issue_state="OPEN", read_error=""):
    github = _GitHub(labels, prs, issue_state, read_error)
    monkeypatch.setattr(service.system, "remote_repo", lambda _root: "harness/repo")
    backend = service.Backend(effects_factory=github.effects)
    monkeypatch.setattr(backend, "known", lambda name: name == "demo")
    monkeypatch.setattr(backend, "overlay_field", lambda _name, field: {"REPO": "o/r"}[field])
    monkeypatch.setattr(service.cli, "journal_paths", lambda _name: ["/state/fix/journal.jsonl"])
    return backend, github


def test_promote_puts_the_issue_on_ai_ready(monkeypatch):
    backend, github = _label_backend(monkeypatch, {("issue", 7): set()})

    assert backend.promote("demo", 7) == (True, "ok")

    assert github.labels[("issue", 7)] == {"ai-ready"}


def test_park_adds_needs_human_to_the_issue_and_its_open_harness_prs(monkeypatch):
    backend, github = _label_backend(
        monkeypatch,
        {("issue", 7): {"ai-ready"}, ("pr", 12): set(), ("pr", 13): set()},
        prs=((12, "fix/7-a"), (13, "feature/7-x"), (15, "fix/8-other")),
    )

    assert backend.park("demo", 7, "ui") == (True, "ok")

    assert github.labels[("issue", 7)] == {"ai-ready", "needs-human"}
    assert github.labels[("pr", 12)] == {"needs-human"}
    # Not a harness branch, or another issue's PR: untouched.
    assert github.labels[("pr", 13)] == set()
    assert ("pr", 15) not in github.labels
    assert {event.kind for event in github.events} == {"operator_park"}


def test_unblock_removes_only_needs_human(monkeypatch):
    backend, github = _label_backend(
        monkeypatch,
        {("issue", 7): {"needs-human", "bug"}, ("pr", 12): {"needs-human", "keep"}},
        prs=((12, "fix/7-a"),),
    )

    assert backend.unblock("demo", 7, "api") == (True, "ok")

    assert github.labels[("issue", 7)] == {"bug"}
    assert github.labels[("pr", 12)] == {"keep"}
    assert {event.kind for event in github.events} == {"operator_unblock"}


def test_park_then_unblock_restores_the_original_labels(monkeypatch):
    backend, github = _label_backend(monkeypatch, {("issue", 7): {"ai-ready"}})

    backend.park("demo", 7)
    backend.unblock("demo", 7)

    assert github.labels[("issue", 7)] == {"ai-ready"}


def test_repeating_park_moves_no_label_twice(monkeypatch):
    backend, github = _label_backend(monkeypatch, {("issue", 7): set()}, prs=((12, "fix/7-a"),))

    backend.park("demo", 7)
    writes = list(github.writes)
    assert backend.park("demo", 7) == (True, "ok")

    assert github.writes == writes


def test_a_refused_write_is_reported_per_target(monkeypatch):
    backend, github = _label_backend(
        monkeypatch, {("issue", 7): set(), ("pr", 12): set()}, prs=((12, "fix/7-a"),),
    )
    github.refuse.add(("pr", 12))

    ok, detail = backend.park("demo", 7)

    assert ok is False and detail.startswith("pr #12:") and "gh refused" in detail
    assert github.labels[("issue", 7)] == {"needs-human"}


@pytest.mark.parametrize("kwargs, expected", [
    ({"issue_state": "CLOSED"}, "issue #7 is not open on o/r"),
    ({"read_error": "gh unavailable"}, "could not read #7"),
])
def test_park_and_unblock_refuse_without_a_write_on_a_closed_or_unreadable_issue(monkeypatch, kwargs, expected):
    backend, github = _label_backend(monkeypatch, {("issue", 7): {"needs-human"}}, **kwargs)

    for action in (backend.park, backend.unblock):
        ok, detail = action("demo", 7)
        assert ok is False and detail.startswith(expected)
    assert github.writes == []


def test_label_actions_on_an_unknown_deployment_never_reach_github(monkeypatch):
    backend, github = _label_backend(monkeypatch, {("issue", 7): set()})

    for action in (backend.park, backend.unblock):
        ok, detail = action("other", 7)
        assert ok is False and "unknown deployment" in detail
    assert github.writes == []
