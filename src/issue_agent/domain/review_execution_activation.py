"""Typed, untrusted legacy sources for review execution activation."""

from __future__ import annotations

from dataclasses import dataclass

from .resolve_activation import LegacyAbsent, LegacyFile, LegacyPresent, LegacyUnreadable, UnmappedLegacyEntry
from .workflow_identity import validate_workflow_integer, validate_workflow_text


def _files(*values: LegacyFile) -> None:
    if any(not isinstance(value, (LegacyAbsent, LegacyPresent, LegacyUnreadable)) for value in values):
        raise ValueError("legacy execution source requires typed file observations")


@dataclass(frozen=True)
class LegacyExecutionPRRecord:
    """Review session and round files for one PR in one state root."""

    pr: int
    session: LegacyFile
    round: LegacyFile
    root: str = "review"

    def __post_init__(self) -> None:
        validate_workflow_integer(self.pr, "legacy execution PR", minimum=1)
        _files(self.session, self.round)
        validate_workflow_text(self.root, "legacy state root label", limit=4096)


@dataclass(frozen=True)
class LegacyExecutionCounterRecord:
    """The signed legacy key retains issue and negated-PR namespaces separately."""

    key: int
    infra_deaths: LegacyFile
    root: str = "review"

    def __post_init__(self) -> None:
        if type(self.key) is not int or self.key == 0 or abs(self.key) > 2**63 - 1:
            raise ValueError("legacy execution counter key must be a nonzero signed target number")
        _files(self.infra_deaths)
        validate_workflow_text(self.root, "legacy state root label", limit=4096)


LegacyExecutionEntry = LegacyExecutionPRRecord | LegacyExecutionCounterRecord | UnmappedLegacyEntry
