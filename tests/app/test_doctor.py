"""Offline doctor checks: credentials per role, the codex binary the daemons
exec, and the installed services' real environment (the 2026-08-10 #650
class), never the operator's shell."""

import os
import plistlib

import pytest

from issue_agent.app.doctor import codex_findings, env_assignments, identity_findings, parse_unit, service_findings

ROLES = ("fix-watcher", "review-watcher")
_WHICH_ALL = lambda binary, path=None: "/usr/bin/" + binary  # noqa: E731
_WHICH_CORE = lambda binary, path=None: (  # noqa: E731
    "/usr/bin/" + binary if binary in ("git", "gh") else None
)


def _unit(path="/usr/bin:/bin", agent_env="/cfg/name.env", workdir=None):
    lines = ["[Service]", "ExecStart=/abs/uv run x"]
    if workdir is not None:
        lines.append(f"WorkingDirectory={workdir}")
    if path is not None:
        lines.append(f"Environment=PATH={path}")
    if agent_env is not None:
        lines.append(f"Environment=AGENT_ENV={agent_env}")
    return "\n".join(lines) + "\n"


def _install(tmp_path, name="demo", unit_text=None, snapshot="A=1\nB=2\n", roles=ROLES):
    unit_dir = tmp_path / "systemd"
    unit_dir.mkdir(exist_ok=True)
    cfg = tmp_path / "installed.env"
    cfg.write_text(snapshot)
    text = unit_text if unit_text is not None else _unit(agent_env=str(cfg))
    for role in roles:
        (unit_dir / f"issue-agent-{role}-{name}.service").write_text(text)
    active = tmp_path / "active.env"
    active.write_text(snapshot)
    return str(unit_dir), str(active), str(cfg)


def _services(tmp_path, unit_dir, active, which=_WHICH_ALL, name="demo"):
    return service_findings(name, active, systemd_dir=unit_dir, launchd_dir=str(tmp_path / "no-launchd"), which=which)


def _findings(tmp_path, composed=None, which=_WHICH_ALL, **install_kwargs):
    unit_dir, active, _ = _install(tmp_path, **install_kwargs)
    return _services(tmp_path, unit_dir, active, which)


def _unit_file(tmp_path, text, name="u.service"):
    path = tmp_path / name
    path.write_text(text) if isinstance(text, str) else path.write_bytes(text)
    return str(path)


# ── parsing ───────────────────────────────────────────────────────────────────
def test_parses_path_agent_env_and_workdir_from_a_systemd_unit(tmp_path):
    env, working = parse_unit(_unit_file(tmp_path, _unit(path="/a:/b", agent_env="/cfg/x.env", workdir="/srv/harness")))
    assert (env["PATH"], env["AGENT_ENV"], working) == ("/a:/b", "/cfg/x.env", "/srv/harness")


def test_systemd_parser_handles_quoted_multi_assignments_and_resets(tmp_path):
    text = (
        "[Service]\n"
        'Environment="PATH=/a:/b" "OTHER=x"\n'
        "Environment=\n"  # documented reset
        "Environment=PATH=/c AGENT_ENV=/cfg/y.env\n"
    )
    env, _ = parse_unit(_unit_file(tmp_path, text))
    assert env == {"PATH": "/c", "AGENT_ENV": "/cfg/y.env"}


def test_parses_a_launchd_plist(tmp_path):
    payload = plistlib.dumps(
        {
            "Label": "issue-agent-fix",
            "WorkingDirectory": "/srv/harness",
            "EnvironmentVariables": {"PATH": "/a:/b", "AGENT_ENV": "/cfg/x.env"},
        }
    )
    env, working = parse_unit(_unit_file(tmp_path, payload, "x.plist"))
    assert (env["PATH"], env["AGENT_ENV"], working) == ("/a:/b", "/cfg/x.env", "/srv/harness")


def test_a_valid_plist_with_a_non_dict_root_degrades_to_a_warning(tmp_path):
    unit_dir = tmp_path / "launchd"
    unit_dir.mkdir()
    (unit_dir / "issue-agent-fix-watcher.demo.plist").write_bytes(plistlib.dumps(["not", "a", "dict"]))
    findings = service_findings(
        "demo", str(tmp_path / "active.env"), systemd_dir=str(tmp_path / "none"), launchd_dir=str(unit_dir)
    )
    assert any(f.level == "warn" and "unreadable" in f.message for f in findings)


def test_env_assignments_tolerate_export_comments_and_malformed_lines():
    parsed = env_assignments("# c\nexport A=1\nB=2\nnot-an-assignment\n\nC=va=lue\n")
    assert parsed == {"A": "1", "B": "2", "C": "va=lue"}


def test_everything_resolving_and_matching_yields_no_findings(tmp_path):
    assert _findings(tmp_path, composed={"FIXER_RUNTIME": "codex"}, snapshot="FIXER_RUNTIME=codex\n") == ()


def test_a_partial_role_pair_is_an_error(tmp_path):
    findings = _findings(tmp_path, roles=("fix-watcher",))
    assert any(f.level == "error" and "review-watcher" in f.message for f in findings)


def test_a_unit_without_baked_path_is_the_650_class_error(tmp_path):
    cfg = tmp_path / "installed.env"
    cfg.write_text("A=1\nB=2\n")
    findings = _findings(tmp_path, unit_text=_unit(path=None, agent_env=str(cfg)))
    errors = [f for f in findings if "minimal PATH" in f.message]
    assert len(errors) == len(ROLES)
    assert all(f.level == "error" for f in errors)
    assert "install-service" in errors[0].message


def test_a_unit_without_agent_env_is_an_error(tmp_path):
    findings = _findings(tmp_path, unit_text=_unit(agent_env=None))
    assert any(f.level == "error" and "no AGENT_ENV" in f.message for f in findings)


def test_required_binaries_come_from_the_installed_snapshot_not_the_composed(tmp_path):
    # The operator switched the overlay to claude, but the running services
    # still read the installed snapshot selecting codex — validate codex.
    findings = _findings(
        tmp_path, composed={"FIXER_RUNTIME": "claude"}, snapshot="FIXER_RUNTIME=codex\n", which=_WHICH_CORE
    )
    assert any(f.level == "error" and "'codex'" in f.message and "dispatch" in f.message for f in findings)


def test_a_transient_or_missing_working_directory_is_an_error(tmp_path):
    cfg = tmp_path / "installed.env"
    cfg.write_text("A=1\n")
    transient = _findings(
        tmp_path, unit_text=_unit(agent_env=str(cfg), workdir="/home/u/repo/.claude/worktrees/agent-x")
    )
    assert any(f.level == "error" and "transient or missing checkout" in f.message for f in transient)


def test_snapshot_drift_between_installed_and_composed_warns_once_with_remedy(tmp_path):
    unit_dir, active, cfg = _install(tmp_path)
    with open(active, "w") as stream:
        stream.write("A=1\nB=CHANGED\n")
    findings = _services(tmp_path, unit_dir, active)
    assert [f.level for f in findings] == ["warn"]  # once, not per unit
    assert "B" in findings[0].message
    assert "install-service" in findings[0].message
    assert "CHANGED" not in findings[0].message


def test_a_missing_installed_snapshot_is_an_error_once(tmp_path):
    unit_dir, active, cfg = _install(tmp_path)
    os.unlink(cfg)
    findings = _services(tmp_path, unit_dir, active)
    assert [f.level for f in findings] == ["error"]
    assert "cannot boot" in findings[0].message


def test_units_for_other_deployments_are_ignored(tmp_path):
    unit_dir, active, _ = _install(tmp_path, name="other")
    assert _services(tmp_path, unit_dir, active) == ()


def test_a_huge_drift_list_is_bounded(tmp_path):
    unit_dir, active, _ = _install(tmp_path, snapshot="".join(f"K{i}=a\n" for i in range(20)))
    with open(active, "w") as stream:
        stream.write("".join(f"K{i}=b\n" for i in range(20)))
    findings = _services(tmp_path, unit_dir, active)
    assert len(findings) == 1
    assert "+15 more" in findings[0].message


def test_retired_tier_knobs_no_longer_require_codex(tmp_path):
    # Slice 5b deleted the per-tier model ladder (Q12): a leftover tier knob
    # selects no provider, so an all-claude deployment needs no codex binary.
    env = {
        "FIXER_RUNTIME": "claude",
        "REVIEWER_RUNTIME": "claude",
        "FIX_MODEL_LOW": "codex:gpt-6-luna",
        "FIX_MODEL_MED": "codex:gpt-6.1-sol",
    }
    snapshot = "\n".join(f'{key}="{value}"' for key, value in env.items()) + "\n"
    findings = _findings(tmp_path, composed=env, snapshot=snapshot, which=_WHICH_CORE)
    assert not any("'codex'" in f.message for f in findings)


# ── identity ──────────────────────────────────────────────────────────────────
def _levels(findings, needle):
    return {f.level for f in findings if needle in f.message}


@pytest.mark.parametrize("env", [{"FIX_BOT_TOKEN": "same", "REVIEW_BOT_TOKEN": "same"}, {}])
def test_fix_and_review_on_one_credential_is_an_error(env):
    assert _levels(identity_findings(env), "same credential") == {"error"}


@pytest.mark.parametrize("var", ["FIX_BOT_TOKEN", "REVIEW_BOT_TOKEN"])
def test_a_whitespace_token_is_an_error(var):
    env = {"FIX_BOT_TOKEN": "a", "REVIEW_BOT_TOKEN": "b", var: "  "}
    assert _levels(identity_findings(env), var) == {"error"}


def test_the_app_identity_needs_all_three_settings_and_a_readable_key(tmp_path):
    key = tmp_path / "app.pem"
    key.write_text("k")
    app = {"FIX_BOT_TOKEN": "a", "GH_APP_ID": "1", "GH_APP_INSTALL_ID": "2"}
    assert identity_findings({**app, "GH_APP_KEY_FILE": str(key)}) == ()
    assert _levels(identity_findings({**app, "GH_APP_KEY_FILE": str(tmp_path / "missing.pem")}), "not readable") == {
        "error"
    }
    assert _levels(
        identity_findings({"FIX_BOT_TOKEN": "a", "REVIEW_BOT_TOKEN": "b", "GH_APP_ID": "1"}), "set together"
    ) == {"error"}


def test_the_app_key_resolves_against_the_daemon_home(tmp_path):
    (tmp_path / "app.pem").write_text("k")
    env = {
        "FIX_BOT_TOKEN": "a",
        "GH_APP_ID": "1",
        "GH_APP_INSTALL_ID": "2",
        "GH_APP_KEY_FILE": "~/app.pem",
        "HOME": str(tmp_path),
    }
    assert identity_findings(env) == ()


def test_identity_findings_never_carry_token_material():
    findings = identity_findings({"FIX_BOT_TOKEN": "ghp_secret", "REVIEW_BOT_TOKEN": "ghp_secret"})
    assert findings and not any("ghp_secret" in f.message for f in findings)


# ── codex binary ──────────────────────────────────────────────────────────────
def test_codex_is_not_checked_when_no_role_runs_it():
    assert codex_findings({"FIXER_RUNTIME": "claude", "REVIEWER_RUNTIME": "claude"}, which=lambda _b: None) == ()


def test_a_bare_codex_bin_warns_with_the_pin_to_set():
    findings = codex_findings({"FIXER_RUNTIME": "codex"}, which=lambda _b: "/home/u/.local/bin/codex")
    assert [f.level for f in findings] == ["warn"]
    assert "CODEX_BIN=/home/u/.local/bin/codex" in findings[0].message


def test_an_unresolvable_codex_is_an_error(tmp_path):
    assert [f.level for f in codex_findings({"REVIEWER_RUNTIME": "codex"}, which=lambda _b: None)] == ["error"]
    pinned = {"CODEX_BIN": str(tmp_path / "missing-codex")}
    assert [f.level for f in codex_findings(pinned)] == ["error"]


def test_an_executable_pinned_codex_is_silent(tmp_path):
    codex = tmp_path / "codex"
    codex.write_text("#!/bin/sh\n")
    codex.chmod(0o755)
    assert codex_findings({"FIXER_RUNTIME": "codex", "CODEX_BIN": f"'{codex}'"}) == ()
