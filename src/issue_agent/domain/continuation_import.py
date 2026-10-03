"""Closed stopped-import decisions for the fixed fix and steward components."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import re

from . import continuation_components as cc
from .refs import issue_from_branch
from .actions import Target
from .self_clear import BudgetBlock, MachineBlock, MachineBlocker
from .resolve_activation import LegacyAbsent, LegacyFile, LegacyPresent, LegacyUnreadable
from .workflow_identity import (
    WorkflowFamily, WorkflowKey, WorkflowRevision, canonical_workflow_repository,
    family_owns_target, validate_workflow_integer,
)

ISSUE_FIELDS = ("resumed-at", "resumes", "ci-retries", "review-retries", "rebase-retries",
                "infra-deaths", "self-clears", "branch", "session", "model", "machine-block",
                "budget-block", "self-clear-ceiling", "ci-history", "stall-sig")
PR_FIELDS = ("resumed-at", "session", "model", "branch", "infra-deaths", "stall-sig", "ci-rerun-head", "ci-cancel-head")
STEWARD_FIELDS = ("triage-at", "groom-at", "groom-content", "steward-stalls-triage",
                  "steward-stalls-groom", "infra-deaths")
IMPORT_FAMILIES = (WorkflowFamily.ISSUE_RECOVERY, WorkflowFamily.PR_REPAIR, WorkflowFamily.STEWARD)
MAX_IMPORT_TARGETS = 4096


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode()).hexdigest()


def _plan_state_identity(state: ImportedState | None) -> object:
    if isinstance(state, cc.PRRepairState) and (state.ci_rerun is not None or state.ci_cancel is not None):
        return (_plan_state_identity(replace(state, ci_rerun=None, ci_cancel=None)), repr(state.ci_rerun), repr(state.ci_cancel))
    if isinstance(state, cc.IssueRecoveryState) and state.self_clear_ceiling_reported:
        return (_plan_state_identity(replace(state, self_clear_ceiling_reported=False)), "self-clear-ceiling-reported")
    if isinstance(state, cc.IssueRecoveryState) and state.budget_block is not None:
        return (repr(state), repr(state.machine_block), repr(state.budget_block))
    if isinstance(state, cc.IssueRecoveryState) and state.machine_block is not None:
        return (repr(state), repr(state.machine_block))
    if isinstance(state, cc.IssueRecoveryState) and not state.review_failures.review_at and state.review_failures.pr is None and state.review_failures.pending is None:
        return repr(state).replace(f"review_failures={state.review_failures!r}", f"review_failures={state.review_failures.count}", 1)
    if isinstance(state, cc.StewardState) and state.groom_content != cc.GroomContent():
        return (repr(state), repr(state.groom_content))
    if isinstance(state, cc.PRRepairState) and (state.resumes or state.actual_model is not None):
        return (repr(state), state.resumes, repr(state.actual_model))
    return repr(state)


@dataclass(frozen=True)
class LegacyContinuationRecord:
    key: WorkflowKey
    family: WorkflowFamily
    root: str
    fields: tuple[tuple[str, LegacyFile], ...]

    def __post_init__(self) -> None:
        names = ISSUE_FIELDS if self.family is WorkflowFamily.ISSUE_RECOVERY else PR_FIELDS if self.family is WorkflowFamily.PR_REPAIR else STEWARD_FIELDS
        if (not isinstance(self.family, WorkflowFamily) or self.family not in IMPORT_FAMILIES or not isinstance(self.key, WorkflowKey)
                or not family_owns_target(self.family, self.key.target)
                or not isinstance(self.root, str) or not 0 < len(self.root) <= 512
                or not isinstance(self.fields, tuple) or tuple(name for name, _ in self.fields) != names
                or any(not isinstance(source, (LegacyAbsent, LegacyPresent, LegacyUnreadable)) for _, source in self.fields)):
            raise ValueError("continuation source requires its complete fixed field set and owner")

    @property
    def source_digest(self) -> str:
        return digest((self.root, self.family.value, self.key.target.number,
                       tuple((name, type(value).__name__, value.digest if isinstance(value, LegacyPresent)
                              else None if isinstance(value, LegacyAbsent) else value.reason)
                             for name, value in self.fields
                             if name not in {"ci-rerun-head", "ci-cancel-head"} or not isinstance(value, LegacyAbsent))))


ImportedState = cc.IssueRecoveryState | cc.PRRepairState | cc.StewardState


@dataclass(frozen=True)
class ContinuationImportTarget:
    key: WorkflowKey
    family: WorkflowFamily
    revision: WorkflowRevision | None
    source_digest: str
    state: ImportedState | None
    blockers: tuple[cc.ContinuationImportBlocker, ...] = ()
    authority_digest: str = digest(None)

    def __post_init__(self) -> None:
        expected = cc.IssueRecoveryState if self.family is WorkflowFamily.ISSUE_RECOVERY else cc.PRRepairState if self.family is WorkflowFamily.PR_REPAIR else cc.StewardState
        if (not isinstance(self.family, WorkflowFamily) or self.family not in IMPORT_FAMILIES or not isinstance(self.key, WorkflowKey)
                or not family_owns_target(self.family, self.key.target)
                or self.revision is not None and (not isinstance(self.revision, WorkflowRevision) or self.revision.fence.key != self.key)
                or not isinstance(self.source_digest, str) or not re.fullmatch("[0-9a-f]{64}", self.source_digest)
                or not isinstance(self.authority_digest, str) or not re.fullmatch("[0-9a-f]{64}", self.authority_digest)
                or (self.state is None) != bool(self.blockers)
                or self.state is not None and not isinstance(self.state, expected)
                or not isinstance(self.blockers, tuple) or len(self.blockers) > 64
                or any(not isinstance(b, cc.ContinuationImportBlocker) for b in self.blockers)):
            raise ValueError("import target requires exact identity and either verified state or retained blockers")


@dataclass(frozen=True)
class ContinuationImportPlan:
    repository: str
    families: tuple[WorkflowFamily, ...]
    epoch: int
    source_digest: str
    inventory_digest: str
    file_digest: str
    targets: tuple[ContinuationImportTarget, ...]

    def __post_init__(self) -> None:
        if (canonical_workflow_repository(self.repository) != self.repository
                or not isinstance(self.families, tuple) or any(not isinstance(f, WorkflowFamily) for f in self.families)
                or self.families not in ((WorkflowFamily.ISSUE_RECOVERY, WorkflowFamily.PR_REPAIR), (WorkflowFamily.STEWARD,))
                or not isinstance(self.targets, tuple) or len(self.targets) > MAX_IMPORT_TARGETS
                or any(not isinstance(t, ContinuationImportTarget) or t.key.repository != self.repository
                       or t.family not in self.families for t in self.targets)
                or len({(t.key, t.family) for t in self.targets}) != len(self.targets)
                or any(not isinstance(x, str) or not re.fullmatch("[0-9a-f]{64}", x) for x in (self.source_digest, self.inventory_digest, self.file_digest))):
            raise ValueError("continuation import requires one complete closed family plan")
        validate_workflow_integer(self.epoch, "continuation import epoch", minimum=1)

    @property
    def plan_digest(self) -> str:
        return digest((self.repository, tuple(f.value for f in self.families), self.epoch,
                       self.source_digest, self.inventory_digest, self.file_digest,
                       tuple((t.family.value, t.key.target.number, t.source_digest,
                              None if t.revision is None else (t.revision.fence.generation, t.revision.version),
                              t.authority_digest, _plan_state_identity(t.state), tuple((b.source, b.reason) for b in t.blockers)) for t in self.targets)))


def _machine_block(record: LegacyContinuationRecord, text: str) -> MachineBlock:
    if record.family is not WorkflowFamily.ISSUE_RECOVERY or len(text) > 4096:
        raise ValueError("machine hold requires bounded issue evidence")
    value = json.loads(text)
    if not isinstance(value, dict) or set(value) != {"target", "number", "pr", "blocker", "blocked_head"}:
        raise ValueError("machine hold requires its exact fixed fields")
    return MachineBlock(record.key.target.number, Target[value["target"]], value["number"],
                        value["pr"], MachineBlocker(value["blocker"]), value["blocked_head"])


def _budget_block(record: LegacyContinuationRecord, text: str) -> BudgetBlock:
    if record.family is not WorkflowFamily.ISSUE_RECOVERY or len(text) > 4096:
        raise ValueError("budget hold requires bounded issue evidence")
    value = json.loads(text)
    if not isinstance(value, dict) or set(value) != {"target", "number", "reservation_micros", "scope", "cap_micros", "old_headroom_micros", "currency"}:
        raise ValueError("budget hold requires its exact fixed fields")
    block = BudgetBlock(record.key.target.number, Target[value["target"]], value["number"],
                        value["reservation_micros"], value["scope"], value["cap_micros"],
                        value["old_headroom_micros"], value["currency"])
    if block.scope not in {"repository", "global", f"issue:{block.issue}"}:
        raise ValueError("budget hold has an unsupported or mismatched cap scope")
    return block


def classify_record(record: LegacyContinuationRecord) -> ImportedState | tuple[cc.ContinuationImportBlocker, ...]:
    """Only direct typed facts are imported; unsupported provenance stays held."""
    values: dict[str, str] = {}
    blockers: list[cc.ContinuationImportBlocker] = []
    for name, source in record.fields:
        if isinstance(source, LegacyAbsent):
            continue
        reason = source.reason if not isinstance(source, LegacyPresent) else ""
        if isinstance(source, LegacyPresent):
            text = source.text.strip()
            if name == "groom-content":
                if not re.fullmatch(r"[0-9a-f]{64}", text):
                    reason = "malformed canonical title/body SHA-256 signature"
            elif name in {"triage-at", "groom-at"}:
                try:
                    cc.StewardLaneState(baseline_at=text)
                except ValueError as exc:
                    reason = str(exc)
                if not text:
                    reason = "empty present continuation field"
            elif name == "machine-block":
                try:
                    _machine_block(record, text)
                except (ValueError, TypeError, KeyError) as exc:
                    reason = str(exc)
            elif name == "budget-block":
                try:
                    _budget_block(record, text)
                except (ValueError, TypeError, KeyError) as exc:
                    reason = str(exc)
            elif name == "self-clear-ceiling":
                if text != "1":
                    reason = "malformed shared self-clear ceiling report marker"
            elif name in {"ci-rerun-head", "ci-cancel-head"}:
                try:
                    head, separator, at = text.partition("@") if name == "ci-cancel-head" else (text, "", "")
                    cc.ImportedCIRemediation(head, at if separator else "", "legacy:" + digest((record.root, name, source.digest)))
                except (ValueError, TypeError) as exc:
                    reason = str(exc)
            elif name in {"session", "model", "ci-history", "stall-sig"} or name == "infra-deaths" and record.family is WorkflowFamily.STEWARD:
                reason = "requires its fixed family provenance parser; no empty-state inference"
            elif name == "review-retries":
                at, separator, number = text.rpartition(" ")
                try:
                    if not separator or not re.fullmatch(r"0|[1-9][0-9]{0,18}", number):
                        raise ValueError("requires qualifying review timestamp and bounded count")
                    cc.ReviewRetryState(at, int(number))
                    if not at:
                        raise ValueError("qualifying review timestamp is absent")
                except ValueError as exc:
                    reason = str(exc)
            elif name in {"resumes", "ci-retries", "rebase-retries", "infra-deaths", "self-clears", "steward-stalls-triage", "steward-stalls-groom"}:
                if not re.fullmatch(r"0|[1-9][0-9]{0,18}", text) or int(text) > 2**63 - 1:
                    reason = "malformed nonnegative bounded counter"
            elif not text:
                reason = "empty present continuation field"
            values[name] = text
        if reason:
            blockers.append(cc.ContinuationImportBlocker("legacy:" + digest((record.root, name)), f"{name}: {reason}"))
    if blockers:
        return tuple(blockers)
    if values.get("groom-content") and not values.get("groom-at"):
        return (cc.ContinuationImportBlocker("legacy:" + digest((record.root, "groom-content")),
                "groom-content: signature has no matching observed groom-at"),)
    try:
        count = lambda name: int(values.get(name, "0"))
        if record.family is WorkflowFamily.ISSUE_RECOVERY:
            if values.get("branch") and issue_from_branch(values["branch"]) != record.key.target.number:
                raise ValueError("remembered issue branch lacks matching target ownership")
            review_at, _, review_count = values.get("review-retries", " 0").rpartition(" ")
            return cc.IssueRecoveryState(values.get("resumed-at", ""), count("resumes"), values.get("branch", ""),
                                         count("ci-retries"), cc.ReviewRetryState(review_at, int(review_count)), count("rebase-retries"),
                                         count("infra-deaths"), count("self-clears"),
                                         machine_block=_machine_block(record, values["machine-block"]) if "machine-block" in values else None,
                                         budget_block=_budget_block(record, values["budget-block"]) if "budget-block" in values else None,
                                         self_clear_ceiling_reported="self-clear-ceiling" in values)
        if record.family is WorkflowFamily.PR_REPAIR:
            def imported(name):
                text = values.get(name)
                if text is None:
                    return None
                head, separator, at = text.partition("@") if name == "ci-cancel-head" else (text, "", "")
                source = dict(record.fields)[name]
                assert isinstance(source, LegacyPresent)
                return cc.ImportedCIRemediation(head, at if separator else "", "legacy:" + digest((record.root, name, source.digest)))
            return cc.PRRepairState(consumed_reply_at=values.get("resumed-at", ""), branch=values.get("branch", ""),
                                    infra_failures=count("infra-deaths"), ci_rerun=imported("ci-rerun-head"), ci_cancel=imported("ci-cancel-head"))
        return cc.StewardState(cc.StewardLaneState(values.get("triage-at", ""), count("steward-stalls-triage"), count("infra-deaths")),
                              cc.StewardLaneState(values.get("groom-at", ""), count("steward-stalls-groom"), count("infra-deaths")),
                              cc.GroomContent(values.get("groom-content", ""), values.get("groom-content", "")))
    except (ValueError, TypeError) as exc:
        return (cc.ContinuationImportBlocker("legacy:" + record.source_digest, str(exc)),)


def issue_reply_shadow(plan: ContinuationImportPlan, records: tuple[LegacyContinuationRecord, ...]) -> tuple[tuple[WorkflowKey, tuple[str, int, str], tuple[str, int, str]], ...]:
    comparisons = []
    for target in plan.targets:
        if not isinstance(target.state, cc.IssueRecoveryState):
            continue
        found = tuple(record for record in records if record.key == target.key and record.family is WorkflowFamily.ISSUE_RECOVERY)
        if len(found) > 1:
            raise ValueError("eligible shadow target has ambiguous source ownership")
        fields = dict(found[0].fields) if found else {}
        def legacy(name: str, default: str) -> str:
            source = fields.get(name, LegacyAbsent())
            if isinstance(source, LegacyAbsent):
                return default
            if not isinstance(source, LegacyPresent):
                raise ValueError("eligible shadow target has unreadable selected input")
            return source.text.strip()
        before = (legacy("resumed-at", ""), int(legacy("resumes", "0")), legacy("branch", ""))
        after = (target.state.consumed_reply_at, target.state.resumes, target.state.remembered_branch)
        comparisons.append((target.key, before, after))
    return tuple(comparisons)


def ci_shadow(plan: ContinuationImportPlan, records: tuple[LegacyContinuationRecord, ...]) -> tuple[tuple[WorkflowKey, int, int], ...]:
    comparisons = []
    for target in plan.targets:
        if not isinstance(target.state, cc.IssueRecoveryState):
            continue
        found = tuple(record for record in records if record.key == target.key and record.family is WorkflowFamily.ISSUE_RECOVERY)
        if len(found) > 1:
            raise ValueError("eligible CI shadow has ambiguous source ownership")
        source = dict(found[0].fields).get("ci-retries", LegacyAbsent()) if found else LegacyAbsent()
        if not isinstance(source, (LegacyAbsent, LegacyPresent)):
            raise ValueError("eligible CI shadow has unreadable selected debt")
        before = int(source.text.strip()) if isinstance(source, LegacyPresent) else 0
        comparisons.append((target.key, before, target.state.ci_failures))
    return tuple(comparisons)


def rebase_shadow(plan: ContinuationImportPlan, records: tuple[LegacyContinuationRecord, ...]) -> tuple[tuple[WorkflowKey, int, int], ...]:
    comparisons = []
    for target in plan.targets:
        if not isinstance(target.state, cc.IssueRecoveryState):
            continue
        found = tuple(record for record in records if record.key == target.key and record.family is WorkflowFamily.ISSUE_RECOVERY)
        if len(found) > 1:
            raise ValueError("eligible rebase shadow has ambiguous source ownership")
        source = dict(found[0].fields).get("rebase-retries", LegacyAbsent()) if found else LegacyAbsent()
        if not isinstance(source, (LegacyAbsent, LegacyPresent)):
            raise ValueError("eligible rebase shadow has unreadable selected debt")
        before = int(source.text.strip()) if isinstance(source, LegacyPresent) else 0
        comparisons.append((target.key, before, target.state.rebase_failures))
    return tuple(comparisons)


def review_shadow(plan: ContinuationImportPlan, records: tuple[LegacyContinuationRecord, ...]) -> tuple[tuple[WorkflowKey, tuple[str, int], tuple[str, int]], ...]:
    comparisons = []
    for target in plan.targets:
        if not isinstance(target.state, cc.IssueRecoveryState):
            continue
        found = tuple(record for record in records if record.key == target.key and record.family is WorkflowFamily.ISSUE_RECOVERY)
        if len(found) > 1:
            raise ValueError("eligible review shadow has ambiguous source ownership")
        source = dict(found[0].fields).get("review-retries", LegacyAbsent()) if found else LegacyAbsent()
        if not isinstance(source, (LegacyAbsent, LegacyPresent)):
            raise ValueError("eligible review shadow has unreadable selected debt")
        at, _, count = source.text.strip().rpartition(" ") if isinstance(source, LegacyPresent) else ("", "", "0")
        before = (at, int(count))
        after = (target.state.review_failures.review_at, target.state.review_failures.count)
        comparisons.append((target.key, before, after))
    return tuple(comparisons)


def triage_shadow(plan: ContinuationImportPlan, records: tuple[LegacyContinuationRecord, ...]) -> tuple[tuple[WorkflowKey, tuple[str, int], tuple[str, int]], ...]:
    comparisons = []
    for target in plan.targets:
        if not isinstance(target.state, cc.StewardState):
            continue
        found = tuple(record for record in records if record.key == target.key and record.family is WorkflowFamily.STEWARD)
        if len(found) > 1:
            raise ValueError("eligible triage shadow has ambiguous source ownership")
        fields = dict(found[0].fields) if found else {}
        baseline, stalls = fields.get("triage-at", LegacyAbsent()), fields.get("steward-stalls-triage", LegacyAbsent())
        if not isinstance(baseline, (LegacyAbsent, LegacyPresent)) or not isinstance(stalls, (LegacyAbsent, LegacyPresent)):
            raise ValueError("eligible triage shadow has unreadable selected input")
        before = (baseline.text.strip() if isinstance(baseline, LegacyPresent) else "",
                  int(stalls.text.strip()) if isinstance(stalls, LegacyPresent) else 0)
        after = (target.state.triage.baseline_at, target.state.triage.stalls)
        comparisons.append((target.key, before, after))
    return tuple(comparisons)


def groom_shadow(plan: ContinuationImportPlan, records: tuple[LegacyContinuationRecord, ...]) -> tuple[tuple[WorkflowKey, tuple[str, str, int], tuple[str, str, int]], ...]:
    comparisons = []
    for target in plan.targets:
        if not isinstance(target.state, cc.StewardState):
            continue
        found = tuple(record for record in records if record.key == target.key and record.family is WorkflowFamily.STEWARD)
        if len(found) > 1:
            raise ValueError("eligible groom shadow has ambiguous source ownership")
        fields = dict(found[0].fields) if found else {}
        values = tuple(fields.get(name, LegacyAbsent()) for name in ("groom-at", "groom-content", "steward-stalls-groom"))
        if any(not isinstance(value, (LegacyAbsent, LegacyPresent)) for value in values):
            raise ValueError("eligible groom shadow has unreadable selected input")
        baseline, signature, stalls = tuple(value.text.strip() if isinstance(value, LegacyPresent) else "" for value in values)
        before = (baseline, signature, int(stalls or "0"))
        after = (target.state.groom.baseline_at, target.state.groom_content.successful, target.state.groom.stalls)
        comparisons.append((target.key, before, after))
    return tuple(comparisons)


def machine_shadow(plan: ContinuationImportPlan, records: tuple[LegacyContinuationRecord, ...]) -> tuple[tuple[WorkflowKey, tuple | None, tuple | None], ...]:
    def fact(block: MachineBlock | None) -> tuple | None:
        return ((block.issue, block.target.name, block.number, block.pr, block.blocker.value, block.blocked_head)
                if block is not None else None)
    comparisons = []
    for target in plan.targets:
        if not isinstance(target.state, cc.IssueRecoveryState):
            continue
        found = tuple(record for record in records if record.key == target.key and record.family is WorkflowFamily.ISSUE_RECOVERY)
        if len(found) > 1:
            raise ValueError("eligible machine shadow has ambiguous source ownership")
        source = dict(found[0].fields).get("machine-block", LegacyAbsent()) if found else LegacyAbsent()
        if not isinstance(source, (LegacyAbsent, LegacyPresent)):
            raise ValueError("eligible machine shadow has unreadable selected evidence")
        before = _machine_block(found[0], source.text) if isinstance(source, LegacyPresent) else None
        comparisons.append((target.key, fact(before), fact(target.state.machine_block)))
    return tuple(comparisons)


def budget_shadow(plan: ContinuationImportPlan, records: tuple[LegacyContinuationRecord, ...]) -> tuple[tuple[WorkflowKey, tuple | None, tuple | None], ...]:
    def fact(block: BudgetBlock | None) -> tuple | None:
        return ((block.issue, block.target.name, block.number, block.reservation_micros, block.scope,
                 block.cap_micros, block.old_headroom_micros, block.currency) if block is not None else None)
    comparisons = []
    for target in plan.targets:
        if not isinstance(target.state, cc.IssueRecoveryState):
            continue
        found = tuple(record for record in records if record.key == target.key and record.family is WorkflowFamily.ISSUE_RECOVERY)
        if len(found) > 1:
            raise ValueError("eligible budget shadow has ambiguous source ownership")
        source = dict(found[0].fields).get("budget-block", LegacyAbsent()) if found else LegacyAbsent()
        if not isinstance(source, (LegacyAbsent, LegacyPresent)):
            raise ValueError("eligible budget shadow has unreadable selected evidence")
        before = _budget_block(found[0], source.text) if isinstance(source, LegacyPresent) else None
        comparisons.append((target.key, fact(before), fact(target.state.budget_block)))
    return tuple(comparisons)


def self_clear_shadow(plan: ContinuationImportPlan, records: tuple[LegacyContinuationRecord, ...]) -> tuple[tuple[WorkflowKey, tuple[int, bool], tuple[int, bool]], ...]:
    comparisons = []
    for target in plan.targets:
        if not isinstance(target.state, cc.IssueRecoveryState):
            continue
        found = tuple(record for record in records if record.key == target.key and record.family is WorkflowFamily.ISSUE_RECOVERY)
        if len(found) > 1:
            raise ValueError("eligible self-clear shadow has ambiguous source ownership")
        fields = dict(found[0].fields) if found else {}
        count = fields.get("self-clears", LegacyAbsent())
        report = fields.get("self-clear-ceiling", LegacyAbsent())
        if not isinstance(count, (LegacyAbsent, LegacyPresent)) or not isinstance(report, (LegacyAbsent, LegacyPresent)):
            raise ValueError("eligible self-clear shadow has unreadable selected allowance")
        before = (int(count.text.strip()) if isinstance(count, LegacyPresent) else 0, isinstance(report, LegacyPresent))
        comparisons.append((target.key, before, (target.state.self_clears, target.state.self_clear_ceiling_reported)))
    return tuple(comparisons)
