"""Installer identity refusals must precede host mutations or registration."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

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
if tool == 'gh':
    with open(fixture['gh_log'], 'a') as log: log.write(' '.join(args) + '\\n')
    print('true'); sys.exit(0)
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

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "bin/install-ci-runner.sh"
SYSTEMD = ROOT / "ops/systemd"
KVM_GROUP = "kvm:x:78:"


class Profile:
    def __init__(self, name, args, user, home, uid, repository):
        self.name, self.args, self.user, self.home = name, args, user, home
        self.uid, self.repository = uid, repository
        self.group = f"{user}:x:{uid}:"
        self.dedicated = f"{user}:x:{uid}:{uid}::{home}:/usr/bin/nologin"

    def account(self, uid=None, gid=None, home=None, shell="/usr/bin/nologin"):
        return f"{self.user}:x:{uid or self.uid}:{gid or self.uid}::{home or self.home}:{shell}"

    def other_user(self):
        return f"another-user:x:{self.uid}:{self.uid}::/srv:/usr/bin/nologin"


DEFAULT = Profile("default", [], "issue-agent-ci", "/var/lib/issue-agent-ci", "968", "xnmp/autonomous-issue-agent")
BALLAST = Profile("ballast", ["ballast"], "ballast-ci", "/var/lib/ballast-ci", "969", "xnmp/ballast")
PROFILES = pytest.mark.parametrize("profile", [DEFAULT, BALLAST], ids=lambda p: p.name)


def run_installer(tmp_path, profile, passwd, group, groups, host_groups=(KVM_GROUP,), args=None):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    mutation = tmp_path / "host-mutation"
    gh_log = tmp_path / "gh.log"
    fixture = {
        "passwd": passwd, "group": [*group, *host_groups], "groups": groups,
        "mutation": str(mutation), "gh_log": str(gh_log),
    }
    for tool in ("sudo", "gh", "id", "getent"):
        target = binaries / tool
        target.write_text(f"#!{sys.executable}\n{STUB}")
        target.chmod(0o755)
    result = subprocess.run(
        ["bash", str(INSTALLER), *(profile.args if args is None else args)],
        env={**os.environ, "PATH": f"{binaries}:/usr/bin:/bin", "CI_INSTALL_FIXTURE": json.dumps(fixture)},
        capture_output=True, text=True, check=False,
    )
    return result, mutation, gh_log


def refusal_cases(p):
    return {
        "root": ([p.account(uid="0")], [p.group], p.uid),
        "operator": ([p.account(uid="1000")], [p.group], p.uid),
        "root-group": ([p.account(gid="0")], ["root:x:0:"], "0"),
        "uid-alias-first": ([p.other_user(), p.dedicated], [p.group], p.uid),
        "uid-alias-later": ([p.dedicated, p.other_user()], [p.group], p.uid),
        "duplicate-name": ([p.dedicated, f"{p.user}:x:970:{p.uid}::/srv:/usr/bin/nologin"], [p.group], p.uid),
        "privileged-primary": ([p.dedicated], [f"wheel:x:{p.uid}:"], p.uid),
        "gid-alias-later": ([p.dedicated], [p.group, f"wheel:x:{p.uid}:"], p.uid),
        "foreign-home": ([p.account(home="/home/operator")], [p.group], p.uid),
        "login-shell": ([p.account(shell="/bin/bash")], [p.group], p.uid),
        "extra-group": ([p.dedicated], [p.group], f"{p.uid} 998"),
        "malformed-uid": ([p.account(uid="invalid")], [p.group], p.uid),
    }


@PROFILES
@pytest.mark.parametrize("case", list(refusal_cases(DEFAULT)))
def test_existing_runner_identity_is_refused_before_any_host_mutation(tmp_path, profile, case):
    passwd, group, groups = refusal_cases(profile)[case]

    result, mutation, _ = run_installer(tmp_path, profile, passwd, group, groups)

    assert result.returncode == 2, result.stderr
    assert not mutation.exists()


@PROFILES
def test_dedicated_runner_identity_is_accepted_and_reaches_host_setup(tmp_path, profile):
    result, mutation, gh_log = run_installer(
        tmp_path, profile, ["root:x:0:0::/root:/bin/bash", profile.dedicated],
        ["root:x:0:", profile.group], profile.uid,
    )

    assert "dedicated unprivileged identity" not in result.stderr
    assert mutation.exists()
    assert f"repos/{profile.repository}" in gh_log.read_text()


@PROFILES
def test_a_new_account_is_created_with_the_profile_identity(tmp_path, profile):
    result, mutation, _ = run_installer(tmp_path, profile, [], [], "")

    assert result.returncode == 99
    assert mutation.read_text() == (
        f"-n useradd --system --user-group --home-dir {profile.home} --shell /usr/bin/nologin {profile.user}"
    )


def test_ballast_refuses_when_the_unit_supplementary_group_is_missing(tmp_path):
    result, mutation, _ = run_installer(
        tmp_path, BALLAST, [BALLAST.dedicated], [BALLAST.group], BALLAST.uid, host_groups=(),
    )

    assert result.returncode == 2
    assert "kvm" in result.stderr
    assert not mutation.exists()


def test_the_default_profile_does_not_require_the_kvm_group(tmp_path):
    result, mutation, _ = run_installer(
        tmp_path, DEFAULT, [DEFAULT.dedicated], [DEFAULT.group], DEFAULT.uid, host_groups=(),
    )

    assert "kvm" not in result.stderr
    assert mutation.exists()


@pytest.mark.parametrize("args", [["unknown"], [""], ["default", "ballast"], ["BALLAST"]])
def test_unknown_or_extra_profile_arguments_are_refused_before_any_work(tmp_path, args):
    result, mutation, gh_log = run_installer(
        tmp_path, DEFAULT, [DEFAULT.dedicated], [DEFAULT.group], DEFAULT.uid, args=args,
    )

    assert result.returncode == 2
    assert not mutation.exists()
    assert not gh_log.exists()


def test_omitted_and_explicit_default_profile_behave_identically(tmp_path):
    outcomes = []
    for name, args in (("omitted", []), ("explicit", ["default"])):
        directory = tmp_path / name
        directory.mkdir()
        result, mutation, gh_log = run_installer(
            directory, DEFAULT, [DEFAULT.dedicated], [DEFAULT.group], DEFAULT.uid, args=args,
        )
        outcomes.append((result.returncode, mutation.read_text(), gh_log.read_text()))

    assert outcomes[0] == outcomes[1]


def profile_values(name):
    """Evaluate the installer's own profile table, the single source of truth."""
    script = INSTALLER.read_text()
    block = script[script.index("# BEGIN PROFILES"):script.index("# END PROFILES")]
    fields = "repository runner_user runner_home runner_name runner_label service unit_file unit_groups runner_dir"
    code = f'{block}\nload_profile "$1"\nfor f in {fields}; do printf "%s=%s\\n" "$f" "${{!f}}"; done'
    out = subprocess.run(["bash", "-c", code, "_", name], capture_output=True, text=True, check=True).stdout
    return dict(line.split("=", 1) for line in out.splitlines())


def test_default_profile_keeps_the_existing_runner_identity():
    assert profile_values("default") == {
        "repository": "xnmp/autonomous-issue-agent",
        "runner_user": "issue-agent-ci",
        "runner_home": "/var/lib/issue-agent-ci",
        "runner_name": "issue-agent-ci",
        "runner_label": "issue-agent-ci",
        "service": "issue-agent-ci-runner.service",
        "unit_file": "github-actions-runner.service",
        "unit_groups": "",
        "runner_dir": "/var/lib/issue-agent-ci/runner",
    }


def test_ballast_profile_is_the_documented_private_repository_runner():
    values = profile_values("ballast")

    assert values["repository"] == "xnmp/ballast"
    assert values["runner_user"] == values["runner_name"] == values["runner_label"] == "ballast-ci"
    assert values["runner_home"] == "/var/lib/ballast-ci"
    assert values["service"] == "ballast-ci-runner.service"
    assert values["unit_groups"] == "kvm"


def test_profiles_share_no_account_home_service_name_label_unit_or_repository():
    default, ballast = profile_values("default"), profile_values("ballast")

    for field in default:
        assert default[field] != ballast[field], field
    assert not ballast["runner_home"].startswith(default["runner_home"])
    assert not default["runner_home"].startswith(ballast["runner_home"])


def unit_directives(path):
    return [line for line in path.read_text().splitlines() if line and not line.startswith("#")]


def test_ballast_unit_differs_from_the_hardened_template_only_in_limits_and_kvm():
    template = unit_directives(SYSTEMD / "github-actions-runner.service")
    ballast = unit_directives(SYSTEMD / "github-actions-runner-ballast.service")
    renamed = [
        line.replace("issue-agent-ci", "ballast-ci").replace("autonomous-issue-agent", "xnmp/ballast")
        for line in template
    ]

    assert set(ballast) - set(renamed) == {
        "CPUQuota=1200%", "MemoryMax=24G", "TasksMax=2048", "SupplementaryGroups=kvm",
    }
    assert {line.split("=")[0] for line in set(renamed) - set(ballast)} == {"CPUQuota", "MemoryMax", "TasksMax"}
    assert len(ballast) == len(renamed) + 1


@PROFILES
def test_each_unit_runs_as_its_profile_account_and_confines_writes_to_its_home(profile):
    values = profile_values(profile.name)
    unit = dict(line.split("=", 1) for line in unit_directives(SYSTEMD / values["unit_file"]) if "=" in line)

    assert unit["User"] == unit["Group"] == values["runner_user"]
    assert unit["ReadWritePaths"] == values["runner_home"]
    assert unit["WorkingDirectory"] == values["runner_dir"]
    assert unit["ExecStart"] == f"{values['runner_dir']}/runsvc.sh"
    assert unit.get("SupplementaryGroups", "") == values["unit_groups"]
    assert (unit["ProtectSystem"], unit["NoNewPrivileges"], unit["MemorySwapMax"]) == ("strict", "true", "0")


def test_installer_hardcodes_no_profile_path_outside_the_profile_table():
    script = INSTALLER.read_text()
    outside = script[script.index("# END PROFILES"):]

    assert "/var/lib/" not in outside
    assert "xnmp/" not in outside
    assert "issue-agent-ci" not in outside
