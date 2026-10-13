"""The two labels of the minimal-harness design
(docs/open/2026-10-12-minimal-harness-design.md): a human queues an issue with
`ai-ready`; only policy writes `needs-human`, and a human reply or removing it
releases the item. Other repository labels are not parsed (`github._labels`
drops them)."""

from __future__ import annotations

from enum import Enum


class Label(str, Enum):
    """`str` mixin so values compare to raw GitHub label strings
    (Label.AI_READY == "ai-ready")."""

    AI_READY = "ai-ready"
    NEEDS_HUMAN = "needs-human"
