"""Pure terminal-output presentation, including unfinished stream escapes."""

import re


_ESCAPES = re.compile(
    rb"\x1b\[[0-?]*[ -/]*[@-~]"
    rb"|\x1b\](?:[^\x07\x1b]|\x1b(?!\\))*(?:\x07|\x1b\\)"
    rb"|\x1b[ -/]*(?![\[\]])[0-~]"
    rb"|(?P<pending>\x1b\[[0-?]*[ -/]*$"
    rb"|\x1b\](?:[^\x07\x1b]|\x1b(?!\\))*$|\x1b[ -/]*$)"
    rb"|\x1b"
)


def strip_ansi(text: str) -> str:
    return _ESCAPES.sub(b"", text.encode()).decode()


def clean_terminal_tail(output: bytes, limit: int) -> tuple[bytes, bool]:
    """Strip complete escapes before cutting; retain an unfinished escape for the next chunk.

    A very long unfinished OSC/CSI can discard its payload but must retain its
    opener, so a later chunk cannot expose the remainder as ordinary text.
    """
    pending = b""
    def replace(match: re.Match[bytes]) -> bytes:
        nonlocal pending
        if match.group("pending"):
            pending = match.group()
            return pending
        return b""
    cleaned = _ESCAPES.sub(replace, output)
    if len(cleaned) <= limit:
        return cleaned, False
    if len(pending) >= limit:
        terminator_start = b"\x1b" if pending.startswith(b"\x1b]") and pending.endswith(b"\x1b") else b""
        return pending[:2] + terminator_start, True
    return cleaned[-limit:], True
