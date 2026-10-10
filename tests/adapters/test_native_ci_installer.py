"""Installer identity refusals must precede host mutations or registration."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

DEDICATED = "issue-agent-ci:x:968:968::/var/lib/issue-agent-ci:/usr/bin/nologin"
DEDICATED_GROUP = "issue-agent-ci:x:968:"

# Behaves like getent: a keyed lookup returns the first match, enumeration
# returns every record. Any sudo call other than the credential probe is a
# host mutation from the test's point of view.
STUB = """
import json, os, sys
from pathlib import Path
fixture = json.loads(os.environ['CI_INSTALL_FIXTURE'])
tool = Path(sys.argv[0]).name
args = sys.argv[1:]
if tool == 'sudo':
    if args == ['-n', 'true']: sys.exit(0)
    Path(fixture['mutation']).write_text(' '.join(args))
    sys.exit(99)
if tool == 'gh': print('true'); sys.exit(0)
if tool == 'id':
    print(fixture['groups'] if args[0] == '-G' else '1000')
    sys.exit(0)
if tool == 'getent':
    records = fixture[args[0]]
    if len(args) == 1:
        print('\\n'.join(records)); sys.exit(0)
    field = 2 if args[1].isdigit() else 0
    match = next((r for r in records if r.split(':')[field] == args[1]), None)
    if match is None: sys.exit(2)
    print(match); sys.exit(0)
sys.exit(99)
"""


def run_installer(tmp_path, passwd, group, groups):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    mutation = tmp_path / "host-mutation"
    fixture = {"passwd": passwd, "group": group, "groups": groups, "mutation": str(mutation)}
    for tool in ("sudo", "gh", "id", "getent"):
        target = binaries / tool
        target.write_text(f"#!{sys.executable}\n{STUB}")
        target.chmod(0o755)
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["bash", str(root / "bin/install-ci-runner.sh")],
        env={**os.environ, "PATH": f"{binaries}:/usr/bin:/bin", "CI_INSTALL_FIXTURE": json.dumps(fixture)},
        capture_output=True, text=True, check=False,
    )
    return result, mutation


def account(uid="968", gid="968", home="/var/lib/issue-agent-ci", shell="/usr/bin/nologin"):
    return f"issue-agent-ci:x:{uid}:{gid}::{home}:{shell}"


@pytest.mark.parametrize(
    "passwd,group,groups",
    [
        ([account(uid="0")], [DEDICATED_GROUP], "968"),
        ([account(uid="1000")], [DEDICATED_GROUP], "968"),
        ([account(gid="0")], ["root:x:0:"], "0"),
        (["another-user:x:968:968::/srv:/usr/bin/nologin", DEDICATED], [DEDICATED_GROUP], "968"),
        ([DEDICATED, "another-user:x:968:968::/srv:/usr/bin/nologin"], [DEDICATED_GROUP], "968"),
        ([DEDICATED, "issue-agent-ci:x:969:968::/srv:/usr/bin/nologin"], [DEDICATED_GROUP], "968"),
        ([DEDICATED], ["wheel:x:968:"], "968"),
        ([DEDICATED], [DEDICATED_GROUP, "wheel:x:968:"], "968"),
        ([account(home="/home/operator")], [DEDICATED_GROUP], "968"),
        ([account(shell="/bin/bash")], [DEDICATED_GROUP], "968"),
        ([DEDICATED], [DEDICATED_GROUP], "968 998"),
        ([account(uid="invalid")], [DEDICATED_GROUP], "968"),
    ],
    ids=[
        "root", "operator", "root-group", "uid-alias-first", "uid-alias-later",
        "duplicate-name", "privileged-primary", "gid-alias-later", "foreign-home",
        "login-shell", "extra-group", "malformed-uid",
    ],
)
def test_existing_runner_identity_is_refused_before_any_host_mutation(tmp_path, passwd, group, groups):
    result, mutation = run_installer(tmp_path, passwd, group, groups)

    assert result.returncode == 2, result.stderr
    assert not mutation.exists()


def test_dedicated_runner_identity_is_accepted_and_reaches_host_setup(tmp_path):
    result, mutation = run_installer(
        tmp_path, ["root:x:0:0::/root:/bin/bash", DEDICATED], ["root:x:0:", DEDICATED_GROUP], "968",
    )

    assert "dedicated unprivileged identity" not in result.stderr
    assert mutation.exists()
