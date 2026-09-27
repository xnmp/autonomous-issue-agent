"""Read-only inventory of legacy review execution files across state roots.

This inventory retains raw observations. Mapping issue counters to PRs and
validating their values require SQL run history and fresh linked-issue evidence.
"""

from __future__ import annotations

import os
import re

from ..domain.resolve_activation import LegacyAbsent, LegacyFile, UnmappedLegacyEntry
from ..domain.review_execution_activation import (
    LegacyExecutionCounterRecord, LegacyExecutionEntry, LegacyExecutionPRRecord,
)
from .resolve_activation import LegacyRoots, _printable, _read_legacy_file, distinct_roots

_PR_NAME = re.compile(r"pr-(.*)\.(session|round)", re.DOTALL)
_COUNTER_NAME = re.compile(r"issue-(.*)\.infra-deaths", re.DOTALL)
_POSITIVE = re.compile(r"[1-9][0-9]{0,18}")
_SIGNED = re.compile(r"-?[1-9][0-9]{0,18}")
_MAX_NUMBER = 2**63 - 1


def _number(raw: str, pattern: re.Pattern[str]) -> int | None:
    if pattern.fullmatch(raw) is None:
        return None
    number = int(raw)
    return number if abs(number) <= _MAX_NUMBER else None


def read_legacy_execution_state(state_dir: str, root: str = "review") -> tuple[LegacyExecutionEntry, ...]:
    """Every legacy review execution key in one labelled root, without writes."""
    try:
        names = sorted(entry.name for entry in os.scandir(state_dir))
    except FileNotFoundError:
        return ()
    except OSError as exc:
        return (UnmappedLegacyEntry("<state-dir>", _printable(f"state directory unreadable: {exc.strerror or exc}"), root),)

    pr_files: dict[int, dict[str, LegacyFile]] = {}
    counters: list[LegacyExecutionCounterRecord] = []
    unmapped: list[UnmappedLegacyEntry] = []
    for name in names:
        pr_match = _PR_NAME.fullmatch(name)
        if pr_match is not None:
            raw, suffix = pr_match.groups()
            pr = _number(raw, _POSITIVE)
            if pr is None:
                unmapped.append(UnmappedLegacyEntry(_printable(name), "not a canonical positive pull request number", root))
            else:
                pr_files.setdefault(pr, {})[suffix] = _read_legacy_file(os.path.join(state_dir, name))
            continue
        counter_match = _COUNTER_NAME.fullmatch(name)
        if counter_match is not None:
            key = _number(counter_match.group(1), _SIGNED)
            if key is None:
                unmapped.append(UnmappedLegacyEntry(_printable(name), "not a canonical signed issue or PR key", root))
            else:
                counters.append(LegacyExecutionCounterRecord(key, _read_legacy_file(os.path.join(state_dir, name)), root))

    prs = [LegacyExecutionPRRecord(pr, found.get("session", LegacyAbsent()), found.get("round", LegacyAbsent()), root)
           for pr, found in sorted(pr_files.items())]
    return tuple(prs + sorted(counters, key=lambda record: record.key) + unmapped)


def read_legacy_execution_roots(roots: LegacyRoots) -> tuple[LegacyExecutionEntry, ...]:
    """Retain duplicate targets in distinct roots for later ambiguity checks."""
    return tuple(entry for label, path in distinct_roots(roots) for entry in read_legacy_execution_state(path, label))
