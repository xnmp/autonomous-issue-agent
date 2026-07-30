"""Typed boundary and lossless encoding for untrusted prompt content."""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class Untrusted:
    """Content branded at an inbound boundary before prompt construction."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise TypeError("untrusted content must be text")


def as_untrusted(value: str) -> Untrusted:
    return Untrusted(value)


def encode_untrusted(value: Untrusted) -> str:
    """JSON encode and neutralize every prompt delimiter character."""

    if not isinstance(value, Untrusted):
        raise TypeError("prompt content must be branded as Untrusted")
    return (
        json.dumps(value.value, ensure_ascii=False)
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
