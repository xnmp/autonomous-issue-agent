"""Offline resolve-family activation planning; ADR 0015 "Migration and rollback".

Legacy file observations and a verified deployment inventory are parsed into
either one exact activation plan or an explicit refusal. There is no permissive
default: corrupt, unreadable, ambiguous or unmapped legacy keys, unfinished
work and existing authority all refuse. The shadow comparison derives the new
authority through the real ceiling policy and must agree with the legacy file
semantics before anything is committed. Shadow data never decides eligibility.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from .workflow import MAX_DETAIL_CHARS, ResolveEpisode, WorkflowChanged, WorkflowSnapshot
from .workflow_identity import (
    FamilyAuthority, IssueRef, WorkflowFamily, WorkflowFence, WorkflowKey, WorkflowRevision,
    canonical_workflow_repository, validate_workflow_identifier, validate_workflow_integer,
    validate_workflow_text,
)
from .workflow_policy import reconcile_resolve_ceiling

LEGACY_RETRIES = "resolve-retries"
LEGACY_LAST_ERROR = "last-error"
LEGACY_SUFFIXES = (LEGACY_RETRIES, LEGACY_LAST_ERROR)
MAX_LEGACY_FILE_BYTES = 65536
MAX_LEGACY_RETRIES = 2**31
MAX_INVENTORY_ITEM_CHARS = 512
FIRST_EPOCH = 1

_DIGEST = re.compile(r"[0-9a-f]{64}")
_RETRIES = re.compile(r"[0-9]{1,10}\n?")


def _digest(value: str, name: str) -> None:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase sha256 hex digest")


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


@dataclass(frozen=True)
class LegacyAbsent:
    """The file does not exist; distinct from an unreadable or empty file."""


@dataclass(frozen=True)
class LegacyPresent:
    digest: str
    text: str

    def __post_init__(self) -> None:
        _digest(self.digest, "legacy source digest")
        if not isinstance(self.text, str) or len(self.text.encode("utf-8", "surrogatepass")) > MAX_LEGACY_FILE_BYTES:
            raise ValueError("legacy source text must be bounded decoded UTF-8")


@dataclass(frozen=True)
class LegacyUnreadable:
    reason: str

    def __post_init__(self) -> None:
        validate_workflow_text(self.reason, "unreadable legacy source reason", limit=MAX_DETAIL_CHARS)


LegacyFile = LegacyAbsent | LegacyPresent | LegacyUnreadable


@dataclass(frozen=True)
class LegacyResolveRecord:
    """`root` labels the scanned state root, so a moved file changes the digest."""

    issue: int
    retries: LegacyFile
    last_error: LegacyFile
    root: str = "fix"

    def __post_init__(self) -> None:
        validate_workflow_integer(self.issue, "legacy issue number", minimum=1)
        _root(self.root)
        for value in (self.retries, self.last_error):
            if not isinstance(value, (LegacyAbsent, LegacyPresent, LegacyUnreadable)):
                raise ValueError("legacy record requires typed file observations")


@dataclass(frozen=True)
class UnmappedLegacyEntry:
    """A legacy-looking resolve key that maps to no single issue target."""

    name: str
    reason: str
    root: str = "fix"

    def __post_init__(self) -> None:
        validate_workflow_identifier(self.name, "unmapped legacy entry")
        validate_workflow_identifier(self.reason, "unmapped legacy reason")
        _root(self.root)


LegacyEntry = LegacyResolveRecord | UnmappedLegacyEntry


def _root(value: str) -> None:
    validate_workflow_text(value, "legacy state root label", limit=4096)


def _sorted_unique(values: tuple[str, ...], name: str) -> None:
    if not isinstance(values, tuple) or any(
        not isinstance(item, str) or not item or len(item) > MAX_INVENTORY_ITEM_CHARS or "\x00" in item
        for item in values
    ):
        raise ValueError(f"{name} must be a tuple of bounded identifiers")
    if list(values) != sorted(set(values)):
        raise ValueError(f"{name} must be sorted and unique")


@dataclass(frozen=True)
class ExistingActivation:
    """Retained epoch and its recorded evidence: the ceiling and confirmed plan
    digest are durable even when the activation imported no target."""

    epoch: int
    source_digest: str
    inventory_digest: str
    imports_digest: str
    ceiling: int
    plan_digest: str

    def __post_init__(self) -> None:
        validate_workflow_integer(self.epoch, "activation epoch", minimum=1)
        validate_workflow_integer(self.ceiling, "activation ceiling")
        for value, name in ((self.source_digest, "activation source digest"),
                            (self.inventory_digest, "activation inventory digest"),
                            (self.imports_digest, "activation imports digest"),
                            (self.plan_digest, "activation plan digest")):
            _digest(value, name)


@dataclass(frozen=True)
class DeploymentInventory:
    """Non-quiescent work and retained authority for one repository.

    Runs and leases are counted whatever their workflow binding: indexed
    bound-workflow queries cannot prove that unbound legacy work has stopped.
    """

    repository: str
    active_runs: tuple[str, ...] = ()
    leases: tuple[str, ...] = ()
    dispatch_claims: tuple[str, ...] = ()
    open_commands: tuple[str, ...] = ()
    active_reservations: tuple[str, ...] = ()
    activation: ExistingActivation | None = None
    retained_targets: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if canonical_workflow_repository(self.repository) != self.repository:
            raise ValueError("inventory repository must be canonical")
        for name in ("active_runs", "leases", "dispatch_claims", "open_commands",
                     "active_reservations", "retained_targets"):
            _sorted_unique(getattr(self, name), name.replace("_", " "))
        if self.activation is not None and not isinstance(self.activation, ExistingActivation):
            raise ValueError("inventory activation must be typed")

    @property
    def quiescent(self) -> bool:
        return not (self.active_runs or self.leases or self.dispatch_claims
                    or self.open_commands or self.active_reservations)

    @property
    def digest(self) -> str:
        # Activation evidence is this plan's own output, so it cannot be an
        # input to the digest that the activation records.
        return _sha256({
            "repository": self.repository,
            "active_runs": list(self.active_runs), "leases": list(self.leases),
            "dispatch_claims": list(self.dispatch_claims),
            "open_commands": list(self.open_commands),
            "active_reservations": list(self.active_reservations),
        })

    def blockers(self) -> tuple[str, ...]:
        labels = (("active_runs", "active run"), ("leases", "process lease"),
                  ("dispatch_claims", "dispatch claim"), ("open_commands", "non-terminal lifecycle command"),
                  ("active_reservations", "unsettled budget reservation"))
        return tuple(
            f"{label}s must be drained or recovered first: {', '.join(values[:8])}"
            + (f" (+{len(values) - 8} more)" if len(values) > 8 else "")
            for name, label in labels if (values := getattr(self, name))
        )


@dataclass(frozen=True)
class ImportedResolveTarget:
    issue: int
    episode_id: str
    failures: int
    last_failure: str
    ceiling: int
    held: bool
    retries_source: str
    last_error_source: str

    def __post_init__(self) -> None:
        validate_workflow_integer(self.issue, "imported issue", minimum=1)
        validate_workflow_identifier(self.episode_id, "imported episode")
        validate_workflow_integer(self.failures, "imported failures")
        if self.failures > MAX_LEGACY_RETRIES:
            raise ValueError("imported failures exceed the legacy counter range")
        validate_workflow_text(self.last_failure, "imported last failure", limit=MAX_DETAIL_CHARS, empty=True)
        validate_workflow_integer(self.ceiling, "imported ceiling")
        if type(self.held) is not bool:
            raise ValueError("imported hold must be a boolean")
        if self.held != bool(self.ceiling and self.failures >= self.ceiling):
            raise ValueError("imported hold contradicts its ceiling and failures")
        for source in (self.retries_source, self.last_error_source):
            if source != "absent":
                _digest(source, "imported source")

    def canonical(self) -> str:
        return _canonical_json({
            "issue": self.issue, "episode_id": self.episode_id, "failures": self.failures,
            "last_failure": self.last_failure, "ceiling": self.ceiling, "held": self.held,
            "retries_source": self.retries_source, "last_error_source": self.last_error_source,
        })

    @classmethod
    def parse(cls, raw: str) -> ImportedResolveTarget:
        """Validate a retained decision; only its exact canonical form is trusted."""
        # ASCII-escaped JSON spends up to 12 characters per astral code point.
        if not isinstance(raw, str) or len(raw) > 12 * MAX_DETAIL_CHARS + 4096:
            raise ValueError("import decision must be bounded text")
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {
            "issue", "episode_id", "failures", "last_failure", "ceiling", "held",
            "retries_source", "last_error_source",
        }:
            raise ValueError("import decision has the wrong fields")
        decision = cls(**value)
        if decision.canonical() != raw:
            raise ValueError("import decision is not canonical")
        return decision


def imports_digest(imports: tuple[ImportedResolveTarget, ...]) -> str:
    return _sha256([item.canonical() for item in sorted(imports, key=lambda item: item.issue)])


@dataclass(frozen=True)
class ResolveActivationPlan:
    repository: str
    epoch: int
    ceiling: int
    source_digest: str
    inventory_digest: str
    imports: tuple[ImportedResolveTarget, ...]

    def __post_init__(self) -> None:
        if canonical_workflow_repository(self.repository) != self.repository:
            raise ValueError("activation repository must be canonical")
        validate_workflow_integer(self.epoch, "activation epoch", minimum=1)
        validate_workflow_integer(self.ceiling, "activation ceiling")
        _digest(self.source_digest, "activation source digest")
        _digest(self.inventory_digest, "activation inventory digest")
        if not isinstance(self.imports, tuple) or any(not isinstance(i, ImportedResolveTarget) for i in self.imports):
            raise ValueError("activation imports must be typed")
        issues = [item.issue for item in self.imports]
        if issues != sorted(set(issues)):
            raise ValueError("activation imports must be sorted by unique issue")
        if any(item.ceiling != self.ceiling for item in self.imports):
            raise ValueError("every import decision must use the plan ceiling")

    @property
    def authority(self) -> FamilyAuthority:
        return FamilyAuthority(WorkflowFamily.RESOLVE, self.epoch)

    @property
    def imports_digest(self) -> str:
        return imports_digest(self.imports)

    @property
    def plan_digest(self) -> str:
        """What an operator reviews and confirms: every committed decision."""
        return _sha256({
            "repository": self.repository, "family": WorkflowFamily.RESOLVE.value,
            "epoch": self.epoch, "ceiling": self.ceiling, "source_digest": self.source_digest,
            "inventory_digest": self.inventory_digest,
            "imports": [item.canonical() for item in self.imports],
        })

    def matches(self, existing: ExistingActivation) -> bool:
        return (existing.epoch, existing.source_digest, existing.inventory_digest, existing.imports_digest,
                existing.ceiling, existing.plan_digest) == (
            self.epoch, self.source_digest, self.inventory_digest, self.imports_digest,
            self.ceiling, self.plan_digest)


@dataclass(frozen=True)
class AlreadyActive:
    """The retained activation is exactly the plan these sources produce."""

    plan: ResolveActivationPlan


@dataclass(frozen=True)
class ActiveWithDifferences:
    """Activated authority stands; current sources or configuration differ.

    Retained legacy files are evidence only: they are never re-imported and a
    different plan is never activated over an existing epoch.
    """

    activation: ExistingActivation
    reasons: tuple[str, ...]
    recorded: ResolveActivationPlan | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.activation, ExistingActivation) or not self.reasons:
            raise ValueError("activation differences require the activation and reasons")
        if self.recorded is not None and not self.recorded.matches(self.activation):
            raise ValueError("a recorded plan must reproduce its activation evidence")


@dataclass(frozen=True)
class ActivationBlocked:
    repository: str
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.reasons, tuple) or not self.reasons or any(
            not isinstance(reason, str) or not reason for reason in self.reasons
        ):
            raise ValueError("a blocked activation requires its reasons")


ActivationDecision = ResolveActivationPlan | AlreadyActive | ActiveWithDifferences | ActivationBlocked


class _Blocked(ValueError):
    pass


def _source(file: LegacyFile) -> str:
    if isinstance(file, LegacyPresent):
        return file.digest
    return "absent" if isinstance(file, LegacyAbsent) else "unreadable"


def _entry_sources(entry: LegacyEntry) -> list[list[object]]:
    if isinstance(entry, UnmappedLegacyEntry):
        return [["unmapped", entry.root, entry.name, entry.reason]]
    return [[entry.root, entry.issue, LEGACY_RETRIES, _source(entry.retries)],
            [entry.root, entry.issue, LEGACY_LAST_ERROR, _source(entry.last_error)]]


def source_digest(repository: str, entries: tuple[LegacyEntry, ...]) -> str:
    rows = sorted((row for entry in entries for row in _entry_sources(entry)), key=_canonical_json)
    return _sha256({"repository": repository, "family": WorkflowFamily.RESOLVE.value, "sources": rows})


def parse_legacy_retries(file: LegacyFile) -> int:
    """The retired counter's exact shape; absent reads as zero, as it did live."""
    if isinstance(file, LegacyAbsent):
        return 0
    if isinstance(file, LegacyUnreadable):
        raise _Blocked(f"{LEGACY_RETRIES} is unreadable: {file.reason}")
    if _RETRIES.fullmatch(file.text) is None:
        raise _Blocked(f"{LEGACY_RETRIES} is not a bounded ASCII decimal counter")
    value = int(file.text)
    if value > MAX_LEGACY_RETRIES:
        raise _Blocked(f"{LEGACY_RETRIES} exceeds {MAX_LEGACY_RETRIES}")
    return value


def parse_legacy_last_error(file: LegacyFile) -> str:
    if isinstance(file, LegacyAbsent):
        return ""
    if isinstance(file, LegacyUnreadable):
        raise _Blocked(f"{LEGACY_LAST_ERROR} is unreadable: {file.reason}")
    if "\x00" in file.text:
        raise _Blocked(f"{LEGACY_LAST_ERROR} contains NUL")
    normalized = " ".join(file.text.split())
    if len(normalized) > MAX_DETAIL_CHARS:
        raise _Blocked(f"{LEGACY_LAST_ERROR} exceeds {MAX_DETAIL_CHARS} characters")
    return normalized


def imported_episode_id(repository: str, issue: int, epoch: int, digest: str) -> str:
    material = f"{repository}\0{issue}\0{WorkflowFamily.RESOLVE.value}\0{epoch}\0{digest}"
    return "resolve-imported:" + hashlib.sha256(material.encode()).hexdigest()


def import_evidence_id(plan: ResolveActivationPlan, target: ImportedResolveTarget) -> str:
    return "resolve-activation-import:" + hashlib.sha256(
        f"{plan.repository}\0{target.issue}\0{plan.epoch}\0{plan.source_digest}".encode()
    ).hexdigest()


def imported_snapshot(plan: ResolveActivationPlan, target: ImportedResolveTarget) -> WorkflowSnapshot:
    """The row an import installs: generation 0, version 0, the imported episode."""
    return WorkflowSnapshot(
        WorkflowRevision(WorkflowFence(WorkflowKey(plan.repository, IssueRef(target.issue)), 0), 0),
        plan.authority, ResolveEpisode(target.episode_id, target.failures, target.last_failure),
    )


def imported_workflow(plan: ResolveActivationPlan, target: ImportedResolveTarget) -> WorkflowSnapshot:
    """An imported exhausted episode is held before its first new attempt."""
    initial = imported_snapshot(plan, target)
    transition = reconcile_resolve_ceiling(
        initial, ceiling=plan.ceiling, evidence_ids=(import_evidence_id(plan, target),),
    )
    return transition.snapshot if isinstance(transition, WorkflowChanged) else initial


@dataclass(frozen=True)
class ShadowRow:
    """Remaining allowance is None when the ceiling is disabled (unbounded)."""

    issue: int
    legacy_failures: int
    legacy_exhausted: bool
    legacy_remaining: int | None
    new_failures: int
    new_held: bool
    new_remaining: int | None

    @property
    def legacy_eligible(self) -> bool:
        return not self.legacy_exhausted

    @property
    def new_eligible(self) -> bool:
        return not self.new_held

    @property
    def agrees(self) -> bool:
        return (self.legacy_failures, self.legacy_exhausted, self.legacy_remaining) == (
            self.new_failures, self.new_held, self.new_remaining)


def _legacy_row(issue: int, failures: int, ceiling: int) -> tuple[int, bool, int | None]:
    exhausted = ceiling > 0 and failures >= ceiling
    return failures, exhausted, (max(0, ceiling - failures) if ceiling > 0 else None)


def shadow_compare(
    plan: ResolveActivationPlan, records: tuple[LegacyEntry, ...], ceiling: int,
) -> tuple[ShadowRow, ...]:
    """Legacy file semantics versus the authority the plan would install."""
    validate_workflow_integer(ceiling, "shadow ceiling")
    imports = {target.issue: target for target in plan.imports}
    rows: list[ShadowRow] = []
    for record in sorted((r for r in records if isinstance(r, LegacyResolveRecord)), key=lambda r: r.issue):
        try:
            legacy = _legacy_row(record.issue, parse_legacy_retries(record.retries), ceiling)
        except _Blocked:
            continue
        target = imports.get(record.issue)
        if target is None:
            new_failures, held = 0, False
        else:
            workflow = imported_workflow(plan, target)
            new_failures, held = workflow.episode.failures, bool(workflow.gates)
        remaining = None if ceiling == 0 else 0 if held else max(0, ceiling - new_failures)
        rows.append(ShadowRow(record.issue, *legacy, new_failures, held, remaining))
    return tuple(rows)


def _imports(
    repository: str, entries: tuple[LegacyEntry, ...], digest: str, ceiling: int,
) -> tuple[tuple[ImportedResolveTarget, ...], tuple[str, ...]]:
    blockers: list[str] = []
    seen: dict[int, int] = {}
    for entry in entries:
        if isinstance(entry, UnmappedLegacyEntry):
            blockers.append(f"unmapped legacy key {entry.name!r} in {entry.root}: {entry.reason}")
        else:
            seen[entry.issue] = seen.get(entry.issue, 0) + 1
    roots: dict[int, list[str]] = {}
    for entry in entries:
        if isinstance(entry, LegacyResolveRecord):
            roots.setdefault(entry.issue, []).append(entry.root)
    blockers.extend(
        f"issue #{issue}: ambiguous duplicate legacy records in {', '.join(sorted(roots[issue]))}"
        for issue, count in sorted(seen.items()) if count > 1
    )
    imports: list[ImportedResolveTarget] = []
    for record in sorted((e for e in entries if isinstance(e, LegacyResolveRecord)), key=lambda r: r.issue):
        if seen[record.issue] > 1:
            continue
        if isinstance(record.retries, LegacyAbsent) and isinstance(record.last_error, LegacyAbsent):
            continue
        try:
            failures = parse_legacy_retries(record.retries)
            last_failure = parse_legacy_last_error(record.last_error)
        except _Blocked as exc:
            blockers.append(f"issue #{record.issue}: {exc}")
            continue
        imports.append(ImportedResolveTarget(
            record.issue, imported_episode_id(repository, record.issue, FIRST_EPOCH, digest),
            failures, last_failure, ceiling, bool(ceiling and failures >= ceiling),
            _source(record.retries), _source(record.last_error),
        ))
    return tuple(imports), tuple(blockers)


def _recognise(
    repository: str, records: tuple[LegacyEntry, ...], digest: str, ceiling: int, existing: ExistingActivation,
) -> AlreadyActive | ActiveWithDifferences:
    """Rebuild the activated plan from its recorded evidence, never from
    today's live work or configuration, then diagnose what differs now."""
    if digest != existing.source_digest:
        return ActiveWithDifferences(existing, (
            f"legacy sources changed since activation at epoch {existing.epoch} "
            f"(activated source digest {existing.source_digest}, current {digest}); "
            "retained files are evidence only and are never re-imported",
        ))
    imports, blockers = _imports(repository, records, digest, existing.ceiling)
    recorded = None if blockers else ResolveActivationPlan(
        repository, existing.epoch, existing.ceiling, digest, existing.inventory_digest, imports,
    )
    if recorded is None or not recorded.matches(existing):
        return ActiveWithDifferences(existing, tuple(blockers) or (
            "recorded activation evidence does not reproduce from its unchanged sources",
        ))
    if existing.ceiling != ceiling:
        return ActiveWithDifferences(existing, (
            f"configured resolve ceiling {ceiling} differs from the ceiling {existing.ceiling} "
            "used at activation; imported holds keep their activated decisions",
        ), recorded)
    return AlreadyActive(recorded)


def plan_resolve_activation(
    repository: str, records: tuple[LegacyEntry, ...], inventory: DeploymentInventory, *, ceiling: int,
) -> ActivationDecision:
    repository = canonical_workflow_repository(repository)
    validate_workflow_integer(ceiling, "resolve ceiling")
    if not isinstance(records, tuple) or any(
        not isinstance(r, (LegacyResolveRecord, UnmappedLegacyEntry)) for r in records
    ):
        raise ValueError("activation requires typed legacy observations")
    if not isinstance(inventory, DeploymentInventory) or inventory.repository != repository:
        raise ValueError("activation inventory belongs to another repository")
    digest = source_digest(repository, records)
    imports, blockers = _imports(repository, records, digest, ceiling)
    candidate = None if blockers else ResolveActivationPlan(
        repository, FIRST_EPOCH, ceiling, digest, inventory.digest, imports,
    )
    reasons = list(blockers)
    if candidate is not None:
        reasons.extend(
            f"issue #{row.issue}: shadow comparison disagrees with the legacy ceiling semantics"
            for row in shadow_compare(candidate, records, ceiling) if not row.agrees
        )
    if inventory.activation is not None:
        return _recognise(repository, records, digest, ceiling, inventory.activation)
    reasons.extend(inventory.blockers())
    if inventory.retained_targets:
        reasons.append("workflow authority is retained without an activation: "
                       + ", ".join(inventory.retained_targets[:8]))
    if reasons or candidate is None:
        return ActivationBlocked(repository, tuple(reasons))
    return candidate


@dataclass(frozen=True)
class ActivationCommitted:
    """`idempotent` reports that this exact plan was already the authority."""

    plan: ResolveActivationPlan
    idempotent: bool = False
