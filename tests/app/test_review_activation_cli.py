"""The review CLI uses real activation and the shared old-writer fence."""

import json
from pathlib import Path

import pytest

from issue_agent.app import cli, review_activation
from tests.app.test_resolve_activation_cli import deployment, dump, heartbeat, write_snapshot
from tests.app.test_review_activation_app import BOT, Remote, seed, settle


@pytest.fixture
def review_launch(deployment, tmp_path, monkeypatch):
    _fix, control, env = deployment
    write_snapshot(tmp_path, {**env, "REVIEW_BOT": BOT})
    state = Path(env["REVIEW_STATE_DIR"])
    state.mkdir()
    seed(state)
    remote = Remote()
    monkeypatch.setattr(review_activation, "GitHubGateway", lambda *_a, **_kw: remote)
    return state, control, env, remote


def run(capsys, *argv, family="review"):
    args = cli.build_parser().parse_args([f"{family}-activation", *argv, "--name", "target"])
    code = args.func(args)
    out = capsys.readouterr()
    return code, json.loads(out.out) if out.out.strip() else None, out.err


def test_cli_imports_both_families_and_exact_replay_does_not_repeat_activation(review_launch, capsys):
    state, control, _env, remote = review_launch
    before = dump(control)
    assert run(capsys, "inventory")[1]["status"] == "ready"
    code, shadow, _ = run(capsys, "shadow")
    assert code == 0 and dump(control) == before
    digest = shadow["plan"]["plan_digest"]
    assert run(capsys, "activate")[0] == 2
    assert run(capsys, "activate", "--confirm-plan-digest", "0" * 64)[0] == 2
    assert dump(control) == before

    resolve_digest = run(capsys, "shadow", family="resolve")[1]["plan"]["plan_digest"]
    assert run(capsys, "activate", "--confirm-plan-digest", resolve_digest, family="resolve")[0] == 0
    digest = run(capsys, "shadow")[1]["plan"]["plan_digest"]
    code, result, error = run(capsys, "activate", "--confirm-plan-digest", digest)
    assert code == 0 and result["status"] == "activated", error
    committed = dump(control)
    assert run(capsys, "activate", "--confirm-plan-digest", digest)[1]["status"] == "already_active_with_this_plan"
    assert dump(control) == committed
    assert (state / "pr-7.operator-guidance").read_text() == "Keep the API stable"
    assert all("--method" not in args for args in remote.calls)


@pytest.mark.parametrize("live", ["role", "launcher", "heartbeat", "unreadable", "another-deployment"])
def test_live_writer_evidence_refuses_review_activation_without_mutation(review_launch, capsys, monkeypatch, tmp_path, live):
    _state, control, env, _remote = review_launch
    digest = run(capsys, "shadow")[1]["plan"]["plan_digest"]
    if live == "role":
        monkeypatch.setattr(cli, "_role_pids", lambda _name: {"review": [123]})
    elif live == "launcher":
        monkeypatch.setattr(cli, "_launcher_pids", lambda _name: [123])
    elif live in {"heartbeat", "unreadable"}:
        heartbeat(Path(env["REVIEW_STATE_DIR"]), raw="{broken" if live == "unreadable" else None)
    else:
        other = tmp_path / "other" / "review"
        (tmp_path / "config" / "repos").mkdir()
        (tmp_path / "config" / "repos" / "other.env").write_text(f"REPO='O/R'\nREVIEW_STATE_DIR='{other}'\n")
        heartbeat(other)
    before = dump(control)
    code, _result, error = run(capsys, "activate", "--confirm-plan-digest", digest)
    assert code == 2 and "watchers may be running" in error
    assert dump(control) == before


def test_review_cli_rehearsal_writes_only_the_requested_copy(review_launch, capsys, tmp_path):
    _state, control, _env, _remote = review_launch
    before = dump(control)
    assert run(capsys, "rehearse")[0] == 2
    settle(control)
    code, result, error = run(capsys, "rehearse", "--copy-to", str(tmp_path / "rehearsal"))
    assert code == 0 and result["status"] == "rehearsed", error
    assert dump(control) == before


def test_review_cli_includes_extra_legacy_roots(review_launch, capsys, tmp_path):
    _state, control, _env, _remote = review_launch
    extra = tmp_path / "old-review"
    extra.mkdir()
    (extra / "pr-7.last-sha").write_text("b" * 40)
    before = dump(control)
    code, result, _ = run(capsys, "shadow", "--legacy-state-dir", str(extra))
    assert code == 0 and result["status"] == "blocked"
    assert any("multiple roots" in reason or "duplicate" in reason for reason in result["blockers"])
    assert dump(control) == before


def test_compatible_old_marker_rehearses_and_activates_without_dropping_sources(review_launch, capsys, tmp_path):
    state, control, _env, _remote = review_launch
    settle(control)
    extra = tmp_path / 'compatible-old-review'
    extra.mkdir()
    (extra / 'pr-7.last-sha').write_bytes((state / 'pr-7.last-sha').read_bytes())
    before = dump(control)
    files = {str(f): f.read_bytes() for root in (state, extra) for f in root.iterdir() if f.is_file()}
    flags = ('--legacy-state-dir', str(extra))
    code, report, error = run(capsys, 'shadow', *flags)
    assert code == 0 and report['status'] == 'ready', error
    assert len(report['legacy_roots']) == 3
    code, rehearsal, error = run(capsys, 'rehearse', *flags, '--copy-to', str(tmp_path / 'compatible-copy'))
    assert code == 0 and all(rehearsal['checks'].values()), error
    assert dump(control) == before
    assert {name: Path(name).read_bytes() for name in files} == files
    code, accepted, error = run(capsys, 'activate', *flags, '--confirm-plan-digest', report['plan']['plan_digest'])
    assert code == 0 and accepted['status'] == 'activated', error
    assert {name: Path(name).read_bytes() for name in files} == files
