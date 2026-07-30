"""Single encoding seam for repository- and agent-authored prompt content."""

from __future__ import annotations

import json
from typing import NewType

Untrusted = NewType("Untrusted", str)


def as_untrusted(value: str) -> Untrusted:
    return Untrusted(value)


def encode_untrusted(value: Untrusted) -> str:
    """JSON encode and neutralize every prompt delimiter character."""

    return (
        json.dumps(str(value), ensure_ascii=False)
        .replace("`", "\\u0060")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def untrusted_block(name: str, value: Untrusted) -> str:
    if not name.replace("_", "").isalnum():
        raise ValueError("untrusted block name must be alphanumeric")
    return (
        f"<untrusted_{name}_json>\n"
        f"{encode_untrusted(value)}\n"
        f"</untrusted_{name}_json>"
    )
