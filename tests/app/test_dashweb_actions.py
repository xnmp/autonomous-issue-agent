"""The dashboard's POST actions — asserted as the intent they build, never
executed.

An action test that shelled out would either need a live repo or a mock deep
enough to prove nothing. What matters is the command: `start` must go through
`make point-at` (not a reimplemented launch), a priority swap must be ONE
edit so a half-applied change cannot leave an issue with no priority at all,
and park/unblock are label moves on GitHub and nothing else (refactor
decision Q19).
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



# ── promote / park / unblock: label moves only ───────────────────────────────
class _Labels:
    """GitHub's label state for the backend's effects, keyed by (kind, number)."""

    def __init__(self, labels):
        self.labels = {key: set(value) for key, value in labels.items()}
        self.refuse = set()
        self.moves = []

    def effects(self, repo, *, identity, journal=None):
        assert repo == "o/r" and identity == "o/r:dashboard"
        return self

    def set_labels(self, kind, number, *, add=(), remove=(), action_kind="", action_attributes=None):
        from issue_agent.adapters.operator_effects import LabelEffect

        if (kind, number) in self.refuse:
            return LabelEffect(False, "gh refused")
        current = self.labels.setdefault((kind, number), set())
        added = tuple(label for label in add if label not in current)
        removed = tuple(label for label in remove if label in current and label not in add)
        current.update(added)
        current.difference_update(removed)
        if added or removed:
            self.moves.append((action_kind, kind, number, added, removed))
        return LabelEffect(True, "ok", added, removed)

    def set_issue_labels(self, issue, *, add=(), remove=()):
        effect = self.set_labels("issue", issue, add=add, remove=remove, action_kind="operator_relabel")
        return effect.applied, effect.detail


def _label_backend(monkeypatch, labels, *, prs=(), issue_open=True, gh_error=""):
    """A Backend over one issue (#7) and `prs` [(number, head branch, state)]."""
    store = _Labels(labels)

    class Gh:
        def issues(self, _repo, limit=1000):
            rows = [{"number": 7, "state": "OPEN"}] if issue_open else []
            return (None, gh_error) if gh_error else (rows, "")

        def prs(self, _repo, limit=200):
            return ([{"number": n, "headRefName": head, "state": state} for n, head, state in prs], "")

    monkeypatch.setattr(service.system, "remote_repo", lambda _root: "harness/repo")
    backend = service.Backend(gh=Gh(), effects_factory=store.effects)
    monkeypatch.setattr(backend, "known", lambda name: name == "demo")
    monkeypatch.setattr(backend, "overlay_field", lambda _name, field: {"REPO": "o/r"}[field])
    monkeypatch.setattr(service.cli, "journal_paths", lambda _name: ["/state/fix/journal.jsonl"])
    return backend, store


def test_promote_puts_the_issue_on_ai_ready(monkeypatch):
    backend, store = _label_backend(monkeypatch, {("issue", 7): set()})

    assert backend.promote("demo", 7) == (True, "ok")

    assert store.labels[("issue", 7)] == {"ai-ready"}


def test_park_adds_needs_human_to_the_issue_and_its_open_harness_prs(monkeypatch):
    backend, store = _label_backend(
        monkeypatch,
        {("issue", 7): {"ai-ready"}, ("pr", 12): set(), ("pr", 13): set(), ("pr", 14): set()},
        prs=((12, "fix/7-a", "OPEN"), (13, "feature/7-x", "OPEN"), (14, "fix/7-old", "MERGED"),
             (15, "fix/8-other", "OPEN")),
    )

    assert backend.park("demo", 7, "ui") == (True, "ok")

    assert store.labels[("issue", 7)] == {"ai-ready", "needs-human"}
    assert store.labels[("pr", 12)] == {"needs-human"}
    # Not a harness branch, not open, or another issue's PR: untouched.
    assert store.labels[("pr", 13)] == set() and store.labels[("pr", 14)] == set()
    assert ("pr", 15) not in store.labels
    assert {move[0] for move in store.moves} == {"operator_park"}


def test_unblock_removes_needs_human_everywhere_and_rearms_the_issue(monkeypatch):
    backend, store = _label_backend(
        monkeypatch,
        {("issue", 7): {"needs-human"}, ("pr", 12): {"needs-human", "keep"}},
        prs=((12, "fix/7-a", "OPEN"),),
    )

    assert backend.unblock("demo", 7, "api") == (True, "ok")

    assert store.labels[("issue", 7)] == {"ai-ready"}
    assert store.labels[("pr", 12)] == {"keep"}
    assert {move[0] for move in store.moves} == {"operator_unblock"}


def test_park_then_unblock_round_trips_to_a_selectable_issue(monkeypatch):
    backend, store = _label_backend(monkeypatch, {("issue", 7): {"ai-ready"}})

    backend.park("demo", 7)
    backend.unblock("demo", 7)

    assert store.labels[("issue", 7)] == {"ai-ready"}


def test_repeating_park_moves_no_label_twice(monkeypatch):
    backend, store = _label_backend(monkeypatch, {("issue", 7): set()}, prs=((12, "fix/7-a", "OPEN"),))

    backend.park("demo", 7)
    moves = list(store.moves)
    assert backend.park("demo", 7) == (True, "ok")

    assert store.moves == moves


def test_a_refused_write_is_reported_per_target(monkeypatch):
    backend, store = _label_backend(
        monkeypatch, {("issue", 7): set(), ("pr", 12): set()}, prs=((12, "fix/7-a", "OPEN"),),
    )
    store.refuse.add(("pr", 12))

    ok, detail = backend.park("demo", 7)

    assert ok is False and detail == "pr #12: gh refused"
    assert store.labels[("issue", 7)] == {"needs-human"}


@pytest.mark.parametrize("kwargs, expected", [
    ({"issue_open": False}, "issue #7 is not open on o/r"),
    ({"gh_error": "gh unavailable"}, "gh unavailable"),
])
def test_park_and_unblock_refuse_without_a_write_when_the_issue_cannot_be_read(monkeypatch, kwargs, expected):
    backend, store = _label_backend(monkeypatch, {("issue", 7): set()}, **kwargs)

    assert backend.park("demo", 7) == (False, expected)
    assert backend.unblock("demo", 7) == (False, expected)
    assert store.moves == []


def test_label_actions_on_an_unknown_deployment_never_reach_github(monkeypatch):
    backend, store = _label_backend(monkeypatch, {("issue", 7): set()})

    for action in (backend.park, backend.unblock):
        ok, detail = action("other", 7)
        assert ok is False and "unknown deployment" in detail
    assert store.moves == []
