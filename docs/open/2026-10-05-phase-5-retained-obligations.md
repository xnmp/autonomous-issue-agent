# Proposed disposition of retained obligations

Status: prepared for a separate operator decision. The approved Phase 5 windows
do not authorize these accounting and command dispositions. All reported
dispositions so far occurred on private copies; live authority remains stopped
at schema 18. This proposal adds no recovery machinery.

Release capacity held by these three abandoned runs through the existing
`SQLiteControlStore.release` API:

| Run | Leg | Retained maximum |
| --- | --- | --- |
| `1cd6cf8bf1b344d4955ed3815a565821` | TRIAGE | USD 0.75 |
| `727ead9129d6444497d6303f00b57056` | GROOM | USD 2 |
| `4856475277024e529ebb5eb9728bead3` | RESOLVE | USD 20 |

The October 3 host boot proves the original local processes ended. Trusted
provider usage and settled charges remain unavailable. **USD 22.75 is reserved
maximum exposure, not measured spend.** Preserve the abandoned/dead-lease
outcomes, maxima, recovery policy, provider evidence and absence of charges.
Use an explicit operator reason describing unknown spend. The new disposition
timestamp records this operator action, not historical execution completion.
The current settlement API cannot later settle a released reservation normally;
accepting this accounting gap is the material decision. Do not record zero cost
or claim provider reconciliation. Historical exposure is outside the approved
USD 125 bound on new configured-card admissions.

Retire publication command
`a1280b335028132d66d5c0a530fd3d58185caed2a35c830e3314853f49f9de6a`
through the existing permanent-failure disposition. Its retained push stdout
records success; the expected commit belongs to the ancestry of merged PR #623.
Its checkout and branch no longer exist. Preserve its payload, executor and
one-attempt count. Record explicit exceptional retirement of an obsolete
command, without claiming retry-ceiling exhaustion or a new historical-success
receipt. Replaying or recreating the deleted branch is not authorized.

Before any live write, retain a coherent authority backup, witness and the
complete original receipt, obligation rows and provider artifacts. The verified
private archive was copied to durable, restricted evidence at
`/home/chong/.local/state/issue-agent-control/phase-5-recovery-evidence/20261005T003524Z/`.
Every original source still matched its archived hash at capture. The directory
also retains the copied recovery report, current-source all-family qualification
and frozen-source hashes; its manifest records each artifact. Live authority,
configuration and services were unchanged:

| Archived artifact | SHA-256 |
| --- | --- |
| `schema18-coherent.db` | `4449f2386de9b4bea57177d729b4eff7b00c3fd5af9bf6d1554c004aebaefe96` |
| `exact-original-obligations.json` | `db15d5a180757f81055f91e7209aaee7cb8cd8b92596eb6eb6436ab52630b8e3` |
| `original-file-hashes.json` | `f74029658c2af1381863d58292476502b83a03fcb0c3bbe647c5d0aeeddf0d03` |

The copied disposition preserved unrelated SQL rows and added five durable
events. Original legacy resolve histories remain exhausted. The subsequent
bounded continuation import assigns historical runs to sourced holds rather
than granting credit; canonical discovery must preserve those holds across
every family import. Final current-source private readback, original saved-plan
retry and compatible-code reopening passed independent review. The complete
gate passed on 2026-10-05 at 01:35:22 UTC in 15 minutes 30 seconds, with
10,529 domain/adapter/app cases, hook and parity checks, all four architecture
contracts and both type checks. All 22 frozen source/test hashes matched;
dashboard API coverage executed. This evidence qualifies the reviewed
implementation; no proposed live disposition has been applied.

After activation, rollback means keeping deployment stopped or reopening the
retained authority with compatible accepted code. Predecessor `74219a7` does
not understand deferred repair receipts and is incompatible. Do not downgrade
the database, reverse activation, run that predecessor or restore legacy file
authority. Approval of this proposal permits these four dispositions only;
the existing approved launch caps and window preconditions still apply.
