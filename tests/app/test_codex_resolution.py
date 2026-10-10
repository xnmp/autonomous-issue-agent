"""Which codex binary the DAEMONS will execute (2026-08-02 keifu incident).

The daemons moved to systemd user units, whose minimal PATH lacks ~/.local/bin;
a bare `codex` then resolved to the older system package, the provider 400'd
every request as "requires a newer version of Codex", and the retry ladders
escalated issues to needs-human-review — while the wizard, asking the OPERATOR'S
shell, kept reporting codex healthy."""

import argparse
import os
import stat
import subprocess
import time

import pytest

from issue_agent.app import cli
from issue_agent.app.codex_resolution import (
    MAX_SCAN_DIRS,
    MAX_VERSION_PROBES,
    VERSION_TIMEOUT_SECONDS,
    CodexCandidate,
    analyse_codex_resolution,
    codex_resolution_report,
    parse_codex_version,
    version_key,
)


# ── version parsing ───────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "output,expected",
    [
        ("codex-cli 0.147.0\n", "0.147.0"),
        ("codex-cli 0.142.3", "0.142.3"),
        ("0.147.0-alpha.4", "0.147.0-alpha.4"),
        ("codex-cli 0.147.0 (rust build)", "0.147.0"),
        ("", ""),
        ("   \n\n", ""),
        ("command not found", ""),
        ("\x00\xff garbage", ""),
        ("codex-cli2 0.147.0", "0.147.0"),
        ("v0.147.0", "0.147.0"),
        ("codex-cli v0.147.0", "0.147.0"),
        ("codex 2026-08-02 nightly", ""),
        ("codex 7", ""),
    ],
)
def test_parse_codex_version(output, expected):
    assert parse_codex_version(output) == expected


def test_parse_codex_version_bounds_huge_output():
    """A wedged or hostile binary can print megabytes; parsing must stay cheap
    and must not mistake late noise for a version."""
    huge = ("x" * 10_000_000) + "\ncodex-cli 9.9.9\n"
    assert parse_codex_version(huge) == ""


def test_parse_codex_version_finds_the_version_on_a_later_line():
    assert parse_codex_version("warning: config\ncodex-cli 0.147.0\n") == "0.147.0"


# ── version ordering ──────────────────────────────────────────────────────────
def test_prerelease_of_a_higher_core_beats_a_lower_release():
    assert version_key("0.147.0-alpha.4") > version_key("0.146.0")


def test_release_beats_its_own_prerelease():
    assert version_key("0.147.0") > version_key("0.147.0-alpha.4")


def test_prereleases_order_by_their_trailing_numbers():
    assert version_key("0.147.0-alpha.10") > version_key("0.147.0-alpha.4")


def test_unparseable_version_is_incomparable():
    assert version_key("") is None
    assert version_key("nightly") is None


# ── shadow scan / findings ────────────────────────────────────────────────────
def _fake_codex(directory, version: str) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "codex"
    path.write_text(f"#!/bin/sh\necho codex-cli {version}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def _versions(**by_path):
    return lambda path: f"codex-cli {by_path[path]}" if path in by_path else ""


def test_newer_codex_elsewhere_on_path_is_reported_with_the_pin_to_set(tmp_path):
    """The keifu failure: PATH order silently selects the OLDER install."""
    system = _fake_codex(tmp_path / "system", "0.142.3")
    local = _fake_codex(tmp_path / "local", "0.147.0")

    report = codex_resolution_report(
        "codex",
        path_env=os.pathsep.join([str(tmp_path / "system"), str(tmp_path / "local")]),
        extra_dirs=(), probe=_versions(**{system: "0.142.3", local: "0.147.0"}),
    )

    assert report.resolved_path == system
    assert report.version == "0.142.3"
    assert report.recommended_bin == local
    messages = " ".join(f.message for f in report.findings)
    assert local in messages and "0.147.0" in messages
    assert f"CODEX_BIN={local}" in messages


def test_an_absolute_codex_bin_draws_no_path_warning(tmp_path):
    local = _fake_codex(tmp_path / "local", "0.147.0")

    report = codex_resolution_report(
        local,
        path_env=str(tmp_path / "local"),
        extra_dirs=(), probe=_versions(**{local: "0.147.0"}),
    )

    assert report.pinned and report.healthy
    assert report.resolved_path == local
    assert report.version == "0.147.0"


def test_a_bare_name_warns_about_the_daemon_path_even_when_it_is_newest(tmp_path):
    """Nothing is wrong in THIS shell — that is exactly the trap. The daemon's
    PATH is a different PATH."""
    local = _fake_codex(tmp_path / "local", "0.147.0")

    report = codex_resolution_report(
        "codex", path_env=str(tmp_path / "local"),
        extra_dirs=(), probe=_versions(**{local: "0.147.0"}),
    )

    assert not report.pinned
    assert [f.level for f in report.findings] == ["warn"]
    assert f"CODEX_BIN={local}" in report.findings[0].message


def test_a_missing_codex_is_an_error_finding(tmp_path):
    report = codex_resolution_report(
        "codex", path_env=str(tmp_path / "empty"), extra_dirs=(), probe=lambda _p: ""
    )

    assert report.resolved_path == ""
    assert [f.level for f in report.findings] == ["error"]


def test_an_absolute_codex_bin_that_does_not_exist_is_an_error(tmp_path):
    report = codex_resolution_report(
        str(tmp_path / "nope" / "codex"), path_env="", extra_dirs=(), probe=lambda _p: ""
    )

    assert [f.level for f in report.findings] == ["error"]


def test_a_binary_that_reports_no_version_is_an_error(tmp_path):
    _fake_codex(tmp_path / "local", "0.147.0")

    report = codex_resolution_report(
        "codex", path_env=str(tmp_path / "local"), extra_dirs=(), probe=lambda _p: "wat"
    )

    levels = [f.level for f in report.findings]
    assert "error" in levels


def test_symlinked_duplicates_are_one_candidate(tmp_path):
    """A symlink farm over one install must not read as a shadowed install."""
    real = _fake_codex(tmp_path / "real", "0.147.0")
    link_dir = tmp_path / "link"
    link_dir.mkdir()
    link = link_dir / "codex"
    link.symlink_to(real)

    report = codex_resolution_report(
        "codex",
        path_env=os.pathsep.join([str(link_dir), str(tmp_path / "real")]),
        extra_dirs=(), probe=_versions(**{real: "0.147.0", str(link): "0.147.0"}),
    )

    assert [c.path for c in report.candidates] == [str(link)]
    assert [f.level for f in report.findings] == ["warn"]  # bare name only


def test_the_recommended_pin_is_the_stable_path_not_the_realpath(tmp_path):
    """NixOS: a realpath is /nix/store/<hash>/bin/codex, which the next garbage
    collection deletes. Pinning it would bake the very class of failure this
    check exists to prevent into the config."""
    store = _fake_codex(tmp_path / "store" / "abc123" / "bin", "0.147.0")
    profile = tmp_path / "profile"
    profile.mkdir()
    stable = profile / "codex"
    stable.symlink_to(store)

    report = codex_resolution_report(
        "codex", path_env=str(profile), extra_dirs=(),
        probe=_versions(**{str(stable): "0.147.0"}),
    )

    assert report.resolved_path == str(stable)
    assert report.recommended_bin == str(stable)
    assert store not in " ".join(f.message for f in report.findings)


def test_an_unknown_effective_version_never_recommends_a_switch(tmp_path):
    """F1: a probe timeout on the real 0.147.0 must not make a stale 0.100.0
    elsewhere look newer — 'unknown' is not 'old'."""
    here = _fake_codex(tmp_path / "here", "0.147.0")
    there = _fake_codex(tmp_path / "there", "0.100.0")

    report = codex_resolution_report(
        here, path_env=os.pathsep.join([str(tmp_path / "here"), str(tmp_path / "there")]),
        extra_dirs=(), probe=_versions(**{there: "0.100.0"}),  # `here` answers ''
    )

    assert report.version == ""
    assert report.recommended_bin == here
    assert [f.level for f in report.findings] == ["error"]
    assert there not in " ".join(f.message for f in report.findings)


def test_extra_directories_survive_a_path_at_the_scan_ceiling(tmp_path):
    """F6: appending the extras to a long PATH and then truncating would drop
    ~/.local/bin — the one directory the whole check exists to notice."""
    local = _fake_codex(tmp_path / "local", "0.147.0")
    filler = [str(tmp_path / f"pad{n}") for n in range(MAX_SCAN_DIRS + 10)]
    system = _fake_codex(tmp_path / "system", "0.142.3")

    report = codex_resolution_report(
        "codex",
        path_env=os.pathsep.join([str(tmp_path / "system"), *filler]),
        extra_dirs=(str(tmp_path / "local"),),
        probe=_versions(**{system: "0.142.3", local: "0.147.0"}),
    )

    assert report.recommended_bin == local
    assert f"CODEX_BIN={local}" in " ".join(f.message for f in report.findings)


def test_the_scan_bounds_how_many_binaries_it_versions(tmp_path):
    dirs = []
    probed: list[str] = []
    for n in range(MAX_VERSION_PROBES + 5):
        directory = tmp_path / f"d{n}"
        _fake_codex(directory, f"0.1{n}.0")
        dirs.append(str(directory))

    def probe(path):
        probed.append(path)
        return "codex-cli 0.147.0"

    codex_resolution_report(
        "codex", path_env=os.pathsep.join(dirs), extra_dirs=(), probe=probe
    )

    assert len(probed) <= MAX_VERSION_PROBES


def test_the_real_probe_survives_a_binary_that_cannot_run(tmp_path):
    """No injected probe: spawning must degrade to 'version unknown', not raise."""
    path = tmp_path / "bin" / "codex"
    path.parent.mkdir()
    path.write_bytes(b"\x7fELF-not-really")
    path.chmod(0o755)

    report = codex_resolution_report(str(path), path_env=str(tmp_path / "bin"), extra_dirs=())

    assert report.version == ""
    assert [f.level for f in report.findings] == ["error"]


def test_analysis_is_pure_over_discovered_candidates():
    """No version floors: a report with one known install and nothing newer is
    clean, whatever the absolute version numbers are."""
    report = analyse_codex_resolution(
        "/opt/codex", "/opt/codex", "0.1.0",
        (CodexCandidate("/opt/codex", "0.1.0"),),
    )
    assert report.healthy


# ── doctor surfaces it ────────────────────────────────────────────────────────
def _doctor_args(name="app"):
    return argparse.Namespace(repo="acme/app", target_dir="/srv/app", name=name)


def _stub_doctor(tmp_path, monkeypatch, env_file: str):
    """Real env composition (shared.env, no overlay); doctor.sh and host install are stubbed,
    so the python-side codex report is exercised against a really-sourced env."""
    monkeypatch.setattr(cli, "REPOS_DIR", str(tmp_path / "repos"))
    monkeypatch.setattr(cli, "entrypoint_drift", lambda: "")
    monkeypatch.setattr(cli, "SHARED_ENV", env_file)
    real_run = subprocess.run

    def run(cmd, *a, **k):
        if any("doctor.sh" in str(part) for part in cmd):
            return argparse.Namespace(returncode=0)
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(cli.subprocess, "run", run)


def test_doctor_reports_the_codex_binary_when_a_role_runs_on_codex(
    tmp_path, monkeypatch, capsys
):
    env_file = tmp_path / "shared.env"
    env_file.write_text('REPO="acme/app"\nREVIEWER_RUNTIME="codex"\n')
    _stub_doctor(tmp_path, monkeypatch, str(env_file))
    monkeypatch.setattr(
        cli, "codex_resolution_report",
        lambda b: analyse_codex_resolution(
            b, "/nix/store/codex", "0.142.3",
            (CodexCandidate("/nix/store/codex", "0.142.3"),
             CodexCandidate("/home/o/.local/bin/codex", "0.147.0")),
        ),
    )

    assert cli.cmd_doctor(_doctor_args()) == 0

    captured = capsys.readouterr()
    assert "/nix/store/codex" in captured.out
    assert "/home/o/.local/bin/codex" in captured.err and "0.147.0" in captured.err
    assert "CODEX_BIN=/home/o/.local/bin/codex" in captured.err


def test_doctor_stays_silent_about_codex_when_no_role_uses_it(
    tmp_path, monkeypatch, capsys
):
    env_file = tmp_path / "shared.env"
    env_file.write_text('REPO="acme/app"\nREVIEWER_RUNTIME="claude"\n')
    _stub_doctor(tmp_path, monkeypatch, str(env_file))
    monkeypatch.setattr(
        cli, "codex_resolution_report", lambda b: pytest.fail("probed the binary")
    )

    assert cli.cmd_doctor(_doctor_args()) == 0
    captured = capsys.readouterr()
    assert "codex" not in captured.out and "codex" not in captured.err


def test_doctor_checks_the_binary_whenever_codex_bin_is_pinned(
    tmp_path, monkeypatch, capsys
):
    """A pinned CODEX_BIN is a claim about a path; doctor is where it is tested,
    even if today's role assignment happens not to use codex."""
    env_file = tmp_path / "shared.env"
    env_file.write_text('REPO="acme/app"\nREVIEWER_RUNTIME="claude"\nCODEX_BIN="/opt/codex"\n')
    _stub_doctor(tmp_path, monkeypatch, str(env_file))
    seen: list[str] = []
    monkeypatch.setattr(
        cli, "codex_resolution_report",
        lambda b: seen.append(b) or analyse_codex_resolution(b, "", "", ()),
    )

    assert cli.cmd_doctor(_doctor_args()) == 0
    assert seen == ["/opt/codex"]
    assert "does not resolve" in capsys.readouterr().err


def test_doctor_prints_the_resolved_path_and_version_when_healthy(
    tmp_path, monkeypatch, capsys
):
    env_file = tmp_path / "shared.env"
    env_file.write_text('REPO="acme/app"\nFIXER_RUNTIME="codex"\nCODEX_BIN="/opt/codex"\n')
    _stub_doctor(tmp_path, monkeypatch, str(env_file))
    monkeypatch.setattr(cli, "codex_resolution_report", lambda b: analyse_codex_resolution(
        b, "/opt/codex", "0.147.0", (CodexCandidate("/opt/codex", "0.147.0"),)
    ))

    assert cli.cmd_doctor(_doctor_args()) == 0

    out = capsys.readouterr().out
    assert "/opt/codex" in out and "0.147.0" in out and "pinned" in out


# ── the real probe's bounds ───────────────────────────────────────────────────
def test_the_probe_abandons_a_spewing_binary_without_buffering_it(tmp_path):
    """F4: capture_output buffers the WHOLE stream before anything truncates it
    (measured: 15.9GiB RSS). The bound has to be at the pipe."""
    path = tmp_path / "bin" / "codex"
    path.parent.mkdir()
    path.write_text("#!/bin/sh\nexec yes 0123456789abcdef\n")
    path.chmod(0o755)

    started = time.monotonic()
    report = codex_resolution_report(str(path), path_env=str(tmp_path / "bin"),
                                     extra_dirs=())
    elapsed = time.monotonic() - started

    # Under the buffer-then-truncate implementation this ran until the
    # timeout fired; bounded at the pipe it returns as soon as it has enough.
    assert elapsed < VERSION_TIMEOUT_SECONDS
    assert [f.level for f in report.findings] == ["error"]


def test_the_probe_reports_a_version_the_binary_prints_after_a_banner(tmp_path):
    path = tmp_path / "bin" / "codex"
    path.parent.mkdir()
    path.write_text("#!/bin/sh\necho 'warning: config drift'\necho 'codex-cli 0.147.0'\n")
    path.chmod(0o755)

    report = codex_resolution_report(str(path), path_env=str(tmp_path / "bin"),
                                     extra_dirs=())

    assert report.version == "0.147.0"
    assert report.healthy


def test_the_probe_never_reads_the_operators_stdin(tmp_path):
    """F5: a probe that inherits stdin swallows the wizard's own answers."""
    path = tmp_path / "bin" / "codex"
    path.parent.mkdir()
    path.write_text("#!/bin/sh\nread line && echo \"codex-cli 9.9.9 $line\" || echo 'codex-cli 0.147.0'\n")
    path.chmod(0o755)

    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"operator-answer\n")
    os.close(write_fd)
    saved = os.dup(0)
    try:
        os.dup2(read_fd, 0)
        report = codex_resolution_report(str(path), path_env=str(tmp_path / "bin"),
                                         extra_dirs=())
    finally:
        os.dup2(saved, 0)
        os.close(saved)
        os.close(read_fd)

    assert report.version == "0.147.0"


def test_the_probe_gives_up_on_a_binary_that_never_answers(tmp_path):
    path = tmp_path / "bin" / "codex"
    path.parent.mkdir()
    path.write_text("#!/bin/sh\nsleep 300\n")
    path.chmod(0o755)

    started = time.monotonic()
    report = codex_resolution_report(str(path), path_env=str(tmp_path / "bin"),
                                     extra_dirs=())

    assert time.monotonic() - started < 30
    assert report.version == ""
    assert [f.level for f in report.findings] == ["error"]
