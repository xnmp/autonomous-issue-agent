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
# A verdict cell usually reads "VERIFIED — <why>"; the badge already shows the
# verdict, so only the prose after it is worth surfacing.
_VERDICT_PREFIX_RE = re.compile(
    r"^\W*(?:MISBEHAVED|VERIFIED|RETIRED|pending)\b[\s\-—–:;,.]*", re.IGNORECASE
)
# One line of unauthored prose, same budget the compact issue rows use.
_REASON_CHARS = 240
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
    reason: str | None = None


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


def _reason_prose(text: str) -> str:
    """The prose of a reason cell: verdict keyword and its punctuation gone."""
    return _VERDICT_PREFIX_RE.sub("", text.strip()).strip(" -—–:;,.")


def reason_of(verdict_text: str, evidence: str = "") -> str | None:
    """Why a row claims its verdict, verdict keyword stripped.

    Ledger columns drift, so this falls back to the evidence cell rather than
    guessing: a three-column table puts the reasoning there — and puts the
    verdict there too, so the fallback is stripped exactly like the dedicated
    cell or the badge would read "VERIFIED — VERIFIED — …". Nothing to say is
    `None`, never an empty badge tooltip.

    The ledger is hand-written prose of no bounded length; a reason is a one
    line tooltip, so it is truncated to the same budget as every other piece of
    unauthored text the compact rows carry.
    """
    reason = _reason_prose(verdict_text) or _reason_prose(evidence)
    return reason[:_REASON_CHARS] or None


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
        evidence = _clean(cells[2]) if len(cells) > 2 else ""
        verdict_cell = _clean(cells[-1]) if len(cells) > 3 else ""
        rows.append(
            VerificationRow(
                id=ident.group(0) if ident else change,
                change=change,
                shipped=_clean(cells[1]) if len(cells) > 1 else "",
                evidence=evidence,
                status=status_of(verdict_cell if len(cells) > 3 else stripped),
                raw=stripped,
                reason=reason_of(verdict_cell, evidence),
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
