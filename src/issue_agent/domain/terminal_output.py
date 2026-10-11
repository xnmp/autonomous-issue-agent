"""Pure presentation of a decoded terminal-output tail."""

import re


_ESCAPES = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b[\]P_^X][^\x07\x1b\n]*(?:\x07|\x1b\\)"
    r"|\x1b(?![\[\]P_^X])[ -/]*[0-~]"
    r"|\x1b"
)
_CSI_REMAINDER = re.compile(r"\A(?:\[[0-?]*|[0-?]+)[ -/]*[@-~]")


def strip_ansi(text: str, *, truncated: bool = False) -> str:
    # Only a byte-tail cut can leave a CSI suffix without its ESC opener.
    if truncated:
        text = _CSI_REMAINDER.sub("", text, count=1)
    return _ESCAPES.sub("", text)
