"""The public config command works without a target checkout or deployment anchor."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from issue_agent.app import cli
from issue_agent.app.config import from_env
from issue_agent.app.live_config import LiveConfig
from issue_agent.adapters import config_files


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def config_root(tmp_path, monkeypatch):
    root = tmp_path / "harness"
    (root / "config").mkdir(parents=True)
    (root / "bin").mkdir()
    for script in ("compose-env.sh", "point-at.sh"):
        shutil.copy(ROOT / "bin" / script, root / "bin" / script)
    shared = root / "config" / "shared.env"
    shared.write_text("FIXER_RUNTIME=codex\nREVIEWER_RUNTIME=codex\n")
    shared.chmod(0o600)
    monkeypatch.setattr(cli, "HARNESS_ROOT", str(root))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(cli.GitHubGateway, "open_issue_count", lambda self: 0)
    return root


def command(*arguments):
    args = cli.build_parser().parse_args(["config", *arguments])
    return args.func(args)


def deploy(root, name, *, overlay=""):
    repos = root / "config" / "repos"
    repos.mkdir(exist_ok=True)
    state = root.parent / f"state-{name}"
    (repos / f"{name}.env").write_text(
        f"REPO=owner/{name}\nFIX_BOT=fixer\nREVIEW_BOT=reviewer\nTARGET_DIR={root.parent}\n"
        f"STATE_DIR={state}\n{overlay}")
    subprocess.run(["bash", str(root / "bin" / "compose-env.sh"), name],
                   env=dict(os.environ), check=True, capture_output=True)
    active = root / "config" / f".active.{name}.env"
    snapshot = Path(os.environ["HOME"]) / ".config" / "issue-agent" / f"{name}.env"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(active, snapshot)
    return active, snapshot


def test_shared_get_set_unset_need_no_repository_or_configured_deployment(config_root, monkeypatch, capsys):
    monkeypatch.setenv("FIX_MODEL_MED", "codex:ambient:low")
    foreign = config_root.parent / "foreign.env"
    foreign.write_text("FIX_MODEL_MED=codex:foreign:high\n")
    monkeypatch.setenv("AGENT_ENV", str(foreign))

    assert command("set", "--shared", "FIX_MODEL_MED", "codex:gpt-6.1-sol:medium") == 0
    capsys.readouterr()
    assert command("get", "--shared", "FIX_MODEL_MED") == 0
    assert capsys.readouterr().out.strip() == "codex:gpt-6.1-sol:medium"
    assert command("unset", "--shared", "FIX_MODEL_MED") == 0
    capsys.readouterr()
    assert command("get", "--shared", "FIX_MODEL_MED") == 0
    assert capsys.readouterr().out == "\n"
    assert not (config_root / "config" / "repos").exists()
    assert not list((config_root / "config").glob(".active.*"))
    assert (config_root / "config" / "shared.env").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("key,value", [
    ("FIX_MODEL_MED", "gemini:pro"),
    ("FIX_MODEL_MED", "codex:gpt-6.1-sol:sideways"),
    ("FIX_DISPATCH_CONCURRENCY", "0"),
    ("FIX_DISPATCH_CONCURRENCY", "9999999999999999999999999"),
    ("DAILY_SPEND_CAP_USD", "inf"),
    ("DAILY_SPEND_CAP_USD", "-1"),
    ("ISSUE_PICKUP", "everything"),
    ("FIX_MODEL", "x" * 1001),
    ("FIX_MODEL", "x\nREPO=foreign/repo"),
    ("FIX_MODEL", "x\0y"),
])
def test_invalid_shared_defaults_are_rejected_without_a_deployment(config_root, key, value):
    shared = config_root / "config" / "shared.env"
    before = shared.read_bytes()
    assert command("set", "--shared", key, value) == 1
    assert shared.read_bytes() == before


@pytest.mark.parametrize("arguments", [
    ("set", "FIX_MODEL_MED", "codex:m:medium"),
    ("set", "--shared", "--name", "demo", "FIX_MODEL_MED", "codex:m:medium"),
    ("set", "--shared", "FIX_BOT_TOKEN", "secret"),
    ("set", "--name", "../escape", "FIX_MODEL_MED", "codex:m:medium"),
    ("unset", "--shared", "FIX_MODEL_MED", "extra"),
])
def test_scope_and_live_key_contract_are_explicit(config_root, arguments):
    with pytest.raises(SystemExit) as error:
        command(*arguments)
    assert error.value.code == 2


def test_global_save_publishes_eligible_deployments_and_holds_pending_ones(config_root, capsys):
    active, snapshot = deploy(config_root, "ready")
    pending, pending_snapshot = deploy(config_root, "pending")
    overlay = config_root / "config" / "repos" / "pending.env"
    overlay.write_text(overlay.read_text() + "POLL_SECONDS=5\n")
    before = pending.read_bytes(), pending_snapshot.read_bytes()
    live = LiveConfig(from_env({"AGENT_ENV": str(snapshot)}), str(snapshot))

    assert command("set", "--shared", "FIX_MODEL_MED", "codex:gpt-6.1-sol:medium") == 0

    assert live.refresh().fix_model_med == "codex:gpt-6.1-sol:medium"
    assert active.read_bytes() == snapshot.read_bytes()
    assert (pending.read_bytes(), pending_snapshot.read_bytes()) == before
    assert "pending" in capsys.readouterr().err


def test_repo_override_can_be_unset_to_inherit_shared_defaults(config_root, capsys):
    assert command("set", "--shared", "FIX_MODEL_MED", "codex:shared:medium") == 0
    active, snapshot = deploy(config_root, "demo", overlay="FIX_MODEL_MED=codex:override:high\n")
    capsys.readouterr()
    assert command("get", "--name", "demo", "FIX_MODEL_MED") == 0
    assert capsys.readouterr().out.strip() == "codex:override:high"
    assert command("unset", "--name", "demo", "FIX_MODEL_MED") == 0
    assert from_env({"AGENT_ENV": str(snapshot)}).fix_model_med == "codex:shared:medium"
    assert active.read_bytes() == snapshot.read_bytes()


def test_invalid_global_value_cannot_hide_behind_a_repository_override(config_root):
    active, snapshot = deploy(config_root, "demo", overlay="FIX_MODEL_MED=codex:valid:medium\n")
    before = active.read_bytes(), snapshot.read_bytes()
    assert command("set", "--shared", "FIX_MODEL_MED", "gemini:pro") == 1
    assert (active.read_bytes(), snapshot.read_bytes()) == before


def test_global_open_pickup_fails_closed_when_issue_count_is_unavailable(config_root, monkeypatch):
    active, snapshot = deploy(config_root, "demo")
    shared = config_root / "config" / "shared.env"
    before = shared.read_bytes(), active.read_bytes(), snapshot.read_bytes()
    def unavailable(self):
        raise cli.GhUnavailable("network", "unavailable")

    monkeypatch.setattr(cli.GitHubGateway, "open_issue_count", unavailable)
    assert command("set", "--shared", "ISSUE_PICKUP", "open") == 1
    assert (shared.read_bytes(), active.read_bytes(), snapshot.read_bytes()) == before


@pytest.mark.parametrize("count,accepted", [(0, True), (999, True), (1000, False), (10**30, False)])
def test_global_open_pickup_enforces_the_observation_ceiling(config_root, monkeypatch, count, accepted):
    active, snapshot = deploy(config_root, "demo")
    shared = config_root / "config" / "shared.env"
    before = shared.read_bytes(), active.read_bytes(), snapshot.read_bytes()
    monkeypatch.setattr(cli.GitHubGateway, "open_issue_count", lambda self: count)

    assert command("set", "--shared", "ISSUE_PICKUP", "open") == (0 if accepted else 1)
    if accepted:
        assert from_env({"AGENT_ENV": str(snapshot)}).pickup.value == "open"
    else:
        assert (shared.read_bytes(), active.read_bytes(), snapshot.read_bytes()) == before


def test_shared_validation_does_not_weaken_deployment_launch_validation():
    cfg = from_env({"FIX_MODEL_MED": "codex:gpt-6.1-sol:medium"})
    assert not cfg.settings_errors()
    assert any("REPO must be set" in error for error in cfg.validation_errors())
    assert any("FIX_BOT must be set" in error for error in cfg.validation_errors())


def test_a_journal_error_reports_the_setting_as_applied(config_root, monkeypatch, capsys):
    active, snapshot = deploy(config_root, "demo")

    def unreadable_journal(*args):
        raise OSError("journal unavailable")

    monkeypatch.setattr(config_files, "journal_change", unreadable_journal)
    assert command("set", "--shared", "FIX_MODEL_MED", "codex:gpt-6.1-sol:medium") == 0
    assert "setting applied" in capsys.readouterr().err
    assert from_env({"AGENT_ENV": str(snapshot)}).fix_model_med == "codex:gpt-6.1-sol:medium"
    assert active.read_bytes() == snapshot.read_bytes()
