"""Installed-service doctor checks: the daemons' real environment, not the
operator's shell (the 2026-08-10 #650 class)."""

import os
import plistlib

from issue_agent.app.service_doctor import (
    env_file_assignments,
    installed_service_findings,
    parse_launchd_plist,
    parse_systemd_unit,
    snapshot_drift_keys,
)

ROLES = ("fix-watcher", "review-watcher", "steward")
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


def _install(tmp_path, name="demo", unit_text=None, snapshot="A=1\nB=2\n",
             roles=ROLES):
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


def _findings(tmp_path, composed=None, which=_WHICH_ALL, **install_kwargs):
    unit_dir, active, _ = _install(tmp_path, **install_kwargs)
    return installed_service_findings(
        "demo", composed or {}, active,
        systemd_dir=unit_dir, launchd_dir=None, which=which,
    )


# ── parsing ───────────────────────────────────────────────────────────────────
def test_parses_path_agent_env_and_workdir_from_a_systemd_unit():
    unit = parse_systemd_unit(_unit(path="/a:/b", agent_env="/cfg/x.env",
                                    workdir="/srv/harness"))
    assert unit.baked_path == "/a:/b"
    assert unit.agent_env == "/cfg/x.env"
    assert unit.working_dir == "/srv/harness"


def test_systemd_parser_handles_quoted_multi_assignments_and_resets():
    text = (
        "[Service]\n"
        'Environment="PATH=/a:/b" "OTHER=x"\n'
        "Environment=\n"                       # documented reset
        "Environment=PATH=/c AGENT_ENV=/cfg/y.env\n"
    )
    unit = parse_systemd_unit(text)
    assert unit.baked_path == "/c"
    assert unit.agent_env == "/cfg/y.env"


def test_parses_a_launchd_plist():
    payload = plistlib.dumps({
        "Label": "issue-agent-fix",
        "WorkingDirectory": "/srv/harness",
        "EnvironmentVariables": {"PATH": "/a:/b", "AGENT_ENV": "/cfg/x.env"},
    })
    unit = parse_launchd_plist(payload)
    assert unit.baked_path == "/a:/b"
    assert unit.agent_env == "/cfg/x.env"
    assert unit.working_dir == "/srv/harness"


def test_a_valid_plist_with_a_non_dict_root_degrades_to_a_warning(tmp_path):
    unit_dir = tmp_path / "launchd"
    unit_dir.mkdir()
    (unit_dir / "issue-agent-fix-watcher.demo.plist").write_bytes(
        plistlib.dumps(["not", "a", "dict"])
    )
    findings = installed_service_findings(
        "demo", {}, str(tmp_path / "active.env"),
        systemd_dir=str(tmp_path / "none"), launchd_dir=str(unit_dir),
    )
    assert any(
        f.level == "warn" and "unreadable" in f.message for f in findings
    )


def test_snapshot_drift_reports_key_names_never_values():
    keys = snapshot_drift_keys(
        "# header\nTOKEN=secret-old\nSAME=x\n",
        "TOKEN=secret-new\nSAME=x\nADDED=1\n",
    )
    assert keys == ("ADDED", "TOKEN")
    assert "secret-old" not in "".join(keys) and "secret-new" not in "".join(keys)


def test_env_assignments_tolerate_export_comments_and_malformed_lines():
    parsed = env_file_assignments(
        "# c\nexport A=1\nB=2\nnot-an-assignment\n\nC=va=lue\n"
    )
    assert parsed == {"A": "1", "B": "2", "C": "va=lue"}


# ── findings ──────────────────────────────────────────────────────────────────
def test_no_installed_units_is_silent(tmp_path):
    assert installed_service_findings(
        "demo", {}, str(tmp_path / "active.env"),
        systemd_dir=str(tmp_path / "none"), launchd_dir=str(tmp_path / "none2"),
    ) == ()


def test_everything_resolving_and_matching_yields_no_findings(tmp_path):
    assert _findings(
        tmp_path, composed={"FIXER_RUNTIME": "codex"},
        snapshot="FIXER_RUNTIME=codex\n",
    ) == ()


def test_a_partial_role_trio_is_an_error(tmp_path):
    findings = _findings(tmp_path, roles=("fix-watcher",))
    assert any(
        f.level == "error"
        and "review-watcher" in f.message and "steward" in f.message
        for f in findings
    )


def test_a_unit_without_baked_path_is_the_650_class_error(tmp_path):
    cfg = tmp_path / "installed.env"
    cfg.write_text("A=1\nB=2\n")
    findings = _findings(
        tmp_path, unit_text=_unit(path=None, agent_env=str(cfg))
    )
    errors = [f for f in findings if "minimal PATH" in f.message]
    assert len(errors) == len(ROLES)
    assert all(f.level == "error" for f in errors)
    assert "install-service" in errors[0].message


def test_a_unit_without_agent_env_is_an_error(tmp_path):
    findings = _findings(tmp_path, unit_text=_unit(agent_env=None))
    assert any(
        f.level == "error" and "no AGENT_ENV" in f.message for f in findings
    )


def test_required_binaries_come_from_the_installed_snapshot_not_the_composed(
    tmp_path,
):
    # The operator switched the overlay to claude, but the running services
    # still read the installed snapshot selecting codex — validate codex.
    findings = _findings(
        tmp_path,
        composed={"FIXER_RUNTIME": "claude"},
        snapshot="FIXER_RUNTIME=codex\n",
        which=_WHICH_CORE,
    )
    assert any(
        f.level == "error" and "'codex'" in f.message and "dispatch" in f.message
        for f in findings
    )


def test_an_unpinned_claude_runtime_warns_rather_than_errors(tmp_path):
    # The SDK can fall back to its bundled binary; absence from the service
    # PATH is suspicious, not fatal — unless CLAUDE_BIN pins it.
    unpinned = _findings(
        tmp_path, snapshot="FIXER_RUNTIME=claude\n", which=_WHICH_CORE,
    )
    pinned = _findings(
        tmp_path,
        snapshot="FIXER_RUNTIME=claude\nCLAUDE_BIN=/opt/claude\n",
        which=_WHICH_CORE,
    )
    assert {f.level for f in unpinned if "claude" in f.message} == {"warn"}
    assert {f.level for f in pinned if "claude" in f.message} == {"error"}


def test_a_transient_or_missing_working_directory_is_an_error(tmp_path):
    cfg = tmp_path / "installed.env"
    cfg.write_text("A=1\n")
    transient = _findings(
        tmp_path,
        unit_text=_unit(
            agent_env=str(cfg),
            workdir="/home/u/repo/.claude/worktrees/agent-x",
        ),
    )
    assert any(
        f.level == "error" and "transient or missing checkout" in f.message
        for f in transient
    )


def test_snapshot_drift_between_installed_and_composed_warns_once_with_remedy(
    tmp_path,
):
    unit_dir, active, cfg = _install(tmp_path)
    with open(active, "w") as stream:
        stream.write("A=1\nB=CHANGED\n")
    findings = installed_service_findings(
        "demo", {}, active,
        systemd_dir=unit_dir, launchd_dir=None, which=_WHICH_ALL,
    )
    assert [f.level for f in findings] == ["warn"]  # once, not per unit
    assert "B" in findings[0].message
    assert "install-service" in findings[0].message
    assert "CHANGED" not in findings[0].message


def test_a_missing_installed_snapshot_is_an_error_once(tmp_path):
    unit_dir, active, cfg = _install(tmp_path)
    os.unlink(cfg)
    findings = installed_service_findings(
        "demo", {}, active,
        systemd_dir=unit_dir, launchd_dir=None, which=_WHICH_ALL,
    )
    assert [f.level for f in findings] == ["error"]
    assert "cannot boot" in findings[0].message


def test_units_for_other_deployments_are_ignored(tmp_path):
    unit_dir, active, _ = _install(tmp_path, name="other")
    assert installed_service_findings(
        "demo", {}, active, systemd_dir=unit_dir, launchd_dir=None,
    ) == ()


def test_a_huge_drift_list_is_bounded(tmp_path):
    unit_dir, active, _ = _install(
        tmp_path, snapshot="".join(f"K{i}=a\n" for i in range(20))
    )
    with open(active, "w") as stream:
        stream.write("".join(f"K{i}=b\n" for i in range(20)))
    findings = installed_service_findings(
        "demo", {}, active,
        systemd_dir=unit_dir, launchd_dir=None, which=_WHICH_ALL,
    )
    assert len(findings) == 1
    assert "+15 more" in findings[0].message
