# Hook fixture incident and recovery

Date: 2026-09-30 Australia/Sydney. Phase 4 and the architecture plan remain
active and unaccepted beyond their recorded gates.

## Incident

A full-suite retry started before its asynchronous scratch cleanup completed.
The cleanup removed the new scratch directory. `tests/test_hooks.sh` continued
after `mktemp` and directory changes failed, so fixtures initialized, committed,
branched and pushed against the invoking checkout and its real origin.

The invalid test run is `/tmp/phase4-checkpoint-publication-closed-connections-gate.log`.
It is not acceptance evidence. No intended foundation commit was made.

## Local recovery

The verified pre-incident local `dev` tip is
`c4404a28e922ab33177721449f95c02732878da7`. The authorized pending diff was saved
as `/tmp/phase4-authorized-pending-recovery.patch`, with 44 file digests in
`/tmp/phase4-authorized-pending-recovery-sha256.json`.

Local `dev` was restored to that tip and the patch applied. All 44 files matched
their saved SHA256 digests. The original author identity was restored to
`xnmp <chonw89@gmail.com>`. A forensic Git configuration copy remains at
`/tmp/phase4-incident-git-config`. Seven fixture-created local branches were
removed after checking exact tips and their incident-time creation reflogs.
Their complete history and the two accidental remote-tracking tips are saved
in the verified `/tmp/phase4-hook-fixture-incident.bundle`; they are not accepted
work. The original local
`commit.gpgsign` setting has not been established; fixtures set it to false.

All seven installed services were checked and remain inactive. No service
activation is authorized.

## Approved remote repair completed

Read-only remote verification found:

| Ref | Pre-incident evidence | Accidental tip |
| --- | --- | --- |
| `dev` | GitHub push event and remote-tracking reflog: `0bf3ba8bd92ede680e3b4cdf1e04324fb5e6ee39` | `af920c248706e8a19f9c7ee65855a8c51804153c` |
| `fix/7-token` | No earlier local remote-tracking record; fixture created/published the branch | `fd9d43b2315b702daa9438ead2e9d80a235d2a49` |

The published fixture tip differs from the accepted local tip by `CHANGELOG.md`,
`seed.txt`, `tests/test_token_regression.py` and `token.py`. Pending foundation
changes were captured by later local fixture commits, after the observed pushes.

The user explicitly approved guarded rollback of both branches. The prepared
script rechecked the origin URL and both exact accidental tips, then used one
atomic push with explicit force-with-lease expectations. Read-only verification
confirmed `dev` at `0bf3ba8bd92ede680e3b4cdf1e04324fb5e6ee39` and no
`fix/7-token` ref. No accepted local work was pushed. The existing no-push
instruction remains in force for subsequent work.

A read-only GitHub repository event query independently confirmed the first
accidental push at `2026-09-30T02:13:30Z`: `refs/heads/dev` changed from
`0bf3ba8bd92ede680e3b4cdf1e04324fb5e6ee39` to
`b4f91545a104b37c374a31808f387200532090b1`. Thus the proposed `dev` rollback
target is established by server evidence, rather than only an old local reflog.
The query did not establish a prior remote `fix/7-token` state.

## Required prevention and next gate

Harden fixture allocation, directory changes and initialization to fail closed.
Exercise actual commands in isolated sentinel repositories and assert unchanged
HEAD, configuration, worktree, branches and remote refs after injected failures.
An independent reviewer must inspect the repair. Wait for scratch cleanup to
finish before creating a new scratch directory or starting a gate. Fence network
Git transport in offline test runs while allowing local fixture remotes.

Fixture hardening passed independent Sol/Luna review, eight isolated failure
regressions, Bash syntax and Pyrefly. The restarted full `make test` passed:
123 hook checks, 3,358 domain, 3,796 adapter and 2,909 app tests, parity,
all four architecture contracts and both Pyrefly checks. Log:
`/tmp/phase4-checkpoint-isolated-gate.log`. Its disk scratch is allocated outside
the source checkout, with no concurrent cleanup. Git transport is file-only,
global/system Git config is disabled, and the hook suite defaults to a
fail-closed `gh` shim unless a case supplies its own fixture implementation.

The previous valid full gate had 3,358 domain and 3,785 adapter tests passing, then
one app failure in read-only activation rehearsal. Explicit closure of owned
control-store SQLite connections fixes the observed delayed WAL checkpoint;
all 21 activation CLI tests and its new connection lifecycle regression passed.
Focused retention/recovery checks also passed. This final complete gate supplies
the required evidence for the intended local checkpoint; no remote publication
or live activation is authorized.

An additional independent source-read trace found that lease changes were not
covered by the archive lineage witness. Schema 32 now witnesses every lease
INSERT/UPDATE/DELETE. A real paid checkpoint test first completes certification,
adds a retained lease, observes the next audit blocking admission, then removes
the lease and requires fresh recertification. It passes normally and fails at
the intended assertion when only the lease witness triggers are made inert.
The archive admission/source-version suites pass with this change.

A fresh complete-diff Luna review then reproduced another fixture isolation gap:
ambient `GIT_DIR` could redirect fixture initialization/configuration into a
sentinel repository even when the working directory was isolated. The repair
must scrub repository and configuration selectors before the first Git call,
with exported-selector regressions. The preceding complete gate remains evidence
for its exact tree; the final repaired tree requires another complete gate before
its intended local commit.

The selector repair now clears every inherited `GIT_*` variable before the first
Git call and reinstates only file transport and isolated global/system config.
Three real-command selector regression groups cover repository/index, object and
configuration overrides; all eleven isolation cases pass. Final independent
review cleared the repair. The repaired-tree full `make test` passed: 3,358
domain, 3,799 adapter and 2,924 app tests, 123 hook checks, parity, four
architecture contracts and both Pyrefly checks. All seven services remain
inactive; the checkpoint is local and no accepted work is pushed.
