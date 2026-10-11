# Native repository CI

This private repository runs its existing GitHub Actions checks on one local
Linux x64 runner with the `issue-agent-ci` label. GitHub still owns job dispatch
and the `test` check result. Local test output never substitutes for that result.
Self-hosted execution does not consume GitHub-hosted runner minutes; artifact
storage retains GitHub's normal allowance and billing rules.

The runner uses the dedicated `issue-agent-ci` system account, with no extra
groups, home and checkout under `/var/lib/issue-agent-ci`, and no access to the
operator's home. The tracked [systemd unit](../../ops/systemd/github-actions-runner.service)
uses GitHub's documented `runsvc.sh` entrypoint: one job at a time,
six CPU equivalents, 12 GB memory, no swap, private `/tmp`, and a private runtime
directory for the existing test scratch wrapper. No VM or container is required.
This is for trusted private-repository code; it is not an arbitrary-code sandbox.

## Install

The operator needs repository administration access through `gh`, administrator
access to this host, and `bash`, `curl`, `git`, `jq`, `make`, `tar`, `xz` and
`sha256sum`. The host must support GitHub's Linux runner binaries. The installer
downloads the official runner and missing ShellCheck binary, verifies their
published SHA256 digests, and installs the tracked runner service. Runner
credentials remain in its private home, never in the repository.

```bash
sudo -v
bash bin/install-ci-runner.sh [default|ballast]
gh api repos/xnmp/autonomous-issue-agent/actions/runners \
  --jq '.runners[] | {name,status,busy,labels:[.labels[].name]}'
```

Run installation while the runner is idle: rerunning installation restarts the
service. Existing mismatched accounts, privileged supplementary groups, public
repositories and conflicting remote registrations refuse rather than being
replaced. GitHub's runner application updates itself; the host's OS and tools
remain the operator's responsibility. A powered-off host leaves jobs queued.

An interrupted installation resumes on rerun: an existing matching account and
`.runner` registration are reused rather than registered again. The installer's
download scratch honours `TMPDIR`; point it at disk-backed storage when the
operator's `/tmp` is small or quota-limited, as the first installation needed
after `curl: (23)` from a full `/tmp`.

## Ballast profile

`bin/install-ci-runner.sh ballast` installs a second, independent runner for the
private repository `xnmp/ballast`, whose hosted Actions minutes are exhausted.
Omitting the argument (or `default`) installs the runner above, unchanged. The
two profiles share no account, home, `_work` directory, registration, runner
name, label, service or unit, so caches and credentials cannot cross repositories.

| | default | ballast |
| --- | --- | --- |
| Repository | `xnmp/autonomous-issue-agent` | `xnmp/ballast` |
| Account / home | `issue-agent-ci`, `/var/lib/issue-agent-ci` | `ballast-ci`, `/var/lib/ballast-ci` |
| Runner name / label | `issue-agent-ci` | `ballast-ci` |
| Service | `issue-agent-ci-runner.service` | `ballast-ci-runner.service` |
| Unit | `github-actions-runner.service` | `github-actions-runner-ballast.service` |
| Limits | 6 CPU, 12 GB, 512 tasks | `CPUQuota=1200%`, `MemoryMax=24G`, `TasksMax=2048` |

The Ballast unit is the hardened template with only those limits raised (Android
builds plus an x86_64 emulator). It grants no supplementary groups, and the
installer asserts the installed unit has none. Upstream systemd's udev rules
(`50-udev-default.rules`, shipped by Arch and Debian) make `/dev/kvm` mode
`0666`, so the `kvm` group adds nothing for KVM and would also expose
`/dev/udmabuf` (`0660 root:kvm`). If a host's `/dev/kvm` is not world-readable
and writable, KVM is unavailable to the runner; that is a deliberate operator
decision, not something the installer overrides. Every identity,
public-repository and foreign-registration refusal applies per profile. Root
operates only on the runner home; the runner tree is extracted and inspected as
the runner user, since jobs can write there.

Trust: the runner executes workflow code as `ballast-ci` with no sandbox. Use it
only for the private repository and trusted pull requests; never route fork PRs
to it. Recheck this if Ballast's visibility or contributor model changes.

Ballast's `native-test` needs host packages the operator installs; the runner
never installs them. On Debian/Ubuntu: `libwebkit2gtk-4.1-dev`,
`libayatana-appindicator3-dev`, `librsvg2-dev`, `patchelf`. On Arch:
`webkit2gtk-4.1`, `libayatana-appindicator`, `librsvg`, `patchelf`.

Pointing Ballast's PR jobs at the runner (`runs-on: [self-hosted, linux, x64,
ballast-ci]`) is a change in that repository.

## Workflow and operation

[CI](../../.github/workflows/ci.yml) validates preinstalled tools, installs its
own managed Python and Node, and gives Git an individual job configuration.
It retains the full `make test`, dashboard build and tests, and
`make test-deployment-env`. All validation uses the existing scratch/process
wrapper, so failure and cancellation clean up the test process group. The
job has a 30-minute timeout and a read-only repository token; checkout does
not persist that token as Git credentials.

Use ordinary systemd operations:

```bash
systemctl status issue-agent-ci-runner.service
journalctl -u issue-agent-ci-runner.service
sudo systemctl stop issue-agent-ci-runner.service
```

Stopping does not delete work, registration or credentials. To remove the runner,
stop and disable its service, remove the installed unit, and use the official
`config.sh remove` command as the runner account in its installation directory.
Confirm removal in GitHub. Keep
logs needed for incident evidence before removing files. The installer refuses
public repositories; recheck this trust assumption before changing visibility.
