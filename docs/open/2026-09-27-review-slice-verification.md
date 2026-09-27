# Review slice — activation and acceptance evidence

Date: 2026-09-27. Continues the
[stopped handover](2026-09-27-review-slice-handover.md) at `84f83ac` and the
[review slice plan](2026-09-25-phase-3-review-slice-plan.md).

Status: **increment 8 and the complete review-rejection slice accepted** after
independent adversarial review and the final frozen full suite. Together with
the accepted resolve slice, this completes phase 3. No real workflow family
was activated and no services were started.

## Increment 8

- Operator-only `review-activation inventory`, `shadow`, `rehearse` and
  digest-confirmed `activate`; complete remote history, strict stopped-state
  parsing, deployment barriers and quiescence checks.
- Atomic epoch, imported episodes, rejection gates, retained decisions and
  journal event. Exact replay verifies the retained decisions against the
  original confirmed plan. Rehearsal changes an isolated copy only.
- Baselines require both valid markers and fresh corroborating bot history.
  Guidance is preserved verbatim. Complete exemption evidence preserves the
  runtime's non-resetting approval rule. Shadow includes adoption and the
  retained unproductive-round breaker.
- Held imports distinguish the harness's rejection-ceiling preamble from an
  independent ESCALATE verdict. An unknown hold is never converted into a
  re-armable rejection ceiling. Partial history cannot hide an unlabelled hold.
- Inactive review authority returns migration-required and buys no review.
  Fix-side guidance defers until review authority is available. Retired Store
  accessors, legacy review dispatch, review dismissal and dashboard reset
  choreography are deleted; stopped files remain import evidence.
- Operator commands validate epoch and episode within their acceptance
  transaction. Exact replay retains its original episode across a successful
  re-arm. Retry replay/stale checks precede the remote read.
- Partial admission failures release the batch's acquired claims. Local store
  failures no longer become remote departed-PR failures with hours of backoff.
- Review eligibility, operator hold facts, submission and trigger projection
  join the linked issue's current lifecycle/dependency holds and SQLite gates.
  Known unreadable linkage is distinct from an explicitly unlinked PR. Its
  evidence uses the existing durable backoff/gate mechanism. Admission fences
  same-head relinking and rechecks linked SQL holds in the spend transaction.

## Behavioural verification map

| Contract | Evidence |
| --- | --- |
| Baseline/guidance import, hold classification, malformed and partial input | `tests/domain/test_review_activation_plan.py` |
| Atomic activation rollback, retained-evidence integrity, quiescence, complete trailer reads | `tests/adapters/test_review_activation_store.py` |
| Shadow parity, grants, independent holds, verbatim guidance, immutable sources, exact replay | `tests/app/test_review_activation_app.py` |
| Real CLI composition, both families, watcher/heartbeat refusals, duplicate roots, isolated rehearsal | `tests/app/test_review_activation_cli.py` |
| Epoch/episode fencing, stale command retirement, retry read order | `tests/adapters/test_review_workflow_control.py` |
| Paid-once review, unchanged patch, fresh review after re-arm, dismissal races, crash boundaries, lost submissions, bounded uncertainty, independent holds | `tests/app/test_review_acceptance.py` |
| Truthful recorded decisions, transactional API results, local-store fault isolation, partial admission recovery | `tests/app/test_review_operator_api.py` |
| No unbound review path or legacy guidance fallback | `tests/app/test_review_workflow.py`, `tests/app/test_fix_workflow.py`, `tests/app/test_roles.py` |
| Resolve compatibility and retired-state retention | `tests/adapters/test_workflow_codec.py`, `tests/adapters/test_store.py`, resolve activation suites |
| Linked lifecycle/SQL holds, strict remote completeness, unavailable versus absent, paid-admission race, normal/authorized-retry effects | `tests/domain/test_review_linkage_policy.py`, `tests/adapters/test_review_linkage.py`, `tests/adapters/test_review_effects.py`, composed review/API suites |
| Dashboard migration state and removal of legacy review reset | review Vitest suites; existing Playwright review scenarios against the production app/bundle |

Independent review found retained import rows were not checked against the
original digest, partial unlabelled history could conceal a held ceiling, and
an explicit ESCALATE verdict at the threshold could be mistaken for a rejection
ceiling. All three received production fixes and behavioural regressions;
fresh re-review verified them.

A subsequent whole-slice pass reproduced a paid review of a parked linked
issue, exposing an unimplemented ADR 0015 contract. The correction joins that
issue into runtime/operator decisions and remote-effect guards. Cross-review
then caught incomplete remote responses being defaulted to clear, an
authorized-retry path omitting the new guard, guard reads outside the effect
authority span, and fresh linkage disagreeing with the captured dispatch claim.
Each was corrected and received a behavioural regression. Local authority
errors retain their classification rather than consuming remote-read retries.
Two independent reviewers found no remaining blockers after the fixes. Their
checks included composed paid-once/relinking outcomes, explicit absent versus
unavailable reads, current SQL gates, ordinary and authorized-retry effects,
and actual deployment-barrier exclusion across fresh guards and remote writes.

The final full run exposed a rehearsal test fixture with unclosed setup
connections: garbage collection checkpointed its committed WAL during the
source-byte comparison. A forced-GC reproducer isolated that checkpoint before
the read-only backup. Both review rehearsal tests now settle setup before
fingerprinting, following the existing resolve fixture. Independent review
confirmed that source immutability assertions remain intact and production
rehearsal opens the original only read-only; no production change was needed.

The strict dependency reader uses the paginated
[GitHub issue dependency endpoint](https://docs.github.com/en/rest/issues/issue-dependencies).
Unavailable pages or malformed lifecycle/linkage fields cannot prove absence.

Dashboard verification: 479 Vitest tests, 3 build tests and 21 Playwright tests
passed; TypeScript reported zero errors. Generated OpenAPI/client contracts
were regenerated. Playwright evidence artifacts were restored afterwards.

Final `make test`: **2,869 domain, 2,401 adapter and 2,861 app tests passed**;
123 hook tests, 13 governor tests, configuration/environment/parity checks,
all four import contracts and both Pyrefly passes also passed. Ten existing
strict-xfail residuals remained expected. Local run log:
`/tmp/review-inc8-final-full.log` (ephemeral; this document retains the result).

Deletion check: a production-tree `rg` for definitions and call sites of
`get/set_last_sha`, `get/set_last_patch_id`, `get/set/clear_operator_guidance`,
`stale_rejections`, `decide_review`, `_rearm_rejections`,
`_reset_review_baseline` and `_apply_operator_guidance` returned no matches.
`decide_review_rearm_reconciliation` is the distinct durable re-arm policy and
remains in use.

## Boundaries retained for later phases

Schema remains v24. Activation does not authorize rollback to a file writer;
rollback requires stopped, reconciled authority under ADR 0015. Operate resolve
and review activation together as described in `HARNESS.md`.

Phase 4 still owns verdict-escalation release, the distinct unproductive-review
breaker, and remaining continuation/counter families. The fix-side UNVERIFIABLE
merge loop remains an explicit trigger-gated residual. Existing review marker,
head-move dismissal and contested-hold limits remain documented in
[residuals](residuals.md). Phase 5 live measurement remains separately withheld.
