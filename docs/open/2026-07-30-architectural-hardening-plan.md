# Architectural hardening plan — trusted control plane, durable accounting, typed events, and supervised processes

**Status:** Integrated into and implemented by
[the authoritative phased plan](2026-07-30-architectural-review-and-plan.md).
Its credential-free publisher remains trigger-gated exactly as that plan and
ADR 0001 specify.

**Date:** 2026-07-30

**Primary objective:** Make the control plane trustworthy under faults and hostile
inputs while preserving the existing pure decision model and incremental delivery.

## 1. Executive summary

The project has a strong core: domain decisions are mostly explicit and pure,
architecture constraints are enforced by import-linter, and the behavioral test
suite is broad. The principal long-term risk is not the decision model. It is that
several operational seams currently use best-effort files, agent-local hooks, and
free-form events as if they were authoritative state.

This plan separates four concepts that must not share failure semantics:

1. **Policy decisions** decide whether an operation should be admitted.
2. **Operational state** records reservations, runs, commands, and idempotency
   durably.
3. **Privileged effects** are executed only by a trusted component holding the
   required credentials.
4. **Telemetry** reports what happened but is never used as the sole authority
   for safety, accounting, or deduplication.

The work is deliberately phased. Early phases correct unsafe ambiguity and create
typed seams without changing behavior. Middle phases introduce durable accounting,
an external effect boundary, and versioned events. Later phases harden process
supervision and remove compatibility paths.

The intended end state is:

- an untrusted agent can propose code and effects but cannot publish them;
- every paid run has an atomic reservation and exactly one terminal accounting
  outcome;
- loss, truncation, or corruption of telemetry cannot disable caps or repeat a
  remote action;
- every privileged mutation has a durable command, an idempotency key, and a
  trusted receipt;
- every run has a stable identity and a terminal state;
- shutdown is a bounded state transition rather than an unstructured process exit;
- application ports express policy-relevant contracts without importing concrete
  adapters;
- legacy files and hooks remain compatibility aids only until their consumers are
  migrated.

## 2. Scope

### 2.1 In scope

- Budget admission, price representation, usage settlement, and cap semantics.
- Governor and gate authority, credential placement, evidence provenance, and
  privileged GitHub/git effects.
- Journal/event schemas, correlation, durability, deduplication, projections, and
  retention.
- Daemon signals, child containment, drain behavior, exit taxonomy, systemd, and
  launchd.
- Cross-cutting application boundaries, failure types, idempotency, configuration,
  observability, testing, documentation, and module ownership.

### 2.2 Non-goals

- Rewriting the domain decision model.
- Converting the entire application to event sourcing.
- Building a distributed, multi-host scheduler.
- Implementing a perfect shell parser as a security boundary.
- Adding Kubernetes as a prerequisite.
- Fetching live pricing on every admission. A versioned operator-managed rate card
  is sufficient.
- Replacing GitHub or CI as the authoritative source for remote repository state.
- Refactoring large modules merely to reduce line count. Splits must establish a
  meaningful ownership boundary.

## 3. Current baseline and problem statement

The following current behaviors drive this plan:

- Budget caps read a JSONL ledger whose writer and reader intentionally tolerate
  I/O failure. A missing or unreadable ledger can therefore appear as zero spend.
- Cap checks occur before dispatch, but charges are recorded after completion.
  Concurrent workers can all pass the same snapshot, and a single run can exceed
  the remaining cap.
- Provider-reported cost, configured list-price estimates, and learned estimates
  share the same `cost_usd` vocabulary.
- Daily caps can stop an entire daemon tick, including reconciliation, cleanup,
  reaping, recurrence handling, and other work that does not incur model cost.
- The governor is injected into an agent-writable worktree and includes documented
  fail-open and indirect-execution gaps. Agents commonly run with elevated tool
  permissions.
- Agent-written `.harness` files are treated as evidence for lifecycle decisions.
- Some privileged GitHub writes bypass the nominal executor boundary.
- Test commands that time out or fail because of infrastructure can be interpreted
  as a valid failing test.
- Journal events have no common versioned envelope, durable identity, producer
  identity, or required correlation identifiers.
- Journal tail scans are used for behavior such as deduplication and recovery even
  though retention truncates the visible history.
- The daemon does not own an explicit signal-to-drain lifecycle. Supervisors
  distinguish neither permanent configuration errors nor deliberate operator
  cancellation from transient failures.
- The nominal dependency layering passes import checks, but application wiring
  depends heavily on concrete adapters, and runtime concerns leak into domain task
  descriptions.

This plan treats these as architectural problems, not isolated bugs.

### 3.1 Validation baseline

At the review date:

- `make test` is green across governor, hooks, orchestration, point-at, domain,
  adapter, application, parity, and import-linter suites;
- five documented governor bypass cases remain strict expected failures;
- both current import-linter contracts pass;
- `uv run --extra dev pyrefly check src` reports no errors;
- repository-wide pyrefly checking is not yet a reliable CI contract and currently
  reports unresolved import roots, primarily in test/demo contexts.

The phases must preserve the green behavioral baseline while converting documented
expected failures into enforceable boundary tests or explicitly accepted residual
risks.

## 4. Non-negotiable invariants

All implementation choices and migration shortcuts must preserve these invariants.

### 4.1 Trust and effects

1. Inbound issue text, pull request content, repository content, model output,
   agent commands, agent environment variables, and agent-writable files are
   untrusted.
2. An agent-local hook is advisory defense in depth. It is not an authorization
   boundary.
3. The process that executes untrusted agent work does not hold credentials
   capable of pushing or mutating GitHub.
4. Every remote mutation is represented by a durable command before execution.
5. Only a trusted policy enforcement point executes a privileged command.
6. Evidence is authoritative only when its producer, scope, and integrity are
   known. Agent claims may initiate verification but cannot satisfy a gate.
7. Policy evaluation distinguishes `ALLOW`, `DENY`, and `INDETERMINATE`.
   Indeterminate security evidence never becomes allow.

### 4.2 Accounting

1. When a monetary or quota cap is armed, unavailable or corrupt accounting blocks
   new paid admission. It does not block free reconciliation and cleanup.
2. Admission is atomic with reservation. A read followed by a later write is not
   sufficient.
3. Each reservation reaches exactly one terminal state: settled or released.
4. A crashed process can be reconciled without double charging or silently losing
   committed spend.
5. Currency money uses an integer minor representation and an explicit currency.
6. Actual provider charges, configured estimates, and quota debits are distinct
   concepts with provenance.
7. Unknown models and missing rates have an explicit configured policy. They never
   silently inherit an unrelated fallback rate.

### 4.3 Operational state and events

1. Telemetry failure does not alter admission, authorization, deduplication, or
   lifecycle correctness.
2. Every dispatch and every agent run has a stable identifier.
3. A run has at most one terminal outcome.
4. Durable idempotency does not depend on a bounded log tail.
5. New event types are versioned, validated, and correlated.
6. Consumers tolerate unknown additive fields and reject incompatible schema
   versions explicitly.
7. Operational state may emit events through an outbox; events are not the only
   copy of operational state.

### 4.4 Process lifecycle

1. `SIGTERM` and `SIGINT` stop new admission before cancellation or drain begins.
2. Shutdown has a configured deadline and a deterministic escalation policy.
3. Operator cancellation is distinguishable from infrastructure failure.
4. Child processes cannot survive the daemon unintentionally.
5. Reservations and in-progress commands are recoverable after abrupt process
   death.
6. Permanent startup failures do not enter infinite supervisor restart loops.
7. Readiness means the daemon can perform useful work; liveness means its scheduler
   is making progress.

## 5. Target architecture

```text
                         trusted control plane

  operator config ──> composition root ───────────────────────────────┐
                                  │                                  │
                                  v                                  v
                         policy/admission service             lifecycle executor
                                  │                                  │
                                  v                                  v
                         durable control store <──outbox──> typed event sink
                         - runs                                  - JSONL export
                         - reservations                          - OTel export
                         - charges                               - dashboards
                         - commands
                         - receipts
                         - dedupe keys
                                  │
                                  v
                         untrusted runtime port
                         - no publish credentials
                         - isolated worktree
                         - produces commit/artifacts
                         - proposes effects
                                  │
                                  v
                         trusted verification
                         - authoritative tests
                         - patch/base validation
                         - policy decision
                                  │
                                  v
                         privileged effect broker
                         - push with lease
                         - create/update PR
                         - submit review
                         - trusted receipt
```

The design follows the policy decision point/policy enforcement point separation
described by [NIST SP 800-207](https://csrc.nist.gov/pubs/sp/800/207/final).
The project remains a single deployable application initially; these are code and
privilege boundaries, not a requirement for networked microservices. “Trusted
verification” means the harness, command selection, and receipt are controlled by
the control plane; the repository code being tested remains untrusted and executes
without publication credentials.

## 6. Trust model

| Asset or signal | Trust classification | Permitted use |
|---|---|---|
| Operator-owned configuration outside the worktree | Trusted input | Establish policy, credentials, caps, and deployment identity |
| Checked-in target repository policy | Conditionally trusted | Policy input only after operator opts into that repository and pins the trusted base revision |
| Issue, PR, review, and comment content | Untrusted | Classification and task context |
| Repository files at the agent-modified revision | Untrusted | Candidate implementation and claimed test instructions |
| Agent stdout, structured response, and tool calls | Untrusted claim | Trigger trusted validation; provide diagnostics |
| `.harness` and `.codex` files inside the worktree | Untrusted claim | UX hints and diagnostics only |
| Agent-local governor hook | Advisory | Reduce accidental misuse; never authorize publication |
| Trusted verifier result | Trusted evidence | Satisfy configured gates within its recorded scope |
| GitHub/CI result fetched by the trusted control plane | Authoritative external observation | Remote merge/review policy input |
| Durable control store | Authoritative local operational state | Admission, reservations, run state, command state, and deduplication |
| JSONL journal or telemetry backend | Non-authoritative telemetry | Investigation, metrics, audit export, and projections |
| Effect broker receipt | Trusted effect evidence | Confirm a privileged command's result |

An ADR must define how operator configuration and the trusted base revision are
protected in each deployment mode. Until the external effect boundary is live,
the existing restriction to trusted inbound repositories remains mandatory.

## 7. Canonical contracts

Phase 1 introduces these contracts before storage or runtime behavior changes.
Names are illustrative but their distinctions are required.

### 7.1 Pricing and accounting

```python
@dataclass(frozen=True)
class Money:
    currency: CurrencyCode
    micros: int


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


class PricingBasis(Enum):
    PROVIDER_REPORTED = "provider_reported"
    CONFIGURED_RATE_CARD = "configured_rate_card"
    LEARNED_ESTIMATE = "learned_estimate"


@dataclass(frozen=True)
class PriceQuote:
    model: str
    rate_card_id: str
    rate_card_version: str
    effective_at: datetime
    maximum_charge: Money


@dataclass(frozen=True)
class UsageCharge:
    run_id: RunId
    amount: Money
    basis: PricingBasis
    usage: TokenUsage | None
    rate_card_id: str | None
    provider_receipt_id: str | None


@dataclass(frozen=True)
class QuotaDebit:
    run_id: RunId
    quota: QuotaName
    units: int
    calculation_version: str
```

Money and quota are never added together. Dashboards may display them side by side
but must label their basis.

Budget scope makes reset semantics explicit:

```python
@dataclass(frozen=True)
class BudgetScope:
    repository: RepositoryRef
    dimension: BudgetDimension
    currency: CurrencyCode
    period_id: str
    period_start: datetime
    period_end: datetime
    period_timezone: ZoneInfo
```

Two scopes with different currencies, quota dimensions, or calendar windows cannot
share a balance. Period boundaries are calculated once from an immutable
configuration snapshot rather than by calling “today” independently in readers
and writers.

Required accounting operations:

```python
class SpendAccount(Protocol):
    def snapshot(self, scope: BudgetScope) -> AccountingResult: ...
    def reserve(self, request: ReservationRequest) -> ReservationResult: ...
    def settle(self, request: SettlementRequest) -> SettlementResult: ...
    def release(self, request: ReleaseRequest) -> ReleaseResult: ...
    def reconcile(self, owner: ProcessIdentity) -> ReconciliationResult: ...
```

Every result is typed. `UNAVAILABLE`, `CORRUPT`, `CONFLICT`, and `EXHAUSTED` are not
represented as zero values or empty collections.

### 7.2 Runs and dispatch

```python
@dataclass(frozen=True)
class RunIntent:
    dispatch_id: DispatchId
    run_id: RunId
    role: Role
    repository: RepositoryRef
    task: DomainTask
    budget_scope: BudgetScope
    maximum_charge: Money | None


@dataclass(frozen=True)
class RuntimePlan:
    run_id: RunId
    prompt: str
    response_schema: JsonSchema
    tool_policy: ToolPolicy
    working_directory: Path
    environment: Mapping[str, str]
    resume_token: ResumeToken | None
```

`RunIntent` belongs to the application/domain-facing boundary. `RuntimePlan` is an
application-to-adapter contract. Tool names, MCP configuration, environment
variables, SDK session identifiers, and working directories do not belong in the
domain task model.

### 7.3 Test and policy evidence

```python
class TestExecutionStatus(Enum):
    PASSED = "passed"
    TEST_FAILED = "test_failed"
    INFRA_ERROR = "infra_error"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class TestExecution:
    status: TestExecutionStatus
    command: tuple[str, ...]
    exit_code: int | None
    duration_ms: int
    stdout_digest: str
    stderr_digest: str
    executor_identity: str
    repository_revision: str


class PolicyDisposition(Enum):
    ALLOW = "allow"
    DENY = "deny"
    INDETERMINATE = "indeterminate"
```

Only `TEST_FAILED` is evidence that a red test was reproduced. Timeout, setup
failure, cancellation, and unavailable execution are not red-test evidence.

### 7.4 Lifecycle commands and effects

```python
@dataclass(frozen=True)
class LifecycleCommand:
    command_id: CommandId
    idempotency_key: str
    kind: CommandKind
    target: EffectTarget
    expected_revision: str | None
    payload: CommandPayload


@dataclass(frozen=True)
class EffectReceipt:
    command_id: CommandId
    executor_identity: str
    started_at: datetime
    completed_at: datetime
    status: EffectStatus
    remote_object_id: str | None
    remote_revision: str | None
    error: EffectError | None
```

All GitHub and git publication operations, including submitting reviews, use this
contract. Required handlers must never default to no-op.

### 7.5 Event envelope

```json
{
  "schema_version": 1,
  "event_id": "0198...",
  "type": "agent.run.completed.v1",
  "occurred_at": "2026-07-30T12:34:56.123456Z",
  "observed_at": "2026-07-30T12:34:56.125004Z",
  "source": {
    "deployment_id": "host-a:production",
    "repository": "owner/repo",
    "role": "fixer",
    "process_id": "..."
  },
  "subject": "run/0198...",
  "correlation": {
    "dispatch_id": "0198...",
    "run_id": "0198...",
    "action_id": null,
    "causation_id": "0198..."
  },
  "data": {}
}
```

The envelope should align with the useful semantics of the
[CloudEvents specification](https://github.com/cloudevents/spec/blob/main/cloudevents/spec.md)
and remain exportable to the
[OpenTelemetry log data model](https://opentelemetry.io/docs/specs/otel/logs/data-model/).
The internal representation should be typed dataclasses with an explicit codec
registry, not arbitrary `**fields`.

## 8. Cross-cutting design requirements and delivery principles

### 8.1 Identity and correlation

Generate stable identities at the boundary where work is accepted:

- deployment ID identifies one installed control plane;
- process ID identifies one daemon lifetime;
- dispatch ID identifies one admitted application decision;
- run ID identifies one provider invocation and its accounting lifecycle;
- action ID identifies one domain lifecycle action;
- command ID identifies one requested privileged effect;
- event ID identifies one emitted fact.

Identifiers are opaque and never derived from mutable display names. Every durable
record and event carries the smallest applicable set. Logs use structured context
rather than asking consumers to infer relationships from time or order.

### 8.2 Configuration snapshots and hot reload

Configuration is parsed and validated into one immutable snapshot with a digest or
version. Admission, runs, price quotes, policy decisions, and lifecycle commands
record the snapshot version that governed them.

Hot reload must:

1. load the candidate configuration without mutating live state;
2. validate the complete candidate, referenced secrets, rate cards, and storage
   compatibility;
3. atomically swap the snapshot between ticks;
4. let in-flight work finish under its original snapshot;
5. reject or drain on incompatible changes according to an explicit policy;
6. emit the old and new snapshot identities.

A partially loaded configuration must never leak into an active tick. Committing a
checkout change and restarting a daemon remains safe only when startup validation,
exit taxonomy, and rollback behavior are in place.

### 8.3 Time and period semantics

- Persist UTC instants as timezone-aware values.
- Inject a clock into period calculation, expiry, and recovery logic.
- Define budget windows using an explicit period ID, timezone, start, and end.
- Test daylight-saving transitions, leap days, clock movement, and boundary races.
- Use monotonic time for durations and deadlines; use wall time only for recorded
  instants and configured calendar windows.
- A rate quote is locked to a reservation; a later rate-card update does not
  rewrite settled history.

“Today” is not a storage key or accounting primitive.

### 8.4 Failure, retry, and idempotency

Every I/O boundary defines:

- retryable, permanent, indeterminate, and cancellation outcomes;
- maximum attempts and backoff;
- idempotency key and scope;
- timeout and cancellation propagation;
- what is committed before and after the call;
- recovery for “effect may have happened”;
- which failure is fail-open and which is fail-closed.

Retries are application policy, not hidden inside arbitrary adapters. Jitter and
backoff use injectable policies for deterministic tests.

Required degraded-mode behavior:

| Unavailable dependency | Required behavior |
|---|---|
| Telemetry sink/exporter | Continue within bounded buffering/drop policy; surface degradation |
| Accounting while a cap is armed | Block new paid admission; continue safe free work |
| Operational control store | Block new runs and all new mutations; permit read-only diagnostics and explicitly safe repair |
| Trusted verifier | Keep candidate artifact; do not publish gated effects |
| Remote evidence source | Return `INDETERMINATE` for affected policy; do not infer success |
| Effect broker | Keep durable commands pending; reconcile before retry |
| Provider runtime | Stop new provider dispatch; reconcile or cancel known active runs |
| Candidate configuration | Retain the last known-good snapshot; reject the candidate atomically |

“Free” means no budget debit; it does not automatically mean safe. A free
operation that needs unavailable idempotency or authorization state remains
blocked.

### 8.5 Secret and untrusted-data handling

- Secrets are represented as references until the narrow adapter needs their
  values.
- Secret values never enter domain objects, events, prompts, command payloads,
  exception strings, or test snapshots.
- Child environments are allowlists.
- Paths crossing a trust boundary are canonicalized and checked against an allowed
  root.
- Repository-controlled hooks, filters, attributes, submodules, symlinks, and
  configuration are treated as executable or redirecting input.
- Size, count, nesting, and output limits exist for patches, event payloads,
  provider responses, and command results.

### 8.6 Observability and privacy

- Metrics use bounded-cardinality labels; run, issue, PR, and command IDs belong in
  traces/logs, not metric labels.
- Logs and events carry a sensitivity classification where payloads may contain
  repository or user content.
- Redaction happens before persistence and export.
- Health reports distinguish ready, degraded, draining, and failed.
- Operator alerts identify the failed invariant and the safe recovery action.

### 8.7 Delivery principles

- Each phase ends in a coherent, deployable state.
- Compatibility adapters are preferred over simultaneous rewrites.
- New authority is introduced before old authority is removed.
- Dual writes are time-bounded and measured; they are not a permanent
  architecture.
- Feature flags have an owner, removal condition, and expiry issue.
- A migration must be reversible until its shadow comparison and canary gates pass.
- Domain decisions remain pure. I/O, clocks, processes, credentials, and storage
  stay behind application ports.
- Tests assert contracts and externally visible outcomes, including malformed,
  absent, huge, concurrent, and crash-recovery cases.
- Every phase updates the capability matrix and architecture documents before it
  closes.

## 9. Phase 0 — Correct the contract and contain immediate risk

**Goal:** Remove misleading claims and make current degraded behavior visible
without starting a large redesign.

### 9.1 Work items

#### P0.1 Record architectural decisions

Create accepted ADRs for:

1. Trust boundary and privileged effect broker.
2. Money, quota, cap, rate-card, and unknown-model semantics.
3. Durable local control store and concurrency model.
4. Event envelope, compatibility, and retention.
5. Authoritative gate evidence and trusted test execution.
6. Signal, drain, exit-code, and supervisor contract.
7. Domain intent versus provider runtime plan.

Each ADR must include rejected alternatives and migration consequences.

#### P0.2 Publish a capability and authority matrix

For every supported runtime and deployment mode, document:

- credentials visible to the daemon;
- credentials visible to agent child processes;
- outbound network reachability;
- available tools and sandbox mode;
- hook enforcement coverage;
- source of test evidence;
- source of merge and review evidence;
- supported supervisor features;
- authoritative storage location.

The matrix is executable documentation where possible. Add contract tests for
configured permissions and generated service definitions.

#### P0.3 Correct documentation drift

At minimum:

- stop claiming that every GitHub write already passes through the executor;
- distinguish lifecycle commands from direct gateway calls;
- correct stale runtime-limit and synchronous-fixer descriptions;
- document when `AGENTS.md` is generated in issue worktrees;
- correct claims that sandbox mode can never reach real GitHub;
- state clearly that current hooks are advisory and have known bypasses;
- label JSONL history as bounded telemetry, not durable control state.

#### P0.4 Make accounting degradation explicit

Before Phase 2, add a startup and pre-admission health check for armed caps:

- an absent ledger never silently means zero spend;
- first use requires an explicit trusted initialization step that atomically
  records an initialized-empty state;
- missing, uninitialized, unreadable, and malformed states are distinct;
- read/write failure produces an explicit accounting-unavailable result and a
  prominent event/log;
- new paid dispatches are blocked while accounting is unavailable;
- observation, reconciliation, reaping, recurrence handling, and cleanup continue;
- cap-disabled deployments retain current best-effort telemetry behavior.

This is temporary containment, not a substitute for atomic reservations.

#### P0.5 Harden generated deployment artifacts

- Resolve and record an absolute executable path during service installation.
- Fail installation when the configured target directory is absent or ambiguous.
- Assign stable deployment identities so two installations cannot collide.
- Document existing systemd and launchd restart behavior and its failure modes.

### 9.2 Tests and evidence

- Missing, unreadable, truncated, malformed, and unwritable ledger cases.
- Armed versus unarmed caps under each accounting failure.
- Free reconciliation still runs when paid admission is unavailable.
- Generated unit/plist snapshots use absolute paths and unique identities.
- Documentation links and capability matrix checks.

### 9.3 Exit criteria

- The seven ADRs are accepted.
- Current authority and degraded behavior are accurately documented.
- Armed caps cannot silently interpret an accounting I/O failure as zero spend.
- The existing full test suite remains green.
- No deployment is represented as isolated unless the capability test proves it.

## 10. Phase 1 — Introduce typed seams without changing storage

**Goal:** Establish stable application contracts and explicit failure semantics
before changing persistence or privilege boundaries.

### 10.1 Work items

#### P1.1 Introduce value objects

Add immutable types for:

- `Money`, `CurrencyCode`, `TokenUsage`, and `QuotaDebit`;
- `PriceQuote`, rate-card identity/version, and pricing basis;
- `RunId`, `DispatchId`, `ActionId`, and `CommandId`;
- `BudgetScope` and `ReservationRequest`;
- `RunIntent`, `RunOutcome`, and terminal reason;
- `TestExecution` and `PolicyDisposition`;
- `LifecycleCommand` and `EffectReceipt`;
- `EventEnvelope` and typed event payloads.

Construction must reject negative tokens, invalid currencies, integer overflow,
naive timestamps, missing identifiers, and impossible state combinations.

#### P1.2 Extract application ports

Define ports owned by the application layer:

- `PricingService`;
- `SpendAccount`;
- `RuntimePort`;
- `TrustedVerifier`;
- `LifecycleCommandStore`;
- `LifecycleEffectPort`;
- `OperationalRunStore`;
- `EventSink`;
- `ProcessClock` and ID generation where determinism matters.

Concrete JSONL, GitHub, Claude, Codex, subprocess, SQLite, and system clock types
remain in adapters or composition code.

#### P1.3 Split intent from runtime configuration

Replace infrastructure-rich domain task objects with:

1. a domain/application `RunIntent`; and
2. a provider-specific `RuntimePlan` created by application planning.

Prompt construction may use pure functions, but SDK tool names, response schemas,
MCP servers, `cwd`, environment variables, resume tokens, and provider-specific
permission modes stay outside the domain model.

#### P1.4 Make pricing provenance visible

Add compatibility adapters over the current configuration and journal:

- exact model lookup returns a versioned quote;
- unknown models follow the ADR-defined policy;
- provider-reported charge and estimated charge remain separate;
- synthetic list-price consumption becomes an explicitly named quota or estimate;
- dashboard fields label actual, estimated, and quota values separately.

Do not rename an estimate to “actual cost” for compatibility.

#### P1.5 Replace ambiguous failure values

Migrate call sites away from empty lists, zero spend, booleans, and broad exception
handling where they change policy:

- accounting: `AVAILABLE`, `EXHAUSTED`, `UNAVAILABLE`, `CORRUPT`;
- test execution: `PASSED`, `TEST_FAILED`, `INFRA_ERROR`, `TIMED_OUT`,
  `CANCELLED`;
- policy: `ALLOW`, `DENY`, `INDETERMINATE`;
- lifecycle effects: `SUCCEEDED`, `RETRYABLE_FAILURE`,
  `PERMANENT_FAILURE`, `UNKNOWN_OUTCOME`.

Telemetry remains allowed to fail open because it no longer supplies authority.

#### P1.6 Establish static checking as a repository contract

- Add an explicit pyrefly configuration that covers production and test roots.
- Resolve the current missing-import-root diagnostics rather than excluding broad
  directories.
- Run `uv run --extra dev pyrefly check` in CI.
- Keep import-linter contracts green and add an application-port boundary if the
  adopted module layout supports it.

### 10.2 Tests and evidence

- Property tests for money arithmetic, rounding, large token counts, and invalid
  values.
- Contract tests shared by current and future `SpendAccount` implementations.
- Unknown-model and stale-rate-card behavior.
- Test timeout and setup failure cannot satisfy a red-test gate.
- Runtime-provider contract tests prove provider settings do not re-enter domain
  objects.
- Static checks cover both `src` and tests.

### 10.3 Exit criteria

- New code uses typed contracts; no new policy-relevant `dict[str, Any]` payloads
  are introduced.
- Actual currency, estimate, and quota are distinguishable end to end.
- All policy-relevant failures are explicit.
- Existing JSONL storage remains supported through compatibility adapters.
- Behavior is otherwise unchanged and the full suite is green.

## 11. Phase 2 — Durable control state and atomic budget reservation

**Goal:** Make admission, run state, command state, and deduplication correct under
concurrency and crashes.

### 11.1 Storage decision

Use a local SQLite control database unless the Phase 0 ADR demonstrates a need for
a different embedded transactional store.

Required operating constraints:

- database on a local filesystem, not an unsupported network filesystem;
- WAL mode where deployment characteristics permit it;
- explicit busy timeout and bounded retries;
- short write transactions with no model, network, or subprocess work inside;
- schema migrations with monotonically increasing versions;
- startup integrity check and actionable corruption state;
- restrictive file permissions;
- documented backup, restore, and recovery commands.

SQLite is an operational state store, not a dumping ground for high-volume logs.

### 11.2 Initial schema

The exact DDL belongs in the storage ADR, but the logical schema must include:

- `schema_migrations`;
- `runs`;
- `run_attempts` if one logical run can invoke a provider more than once;
- `budget_reservations`;
- `charges`;
- `quota_debits`;
- `lifecycle_commands`;
- `effect_receipts`;
- `dedupe_keys`;
- `event_outbox`;
- `process_owners` or an equivalent lease/ownership record.

Required database constraints include:

- unique run, command, receipt, provider receipt, and idempotency identifiers;
- legal state values;
- non-negative money and quota units;
- reservation currency equal to its budget scope currency;
- one terminal run outcome;
- one terminal reservation transition;
- command-to-receipt referential integrity;
- unique dedupe key within its namespace and scope.

### 11.3 Work items

#### P2.1 Implement transactional reservation

Admission becomes one transaction:

1. Resolve the immutable configuration snapshot, explicit budget period, and
   locked price quote.
2. Load the applicable cap and current committed amount.
3. Reject unavailable, corrupt, expired, or currency-incompatible policy data.
4. Check `settled + active reservations + requested maximum <= cap`.
5. Insert a reservation bound to a new run ID, quote, period, and configuration
   version.
6. Return a durable reservation receipt.

The admission transaction does not start the agent.

Define maximum-charge semantics for:

- single runs;
- bounded provider retries;
- multi-review legs;
- escalation runs;
- learned estimates;
- providers that can report a charge above the quote.

A new reservation must never cause committed spend to exceed the configured cap.
If a provider can exceed the reserved maximum, the variance is recorded and
alerted; it does not retroactively falsify admission history.

#### P2.2 Implement settlement and release

- `settle` is idempotent by run ID and provider receipt ID.
- `release` is idempotent and records a reason.
- Settlement records the final charge and releases unused reserved capacity in the
  same transaction.
- Repeated settlement with identical data succeeds without a second charge.
- Repeated settlement with conflicting data returns a conflict and alerts.
- Currency conversion is out of scope unless an explicit conversion source and
  rate identity are recorded.
- Missing provider usage does not invent zero cost; it follows the configured
  estimate or unknown-charge policy with provenance.
- Lowering a cap below already committed spend preserves history and blocks new
  reservations; it does not rewrite or forcibly release active reservations.
- Updating a rate card affects new quotes only. Settlement of an estimated charge
  uses the rate card locked by the reservation.

#### P2.3 Implement crash reconciliation

Model crash points explicitly:

- reservation committed before child spawn;
- child spawned before run-start acknowledgement;
- provider completes before usage is persisted;
- command executes before its receipt is persisted;
- daemon dies during settlement;
- daemon dies during release.

Recovery uses stable run IDs, process ownership, provider receipts where
available, and idempotent operations. It must not rely on a short TTL alone.
Stale reservations are quarantined or released only under an ADR-defined,
auditable rule.

#### P2.4 Move behavioral deduplication to the control store

Replace tail-based “ever” queries with durable unique keys for:

- lifecycle actions;
- flake and recurrence actions;
- alert/comment suppression;
- grooming purchases;
- any other externally visible once-only behavior.

Each namespace documents its scope and retention. A dedupe key that may be reused
after a time window stores the window explicitly.

#### P2.5 Preserve free control-plane work

Budget exhaustion or accounting unavailability gates only operations that may
incur the relevant budget:

- observations continue;
- active run reconciliation continues;
- finished child reaping continues;
- command receipt reconciliation continues;
- cleanup and bounded telemetry export continue;
- no-cost lifecycle effects follow their own authorization policy.

If observation itself has API cost, introduce a separate quota and capped-mode
polling policy rather than coupling it to model spend.

This continuation policy assumes the rest of the operational control store is
healthy. If command, idempotency, authorization, or run state is unavailable,
only read-only diagnostics and explicitly safe repair continue.

### 11.4 Migration and rollout

Use a time-bounded `CONTROL_STORE_MODE`:

- `legacy`: current read/write behavior plus Phase 0 safeguards;
- `shadow`: SQLite is written, legacy remains authoritative, discrepancies emit
  alerts;
- `primary`: SQLite is authoritative and legacy JSONL receives compatibility
  exports only.

Migration sequence:

1. Back up the existing ledger and state files.
2. Create and integrity-check the database.
3. Import legacy entries with deterministic migration IDs.
4. Record import source digest, row counts, totals, rejects, and migration version.
5. Enter shadow mode for at least one representative billing window.
6. Compare totals, run correlations, dedupe decisions, and failure behavior.
7. Canary primary mode on one trusted repository.
8. Retain a read-only legacy export for the rollback window.
9. Remove legacy authority only after the exit criteria pass.

Rollback from primary stops paid admission, exports the database state, and returns
to a reviewed migration path. It must not blindly restore an older ledger and lose
new charges.

### 11.5 Tests and evidence

- Two or more processes racing for the final available reservation.
- Hundreds of concurrent reserve/settle/release operations.
- Huge token and money values without float drift.
- Period boundaries across timezones, daylight-saving changes, leap days, and
  concurrent admission at the boundary.
- Rate-card and cap changes while runs are active.
- Duplicate and conflicting provider receipts.
- Process kill at each listed crash point.
- Corrupt database, full disk, read-only filesystem, lock contention, and schema
  mismatch.
- Multi-review and retry scenarios cannot reserve less than their maximum policy.
- A journal larger than its tail window cannot repeat a deduplicated action.
- Free reconciliation proceeds under exhausted and unavailable accounting.
- Migration replay is deterministic and idempotent.

### 11.6 Exit criteria

- Every paid dispatch has a committed reservation before provider invocation.
- Every completed run has one settlement or release outcome.
- No behavior depends on scanning bounded journal history.
- Accounting failure cannot appear as zero spend.
- Shadow comparisons are clean for the agreed observation window.
- Backup, restore, integrity, and crash-recovery drills are documented and pass.

## 12. Phase 3 — Trusted effect boundary and authoritative gates

**Goal:** Ensure untrusted agent work cannot publish or forge gate satisfaction.

### 12.1 Work items

#### P3.1 Separate lifecycle execution from agent dispatch

Replace the current executor/runtime cycle with explicit components:

- `LifecycleExecutor` handles durable lifecycle commands;
- `AgentDispatcher` handles budgeted untrusted runtime invocation;
- the daemon/application service routes decisions to the appropriate component;
- composition constructs required dependencies once;
- no private handler mutation is used after construction;
- no required handler has a default no-op implementation.

Add `SubmitReview` as a first-class lifecycle command and migrate every direct
`gateway.submit_review` path. Apply the same inventory to comments, labels,
branches, pulls, merges, closes, and pushes.

Add an architectural test or narrow static check that prevents new direct remote
mutations outside the effect adapter.

#### P3.2 Remove publication credentials from the agent runtime

At process creation:

- construct an allowlist environment rather than inheriting the daemon
  environment;
- omit GitHub tokens, SSH agent sockets, credential-helper access, cloud metadata
  credentials, and control-store write credentials;
- use a dedicated filesystem scope for the worktree;
- restrict network egress where the runtime supports it;
- record the effective capability profile on the run.

The trusted control plane may hold credentials, but they are never passed through
prompts, files, environment variables, command-line arguments, inherited file
descriptors, or shared writable sockets.

Use three explicit capability zones:

1. **Agent runtime:** executes model-selected commands against an untrusted
   worktree; no control-store or publication access.
2. **Verifier runtime:** executes control-plane-selected tests against untrusted
   repository code; no control-store or publication access.
3. **Effect broker:** holds the narrow publication capability; never executes
   repository code and accepts only typed, authorized commands referring to a
   verified content digest.

The broker should be a minimal separate process, OS-user boundary, or equivalently
enforced credential activation boundary. Merely deleting variables from a child is
insufficient if the child can read the parent's database, credential files,
sockets, process memory, or shared git metadata.

The capability matrix must state residual risks for runtimes that cannot enforce
filesystem or egress isolation. Such runtimes remain limited to trusted inbound
content.

#### P3.3 Define the proposal-to-publish protocol

The agent produces:

- a candidate commit or patch;
- the expected trusted base revision;
- structured claims about tests and acceptance criteria;
- requested lifecycle effects.

The trusted control plane treats the agent worktree, its `.git` state, commit
objects, hooks, attributes, filters, submodules, symlinks, and repository-local git
configuration as untrusted. It:

1. extracts a size-bounded candidate artifact without accepting the agent's refs as
   authoritative;
2. reconstructs the candidate from the operator-pinned base in a fresh trusted
   staging repository with hooks, filters, credential helpers, and unsafe git
   configuration disabled;
3. rejects unexpected parents, paths, file types, submodules, symlink escapes,
   credentials, generated volume, or remote changes;
4. calculates a content digest and records the trusted base and policy snapshot;
5. executes authoritative configured tests in the credential-free verifier
   runtime;
6. evaluates policy using the verifier receipt and authoritative remote evidence;
7. creates a durable lifecycle command bound to the content digest and expected
   remote revision;
8. asks the narrow effect broker to publish with an expected-revision or
   force-with-lease guard;
9. persists the trusted effect receipt;
10. emits correlated telemetry.

The broker uses a sanitized git configuration and never invokes repository-owned
hooks or filters. It cannot be asked to execute an arbitrary command.

An ambiguous remote response produces `UNKNOWN_OUTCOME` and reconciliation, not an
immediate retry that may duplicate the effect.

#### P3.4 Make gates authoritative

- One canonical policy implementation evaluates evidence.
- Provider hooks and shell wrappers may call it, but do not reimplement it.
- The evaluator receives the actual repository/worktree identity and does not
  infer it from an untrusted `git -C` argument.
- Red-test evidence uses `TestExecution`; only a genuine test assertion failure is
  eligible.
- Test plans, acceptance criteria, visual checks, and adversarial verdicts require
  trusted receipts when configured as gates.
- Agent-created marker files become claims that schedule trusted verification.
- Remote state is fetched by the trusted control plane at the revision and object
  identity named in the decision.

#### P3.5 Retain hooks only as advisory controls

The generated governor may continue to prevent accidental misuse, but:

- it is labeled advisory;
- missing runtime dependencies fail visibly;
- its allow/deny logic delegates to the canonical policy engine where feasible;
- bypass coverage is tested as defense in depth;
- no safety claim relies on parsing all possible shell indirection.

### 12.2 Adversarial tests and evidence

- Remove, replace, and corrupt the worktree hook.
- Forge every `.harness` marker.
- Invoke git through `sh -c`, `bash -c`, language `-c`, `eval`, `xargs`, aliases,
  absolute paths, subprocesses, and `git -C`.
- Point the worktree remote at a controlled test endpoint.
- Attempt to read inherited environment, SSH agent sockets, credential helpers,
  daemon files, control database, sibling git metadata, process descriptors, and
  cloud metadata.
- Attempt a push or GitHub mutation with the governor entirely disabled.
- Attempt the same mutations and secret reads from repository-controlled tests in
  the verifier runtime.
- Install malicious git hooks, filters, attributes, credential helpers, submodules,
  symlinks, and repository-local configuration in the candidate.
- Mutate shared refs and commit objects after artifact extraction.
- Race the trusted base revision between verification and publication.
- Repeat a command after the remote effect succeeds but before local receipt
  persistence.
- Make GitHub/CI lookup unavailable and prove the policy returns indeterminate.
- Submit a timeout, setup failure, cancellation, and malformed test result as
  claimed red evidence.

The success condition is not “the hook caught the command.” It is that the
untrusted runtime cannot perform the remote mutation and forged claims cannot
satisfy the authoritative gate.

### 12.3 Rollout

Use a time-bounded `TRUSTED_PUBLISH` migration:

1. Shadow-create commands while legacy publication remains authoritative.
2. Compare intended effects and idempotency keys.
3. Enable the broker for one effect kind, starting with comments or labels.
4. Add reviews, branches, pushes, and pull requests in increasing risk order.
5. Remove credentials from the agent environment only when its required artifact
   return path is proven.
6. Run the adversarial suite in every supported runtime profile.
7. Remove legacy direct-write paths.

### 12.4 Exit criteria

- No agent child has publication credentials in supported hardened profiles.
- Every remote mutation has a durable command and trusted receipt.
- Every review submission uses the lifecycle command path.
- Disabling the hook does not grant remote mutation capability.
- Forged markers do not satisfy gates.
- Test infrastructure failure cannot be interpreted as successful gate evidence.
- Architecture checks prevent reintroduction of direct write paths.

## 13. Phase 4 — Versioned events and durable projections

**Goal:** Make telemetry evolvable and correlated while ensuring it is never used
as hidden control state.

### 13.1 Event taxonomy

Start with a small registry grouped by responsibility:

- `agent.dispatch.admitted.v1`;
- `agent.dispatch.rejected.v1`;
- `agent.run.started.v1`;
- `agent.run.completed.v1`;
- `agent.run.cancelled.v1`;
- `agent.run.reconciled.v1`;
- `budget.reservation.created.v1`;
- `budget.reservation.settled.v1`;
- `budget.reservation.released.v1`;
- `budget.accounting.unavailable.v1`;
- `policy.decision.made.v1`;
- `lifecycle.command.created.v1`;
- `lifecycle.command.completed.v1`;
- `process.drain.started.v1`;
- `process.drain.completed.v1`.

Names describe facts, not imperative requests. Each type owns a payload schema and
compatibility tests.

### 13.2 Work items

#### P4.1 Introduce envelope generation and validation

- Generate sortable unique event IDs.
- Use timezone-aware UTC timestamps with sub-second precision.
- Record occurred and observed time separately.
- Attach deployment, repository, role, process, subject, and correlation context
  centrally.
- Reject or quarantine invalid events before export.
- Remove `default=str` serialization of unknown domain objects.
- Redact secrets at typed field boundaries, not with a late regex-only pass.

#### P4.2 Emit from committed state with an outbox

For operational changes:

1. commit the state transition and outbox row in one transaction;
2. export asynchronously;
3. mark delivery progress idempotently;
4. tolerate duplicate delivery using event IDs;
5. retain or compact outbox rows under an explicit policy.

Pure telemetry that is unrelated to operational transitions may use a fail-open
sink directly.

#### P4.3 Build explicit projections

Dashboard, metrics, and operator summaries consume projection interfaces rather
than raw journal scans. Projections define:

- source event versions;
- rebuild procedure;
- ordering and duplicate behavior;
- late-event policy;
- unknown-event behavior;
- retention assumptions;
- reconciliation against authoritative operational tables.

Run and cost dashboards join by stable run ID, never by coincidental event order.

#### P4.4 Define retention and export

- Rotate JSONL telemetry by size/time.
- Document local retention, compression, and deletion.
- Keep operational database retention independent.
- Provide a supported export to JSONL and optionally OpenTelemetry.
- Ensure a slow or broken exporter cannot block scheduler progress indefinitely.
- Bound exporter queues and define drop/backpressure behavior by event criticality.

#### P4.5 Migrate producers and consumers

Use a time-bounded `EVENT_FORMAT`:

- `legacy`: old event lines only;
- `dual`: typed event plus legacy compatibility event;
- `v1`: typed events only.

Migration order:

1. run lifecycle and budget events;
2. lifecycle commands and effect receipts;
3. policy decisions;
4. observation and classification;
5. dashboard and metrics consumers;
6. delete legacy control queries;
7. retire free-form event construction.

Historical backfill is optional. If performed, mark backfilled events with their
observed time and migration provenance; do not pretend their original precision is
known.

### 13.3 Tests and evidence

- Golden schema snapshots for every event type.
- Backward compatibility tests for supported schema versions.
- Unknown additive fields and unknown event types.
- Missing required fields, invalid timestamps, huge payloads, malformed Unicode,
  and secret-like fields.
- Duplicate, reordered, and late delivery.
- Crash between operational commit and export.
- Exporter outage and bounded queue saturation.
- More than 1 MiB of telemetry followed by restart does not alter dedupe or
  lifecycle behavior.
- Projection rebuild matches authoritative run and accounting totals.
- Every agent terminal event correlates to exactly one run record.

### 13.4 Exit criteria

- All new events use the versioned envelope and typed payloads.
- Every run event has a run ID and one terminal fact.
- Dashboard associations do not depend on event order or dispatch counts.
- Telemetry truncation or exporter failure changes no control decision.
- No application behavior queries raw JSONL.
- Legacy event production has a dated removal issue.

## 14. Phase 5 — Explicit process lifecycle and supervisor hardening

**Goal:** Make startup, readiness, drain, cancellation, and recovery deterministic
across foreground, systemd, and launchd operation.

### 14.1 Daemon state machine

Implement and test:

```text
STARTING ──ready──> RUNNING ──SIGTERM/SIGINT/reload──> DRAINING
    │                  │                                  │
    └──fatal───────────┴──fatal───────────────────────────┤
                                                        v
                                                     STOPPED
```

`DRAINING` performs:

1. atomically stop new admission;
2. stop or finish observation according to policy;
3. classify the shutdown source;
4. cancel or drain active agent runs according to the configured strategy;
5. persist cancellation intent;
6. release or reconcile reservations;
7. finish/reconcile lifecycle commands;
8. flush the bounded event outbox;
9. terminate remaining process groups at the deadline;
10. emit a terminal process event and exit with the appropriate code.

### 14.2 Work items

#### P5.1 Add signal ownership

- Install `SIGTERM` and `SIGINT` handlers in the application entry point.
- Convert the signal into a cancellation token and state transition.
- Make repeated signals escalate predictably.
- Ensure foreground `Ctrl-C` and supervisor termination use the same cleanup path.
- Classify operator cancellation separately from infrastructure death.

#### P5.2 Bound child lifecycle

- Maintain an explicit registry of active runs and process groups.
- Preserve the existing Codex process-group and SDK cancellation strengths.
- Close inherited file descriptors that are not explicitly needed.
- Prevent child survival after the daemon deadline.
- Define provider-specific graceful cancel and hard-kill behavior.
- Record which stage was reached so recovery can reconcile correctly.

#### P5.3 Define drain policy

Configuration must make the trade-off explicit:

- maximum graceful drain duration;
- whether active model runs finish or receive immediate cancellation;
- per-provider cancellation grace;
- command completion grace;
- event flush grace;
- final hard-kill grace.

Defaults must fit inside the supervisor's `TimeoutStopSec` or launchd equivalent.
Hot reload uses the same bounded drain contract.

#### P5.4 Define exit taxonomy

Use stable documented exit codes for:

- clean operator stop;
- transient runtime/infrastructure failure;
- invalid configuration;
- duplicate instance/already running;
- incompatible schema;
- control-store corruption;
- failed invariant requiring operator intervention.

Supervisors restart only transient categories automatically.

#### P5.5 Harden systemd units

Generated units should use, where supported and justified:

- an absolute installed executable and explicit working directory;
- `Type=notify` readiness;
- `WatchdogSec` driven by scheduler progress rather than a blind timer;
- `Restart=on-failure`;
- `RestartPreventExitStatus` for configuration, duplicate instance, incompatible
  schema, and operator-required repair;
- start-rate limiting;
- `TimeoutStopSec` aligned with the application drain deadline;
- explicit `KillMode`;
- `MemoryHigh`, `MemoryMax`, and `TasksMax`;
- restrictive environment and credential loading;
- filesystem, device, privilege, and syscall hardening after compatibility tests.

The exact settings must follow the applicable
[systemd service](https://github.com/systemd/systemd/blob/main/man/systemd.service.xml)
and
[resource-control](https://github.com/systemd/systemd/blob/main/man/systemd.resource-control.xml)
semantics rather than copied defaults.

Readiness is sent only after configuration, lock acquisition, control-store
migration/integrity checks, and required adapter initialization succeed. Watchdog
notifications are sent only when the scheduler has completed a progress checkpoint.

#### P5.6 Harden launchd definitions

- Generate unique, stable labels per deployment.
- Resolve executable paths during installation; do not rely on an interactive
  shell `PATH`.
- Store logs in a deployment-owned persistent directory rather than shared `/tmp`.
- Apply supported resource and restart controls.
- Make duplicate instances and permanent startup failures visible.
- Document launchd feature gaps where readiness/watchdog parity is impossible.

#### P5.7 Validate installation and upgrades

- Add a dry-run renderer and validator for generated service definitions.
- Preserve the previous known-good unit/plist during upgrade.
- Reload/restart only after validation succeeds.
- Report deployment identity, executable, config path, database path, log path, and
  capability profile.
- Make uninstall scope exact and recoverable; never delete worktrees or databases
  implicitly.

### 14.3 Tests and evidence

- Send `SIGTERM` with zero, one, and many active children.
- Send a second signal during drain.
- Kill the daemon abruptly at each lifecycle checkpoint and restart it.
- Verify no child survives the configured deadline.
- Verify operator cancellation is not counted as infrastructure death.
- Verify reservations and unknown command outcomes reconcile.
- Simulate a stuck scheduler and verify watchdog behavior.
- Simulate invalid configuration, duplicate instance, incompatible schema, and
  corruption; verify no restart storm.
- Validate rendered systemd units with available systemd tooling.
- Validate rendered plists and test executable discovery without an interactive
  shell environment.
- Exercise memory/process limits against controlled fixtures.

### 14.4 Exit criteria

- Shutdown is a tested, bounded state transition.
- No supported deployment leaks child processes after the deadline.
- Deliberate restart does not fabricate an infrastructure-death outcome.
- Readiness and watchdog signals correspond to real state.
- Permanent failures do not restart indefinitely.
- Generated service definitions are absolute, unique, validated, and documented.

## 15. Phase 6 — Simplify boundaries and remove compatibility debt

**Goal:** Consolidate the new architecture, reduce change amplification, and make
the intended ownership model obvious to future maintainers.

### 15.1 Work items

#### P6.1 Establish a true composition root

Only application startup and dedicated wiring modules may import concrete adapters.
Application services depend on ports. Domain modules remain pure.

Remove:

- private callback patching after object construction;
- cyclic executor/runtime assembly;
- implicit default no-op handlers;
- concrete adapter construction inside orchestration logic;
- service locator patterns and mutable global registries.

#### P6.2 Split modules by responsibility

Split large modules only along stable seams, for example:

- provider transport, response decoding, process lifecycle, and hook generation;
- per-role application services sharing explicit policy helpers;
- GitHub query adapters versus lifecycle effect adapters;
- CLI parsing, configuration loading, deployment generation, and command handlers;
- event codecs, projections, exporters, and legacy compatibility;
- policy evaluation versus evidence acquisition.

Prefer pure transformations and immutable values. Shared behavior belongs in named
domain/application services, not inheritance hierarchies or broad utility files.

#### P6.3 Remove legacy authorities

Delete after measured cutover:

- JSONL reads used for caps, dedupe, recovery, or run reconstruction;
- free-form `event_line(event, **fields)` production;
- direct privileged gateway writes;
- agent-written marker files used as gate evidence;
- duplicate governor policy logic;
- executor default runtime no-op and private runtime mutation;
- provider/runtime configuration in domain tasks;
- migration feature flags whose exit criteria have passed.

Keep a documented read-only converter for historical JSONL if operators still need
it.

#### P6.4 Close documentation and enforcement gaps

- Update architecture, failure modes, configuration, operations, and residual-risk
  documents.
- Make the capability matrix part of release evidence.
- Keep pyrefly, import-linter, unit, contract, integration, adversarial, and
  migration tests in CI.
- Add ownership guidance for each port, state table, event family, and service
  definition.
- Add a change checklist for new providers, lifecycle effects, event types, and
  budget dimensions.

#### P6.5 Measure performance and operability

Benchmark:

- admission transaction latency and lock contention;
- outbox delay and exporter saturation;
- projection rebuild time;
- tick latency with accounting unavailable or budget exhausted;
- shutdown latency with active children;
- database growth and maintenance operations.

Document capacity assumptions and the point at which SQLite or a single-host
daemon would need re-evaluation. Do not introduce distributed infrastructure
preemptively.

### 15.2 Exit criteria

- Concrete adapters are constructed only in the composition root.
- Domain types contain no provider tool, SDK session, environment, or filesystem
  concepts.
- One implementation owns each policy and privileged effect path.
- All compatibility flags and dual writes have removal decisions.
- Repository-wide pyrefly, import-linter, and full tests are green.
- Operations and residual-risk documents describe the deployed architecture, not
  a prior state.

## 16. Dependency graph and recommended sequence

```text
Phase 0: contract + containment
     │
     v
Phase 1: typed seams
     │
     v
Phase 2: durable state + reservations ─────┐
     │                                     │
     v                                     v
Phase 3: trusted effects + gates       Phase 4: typed events
     │                                     │
     └──────────────────┬──────────────────┘
                        v
              Phase 5: supervision
                        │
                        v
              Phase 6: consolidation
```

Some preparation may overlap:

- Service artifact validation from Phase 5 can begin after Phase 0.
- Event type definitions can begin in Phase 1, but authoritative consumers wait
  for Phase 2.
- Credential inventory and runtime capability tests can begin in Phase 0, but
  credentials are removed only after the trusted artifact/effect path works.
- Module extraction may occur opportunistically when implementing a seam, but a
  broad cleanup phase waits until compatibility behavior is gone.

Do not reverse these dependencies:

- Do not make the journal more authoritative before durable state exists.
- Do not remove agent credentials without a tested artifact publication path.
- Do not treat new event schemas as durable idempotency.
- Do not add supervisor retries around non-idempotent commands.
- Do not split modules before contract ownership is decided.

## 17. Program-wide verification matrix

| Concern | Unit/contract | Integration | Fault/adversarial | Operational evidence |
|---|---|---|---|---|
| Pricing | arithmetic, rate lookup, provenance | provider receipt mapping | huge values, unknown model, stale card | actual/estimate/quota dashboards |
| Reservation | state machine, idempotency | multi-process SQLite | crash points, full disk, corruption | no unreserved paid runs |
| Gates | policy truth table | trusted verifier | forged evidence, unavailable CI, timeout | decision with evidence IDs |
| Effects | command/receipt transitions | controlled GitHub fixture | lost response, repeated command, base race | every mutation has receipt |
| Isolation | capability contract | supported runtimes | hook disabled, credential/socket reads | effective profile per run |
| Events | schema compatibility | outbox/export/projection | duplicate, reorder, outage, huge event | projection reconciliation |
| Supervision | state transitions, exit codes | real child processes | signals, wedge, abrupt kill | bounded drain and no orphans |
| Boundaries | import/static rules | application wiring | missing dependency/handler | composition inventory |

Tests must assert observable contracts. Examples:

- “a second process cannot reserve the final dollar” is useful;
- “the SQLite helper called `BEGIN IMMEDIATE`” is not a sufficient behavioral
  assertion;
- “a forged marker does not cause publication” is useful;
- “the hook function returned false” is not a sufficient security assertion.

Live GitHub tests remain opt-in and use an isolated test repository. Local
adversarial tests must prove safety without assuming live credentials.

## 18. Rollout, rollback, and feature-flag discipline

Every migration flag must have:

- a named owner;
- an introduced version/date;
- environments where it is enabled;
- metrics that determine promotion;
- explicit abort thresholds;
- rollback instructions;
- a removal issue and target date.

Expected temporary flags:

| Flag | Values | Removal condition |
|---|---|---|
| `CONTROL_STORE_MODE` | `legacy`, `shadow`, `primary` | SQLite primary is proven and legacy reads are deleted |
| `TRUSTED_PUBLISH` | `off`, effect-kind allowlist, `on` | All direct writes and agent credentials are removed |
| `EVENT_FORMAT` | `legacy`, `dual`, `v1` | All producers and consumers use versioned events |
| `SUPERVISION_NOTIFY` | `off`, `on` | Supported service managers use the final lifecycle contract |

Abort a canary when any of these occur:

- an admitted paid run lacks a reservation;
- accounting totals diverge without explained provenance;
- a command may have duplicated a remote effect;
- a trusted gate accepts agent-authored evidence;
- a projection differs from authoritative state;
- a deliberate restart is classified as infrastructure death;
- a child survives the shutdown deadline;
- a permanent error enters a restart loop.

Rollback must preserve new authoritative state. Prefer disabling new admission and
reconciling over reverting database files or replaying uncertain effects.

## 19. Success metrics and service objectives

Measure from the start of shadow mode:

- **Paid admission coverage:** 100% of provider invocations have an active
  reservation before start.
- **Settlement coverage:** 100% of terminal paid runs settle or release exactly
  once.
- **Admission safety:** zero new reservations that make committed spend exceed the
  configured cap.
- **Accounting availability behavior:** zero paid admissions when an armed account
  is unavailable or corrupt.
- **Free-work continuity:** reconciliation and reaping continue during cap
  exhaustion.
- **Effect traceability:** 100% of remote mutations map to a command and trusted
  receipt.
- **Credential isolation:** zero publication credentials visible in hardened agent
  profiles.
- **Gate provenance:** 100% of authoritative decisions reference trusted evidence.
- **Event validity:** 100% of new typed events pass schema validation.
- **Run correlation:** 100% of run lifecycle events include stable dispatch and run
  IDs.
- **Telemetry independence:** log rotation/export outage causes zero duplicate
  lifecycle actions and zero admission changes.
- **Shutdown:** no child remains after the configured deadline; track p50/p95 drain
  duration.
- **Supervisor stability:** zero restart loops for classified permanent failures.
- **Static health:** repository-wide pyrefly and import-linter remain green.

Alerts should be actionable and bounded. Repeated accounting or exporter failures
need suppression keyed in durable state, not journal history.

## 20. Risks and mitigations

| Risk | Mitigation |
|---|---|
| SQLite contention slows scheduler ticks | Short transactions, indexes, WAL/busy-timeout tests, capacity benchmark |
| Migration double-counts spend | Deterministic import IDs, source digest, shadow totals, idempotent replay |
| Provider charge exceeds reservation | Conservative maximum, variance record, alert, provider-specific policy |
| External publication changes agent UX | Stable candidate artifact contract and incremental effect-kind rollout |
| Removing credentials breaks legitimate dependency access | Separate read-only dependency egress from publication credentials; capability profiles |
| Dual writes drift indefinitely | Measured comparison, owner, expiry, and phase exit gate |
| Event schema becomes an untyped dumping ground | Registry, typed payload ownership, size limits, compatibility tests |
| Supervisor hardening breaks provider SDK behavior | Capability-specific tests and incremental hardening settings |
| Platform behavior diverges | Shared lifecycle contract plus explicit systemd/launchd capability matrix |
| Broad refactor obscures regressions | Contract-first phases, compatibility adapters, coherent commits, full behavioral suite |
| Security claims exceed runtime isolation | Publish residual risks and restrict untrusted inputs by verified profile |
| Unknown remote outcome duplicates effects | Durable idempotency, remote reconciliation, no blind retry |
| Database corruption stops paid work | Fail safe for paid admission, keep free recovery available, backup/integrity drill |

## 21. Required documentation at completion

The maintained documentation set must include:

- architecture and dependency boundaries;
- trust model and capability matrix;
- accounting semantics and operator cap runbook;
- control-store schema, migration, backup, restore, and corruption recovery;
- event catalog and compatibility policy;
- lifecycle command catalog and idempotency rules;
- runtime isolation profiles and residual risks;
- signal, drain, exit-code, systemd, and launchd operations;
- failure-mode matrix with fail-open/fail-closed behavior;
- provider integration checklist;
- release and rollback checklist.

Documentation claims that can be checked automatically should have tests.

## 22. Program definition of done

This architectural program is complete only when:

1. Untrusted agent execution cannot publish to GitHub or push git state in the
   supported hardened profile, even with local hooks disabled.
2. All privileged effects pass through durable lifecycle commands and trusted
   receipts.
3. Budget admission is transactional, provenance-aware, and fail-safe when caps
   are armed.
4. Actual charges, estimates, and quota debits remain distinct throughout storage,
   events, and dashboards.
5. Operational correctness no longer depends on JSONL tail scans or agent-written
   markers.
6. All events use a versioned envelope with stable run correlation, and telemetry
   can be lost without changing control decisions.
7. Daemon startup, readiness, drain, cancellation, and shutdown are explicit,
   bounded, and supervisor-aware.
8. Application services depend on ports, concrete adapters are wired at the
   composition root, and provider details do not leak into the domain model.
9. Legacy compatibility paths, dual writes, private handler mutation, and
   misleading documentation are removed.
10. Unit, contract, integration, migration, fault-injection, and adversarial suites
    pass, along with repository-wide pyrefly and import-linter checks.

## 23. Suggested issue breakdown

Create one tracking epic per phase and small issues with one observable outcome.
The first implementation issues should be:

1. Accept the seven ADRs and publish the authority/capability matrix.
2. Correct architecture and residual-risk documentation.
3. Add explicit accounting-unavailable containment for armed caps.
4. Introduce typed money, quota, run identity, test result, and policy result.
5. Extract `SpendAccount`, `RuntimePort`, and lifecycle effect ports.
6. Add repository-wide pyrefly configuration and CI enforcement.
7. Implement the SQLite schema and shared accounting contract suite.
8. Implement atomic reserve/settle/release with multi-process tests.
9. Migrate durable dedupe and run records.
10. Introduce lifecycle commands and migrate `SubmitReview`.
11. Build the trusted candidate verification and publication path.
12. Remove publication credentials from the first hardened runtime profile.
13. Run the governor/gate adversarial suite with hooks disabled.
14. Introduce the typed event envelope and outbox.
15. Migrate projections and remove JSONL control queries.
16. Implement signal-driven drain and exit taxonomy.
17. Harden and validate systemd and launchd artifacts.
18. Remove compatibility paths and complete module-boundary consolidation.

Every issue must name:

- the invariant it advances;
- the contract or schema it changes;
- its migration and rollback behavior;
- the required behavioral and fault tests;
- the documentation it updates;
- the phase exit criterion it helps satisfy.

## 24. Current implementation map

This table is a starting inventory for issue scoping. It is not permission to
preserve the current ownership boundaries.

| Concern | Current locations | Intended ownership |
|---|---|---|
| Spend admission and tick gating | `src/issue_agent/app/spend_gate.py`, `src/issue_agent/app/dispatch.py`, `src/issue_agent/app/loop.py` | Application admission service using `PricingService` and `SpendAccount` ports |
| Rates and cap configuration | `src/issue_agent/app/config.py` | Immutable configuration snapshot plus pricing/accounting value objects |
| Cost ledger and spend aggregation | `src/issue_agent/adapters/journal.py` | SQLite accounting adapter; journal becomes telemetry export |
| Cost/run dashboard association | `src/issue_agent/app/dashweb/snapshots.py` | Typed projections joined by stable run ID |
| Domain gate decisions | `src/issue_agent/domain/gate.py` | Pure policy over typed trusted evidence |
| Governor integration and policy | `src/issue_agent/adapters/governor.py`, `src/issue_agent/adapters/governor_policy.py` | Advisory adapter delegating to one canonical policy service |
| Generated Codex controls | `src/issue_agent/adapters/codex_hook.py`, `src/issue_agent/adapters/codex_runner.py` | Runtime capability adapter; never authoritative |
| Claude permissions | `src/issue_agent/adapters/agent_runner.py` | Runtime capability adapter with explicit hardened profile |
| Test worktree evidence | `src/issue_agent/adapters/worktree.py` | Credential-free trusted verifier returning `TestExecution` |
| Agent-local state and markers | `src/issue_agent/adapters/store.py` | Untrusted claims; durable authoritative state moves to control store |
| Runtime task planning | `src/issue_agent/app/planners/tasks.py` | Pure intent planning followed by provider-specific runtime planning |
| Event construction and reading | `src/issue_agent/adapters/journal.py` | Typed event codecs, outbox exporter, and compatibility reader |
| Event consumers | `src/issue_agent/app/execute.py`, `src/issue_agent/app/dispatch.py`, `src/issue_agent/app/dashweb/snapshots.py` | Operational store queries or explicit projections |
| Lifecycle execution | `src/issue_agent/app/execute.py` | `LifecycleExecutor` over durable commands and effect ports |
| Agent dispatch wiring | `src/issue_agent/app/roles.py` | `AgentDispatcher` injected through a composition root |
| Direct review writes | `src/issue_agent/app/workflows/review.py`, `src/issue_agent/adapters/github.py` | `SubmitReview` lifecycle command and narrow GitHub effect adapter |
| Daemon lifecycle | `src/issue_agent/app/main.py`, `src/issue_agent/app/loop.py` | Explicit state machine, signal ownership, drain coordinator, exit taxonomy |
| Provider child containment | `src/issue_agent/adapters/agent_runner.py`, `src/issue_agent/adapters/codex_runner.py` | Runtime adapters implementing one cancellation/process contract |
| systemd deployment | `ops/systemd/`, `bin/install-systemd.sh` | Validated generated units derived from deployment configuration |
| launchd deployment | `ops/launchd/`, `bin/install-launchd.sh` | Validated generated plists with unique identity and persistent paths |
| Boundary enforcement | import-linter configuration and repository tests | Domain purity, application port ownership, and privileged-write checks |

When an issue changes one row, review its adjacent rows for duplicated policy or a
new reverse dependency. The table should be updated as files move so it remains an
ownership map rather than a historical index.
