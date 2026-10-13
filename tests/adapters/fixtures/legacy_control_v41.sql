-- The legacy control-store schema at v41, the version the live store had at
-- the 3-table cutover (slice 5d-2). Dumped from a store migrated to v41.

CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL,
    source_digest TEXT NOT NULL
);

CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,
    dispatch_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN
        ('reserved','starting','running','completed','cancelled','abandoned')),
    config_version TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    outcome TEXT,
    provider_receipt_id TEXT UNIQUE
, workflow_binding_json TEXT, workflow_episode_key TEXT, review_round_id TEXT REFERENCES review_execution_rounds(round_id), review_child_id TEXT, continuations_json TEXT NOT NULL DEFAULT '{"format":1,"value":[]}'
    CHECK (length(CAST(continuations_json AS BLOB)) BETWEEN 1 AND 262144));

CREATE TABLE leases (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
    dispatch_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    pid INTEGER NOT NULL CHECK (pid > 0),
    pgid INTEGER NOT NULL CHECK (pgid > 0),
    process_start_ticks INTEGER NOT NULL DEFAULT 0
        CHECK (process_start_ticks >= 0),
    started_at TEXT NOT NULL
);

CREATE TABLE dedupe_keys (
    namespace TEXT NOT NULL,
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    reusable_after TEXT,
    payload_json TEXT,
    PRIMARY KEY (namespace, scope, key)
);

CREATE TABLE level_state (
    namespace TEXT NOT NULL,
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (namespace, scope, key)
);

CREATE TABLE lifecycle_commands (
    command_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    logical_key TEXT NOT NULL,
    occurrence INTEGER NOT NULL DEFAULT 0 CHECK (occurrence >= 0),
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    expected_revision TEXT,
    config_version TEXT NOT NULL DEFAULT '',
    repository TEXT NOT NULL DEFAULT '',
    executor_identity TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN
        ('pending','executing','succeeded','retryable_failure',
         'permanent_failure','unknown_outcome')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
, refusals INTEGER NOT NULL DEFAULT 0 CHECK (refusals >= 0), workflow_binding_json TEXT, workflow_reconciliation_json TEXT, attempt_owner TEXT, prewrite_read_json TEXT);

CREATE TABLE effect_receipts (
    command_id TEXT PRIMARY KEY
        REFERENCES lifecycle_commands(command_id) ON DELETE RESTRICT,
    executor_identity TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN
        ('succeeded','retryable_failure','permanent_failure','unknown_outcome')),
    remote_object_id TEXT,
    remote_revision TEXT,
    error TEXT,
    receipt_digest TEXT NOT NULL
);

CREATE TABLE budget_reservations (
    reservation_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id) ON DELETE RESTRICT,
    repository TEXT NOT NULL,
    dimension TEXT NOT NULL,
    period_id TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    period_timezone TEXT NOT NULL,
    currency TEXT NOT NULL,
    maximum_micros INTEGER NOT NULL CHECK (maximum_micros >= 0),
    cap_micros INTEGER NOT NULL CHECK (cap_micros >= 0),
    model TEXT NOT NULL,
    rate_card_id TEXT NOT NULL,
    rate_card_version TEXT NOT NULL,
    quote_effective_at TEXT NOT NULL,
    config_version TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('active','settled','released')),
    release_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
, workload_repository TEXT NOT NULL DEFAULT '*'
    CHECK (workload_repository <> ''));

CREATE INDEX reservations_scope_state
ON budget_reservations(repository, dimension, period_id, currency, state);

CREATE TABLE reservation_limits (
    run_id TEXT NOT NULL REFERENCES budget_reservations(run_id) ON DELETE RESTRICT,
    repository TEXT NOT NULL,
    dimension TEXT NOT NULL,
    period_id TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    period_timezone TEXT NOT NULL,
    currency TEXT NOT NULL,
    cap_micros INTEGER NOT NULL CHECK (cap_micros >= 0),
    PRIMARY KEY (run_id, repository, dimension, period_id, currency)
);

CREATE INDEX reservation_limits_scope
ON reservation_limits(repository, dimension, period_id, currency);

CREATE TABLE charges (
    charge_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id) ON DELETE RESTRICT,
    provider_receipt_id TEXT NOT NULL UNIQUE,
    amount_micros INTEGER NOT NULL CHECK (amount_micros >= 0),
    currency TEXT NOT NULL,
    basis TEXT NOT NULL,
    usage_json TEXT,
    rate_card_id TEXT,
    rate_card_version TEXT,
    settled_at TEXT NOT NULL
, origin TEXT NOT NULL DEFAULT 'normal_return'
CHECK (origin IN ('normal_return','recovery_estimate')));

CREATE INDEX charges_scope
ON charges(run_id, currency);

CREATE TABLE quota_debits (
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    quota TEXT NOT NULL,
    units INTEGER NOT NULL CHECK (units >= 0),
    calculation_version TEXT NOT NULL,
    PRIMARY KEY (run_id, quota)
);

CREATE TABLE event_outbox (
    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    destination TEXT NOT NULL DEFAULT '',
    event_json TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    emitted_at TEXT,
    failed_at TEXT,
    failure TEXT
);

CREATE TABLE world_observations (
    deployment TEXT NOT NULL,
    subject_kind TEXT NOT NULL,
    subject_number INTEGER NOT NULL CHECK (subject_number > 0),
    content_hash TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (deployment, subject_kind, subject_number)
);

CREATE TABLE migration_imports (
    migration_id TEXT PRIMARY KEY,
    source_path TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    rejected_count INTEGER NOT NULL,
    amount_micros INTEGER NOT NULL,
    imported_at TEXT NOT NULL
);

CREATE TABLE run_cancellation_intents (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE RESTRICT,
    dispatch_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    issue_number INTEGER CHECK (issue_number IS NULL OR issue_number > 0),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    handover_written_at TEXT
);

CREATE TABLE dispatch_claims (
    repository TEXT NOT NULL,
    role TEXT NOT NULL,
    target TEXT NOT NULL,
    leg TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    owner_pid INTEGER NOT NULL CHECK (owner_pid > 0),
    owner_start_ticks INTEGER NOT NULL CHECK (owner_start_ticks > 0),
    paths_json TEXT NOT NULL,
    claimed_at TEXT NOT NULL, occurrence_id TEXT,
    PRIMARY KEY (repository, role, target)
);

CREATE TABLE dispatch_claim_runs (
    repository TEXT NOT NULL,
    role TEXT NOT NULL,
    target TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    bound_at TEXT NOT NULL,
    PRIMARY KEY (repository, role, target, run_id),
    FOREIGN KEY (repository, role, target)
        REFERENCES dispatch_claims(repository, role, target)
        ON DELETE CASCADE
);

CREATE INDEX dispatch_claim_runs_run
ON dispatch_claim_runs(run_id);

CREATE TABLE retired_owners (
    owner_id TEXT PRIMARY KEY,
    successor_owner_id TEXT NOT NULL,
    repository TEXT NOT NULL,
    role TEXT NOT NULL,
    retired_at TEXT NOT NULL,
    CHECK (owner_id <> ''),
    CHECK (successor_owner_id <> ''),
    CHECK (repository <> ''),
    CHECK (role <> ''),
    CHECK (owner_id <> successor_owner_id)
);

CREATE INDEX retired_owners_successor
ON retired_owners(successor_owner_id);

CREATE TABLE reservation_recovery (
    run_id TEXT PRIMARY KEY REFERENCES budget_reservations(run_id)
        ON DELETE CASCADE,
    policy TEXT NOT NULL CHECK (policy IN ('spawn_pending', 'running',
        'reconcile_only')),
    reason TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE workflow_authority_epochs (
    repository TEXT NOT NULL,
    family TEXT NOT NULL CHECK (length(family) BETWEEN 1 AND 64),
    epoch INTEGER NOT NULL CHECK (epoch > 0),
    source_digest TEXT NOT NULL,
    inventory_digest TEXT NOT NULL,
    activated_at TEXT NOT NULL,
    PRIMARY KEY (repository, family)
);

CREATE TABLE workflows (
    repository TEXT NOT NULL,
    target_kind TEXT NOT NULL CHECK (target_kind IN ('issue', 'pr')),
    target_number INTEGER NOT NULL CHECK (target_number > 0),
    generation INTEGER NOT NULL CHECK (generation >= 0),
    version INTEGER NOT NULL CHECK (version >= 0),
    episode_json TEXT NOT NULL,
    PRIMARY KEY (repository, target_kind, target_number)
);

CREATE TABLE workflow_gates (
    gate_id TEXT PRIMARY KEY,
    repository TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_number INTEGER NOT NULL,
    opened_generation INTEGER NOT NULL CHECK (opened_generation >= 0),
    cause_key TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('open', 'rearming', 'closed')),
    payload_json TEXT NOT NULL,
    UNIQUE (repository, target_kind, target_number, opened_generation, cause_key),
    FOREIGN KEY (repository, target_kind, target_number)
        REFERENCES workflows (repository, target_kind, target_number)
);

CREATE TABLE workflow_rearms (
    request_id TEXT PRIMARY KEY,
    repository TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_number INTEGER NOT NULL,
    gate_id TEXT NOT NULL REFERENCES workflow_gates(gate_id),
    from_generation INTEGER NOT NULL CHECK (from_generation >= 0),
    to_generation INTEGER NOT NULL CHECK (to_generation = from_generation + 1),
    state TEXT NOT NULL CHECK (state IN
        ('pending', 'completed', 'superseded', 'cancelled')),
    request_digest TEXT NOT NULL,
    actor_json TEXT NOT NULL,
    intent_json TEXT NOT NULL,
    UNIQUE (repository, target_kind, target_number, to_generation),
    FOREIGN KEY (repository, target_kind, target_number)
        REFERENCES workflows (repository, target_kind, target_number)
);

CREATE UNIQUE INDEX workflow_one_pending_rearm
    ON workflow_rearms(repository, target_kind, target_number)
    WHERE state = 'pending';

CREATE TABLE workflow_rearm_commands (
    request_id TEXT NOT NULL REFERENCES workflow_rearms(request_id),
    operation_key TEXT NOT NULL,
    command_id TEXT NOT NULL UNIQUE REFERENCES lifecycle_commands(command_id),
    disposition TEXT NOT NULL CHECK (disposition IN ('required', 'retired')),
    disposition_evidence_json TEXT NOT NULL,
    PRIMARY KEY (request_id, operation_key)
);

CREATE TABLE workflow_evidence (
    evidence_id TEXT PRIMARY KEY,
    repository TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_number INTEGER NOT NULL,
    command_id TEXT REFERENCES lifecycle_commands(command_id),
    kind TEXT NOT NULL CHECK (kind IN ('observation', 'absence', 'retry_applicability', 'prior_receipt')),
    payload_json TEXT NOT NULL,
    CHECK (kind = 'observation' OR command_id IS NOT NULL),
    FOREIGN KEY (repository, target_kind, target_number)
        REFERENCES workflows (repository, target_kind, target_number)
);

CREATE TABLE workflow_retry_authorizations (
    authorization_id TEXT PRIMARY KEY,
    request_id TEXT REFERENCES workflow_rearms(request_id),
    command_id TEXT NOT NULL REFERENCES lifecycle_commands(command_id),
    gate_id TEXT NOT NULL REFERENCES workflow_gates(gate_id),
    association_kind TEXT NOT NULL CHECK (association_kind IN ('standalone', 'rearm')),
    binding_json TEXT NOT NULL,
    evidence_id TEXT NOT NULL REFERENCES workflow_evidence(evidence_id),
    request_digest TEXT NOT NULL,
    actor_json TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (generation >= 0),
    consumed_attempt INTEGER CHECK (consumed_attempt > 0), disposition TEXT
    CHECK (disposition IN ('consumed', 'not_needed', 'inapplicable', 'void')), disposed_at TEXT, disposition_reason TEXT, accepted_at TEXT, first_visit_at TEXT, unavailable_reads INTEGER NOT NULL DEFAULT 0
    CHECK (unavailable_reads >= 0),
    CHECK ((association_kind = 'standalone' AND request_id IS NULL)
        OR (association_kind = 'rearm' AND request_id IS NOT NULL))
);

CREATE INDEX runs_adjudication_episode ON runs(workflow_episode_key)
WHERE kind='ADJUDICATE'
    AND NOT(state='cancelled' AND outcome IS 'unlaunched_released' AND provider_receipt_id IS NULL);

CREATE TRIGGER runs_require_workflow_episode_insert BEFORE INSERT ON runs
WHEN (NEW.workflow_binding_json IS NULL) != (NEW.workflow_episode_key IS NULL)
BEGIN
    SELECT RAISE(ABORT, 'workflow admission requires its episode projection');
END;

CREATE TRIGGER runs_require_workflow_episode_update BEFORE UPDATE ON runs
WHEN (NEW.workflow_binding_json IS NULL) != (NEW.workflow_episode_key IS NULL)
    OR (OLD.workflow_episode_key IS NOT NULL AND NEW.workflow_episode_key IS NOT OLD.workflow_episode_key)
BEGIN
    SELECT RAISE(ABORT, 'workflow episode projection is immutable');
END;

CREATE INDEX runs_workflow_scope ON runs(workflow_episode_key);

CREATE INDEX lifecycle_publication_runs ON lifecycle_commands(CAST(json_extract(payload_json,'$.run_id') AS TEXT))
WHERE kind IN ('publish_branch','open_pull_request','update_pull_request_body')
AND state NOT IN ('succeeded','permanent_failure');

CREATE TABLE workflow_activation_imports (
    repository TEXT NOT NULL,
    family TEXT NOT NULL,
    epoch INTEGER NOT NULL CHECK (epoch > 0),
    target_kind TEXT NOT NULL CHECK (target_kind IN ('issue', 'pr')),
    target_number INTEGER NOT NULL CHECK (target_number > 0),
    source_digest TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    PRIMARY KEY (repository, family, target_kind, target_number),
    FOREIGN KEY (repository, family)
        REFERENCES workflow_authority_epochs (repository, family),
    FOREIGN KEY (repository, target_kind, target_number)
        REFERENCES workflows (repository, target_kind, target_number)
);

CREATE TABLE workflow_activation_evidence (
    repository TEXT NOT NULL,
    family TEXT NOT NULL,
    epoch INTEGER NOT NULL CHECK (epoch > 0),
    ceiling INTEGER NOT NULL CHECK (ceiling >= 0),
    plan_digest TEXT NOT NULL CHECK (length(plan_digest) = 64),
    PRIMARY KEY (repository, family, epoch),
    FOREIGN KEY (repository, family)
        REFERENCES workflow_authority_epochs (repository, family)
);

CREATE UNIQUE INDEX workflow_retry_open_grant ON workflow_retry_authorizations(command_id)
WHERE disposition IS NULL;

CREATE TRIGGER workflow_retry_grant_insert BEFORE INSERT ON workflow_retry_authorizations
WHEN NEW.disposition IS NOT NULL OR NEW.consumed_attempt IS NOT NULL OR NEW.disposed_at IS NOT NULL
    OR NEW.disposition_reason IS NOT NULL OR NEW.first_visit_at IS NOT NULL
    OR NEW.unavailable_reads != 0 OR NEW.accepted_at IS NULL
BEGIN
    SELECT RAISE(ABORT, 'a retry grant is created open');
END;

CREATE TRIGGER workflow_retry_grant_update BEFORE UPDATE ON workflow_retry_authorizations
WHEN OLD.disposition IS NOT NULL
    OR NEW.authorization_id IS NOT OLD.authorization_id OR NEW.request_id IS NOT OLD.request_id
    OR NEW.command_id IS NOT OLD.command_id OR NEW.gate_id IS NOT OLD.gate_id
    OR NEW.association_kind IS NOT OLD.association_kind OR NEW.binding_json IS NOT OLD.binding_json
    OR NEW.evidence_id IS NOT OLD.evidence_id OR NEW.request_digest IS NOT OLD.request_digest
    OR NEW.actor_json IS NOT OLD.actor_json OR NEW.generation IS NOT OLD.generation
    OR NEW.accepted_at IS NOT OLD.accepted_at
    OR (OLD.first_visit_at IS NOT NULL AND NEW.first_visit_at IS NOT OLD.first_visit_at)
    OR NEW.unavailable_reads NOT IN (OLD.unavailable_reads, OLD.unavailable_reads + 1)
    OR (NEW.disposition IS NULL AND (NEW.disposed_at IS NOT NULL OR NEW.disposition_reason IS NOT NULL
        OR NEW.consumed_attempt IS NOT NULL))
    OR (NEW.disposition IS NOT NULL AND (NEW.disposed_at IS NULL OR NEW.disposition_reason IS NULL
        OR (NEW.disposition = 'consumed') != (NEW.consumed_attempt IS NOT NULL)))
BEGIN
    SELECT RAISE(ABORT, 'a retry grant is disposed exactly once and is otherwise immutable');
END;

CREATE INDEX effect_receipts_remote_object ON effect_receipts(remote_object_id)
WHERE remote_object_id IS NOT NULL;

CREATE TABLE review_execution_rounds (
    round_id TEXT PRIMARY KEY,
    repository TEXT NOT NULL,
    target_kind TEXT NOT NULL CHECK (target_kind='pr'),
    target_number INTEGER NOT NULL CHECK (target_number > 0),
    execution_epoch INTEGER NOT NULL CHECK (execution_epoch > 0),
    execution_episode TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (generation >= 0),
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    claim_occurrence_id TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    planning_json TEXT NOT NULL,
    closure_json TEXT,
    outcome_json TEXT,
    UNIQUE (repository,target_number,execution_epoch,execution_episode,sequence),
    FOREIGN KEY (repository,target_kind,target_number)
        REFERENCES workflows(repository,target_kind,target_number)
);

CREATE INDEX review_execution_rounds_episode
ON review_execution_rounds(repository,target_number,execution_epoch,execution_episode,sequence);

CREATE INDEX review_execution_rounds_open
ON review_execution_rounds(repository,target_number,execution_epoch,execution_episode,sequence)
WHERE outcome_json IS NULL;

CREATE INDEX review_execution_rounds_unclosed
ON review_execution_rounds(repository,target_number,execution_epoch,execution_episode,sequence)
WHERE closure_json IS NULL;

CREATE UNIQUE INDEX runs_review_child ON runs(review_round_id,review_child_id)
WHERE review_round_id IS NOT NULL;

CREATE TRIGGER runs_review_attachment_insert BEFORE INSERT ON runs
WHEN (NEW.review_round_id IS NULL) != (NEW.review_child_id IS NULL)
BEGIN SELECT RAISE(ABORT,'review round and child must be attached together'); END;

CREATE TRIGGER runs_review_attachment_update BEFORE UPDATE ON runs
WHEN (NEW.review_round_id IS NULL) != (NEW.review_child_id IS NULL)
    OR NEW.review_round_id IS NOT OLD.review_round_id
    OR NEW.review_child_id IS NOT OLD.review_child_id
BEGIN SELECT RAISE(ABORT,'review run attachment is immutable'); END;

CREATE UNIQUE INDEX dispatch_claims_occurrence ON dispatch_claims(occurrence_id);

CREATE TRIGGER dispatch_claims_occurrence_insert BEFORE INSERT ON dispatch_claims
WHEN NEW.occurrence_id IS NULL OR length(NEW.occurrence_id)=0
BEGIN SELECT RAISE(ABORT,'dispatch claim requires an occurrence'); END;

CREATE TRIGGER dispatch_claims_occurrence_update BEFORE UPDATE ON dispatch_claims
WHEN NEW.occurrence_id IS NULL OR length(NEW.occurrence_id)=0
    OR (OLD.occurrence_id IS NOT NULL AND NEW.occurrence_id IS NOT OLD.occurrence_id)
BEGIN SELECT RAISE(ABORT,'dispatch claim occurrence is immutable'); END;

CREATE INDEX review_execution_rounds_repository
ON review_execution_rounds(repository);

CREATE INDEX review_execution_rounds_generation_pending
ON review_execution_rounds(repository,target_number,execution_epoch,execution_episode,generation,sequence);

CREATE INDEX workflow_gates_current
    ON workflow_gates(repository,target_kind,target_number,gate_id)
    WHERE state<>'closed';

CREATE TABLE "workflow_components" (
    repository TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_number INTEGER NOT NULL CHECK (target_number > 0),
    family TEXT NOT NULL,
    epoch INTEGER NOT NULL CHECK (epoch > 0),
    episode_id TEXT NOT NULL CHECK (length(episode_id) BETWEEN 1 AND 512),
    PRIMARY KEY (repository,target_kind,target_number,family),
    FOREIGN KEY (repository,target_kind,target_number)
        REFERENCES workflows(repository,target_kind,target_number),
    CHECK ((family IN ('resolve','issue_recovery','steward') AND target_kind='issue') OR
           (family IN ('review_rejections','review_execution','pr_repair') AND target_kind='pr'))
);

CREATE INDEX workflow_components_family ON workflow_components(repository,family);

CREATE TRIGGER runs_continuation_selections_update BEFORE UPDATE OF continuations_json ON runs
WHEN NEW.continuations_json IS NOT OLD.continuations_json
BEGIN SELECT RAISE(ABORT,'run continuation selections are immutable'); END;

CREATE TRIGGER runs_continuation_identity_update BEFORE UPDATE ON runs
WHEN OLD.continuations_json <> '{"format":1,"value":[]}' AND (
    NEW.workflow_binding_json IS NOT OLD.workflow_binding_json OR
    NEW.workflow_episode_key IS NOT OLD.workflow_episode_key OR
    NEW.dispatch_id IS NOT OLD.dispatch_id OR NEW.owner_id IS NOT OLD.owner_id OR
    NEW.kind IS NOT OLD.kind OR NEW.config_version IS NOT OLD.config_version)
BEGIN SELECT RAISE(ABORT,'continuation run identity is immutable'); END;

CREATE INDEX runs_fix_issue_dispatch ON runs(CASE WHEN json_valid(dispatch_id) THEN json_extract(dispatch_id,'$.repository') END,CASE WHEN json_valid(dispatch_id) THEN json_extract(dispatch_id,'$.issue') END)
WHERE CASE WHEN json_valid(dispatch_id) THEN
    json_type(dispatch_id)='object' AND json_type(dispatch_id,'$.repository')='text'
    AND json_extract(dispatch_id,'$.role')='fix' AND json_type(dispatch_id,'$.issue')='integer'
    AND json_extract(dispatch_id,'$.issue')>0 ELSE 0 END
    AND kind IN ('ROUTE','RESOLVE','RESUME','AUTHOR','IMPLEMENT','ADJUDICATE',
                 'PR_RESUME','FIX_CI','ADDRESS_REVIEW','REBASE');

CREATE INDEX lifecycle_self_clear_pending ON lifecycle_commands(repository,target)
WHERE state IN ('pending','executing','retryable_failure','unknown_outcome')
    AND CASE WHEN json_valid(payload_json) THEN json_type(payload_json,'$.self_clear') END='text';

CREATE INDEX workflow_repair_owed ON workflows(repository,target_kind,target_number)
WHERE json_valid(episode_json) AND (
    COALESCE(json_extract(episode_json,'$.value.fields.pr_repair.fields.watermarks.fields.last_admitted'),0)
        > COALESCE(json_extract(episode_json,'$.value.fields.pr_repair.fields.watermarks.fields.last_applied'),0)
    OR json_type(episode_json,'$.value.fields.issue_recovery.fields.state.fields.pending_ci')='object'
    OR json_type(episode_json,'$.value.fields.issue_recovery.fields.state.fields.pending_rebase')='object'
    OR json_type(episode_json,'$.value.fields.issue_recovery.fields.state.fields.review_failures.fields.pending')='object'
);

CREATE INDEX continuation_pr_admission_identity
ON dedupe_keys(scope,json_extract(payload_json,'$.value[1].fields.attempt.fields.binding'),json_extract(payload_json,'$.value[1].fields.admission_version'))
WHERE namespace='continuation-admission' AND json_valid(payload_json) AND json_extract(payload_json,'$.value[1].fields.attempt.fields.binding.fields.authority.fields.family.value')='pr_repair';

CREATE INDEX reservations_active_workload ON budget_reservations(workload_repository,run_id)
WHERE state='active';

CREATE INDEX event_outbox_pending ON event_outbox(outbox_id)
WHERE emitted_at IS NULL AND failed_at IS NULL;

CREATE INDEX event_outbox_pending_destination ON event_outbox(destination,outbox_id)
WHERE emitted_at IS NULL AND failed_at IS NULL;

CREATE INDEX lifecycle_review_generation_candidates ON lifecycle_commands(repository,target)
WHERE workflow_binding_json IS NOT NULL AND (
    prewrite_read_json IS NOT NULL OR state IN ('succeeded','permanent_failure') OR
    CASE WHEN typeof(workflow_reconciliation_json)='text' AND json_valid(workflow_reconciliation_json)
    THEN NOT (coalesce((json_type(workflow_reconciliation_json,'$.value.fields.outcome_evidence_id')='text' AND length(json_extract(workflow_reconciliation_json,'$.value.fields.outcome_evidence_id')) BETWEEN 1 AND 512 AND trim(json_extract(workflow_reconciliation_json,'$.value.fields.outcome_evidence_id'))<>'' AND instr(json_extract(workflow_reconciliation_json,'$.value.fields.outcome_evidence_id'),char(0))=0 AND json_extract(workflow_reconciliation_json,'$.value.fields.outcome_evidence_id') NOT GLOB '*[^ -~]*' AND json_type(workflow_reconciliation_json,'$.value.fields.retirement_evidence_id')='text' AND length(json_extract(workflow_reconciliation_json,'$.value.fields.retirement_evidence_id')) BETWEEN 1 AND 512 AND trim(json_extract(workflow_reconciliation_json,'$.value.fields.retirement_evidence_id'))<>'' AND instr(json_extract(workflow_reconciliation_json,'$.value.fields.retirement_evidence_id'),char(0))=0 AND json_extract(workflow_reconciliation_json,'$.value.fields.retirement_evidence_id') NOT GLOB '*[^ -~]*'),0) AND workflow_reconciliation_json=
        json_object('format',1,'value',json_object('fields',json_object(
            'outcome_evidence_id',json_extract(workflow_reconciliation_json,'$.value.fields.outcome_evidence_id'),
            'retirement_evidence_id',json_extract(workflow_reconciliation_json,'$.value.fields.retirement_evidence_id')
        ),'type','RetiredSettledCommand')))
    ELSE 1 END
);

INSERT INTO schema_migrations(version, applied_at, source_digest) VALUES (9, '2026-10-01T00:00:00.000Z', 'a148d6f97886d73f3a9b11d863561cac8f50206f8490a089bf5c24a96d4e0cd3');
INSERT INTO schema_migrations(version, applied_at, source_digest) VALUES (10, '2026-10-01T00:00:00.000Z', '4c5f6a64c929cf42baa14628f635d57fd2e950707be3c0d5781266d21f12ebfa');
INSERT INTO schema_migrations(version, applied_at, source_digest) VALUES (11, '2026-10-01T00:00:00.000Z', 'd1433e198769d26d402cc9c9749c241d9be8ed894c89979474af759847a0a3cf');
INSERT INTO schema_migrations(version, applied_at, source_digest) VALUES (12, '2026-10-01T00:00:00.000Z', '3e29a12a4c49ba8342e9cd61d9e4d2fc864da72cd9291ccccdd1b1196256f4ac');
INSERT INTO schema_migrations(version, applied_at, source_digest) VALUES (13, '2026-10-01T00:00:00.000Z', 'f5f42f0231ff3f8f78d460be7f08d5b71d61bcd5a78e70f4f44778f70bc217d8');
INSERT INTO schema_migrations(version, applied_at, source_digest) VALUES (14, '2026-10-01T00:00:00.000Z', 'e6790806f9a83560caad5a559ecb78fa0e156a1bce822e7f51bee092f1a5aaac');
INSERT INTO schema_migrations(version, applied_at, source_digest) VALUES (15, '2026-10-01T00:00:00.000Z', '5e3749118675e80fdeedc26dfe17fd35052111a639352a974aaf59f0af86ae2b');
INSERT INTO schema_migrations(version, applied_at, source_digest) VALUES (16, '2026-10-01T00:00:00.000Z', '396021709822126ac0227b3f21d46afdbd441c3d54c4cd8a005589b9147b6791');
INSERT INTO schema_migrations(version, applied_at, source_digest) VALUES (17, '2026-10-01T00:00:00.000Z', '79a84d757443e1bbe2b8a1e68d9ba37d1db4d813188e0b623074dc3a0e0ff96d');
INSERT INTO schema_migrations(version, applied_at, source_digest) VALUES (18, '2026-10-01T00:00:00.000Z', '51b9f08b5ceaa071b994d770a7946f16a4b05ec2ba4ae7714afe2a364ff89dfe');
INSERT INTO schema_migrations(version, applied_at, source_digest) VALUES (19, '2026-10-01T00:00:00.000Z', '72f884cea0ede27783ac518ceeca54b8da5f40f2dd72f3dd9e2f711342646bf0');
INSERT INTO schema_migrations(version, applied_at, source_digest) VALUES (20, '2026-10-01T00:00:00.000Z', 'ac35acd678744108117109cf97a8761f8e6d9efd1ce8b3097cd4fdeaa495ffa7');
INSERT INTO schema_migrations(version, applied_at, source_digest) VALUES (21, '2026-10-01T00:00:00.000Z', '50a752b3d9009b264b65a8552905b1585156ba3f7a886eee486c29fc68a28847');
INSERT INTO schema_migrations(version, applied_at, source_digest) VALUES (22, '2026-10-01T00:00:00.000Z', 'c8c1f64d68f189d0375e526a38f63e1a3726f8d2d1b2ff1df3a1ba5182081133');
INSERT INTO schema_migrations(version, applied_at, source_digest) VALUES (23, '2026-10-01T00:00:00.000Z', 'd82ef3ee1ca1fe811dae88d55a4379f194b4c8983b42458608a64aa6d6152b14');
INSERT INTO schema_migrations(version, applied_at, source_digest) VALUES (24, '2026-10-01T00:00:00.000Z', 'bcd59e063b7abfde77edfb7eb024edad6eb7f09152c1a0466969b3941ef5602c');
INSERT INTO schema_migrations(version, applied_at, source_digest) VALUES (25, '2026-10-01T00:00:00.000Z', 'b3e5fc5313013fbb2c105ca4e1f56235c7b12dac5830aa71267840036f333800');
INSERT INTO schema_migrations(version, applied_at, source_digest) VALUES (26, '2026-10-01T00:00:00.000Z', '4d814eff8e2cc6f8e5dcac7745e51d86208615223cf7a21fb61d834a81132f9f');
INSERT INTO schema_migrations(version, applied_at, source_digest) VALUES (27, '2026-10-01T00:00:00.000Z', 'a131953e979f9e7b103f29e8b40113e8522d586656eb1aaa14aa46b9576901ae');
INSERT INTO schema_migrations(version, applied_at, source_digest) VALUES (28, '2026-10-01T00:00:00.000Z', '010b721148b57fc384aaaae9e79a02cd662d848959bc42b835e58fd98a628283');
INSERT INTO schema_migrations(version, applied_at, source_digest) VALUES (29, '2026-10-01T00:00:00.000Z', '9cb5699ff62dfdd0cca52805c9c3f07385a11487c3bbae81164894a3db7063f7');
INSERT INTO schema_migrations(version, applied_at, source_digest) VALUES (30, '2026-10-01T00:00:00.000Z', 'da10a8ff42ef2da1ccbda1e25d195cca58a8dfe19877901f07e91fa97dd84a23');
INSERT INTO schema_migrations(version, applied_at, source_digest) VALUES (31, '2026-10-01T00:00:00.000Z', '846d4d477c8302ab51b782aead309de26470072217c8a552e90ca81f5a730868');
INSERT INTO schema_migrations(version, applied_at, source_digest) VALUES (32, '2026-10-01T00:00:00.000Z', '1aeccc827cad69c392f2d9f8989e1916ec9ad96a10fa03a32e857c35eacaf67a');
INSERT INTO schema_migrations(version, applied_at, source_digest) VALUES (33, '2026-10-01T00:00:00.000Z', 'a57f48b07ac92a136004b476df781ed11d0745c8a921404615b26841ce3139c7');
INSERT INTO schema_migrations(version, applied_at, source_digest) VALUES (34, '2026-10-01T00:00:00.000Z', 'ecb3a633df562cfdaf6b0b36a2dd443599a9e3ce276976c05c614f5d448bffe8');
INSERT INTO schema_migrations(version, applied_at, source_digest) VALUES (35, '2026-10-01T00:00:00.000Z', 'cca344be0fe042b90c0518d9b0622adc1d8e3a126438b69499719eb9b8f68711');
INSERT INTO schema_migrations(version, applied_at, source_digest) VALUES (36, '2026-10-01T00:00:00.000Z', '3d25a3ae9500e65fe86a78e7ce994d3dc208ecfbfdac42d7fa1cfa656bf6f2a6');
INSERT INTO schema_migrations(version, applied_at, source_digest) VALUES (37, '2026-10-01T00:00:00.000Z', '207941be8a5653dd1d3ef780f73cf1ce1230b9a51f15336b9736e4774e9bdea2');
INSERT INTO schema_migrations(version, applied_at, source_digest) VALUES (38, '2026-10-01T00:00:00.000Z', '42f42d526b5654f4ce3c67c6eef0c57706395698b5b4933d738d195f3e462ccd');
INSERT INTO schema_migrations(version, applied_at, source_digest) VALUES (39, '2026-10-01T00:00:00.000Z', 'e437ef49c1cdf1ccbcc002d050686740d44f68061f99ba3d09c0128d54247c74');
INSERT INTO schema_migrations(version, applied_at, source_digest) VALUES (40, '2026-10-01T00:00:00.000Z', '0b3b014a1ec832f82b8a0f3a3483cdd147e91e7cb907d8800463be69648c459d');
INSERT INTO schema_migrations(version, applied_at, source_digest) VALUES (41, '2026-10-01T00:00:00.000Z', '673460261ee2565e5c96f28838f8c0125cf252e83411e5bca474f214104f8050');
