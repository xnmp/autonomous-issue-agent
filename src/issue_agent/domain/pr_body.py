"""Harness-owned issue linkage and acceptance criteria for publication."""

import re
from collections.abc import Iterable


# CommonMark: an ATX heading has at most three leading spaces (four is code);
# a Setext heading is a paragraph line underlined with = (h1) or - (h2).
_ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*#*[ \t]*$")
_SETEXT = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
_AC_TITLE = re.compile(r"acceptance criteria\b", re.IGNORECASE)
_TRIAGE_AC = re.compile(
    r"<details><summary>Technical acceptance criteria \(for the engineer\)"
    r"</summary>\s*(.*?)\s*</details>", re.DOTALL,
)


def _visible_lines(text: str) -> tuple[list[str], str]:
    lines = []
    fence = ""
    comment = False
    indented_code = False
    for line in text.splitlines():
        stripped = line.lstrip()
        # An indented code block opens after a blank line (or at the start) and
        # runs until a non-blank line indented less than four columns.
        if not fence and not comment and line.strip() and line.expandtabs(4).startswith("    "):
            if indented_code or not lines or not lines[-1].strip():
                indented_code = True
                lines.append("")
                continue
        elif line.strip():
            indented_code = False
        if fence:
            if re.fullmatch(re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*", stripped):
                fence = ""
            lines.append("")
        else:
            visible = ""
            remaining = line
            while remaining:
                if comment:
                    end = remaining.find("-->")
                    if end < 0:
                        break
                    remaining = remaining[end + 3:]
                    comment = False
                else:
                    start = remaining.find("<!--")
                    if start < 0:
                        visible += remaining
                        break
                    visible += remaining[:start]
                    remaining = remaining[start + 4:]
                    comment = True
            opening = re.match(r"^\s*(`{3,}|~{3,})", visible)
            if opening:
                fence = opening.group(1)
                visible = ""
            lines.append(visible)
    # Appending inside unfinished Markdown would hide the harness's contract.
    return lines, fence or ("-->" if comment else "")


def _headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """(line index, level, title) for every ATX or Setext heading."""
    found = []
    for index, line in enumerate(lines):
        atx = _ATX.match(line)
        if atx:
            found.append((index, len(atx.group(1)), (atx.group(2) or "").strip()))
            continue
        underline = _SETEXT.match(lines[index + 1]) if index + 1 < len(lines) else None
        if underline and line.strip() and not _SETEXT.match(line):
            found.append((index, 1 if underline.group(1)[0] == "=" else 2, line.strip()))
    return found


def _ac_heading(lines: list[str]) -> tuple[int, int, int] | None:
    """(heading line, first content line, level) of the first AC heading."""
    for index, level, title in _headings(lines):
        if _AC_TITLE.match(title):
            setext = not _ATX.match(lines[index])
            return index, index + (2 if setext else 1), level
    return None


def has_acceptance_criteria(body: str) -> bool:
    return _ac_heading(_visible_lines(body)[0]) is not None


def issue_acceptance_criteria(body: str, comments: Iterable[str]) -> str:
    """Prefer the latest posted AC contract, then the issue's original section.

    Triage already parses criteria into TriageVerdict and renders them in a
    details block; fixer-authored contracts use a markdown heading instead.
    """
    for text in (*reversed(tuple(comments)), body):
        lines, _ = _visible_lines(text)
        heading = _ac_heading(lines)
        if heading is not None:
            index, start, level = heading
            # Subsections belong to the contract; only a sibling or parent ends it.
            end = next((i for i, lvl, _ in _headings(lines)
                        if i >= start and lvl <= level), len(lines))
            return "\n".join(text.splitlines()[start:end]).strip()
        for match in _TRIAGE_AC.finditer(text):
            if "<details><summary>" in lines[text[:match.start()].count("\n")]:
                return match.group(1).strip()
    return ""


def _append(body: str, section: str) -> str:
    _, terminator = _visible_lines(body)
    if terminator:
        body = body.rstrip() + "\n" + terminator
    return body.rstrip() + "\n\n" + section if body.strip() else section


def complete_pr_body(body: str, issue: int, repository: str, criteria: str) -> str:
    closing = re.compile(
        rf"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?):?\s+"
        rf"(?:{re.escape(repository)})?#{issue}\b", re.IGNORECASE,
    )
    if not has_acceptance_criteria(body):
        body = _append(body, "## Acceptance Criteria" + (f"\n{criteria}" if criteria else ""))
    visible, _ = _visible_lines(body)
    if not closing.search("\n".join(visible)):
        body = _append(body, f"Closes #{issue}")
    return body
