#!/usr/bin/env bash
# One native runner for this private repository; registration stays in GitHub.
set -euo pipefail

repository=xnmp/autonomous-issue-agent
runner_user=issue-agent-ci
runner_home=/var/lib/issue-agent-ci
runner_dir=$runner_home/runner
runner_name=issue-agent-ci
service=issue-agent-ci-runner.service
root=$(cd "$(dirname "$0")/.." && pwd)

for tool in awk bash curl gh git jq make sha256sum sudo tar xz; do
    command -v "$tool" >/dev/null || { echo "Missing prerequisite: $tool" >&2; exit 2; }
done
[[ $(id -u) != 0 ]] || { echo "Run as the GitHub-authenticated operator, not root." >&2; exit 2; }
sudo -n true || { echo "Authenticate with sudo -v, then rerun." >&2; exit 2; }
[[ $(gh api "repos/$repository" --jq .private) == true ]] || {
    echo "Refusing a native runner for a public repository." >&2; exit 2;
}

if getent passwd "$runner_user" >/dev/null; then
    account=$(getent passwd "$runner_user")
    runner_uid=$(cut -d: -f3 <<< "$account")
    runner_gid=$(cut -d: -f4 <<< "$account")
    [[ $runner_uid =~ ^[0-9]+$ && $runner_gid =~ ^[0-9]+$ ]] || exit 2
    # Keyed getent returns only the first match, so aliases need enumeration.
    passwd_records=$(getent passwd)
    group_records=$(getent group)
    [[ $runner_uid -gt 0 && $runner_uid != "$(id -u)" && $runner_gid -gt 0 &&
       $(awk -F: -v id="$runner_uid" '$3 == id {print $1}' <<< "$passwd_records") == "$runner_user" &&
       $(awk -F: -v name="$runner_user" '$1 == name {print $3}' <<< "$passwd_records") == "$runner_uid" &&
       $(awk -F: -v id="$runner_gid" '$3 == id {print $1}' <<< "$group_records") == "$runner_user" &&
       $(awk -F: -v name="$runner_user" '$1 == name {print $3}' <<< "$group_records") == "$runner_gid" &&
       $(cut -d: -f6 <<< "$account") == "$runner_home" &&
       $(cut -d: -f7 <<< "$account") == /usr/bin/nologin &&
       $(id -G "$runner_user") == "$runner_gid" ]] || {
        echo "Refusing an existing account without the dedicated unprivileged identity." >&2; exit 2;
    }
else
    sudo -n useradd --system --user-group --home-dir "$runner_home" --shell /usr/bin/nologin "$runner_user"
fi
for path in "$runner_home" "$runner_dir"; do
    sudo -n test ! -L "$path" || { echo "Refusing a symlinked runner directory." >&2; exit 2; }
    if sudo -n test -e "$path"; then
        sudo -n test -d "$path" || exit 2
    fi
done
sudo -n install -d -m 0700 -o "$runner_user" -g "$runner_user" "$runner_home" "$runner_dir"

scratch=$(mktemp -d)
trap 'rm -rf -- "$scratch"' EXIT
if ! sudo -n test -f "$runner_dir/.runner"; then
    [[ $(sudo -n find "$runner_dir" -mindepth 1 -maxdepth 1 -print -quit) == "" ]] || {
        echo "Unconfigured runner directory is not empty; inspect it before installing." >&2; exit 2;
    }
    [[ $(gh api "repos/$repository/actions/runners" --jq "[.runners[] | select(.name==\"$runner_name\")] | length") == 0 ]] || {
        echo "An existing GitHub runner uses this name; inspect it before replacing." >&2; exit 2;
    }
    package=$(gh api "repos/$repository/actions/runners/downloads" --jq '.[] | select(.os=="linux" and .architecture=="x64")')
    url=$(jq -er .download_url <<< "$package")
    checksum=$(jq -er .sha256_checksum <<< "$package")
    [[ $url == https://github.com/actions/runner/releases/download/* && $checksum =~ ^[a-f0-9]{64}$ ]] || exit 2
    curl --fail --location --silent --show-error "$url" -o "$scratch/runner.tgz"
    printf '%s  %s\n' "$checksum" "$scratch/runner.tgz" | sha256sum --check --status
    sudo -n tar -xzf "$scratch/runner.tgz" -C "$runner_dir"
    sudo -n chown -R "$runner_user:$runner_user" "$runner_dir"
    token=$(gh api --method POST "repos/$repository/actions/runners/registration-token" --jq .token)
    # Official input transport removes this environment value after reading
    # it. stdin keeps the one-hour credential out of argv and shell traces.
    # config.sh records its PATH into .path, which runsvc.sh exports for jobs.
    printf '%s\n' "$token" | sudo -n -u "$runner_user" /usr/bin/env PATH=/usr/local/bin:/usr/bin:/bin /usr/bin/bash -euc '
        IFS= read -r ACTIONS_RUNNER_INPUT_TOKEN
        export ACTIONS_RUNNER_INPUT_TOKEN
        cd /var/lib/issue-agent-ci/runner
        ./config.sh --unattended --url https://github.com/xnmp/autonomous-issue-agent --name issue-agent-ci --labels issue-agent-ci --work _work
    '
    unset token
else
    sudo -n test ! -L "$runner_dir/.runner" || exit 2
    [[ $(sudo -n jq -er .gitHubUrl "$runner_dir/.runner") == "https://github.com/$repository" ]] || exit 2
fi

if ! command -v shellcheck >/dev/null; then
    package=$(gh api repos/koalaman/shellcheck/releases/latest --jq '.assets[] | select(.name | endswith(".linux.x86_64.tar.xz"))')
    url=$(jq -er .browser_download_url <<< "$package")
    checksum=$(jq -er .digest <<< "$package")
    [[ $url == https://github.com/koalaman/shellcheck/releases/download/* && $checksum =~ ^sha256:[a-f0-9]{64}$ ]] || exit 2
    curl --fail --location --silent --show-error "$url" -o "$scratch/shellcheck.txz"
    printf '%s  %s\n' "${checksum#sha256:}" "$scratch/shellcheck.txz" | sha256sum --check --status
    tar -xf "$scratch/shellcheck.txz" -C "$scratch"
    sudo -n install -m 0755 "$scratch"/shellcheck-*/shellcheck /usr/local/bin/shellcheck
fi

# Official svc.sh install copies bin/runsvc.sh to the root of the runner; do
# that copy as the runner user instead of running runner-writable svc.sh as root.
sudo -n -u "$runner_user" /usr/bin/bash -euc '
    cd /var/lib/issue-agent-ci/runner
    [[ ! -L bin && -f bin/runsvc.sh && ! -L bin/runsvc.sh ]] || exit 1
    [[ ! -L runsvc.sh && ( ! -e runsvc.sh || -f runsvc.sh ) ]] || exit 1
    install -m 0755 bin/runsvc.sh runsvc.sh
    [[ -f runsvc.sh && ! -L runsvc.sh && -x runsvc.sh ]] || exit 1
' || { echo "Refusing an unsafe runner service entrypoint." >&2; exit 2; }

# Root installs the operator-owned unit; runner-owned scripts never run as root.
sudo -n test ! -L "/etc/systemd/system/$service" || exit 2
sudo -n install -m 0644 "$root/ops/systemd/github-actions-runner.service" "/etc/systemd/system/$service"
sudo -n systemctl daemon-reload
[[ $(systemctl show "$service" -p User --value) == "$runner_user" &&
   $(systemctl show "$service" -p Group --value) == "$runner_user" &&
   $(systemctl show "$service" -p DropInPaths --value) == "" &&
   $(systemctl show "$service" -p FragmentPath --value) == "/etc/systemd/system/$service" &&
   $(systemctl show "$service" -p WorkingDirectory --value) == "$runner_dir" ]] || exit 2
[[ $(systemctl show "$service" -p ExecStart --value) == "{ path=$runner_dir/runsvc.sh ; argv[]=$runner_dir/runsvc.sh ;"* ]] || exit 2
sudo -n systemd-analyze verify "/etc/systemd/system/$service"
sudo -n systemctl enable "$service"
sudo -n systemctl restart "$service"
systemctl is-active --quiet "$service"
echo "Native CI runner active: $service"
