"""Installer identity refusals must precede host mutations or registration."""

import json
import os
from pathlib import Path
import shutil
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
    with open(fixture['sudo_log'], 'a') as log: log.write(json.dumps(args) + '\\n')
    Path(fixture['mutation']).write_text(' '.join(args))
    if not fixture['proceed']: sys.exit(99)
    # Success path: 'fresh' has no .runner yet, 'configured' is a reinstall.
    if '-u' in args:
        sys.stdin.read()
        if args[3:5] == ['test', '-f']: sys.exit(0 if fixture['proceed'] == 'configured' else 1)
        if 'jq' in args: print('https://github.com/' + fixture['registered'])
    sys.exit(0)
if tool == 'systemctl':
    if args[0] == 'is-active': sys.exit(0)
    shown = {
        'LoadState': 'loaded' if fixture['loaded'] else 'not-found',
        'User': fixture['user'], 'Group': fixture['user'], 'SupplementaryGroups': '', 'DropInPaths': '',
        'FragmentPath': '/etc/systemd/system/' + fixture['service'],
        'WorkingDirectory': fixture['dir'],
        'ExecStart': '{ path=%s/runsvc.sh ; argv[]=%s/runsvc.sh ;' % (fixture['dir'], fixture['dir']),
    }
    print(shown[args[args.index('-p') + 1]]); sys.exit(0)
if tool == 'gh':
    with open(fixture['gh_log'], 'a') as log: log.write(' '.join(args) + '\\n')
    endpoint = ' '.join(args)
    if endpoint.endswith('/actions/runners'): print('{"runners": []}')
    elif 'downloads' in endpoint:
        print(json.dumps({'download_url': 'https://github.com/actions/runner/releases/download/v1/r.tgz',
                          'sha256_checksum': 'a' * 64}))
    elif 'registration-token' in endpoint: print('SECRET-TOKEN')
    else: print('true')
    sys.exit(0)
if tool == 'curl':
    Path(args[args.index('-o') + 1]).write_bytes(b'archive')
    sys.exit(0)
if tool in ('sha256sum', 'shellcheck'): sys.exit(0)
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


class Profile:
    def __init__(self, name, args, user, home, uid, repository):
        self.name, self.args, self.user, self.home = name, args, user, home
        self.uid, self.repository = uid, repository
        self.group = f"{user}:x:{uid}:"
        self.service = f"{user}-runner.service" if user == "ballast-ci" else "issue-agent-ci-runner.service"
        self.dedicated = f"{user}:x:{uid}:{uid}::{home}:/usr/bin/nologin"

    def account(self, uid=None, gid=None, home=None, shell="/usr/bin/nologin"):
        return f"{self.user}:x:{uid or self.uid}:{gid or self.uid}::{home or self.home}:{shell}"

    def other_user(self):
        return f"another-user:x:{self.uid}:{self.uid}::/srv:/usr/bin/nologin"


DEFAULT = Profile("default", [], "issue-agent-ci", "/var/lib/issue-agent-ci", "968", "xnmp/autonomous-issue-agent")
BALLAST = Profile("ballast", ["ballast"], "ballast-ci", "/var/lib/ballast-ci", "969", "xnmp/ballast")
PROFILES = pytest.mark.parametrize("profile", [DEFAULT, BALLAST], ids=lambda p: p.name)


def operator_checkout(tmp_path):
    # The installer refuses a checkout inside a runner home, which is where CI
    # itself runs these tests, so each run uses a copy outside any runner home.
    checkout = tmp_path / "checkout"
    (checkout / "bin").mkdir(parents=True)
    shutil.copy2(INSTALLER, checkout / "bin" / INSTALLER.name)
    shutil.copytree(SYSTEMD, checkout / "ops" / "systemd")
    return checkout / "bin" / INSTALLER.name


def run_installer(tmp_path, profile, passwd, group, groups, args=None, proceed=None, loaded=False, registered_to=None):
    installer = operator_checkout(tmp_path)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    mutation = tmp_path / "host-mutation"
    gh_log = tmp_path / "gh.log"
    fixture = {
        "passwd": passwd, "group": group, "groups": groups, "proceed": proceed, "loaded": loaded, "registered": registered_to or profile.repository,
        "user": profile.user, "dir": f"{profile.home}/runner", "service": profile.service,
        "mutation": str(mutation), "gh_log": str(gh_log), "sudo_log": str(tmp_path / "sudo.log"),
    }
    for tool in ("sudo", "gh", "id", "getent", "systemctl", "curl", "sha256sum", "shellcheck"):
        target = binaries / tool
        target.write_text(f"#!{sys.executable}\n{STUB}")
        target.chmod(0o755)
    result = subprocess.run(
        ["bash", str(installer), *(profile.args if args is None else args)],
        env={**os.environ, "PATH": f"{binaries}:/usr/bin:/bin", "CI_INSTALL_FIXTURE": json.dumps(fixture)},
        capture_output=True, text=True, check=False,
    )
    return result, mutation, gh_log


def sudo_calls(tmp_path):
    log = tmp_path / "sudo.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


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
        "group-member": ([p.dedicated], [f"{p.user}:x:{p.uid}:alice"], p.uid),
        "malformed-uid": ([p.account(uid="invalid")], [p.group], p.uid),
    }


@PROFILES
@pytest.mark.parametrize("case", list(refusal_cases(DEFAULT)))
def test_existing_runner_identity_is_refused_before_any_host_mutation(tmp_path, profile, case):
    passwd, group, groups = refusal_cases(profile)[case]

    result, mutation, _ = run_installer(tmp_path, profile, passwd, group, groups)

    assert result.returncode == 2, result.stderr
    assert not mutation.exists()


@pytest.mark.parametrize("args", [["unknown"], [""], ["default", "ballast"], ["BALLAST"]])
def test_unknown_or_extra_profile_arguments_are_refused_before_any_work(tmp_path, args):
    result, mutation, gh_log = run_installer(
        tmp_path, DEFAULT, [DEFAULT.dedicated], [DEFAULT.group], DEFAULT.uid, args=args,
    )

    assert result.returncode == 2
    assert not mutation.exists()
    assert not gh_log.exists()


def profile_values(name):
    """Evaluate the installer's own profile table, the single source of truth."""
    script = INSTALLER.read_text()
    block = script[script.index("# BEGIN PROFILES"):script.index("# END PROFILES")]
    fields = "repository runner_user runner_home runner_name runner_label service unit_file runner_dir"
    code = f'{block}\nload_profile "$1"\nfor f in {fields}; do printf "%s=%s\\n" "$f" "${{!f}}"; done'
    out = subprocess.run(["bash", "-c", code, "_", name], capture_output=True, text=True, check=True).stdout
    return dict(line.split("=", 1) for line in out.splitlines())


def test_profiles_share_no_account_home_service_name_label_unit_or_repository():
    default, ballast = profile_values("default"), profile_values("ballast")

    for field in default:
        assert default[field] != ballast[field], field
    assert not ballast["runner_home"].startswith(default["runner_home"])
    assert not default["runner_home"].startswith(ballast["runner_home"])


def unit_directives(path):
    return [line for line in path.read_text().splitlines() if line and not line.startswith("#")]


def test_ballast_unit_differs_from_the_hardened_template_only_in_declared_limits():
    template = unit_directives(SYSTEMD / "github-actions-runner.service")
    ballast = unit_directives(SYSTEMD / "github-actions-runner-ballast.service")
    renamed = [
        line.replace("issue-agent-ci", "ballast-ci").replace("autonomous-issue-agent", "xnmp/ballast")
        for line in template
    ]

    assert set(ballast) - set(renamed) == {"CPUQuota=1200%", "MemoryMax=24G", "TasksMax=2048"}
    assert {line.split("=")[0] for line in set(renamed) - set(ballast)} == {"CPUQuota", "MemoryMax", "TasksMax"}
    assert len(ballast) == len(renamed)


@PROFILES
def test_each_unit_runs_as_its_profile_account_and_confines_writes_to_its_home(profile):
    values = profile_values(profile.name)
    unit = dict(line.split("=", 1) for line in unit_directives(SYSTEMD / values["unit_file"]) if "=" in line)

    assert unit["User"] == unit["Group"] == values["runner_user"]
    assert unit["ReadWritePaths"] == values["runner_home"]
    assert unit["WorkingDirectory"] == values["runner_dir"]
    assert unit["ExecStart"] == f"{values['runner_dir']}/runsvc.sh"
    assert "SupplementaryGroups" not in unit
    assert (unit["ProtectSystem"], unit["NoNewPrivileges"], unit["MemorySwapMax"]) == ("strict", "true", "0")


@PROFILES
def test_fresh_install_registers_and_extracts_as_the_runner_user_with_profile_values(tmp_path, profile):
    result, _, gh_log = run_installer(tmp_path, profile, [profile.dedicated], [profile.group], profile.uid, proceed="fresh")
    calls = sudo_calls(tmp_path)
    runner_dir = f"{profile.home}/runner"

    assert result.returncode == 0, result.stderr
    config = next(c for c in calls if any("config.sh" in arg for arg in c))
    assert config[:3] == ["-n", "-u", profile.user]
    # Values arrive as positional arguments after the script, never inside it.
    assert config[-4:] == [runner_dir, f"https://github.com/{profile.repository}", profile.user, profile.user]
    script = next(arg for arg in config if "config.sh" in arg)
    assert profile.user not in script and profile.repository not in script
    extract = next(c for c in calls if "tar" in c)
    assert extract[:3] == ["-n", "-u", profile.user]
    assert extract[-4:] == ["-xzf", "-", "-C", runner_dir]
    assert "SECRET-TOKEN" not in result.stdout + result.stderr + json.dumps(calls)
    assert f"repos/{profile.repository}/actions/runners/registration-token" in gh_log.read_text()


@PROFILES
def test_root_names_no_path_beneath_the_runner_home(tmp_path, profile):
    run_installer(tmp_path, profile, [profile.dedicated], [profile.group], profile.uid, proceed="fresh")

    root_calls = [c for c in sudo_calls(tmp_path) if c[:2] != ["-n", "-u"]]
    assert root_calls
    for call in root_calls:
        assert not any(arg.startswith(f"{profile.home}/") for arg in call), call


def test_installer_source_never_passes_a_runner_dir_path_to_root_sudo():
    script = INSTALLER.read_text()
    root_lines = [
        line for line in script.splitlines()
        if "sudo -n " in line and 'sudo -n -u "$runner_user"' not in line and not line.lstrip().startswith("#")
    ]

    assert root_lines
    for line in root_lines:
        assert "$runner_dir" not in line, line
    assert "chown -R" not in script


def test_runner_name_lookup_binds_the_name_with_jq_arg_not_interpolation():
    script = INSTALLER.read_text()

    assert "jq --arg name" in script
    assert 'select(.name==\\"' not in script


@PROFILES
def test_reinstall_of_a_configured_runner_reuses_registration_and_restarts_the_service(tmp_path, profile):
    result, _, gh_log = run_installer(
        tmp_path, profile, [profile.dedicated], [profile.group], profile.uid, proceed="configured", loaded=True,
    )
    calls = sudo_calls(tmp_path)

    assert result.returncode == 0, result.stderr
    assert not any("config.sh" in arg for c in calls for arg in c)
    assert not any("tar" in c for c in calls)
    assert "registration-token" not in gh_log.read_text()
    assert ["-n", "systemctl", "restart", profile.service] in calls


@PROFILES
def test_a_configured_runner_registered_to_another_repository_is_refused(tmp_path, profile):
    result, _, _ = run_installer(
        tmp_path, profile, [profile.dedicated], [profile.group], profile.uid,
        proceed="configured", registered_to="someone/else",
    )

    assert result.returncode == 2
    assert not any(c[:3] == ["-n", "systemctl", "restart"] for c in sudo_calls(tmp_path))


@PROFILES
def test_a_loaded_service_is_stopped_before_any_runner_uid_step(tmp_path, profile):
    run_installer(tmp_path, profile, [profile.dedicated], [profile.group], profile.uid, proceed="configured", loaded=True)
    calls = sudo_calls(tmp_path)

    stop = calls.index(["-n", "systemctl", "stop", profile.service])
    first_runner_step = next(i for i, c in enumerate(calls) if c[:3] == ["-n", "-u", profile.user])
    assert stop < first_runner_step


@pytest.mark.skipif(
    not str(ROOT.resolve()).startswith(f"{DEFAULT.home}/"),
    reason="only a checkout inside the default runner home (the CI workspace) exercises this",
)
def test_a_checkout_inside_the_runner_home_is_refused_before_any_privileged_step(tmp_path):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for tool in ("sudo", "gh"):
        (binaries / tool).write_text("#!/bin/sh\necho called >> \"$0.log\"\nexit 99\n")
        (binaries / tool).chmod(0o755)
    result = subprocess.run(["bash", str(INSTALLER)], env={**os.environ, "PATH": f"{binaries}:/usr/bin:/bin"},
                            capture_output=True, text=True, check=False)
    assert result.returncode == 2
    assert "inside /var/lib/issue-agent-ci" in result.stderr
    assert not (binaries / "sudo.log").exists() and not (binaries / "gh.log").exists()
