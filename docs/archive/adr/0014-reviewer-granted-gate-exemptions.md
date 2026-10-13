# ADR 0014 — Reviewer-granted gate exemptions

Governs: src/issue_agent/domain/exemptions.py src/issue_agent/domain/gate.py

Status: Superseded by #325 (2026-10-12)
Date: 2026-08-03.

The test-modification grant chain and required-test gate have been removed.
Existing test edits are listed from the diff for ordinary reviewer judgment.
Red-fail, suite-green, push-time checks and test-first commit order remain.
The decision below is retained as historical context.

## Context

The publish gates deny by rule. Two incidents showed a rule being RIGHT in
general and WRONG for a specific change, with no path forward the harness
could take by itself:

- keifu #126: the fixer needed to change an existing test whose expectation
  was itself the bug. The tests-immutable gate denied every attempt; the leg
  burned budget and escalated with a human question ("how do I comply?").
- The same shape recurs for required-test on genuinely untestable changes
  (pure config, tooling glue).

PR #134 (rejected) tried self-service bypass: the fixer both requested and
effectively authorized its own exemption. Its review produced the split this
ADR adopts.

## Decision

One general mechanism in `domain/exemptions.py`, split into two artifacts
with different authors and different powers:

- **Declaration** — how a change requests an exemption. Revised 2026-08-03
  (operator directive: the trailer ceremony produced comply-how loops —
  keifu #126/#163 — with no security value, since the grant is what
  authorizes):
  - `tests-immutable` is **AUTO-DECLARED from the diff**: every modified or
    deleted existing test file IS the request. The gate only bounds scope
    (>7 such files denies as a rewrite; raised from 3 after tauri #589,
    2026-08-09, where four mechanical spec-path updates from one shared
    interaction change hit the wall — 7 keeps the per-rule caps summing
    below the total declaration cap of 8, so a change at every bound still
    fits the reviewer grant schema. Guardrail to watch: grants per merged
    PR and reviewer refusal rate — a single sloppy APPROVE now mints up to
    7 test-file grants in one round) and refuses paths that cannot be
    written as a grant subject; the fixer edits tests on its own judgment
    and the machinery frames the hazard for review.

    Because absence of a derived subject now MEANS "no grant needed", the
    derivation must never under-report. Subjects come from `diff --git`
    records — emitted unconditionally for every changed path — and NOT from
    `--- a/` hunk headers, which git omits for binary files, pure renames,
    mode-only changes, and paths suppressed by `.gitattributes -diff`: one
    `tests/** -diff` line would otherwise have disabled the whole regime
    and blinded the reviewer's own diff (round-4 adversarial review). A
    rename reports its OLD path (renaming a test out of the tree removes it
    from the suite). A record whose path cannot be resolved yields an
    UNDECIDABLE count that withholds and escalates — never "touched no
    tests". The gate derives from the worktree instead of a diff blob and
    can disagree at the margins; both directions are safe by construction
    (gate stricter → denied pre-publish; merge stricter → withheld with a
    named reason), and the shell twin uses the same old-side derivation.
  - `required-test` stays a `Gate-Exemption: required-test *` commit
    trailer: absence of tests is ambiguous between "forgot" and "genuinely
    untestable", so the fixer's stated intent carries information the diff
    cannot, and the free gate denial remains the cheapest teacher.
  A declaration authorizes nothing either way.
- **Grant** (REVIEW_BOT only): the reviewer grants via a STRUCTURED output
  field (`exemption_grants: [{rule, subject}]` in the review schema), never
  via prose — a first cut that asked the model to copy grant lines into its
  body was defeated in adversarial review by fence-quoting echoes
  (string-matching authority out of free text is unfixably fragile). The
  schema makes bad grants unrepresentable: `exemption_grants` exists only in
  the schema variant used when the PR declares exemptions, and its items are
  an object-level enum over exactly the declared {rule, subject} pairs — an
  undeclared rule or cross-matched subject cannot be generated, only
  refused-by-omission (`review_schema_with_exemptions`). The harness
  additionally validates each entry against the declarations (verbatim
  match, APPROVE verdict only; belt-and-braces for runtimes that enforce
  schemas loosely) and itself renders the canonical line into the review
  body — `gate-exemption: granted rule=<r> issue=<n> subject=<s> patch=<id>
  mac=<hmac>`. The patch id (rebase-invariant `patch_signature`) proves
  FRESHNESS only — it is a deterministic hash of the public diff, so the
  fixer can compute it (round-3 adversarial review, BLOCKER 1). Authorship
  is the `mac`: HMAC-SHA256 over rule|issue|subject|patch under a
  harness-held key (explicit `EXEMPTION_MAC_KEY`, else derived one-way from
  `REVIEW_BOT_TOKEN`, which both daemons already share via the env
  snapshot). The key is never placed in any agent environment or prompt;
  the HMAC is computed and verified harness-side only, so a grant line
  composed in prose — by a prompt-injected reviewer or anyone writing on
  the PR — cannot carry a valid tag. No key configured → no grant can be
  minted or verified; declared PRs escalate with that reason. Body parsing
  on the merge side is serialization between harness components; its
  hardening (column 0, outside any CommonMark fence with real open/close
  matching where a closing fence must be bare, APPROVE bodies only,
  authorship authenticated with `login_eq` against REVIEW_BOT before
  reaching the domain) stays as defense in depth. A grant from any other
  author, including the operator, is dropped (a human override is a hand
  merge, outside the harness). Subjects are a bounded path-ish charset
  (`[A-Za-z0-9._/+-]{1,200}` or `*`), never arbitrary tokens (they reach
  the reviewer prompt); a declaration whose subject falls outside it (e.g.
  `@scope/pkg`) cannot be declared — the gate denial names the malformed
  trailer so the limitation teaches instead of looping. Total declarations
  per change are capped at 8 at parse (prompt/schema inflation bound);
  excess is dropped, which fails closed — the undeclared violation is
  still denied by its gate.

Enforcement sits at MERGE: `should_automerge` withholds until every
declaration carries a matching authenticated grant against the CURRENT patch
id. Grants bind to PATCH CONTENT: a push that changes the net diff voids
them, and a mechanical rebase (same patch id) carries them forward — the
same invariant as the re-review skip. (Corollary: a head whose diff returns
to previously-granted content re-validates the old grant; a reviewer
refusal does not revoke an earlier grant for identical content. Accepted:
the reviewer granted exactly that patch.) The merge-side verdict is
three-valued:

- PERMIT — no declarations, or all granted with valid MACs;
- WITHHELD (durable for this head — ungranted, scope exceeded, no
  three-identity trust anchor, issueless branch, no MAC key configured, or
  a >100-commit branch whose declarations cannot be verified at all):
  TERMINAL, since the reviewer already reviewed this head and nothing
  dispatches another round, so `decide_fix` pairs it with a ONE-SHOT
  escalation to needs-human-review stating the actual reason — never a
  silent stall. The escalation fires only where auto-merge itself is in
  scope (AUTO_MERGE on, harness-authored branch, three-identity, CLEAN):
  where the harness would not have merged anyway, "withheld" is not what
  stands between the PR and a merge.
- UNVERIFIABLE (transient — commit messages unreadable this tick, patch id
  uncomputable this tick): withhold and RETRY next tick, with the cause
  logged by the collector. Escalating these would hand a human a healthy
  PR with a false "reviewer refused" reason.

**MULTI-mode is incompatible with this design and must not be enabled until
its synthesis carries a grant channel.** A PR whose exemption cannot be
granted (no notice, no structured field) escalates via the withheld arm. Under
the trailer design that hit only opt-in declarations — rare. Under
auto-declaration it hits EVERY PR that modifies any existing test, which is a
large share of them, so `REVIEW_MODE=multi` would convert routine test
corrections into a stream of human interrupts. The same applies to any
reviewer runtime whose task carries no structured verdict schema. SINGLE and
DELEGATED modes are the supported configurations.

**Which rules are exemptable is a domain decision, made once:**

- Exemptable (quality rules, reviewer judgment is the right authority):
  `tests-immutable` (max 7 declared files), `required-test` (whole-change
  subject `*` only).
- Never exemptable (security / trust-boundary rules): `.harness/` repo
  content, non-image `evidence/`, broker provenance and force-push fences,
  identity rules, and the test-EXECUTION gates (red-must-fail, suite-green).
  A reviewer grant is an agent-readable artifact in the trust domain the
  fixer operates in; security fences are not negotiable there, and waiving
  "tests pass" trades the recurrence guardrail for convenience — an operator
  call, not a review verdict.

Declarations naming non-exemptable or unknown rules are inert everywhere
(dropped at parse), so they can neither unlock a gate nor wedge a merge.

Preconditions, all failing closed toward "no exemption available":
IDENTITY_MODE=three with distinct fix/review identities (never degrade to
self-authorization); resolvable issue number; computable patch id; per-rule
scope caps. The shell gate stack (`plugin/hooks/checks/`) carries a lockstep
twin, covered by the parity goldens.

## Consequences

- The #126 class resolves inside the loop: declare → reviewer judges the
  visible edit → grant or refuse → merge proceeds or escalates on the normal
  ceiling. No operator intervention, no target-repo edits by hand.
- Adding an exemptable rule is a one-line classification change plus a gate
  check consulting its declared subjects — the grammar, merge rule, reviewer
  notice, and collection pipeline are shared.
- New guardrail to watch: grants per merged PR and refusal rate. A reviewer
  that rubber-stamps grants converts the tests-immutable gate into noise;
  the review prompt frames DEFAULT IS REFUSE, and the metric adjudicates.
