"""doctor.sh live write probe for the review identity (keifu, 2026-08-01).

Every identity check in `== github access ==` was a READ.
`repos/O/R.permissions.push` reports the COLLABORATOR ROLE, and a fine-grained
PAT carries a second permission set no read can introspect — so doctor printed
"review identity authenticates and has WRITE access" over a token that 403'd on
every label write. The harness only found out after a paid leg produced a
verdict it could never post, and then re-tried that denied write on every tick.

The probe creates and immediately deletes a repo LABEL with that identity's
token: repo-level, so no issue is touched and nobody is notified, and gated by
exactly the Issues: Write scope the harness's lifecycle writes need.

These drive the REAL bin/doctor.sh with a `gh` stub whose label endpoints are
scripted per case, mirroring the `_fake_ci_tools`/`_ci_doctor` pattern.
"""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCTOR_SH = ROOT / "bin" / "doctor.sh"


def _fake_bin(
    tmp_path: Path,
    *,
    label_post: str,
    label_delete_rc: int = 0,
    repo_read: str = "echo widget; exit 0",
) -> Path:
    """PATH dir whose `gh` answers every read the identity section makes as a
    healthy write-capable collaborator, and whose label POST is scripted by
    `label_post` (a bash snippet run for `gh api -X POST .../labels`).

    Each label call appends its verb to $PROBE_LOG so a test can assert the
    probe cleaned up after itself.
    """
    fake_bin = tmp_path / "fake_bin"
    fake_bin.mkdir()
    gh = f"""#!/usr/bin/env bash
if [ "$1" = "--version" ]; then echo "gh version 0.0.0-fake"; exit 0; fi
if [ "$1" = "repo" ] && [ "$2" = "view" ]; then exit 0; fi
if [ "$1" = "pr" ] && [ "$2" = "list" ]; then echo '[]'; exit 0; fi
if [ "$1" = "api" ] && [ "$2" = "user" ]; then echo "acme-review"; exit 0; fi
for a in "$@"; do
  case "$a" in
    */labels|*/labels/*)
      if [ "$2" = "-X" ] && [ "$3" = "POST" ]; then
        echo "POST $a" >> "$PROBE_LOG"
        {label_post}
      fi
      if [ "$2" = "-X" ] && [ "$3" = "DELETE" ]; then
        echo "DELETE $a" >> "$PROBE_LOG"
        exit {label_delete_rc}
      fi
      ;;
  esac
done
if [ "$1" = "api" ]; then
  case "$2" in
    */actions/workflows) echo '1'; exit 0 ;;
    repos/*)
      if [ "$4" = ".name" ]; then {repo_read}; fi
      echo 'true'; exit 0 ;;
  esac
fi
exit 0
"""
    (fake_bin / "gh").write_text(gh)
    for name, body in {
        "claude": '#!/usr/bin/env bash\necho "claude 0.0.0-fake"\n',
        "jq": '#!/usr/bin/env bash\necho "jq-0.0.0-fake"\n',
        "uv": (
            "#!/usr/bin/env bash\n"
            'if [ "$1" = "--version" ]; then echo "uv 0.0.0-fake"; exit 0; fi\n'
            "exit 2\n"
        ),
    }.items():
        (fake_bin / name).write_text(body)
    for p in fake_bin.iterdir():
        p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return fake_bin


def _doctor(tmp_path: Path, **fixture) -> tuple[str, str]:
    """Run the real doctor with a write-probe fixture. Returns (stdout, log)."""
    fake_bin = _fake_bin(tmp_path, **fixture)
    harness = tmp_path / "harness"
    (harness / "config" / "repos").mkdir(parents=True, exist_ok=True)
    empty = Path(tempfile.mkdtemp(prefix="doctor-cfg-")) / "empty.env"
    empty.write_text("")
    probe_log = tmp_path / "probe.log"
    probe_log.write_text("")
    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "HARNESS_ROOT": str(harness),
            "AGENT_ENV": str(empty),
            "PROBE_LOG": str(probe_log),
            "REPO": "acme/widget",
            "BASE_BRANCH": "trunk",
            "FIX_BOT": "acme-fixer",
            "REVIEW_BOT": "acme-review",
            "REVIEW_BOT_TOKEN": "github_pat_fake",
        }
    )
    result = subprocess.run(
        ["bash", str(DOCTOR_SH)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.stdout, probe_log.read_text()


def test_successful_probe_passes_and_removes_the_label(tmp_path):
    out, log = _doctor(tmp_path, label_post="exit 0")

    assert "live-probed" in out
    assert "✓" in out.split("live-probed")[0].splitlines()[-1]
    # Created exactly one label and deleted that same label.
    posts = [line for line in log.splitlines() if line.startswith("POST")]
    deletes = [line for line in log.splitlines() if line.startswith("DELETE")]
    assert len(posts) == 1 and len(deletes) == 1
    name = posts[0].rsplit("/", 1)[-1] if "/" in posts[0] else ""
    probe = deletes[0].rsplit("/", 1)[-1]
    assert probe.startswith("harness-probe-")
    assert len(probe) == len("harness-probe-") + 8
    del name


def test_denied_write_is_an_error_naming_the_scopes_to_remint(tmp_path):
    out, _log = _doctor(
        tmp_path,
        label_post=(
            'echo "gh: Resource not accessible by personal access token (HTTP 403)" >&2; '
            "exit 1"
        ),
    )

    assert "CANNOT write" in out
    error_line = next(line for line in out.splitlines() if "CANNOT write" in line)
    assert "✗" in error_line
    # Cross-account personal repos need a CLASSIC PAT — fine-grained tokens
    # cannot select another user's personal repo as resource owner, so naming
    # only fine-grained scopes sent the operator down a dead end (2026-08-02:
    # a re-minted fine-grained token 403'd identically).
    assert "CLASSIC PAT" in error_line
    assert "repo" in error_line
    assert "Issues: Read+Write" in error_line
    assert "REVIEW_BOT_TOKEN" in error_line


def test_network_failure_stays_a_warning_not_an_error(tmp_path):
    """A dead network says nothing about the token's scopes. Failing the
    preflight on it would make doctor unusable offline and would misattribute
    an outage to a permission problem."""
    out, _log = _doctor(
        tmp_path,
        label_post='echo "error connecting to api.github.com" >&2; exit 1',
    )

    assert "CANNOT write" not in out
    warning = next(line for line in out.splitlines() if "write probe" in line)
    assert "⚠" in warning
    assert "UNVERIFIED" in warning


def test_probe_label_is_reported_when_cleanup_fails(tmp_path):
    out, log = _doctor(tmp_path, label_post="exit 0", label_delete_rc=1)

    leftover = next(line for line in out.splitlines() if "could not be deleted" in line)
    assert "⚠" in leftover
    assert "harness-probe-" in leftover
    # The write capability itself is still reported as proven.
    assert "live-probed" in leftover
    assert any(line.startswith("DELETE") for line in log.splitlines())


def test_probe_does_not_delete_a_label_it_did_not_create(tmp_path):
    """A 422 means the name collided — permission was fine, but deleting that
    label would destroy a real one."""
    out, log = _doctor(
        tmp_path,
        label_post='echo "already_exists (HTTP 422)" >&2; exit 1',
    )

    assert "live-probed" in out
    assert "CANNOT write" not in out
    assert not [line for line in log.splitlines() if line.startswith("DELETE")]


def test_a_rate_limited_bot_is_not_misdiagnosed_as_missing_access(tmp_path):
    """2026-08-02: the denied-write retry loop exhausted the bot's hourly
    GraphQL quota, and the (then GraphQL-backed) access check reported
    'cannot access — invite it as a collaborator' for an identity whose
    access was fine. The read is REST now, and a rate-limited read must say
    RATE LIMITED — sending the operator to re-invite a collaborator during a
    quota exhaustion hides the actual incident."""
    out, _log = _doctor(
        tmp_path,
        label_post="exit 0",
        repo_read='echo "API rate limit exceeded for user ID 1"; exit 1',
    )
    assert "RATE LIMITED" in out
    assert "do not re-invite" in out
    assert "invite it as a collaborator (write) and ACCEPT" not in out
