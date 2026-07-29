"""Infrastructure adapter for the field-verification ledger.

The ledger is prose in a markdown table, hand-edited every deployment, and its
columns have already changed shape once. So this parser commits to almost
nothing: the first cell is the change, a status keyword anywhere in the row is
the verdict, and the raw row text is always kept so the UI can show what the
parser could not classify. A row it cannot read is still a row.
"""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass

# The three verdicts plus the default. Ordered longest-first so `MISBEHAVED`
# cannot be swallowed by a prefix match.
_STATUS_RE = re.compile(r"\b(MISBEHAVED|VERIFIED|RETIRED|pending)\b", re.IGNORECASE)
_SEPARATOR_RE = re.compile(r"^\|[\s:|-]+\|$")
_ID_RE = re.compile(r"#\d+")


@dataclass(frozen=True)
class VerificationRow:
    id: str
    change: str
    shipped: str
    status: str
    evidence: str
    raw: str


def default_path(harness_root: str) -> str:
    return os.path.join(harness_root, "docs", "field-verification.md")


def _cells(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [c.strip() for c in body.split("|")]


def _clean(text: str) -> str:
    return re.sub(r"\*\*|`", "", text).strip()


def status_of(text: str) -> str:
    """The verdict a row claims. First keyword wins: a row reading
    "VERIFIED … but MISBEHAVED on reversal" is a verified change with a filed
    follow-up, not a misbehaving one."""
    match = _STATUS_RE.search(text)
    if not match:
        return "pending"
    word = match.group(1)
    return word.lower() if word.lower() == "pending" else word.upper()


def parse_verification(text: str) -> list[VerificationRow]:
    """Every table row in the document, header and separator rows excluded."""
    rows: list[VerificationRow] = []
    header_seen = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            header_seen = False  # a blank/prose line ends the current table
            continue
        if _SEPARATOR_RE.match(stripped):
            continue
        cells = _cells(stripped)
        if not header_seen:
            header_seen = True
            if _looks_like_header(cells):
                continue
        change = _clean(cells[0]) if cells else ""
        if not change:
            continue
        ident = _ID_RE.search(change)
        rows.append(
            VerificationRow(
                id=ident.group(0) if ident else change,
                change=change,
                shipped=_clean(cells[1]) if len(cells) > 1 else "",
                evidence=_clean(cells[2]) if len(cells) > 2 else "",
                status=status_of(_clean(cells[-1]) if len(cells) > 3 else stripped),
                raw=stripped,
            )
        )
    return rows


def _looks_like_header(cells) -> bool:
    return bool(cells) and cells[0].strip().lower() in ("change", "id", "item")


def verification_payload(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        return {"rows": [], "warnings": [f"field-verification.md unreadable ({exc.strerror or 'error'})"]}
    return {"rows": [asdict(r) for r in parse_verification(text)], "warnings": []}
