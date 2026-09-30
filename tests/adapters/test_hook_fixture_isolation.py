"""A broken hook fixture must never write into the invoking repository."""

from __future__ import annotations

import os
import hashlib
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest


HOOK_SUITE = Path(__file__).resolve().parents[1] / "test_hooks.sh"


def _run(*args: str, cwd: Path, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        args, cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


def _snapshot(repo: Path, remote: Path, env: dict[str, str]) -> tuple[str, ...]:
    index = repo / ".git" / "index"
    objects = repo / ".git" / "objects"
    return (
        _run("git", "rev-parse", "HEAD", cwd=repo, env=env),
        _run("git", "branch", "--list", cwd=repo, env=env),
        _run("git", "show-ref", cwd=repo, env=env),
        _run("git", "config", "--local", "--list", cwd=repo, env=env),
        _run("git", "status", "--porcelain=v1", "--untracked-files=all", cwd=repo, env=env),
        _run("git", "--git-dir", str(remote), "show-ref", cwd=repo, env=env),
        (repo / "keep.txt").read_text(),
        (repo / ".git" / "config").read_text(),
        hashlib.sha256(index.read_bytes()).hexdigest(),
        "\n".join(sorted(str(path.relative_to(objects)) for path in objects.rglob("*") if path.is_file())),
        (remote / "config").read_text(),
    )


def _clean_git_env() -> dict[str, str]:
    return dict(
        (key, value) for key, value in os.environ.items() if not key.startswith("GIT_")
    ) | {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


@pytest.mark.parametrize(
    "fault",
    [
        "unset_tmpdir_and_git_init_failure",
        "absent_tmpdir_directory",
        "mktemp_failure",
        "later_mktemp_failure",
        "invalid_output",
        "removed_output",
        "git_init_failure",
        "temp_inside_checkout",
    ],
)
def test_hook_fixture_setup_failure_preserves_invoking_repo(
    tmp_path: Path, fault: str
) -> None:
    repo = tmp_path / "sentinel"
    remote = tmp_path / "local-origin.git"
    repo.mkdir()
    real_git = shutil.which("git")
    assert real_git is not None
    clean_env = _clean_git_env()
    _run(real_git, "init", "-q", "-b", "dev", cwd=repo, env=clean_env)
    _run(real_git, "config", "user.email", "sentinel@example.test", cwd=repo, env=clean_env)
    _run(real_git, "config", "user.name", "Sentinel", cwd=repo, env=clean_env)
    _run(real_git, "config", "commit.gpgsign", "false", cwd=repo, env=clean_env)
    (repo / "keep.txt").write_text("original\n")
    _run(real_git, "add", "keep.txt", cwd=repo, env=clean_env)
    _run(real_git, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "seed", cwd=repo, env=clean_env)
    _run(real_git, "init", "-q", "--bare", str(remote), cwd=repo, env=clean_env)
    _run(real_git, "remote", "add", "origin", str(remote), cwd=repo, env=clean_env)
    _run(real_git, "push", "-q", "origin", "dev", cwd=repo, env=clean_env)
    before = _snapshot(repo, remote, clean_env)

    wrapper_dir = tmp_path / "bin"
    wrapper_dir.mkdir()
    ambient_gh_calls = tmp_path / "ambient-gh-calls"
    (wrapper_dir / "gh").write_text(
        "#!/usr/bin/env bash\n"
        f"printf 'called\\n' >> '{ambient_gh_calls}'\n"
        "exit 93\n"
    )
    (wrapper_dir / "gh").chmod(0o755)
    fixture_tmp = tmp_path / "fixture-temp"
    fixture_tmp.mkdir()
    if fault in {"mktemp_failure", "later_mktemp_failure", "invalid_output", "removed_output"}:
        real_mktemp = shutil.which("mktemp")
        assert real_mktemp is not None
        (wrapper_dir / "mktemp").write_text(
            "#!/usr/bin/env bash\n"
            + {
                "mktemp_failure": "exit 47\n",
                "later_mktemp_failure": (
                    f"counter='{tmp_path}/mktemp-count'; "
                    "n=$(cat \"$counter\" 2>/dev/null || echo 0); n=$((n+1)); "
                    "printf '%s\\n' \"$n\" > \"$counter\"; "
                    f"if [ \"$n\" -le 3 ]; then exec '{real_mktemp}' \"$@\"; fi; exit 47\n"
                ),
                "invalid_output": f"printf '%s\\n' '{repo}'\n",
                "removed_output": (
                    f"d='{fixture_tmp}/hooks-fixture.removed'; "
                    "mkdir -p \"$d\"; rmdir \"$d\"; printf '%s\\n' \"$d\"\n"
                ),
            }[fault]
        )
        (wrapper_dir / "mktemp").chmod(0o755)
    if fault in {"unset_tmpdir_and_git_init_failure", "git_init_failure"}:
        (wrapper_dir / "git").write_text(
            "#!/usr/bin/env bash\n"
            "if [ \"${1:-}\" = init ]; then exit 47; fi\n"
            f'exec "{real_git}" "$@"\n'
        )
        (wrapper_dir / "git").chmod(0o755)

    case_env = dict(clean_env, PATH=f"{wrapper_dir}:{os.environ['PATH']}")
    if fault == "unset_tmpdir_and_git_init_failure":
        case_env.pop("TMPDIR", None)
    elif fault == "absent_tmpdir_directory":
        case_env["TMPDIR"] = str(tmp_path / "does-not-exist")
    elif fault == "temp_inside_checkout":
        case_env["TMPDIR"] = str(repo)
    else:
        case_env["TMPDIR"] = str(fixture_tmp)
    result = subprocess.run(
        ("bash", str(HOOK_SUITE)),
        cwd=repo,
        env=case_env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode != 0, result.stdout[-1000:]
    assert "hook fixture setup failed" in result.stderr
    if fault == "later_mktemp_failure":
        assert "generated AGENTS.override.md blocked" in result.stdout
    assert not ambient_gh_calls.exists()
    assert _snapshot(repo, remote, clean_env) == before


@pytest.mark.parametrize("selector_family", ["repository", "objects", "config"])
def test_ambient_git_selectors_are_cleared_before_fixture_git_commands(
    tmp_path: Path, selector_family: str
) -> None:
    repo = tmp_path / "sentinel"
    remote = tmp_path / "local-origin.git"
    repo.mkdir()
    real_git = shutil.which("git")
    real_mktemp = shutil.which("mktemp")
    assert real_git is not None and real_mktemp is not None
    clean_env = _clean_git_env()
    _run(real_git, "init", "-q", "-b", "dev", cwd=repo, env=clean_env)
    _run(real_git, "config", "user.email", "sentinel@example.test", cwd=repo, env=clean_env)
    _run(real_git, "config", "user.name", "Sentinel", cwd=repo, env=clean_env)
    _run(real_git, "config", "commit.gpgsign", "false", cwd=repo, env=clean_env)
    (repo / "keep.txt").write_text("original\n")
    _run(real_git, "add", "keep.txt", cwd=repo, env=clean_env)
    _run(real_git, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "seed", cwd=repo, env=clean_env)
    _run(real_git, "init", "-q", "--bare", str(remote), cwd=repo, env=clean_env)
    _run(real_git, "remote", "add", "origin", str(remote), cwd=repo, env=clean_env)
    _run(real_git, "push", "-q", "origin", "dev", cwd=repo, env=clean_env)
    before = _snapshot(repo, remote, clean_env)

    wrapper_dir = tmp_path / "bin"
    wrapper_dir.mkdir()
    ambient_gh_calls = tmp_path / "ambient-gh-calls"
    (wrapper_dir / "gh").write_text(
        "#!/usr/bin/env bash\n"
        f"printf 'called\\n' >> {shlex.quote(str(ambient_gh_calls))}\n"
        "exit 93\n"
    )
    (wrapper_dir / "gh").chmod(0o755)
    (wrapper_dir / "git").write_text(
        "#!/usr/bin/env bash\n"
        'for git_knob in "${!GIT_@}"; do\n'
        '  case "$git_knob" in GIT_ALLOW_PROTOCOL|GIT_CONFIG_GLOBAL|GIT_CONFIG_NOSYSTEM) ;;\n'
        '    *) printf "ambient Git selector reached fixture: %s\\n" "$git_knob" >&2; exit 81 ;;\n'
        '  esac\n'
        'done\n'
        f"exec {shlex.quote(real_git)} \"$@\"\n"
    )
    (wrapper_dir / "git").chmod(0o755)
    (wrapper_dir / "mktemp").write_text(
        "#!/usr/bin/env bash\n"
        f"counter={shlex.quote(str(tmp_path / 'mktemp-count'))}\n"
        'n=$(cat "$counter" 2>/dev/null || echo 0); n=$((n+1))\n'
        'printf "%s\\n" "$n" > "$counter"\n'
        f"if [ \"$n\" -le 3 ]; then exec {shlex.quote(real_mktemp)} \"$@\"; fi\n"
        "exit 47\n"
    )
    (wrapper_dir / "mktemp").chmod(0o755)

    selectors = {
        "repository": {
            "GIT_DIR": str(repo / ".git"),
            "GIT_WORK_TREE": str(repo),
            "GIT_COMMON_DIR": str(repo / ".git"),
            "GIT_INDEX_FILE": str(repo / ".git" / "index"),
            "GIT_NAMESPACE": "ambient",
            "GIT_PREFIX": "ambient/",
        },
        "objects": {
            "GIT_OBJECT_DIRECTORY": str(repo / ".git" / "objects"),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(repo / ".git" / "objects"),
            "GIT_QUARANTINE_PATH": str(repo / ".git" / "objects"),
            "GIT_TEMPLATE_DIR": str(repo / ".git"),
        },
        "config": {
            "GIT_CONFIG": str(repo / ".git" / "config"),
            "GIT_CONFIG_SYSTEM": str(repo / ".git" / "config"),
            "GIT_CONFIG_GLOBAL": str(repo / ".git" / "config"),
            "GIT_CONFIG_NOSYSTEM": "0",
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": str(repo / ".git" / "hooks"),
            "GIT_CONFIG_KEY_1": "core.worktree",
            "GIT_CONFIG_VALUE_1": str(repo),
            "GIT_CONFIG_KEY_7": "core.bare",
            "GIT_CONFIG_VALUE_7": "true",
            "GIT_CONFIG_ENV_0": "AMBIENT_GIT_CONFIG_VALUE",
            "GIT_CONFIG_PARAMETERS": "'core.bare=true'",
        },
    }[selector_family]
    fixture_tmp = tmp_path / "fixture-temp"
    fixture_tmp.mkdir()
    case_env = dict(
        clean_env,
        PATH=f"{wrapper_dir}:{os.environ['PATH']}",
        TMPDIR=str(fixture_tmp),
        **selectors,
    )
    result = subprocess.run(
        ("bash", str(HOOK_SUITE)), cwd=repo, env=case_env,
        capture_output=True, text=True, timeout=30, check=False,
    )

    assert result.returncode != 0, result.stdout[-1000:]
    assert "hook fixture setup failed" in result.stderr
    assert "generated AGENTS.override.md blocked" in result.stdout
    assert "ambient Git selector reached fixture" not in result.stderr
    assert not ambient_gh_calls.exists()
    assert _snapshot(repo, remote, clean_env) == before
