"""Operator review-guidance parsing — one rule set, two enforcement points.

The note an operator attaches to an unblock is validated at the HTTP boundary
(`contracts.UnblockRequest`) AND again in the service, which is callable
directly. Both call `parse_guidance`, so the ceiling, the fence-marker ban and
the encodability rule cannot drift apart between the two.

Pure: no I/O, no imports beyond the stdlib.
"""

from __future__ import annotations

# A scope note is prompt context for every later review round of a PR, not a
# document: the ceiling keeps a paste-bomb out of the token bill (and out of the
# state dir) at the boundary, before anything is written.
GUIDANCE_MAX_CHARS = 4000

# The note is injected into the review prompt as harness-owned instruction
# inside a `<<<OPERATOR_GUIDANCE>>>` fence. A note containing the fence syntax
# could close its own block early and promote whatever follows to unfenced
# harness voice, so the marker prefix is refused outright rather than escaped:
# an operator has no need for it, and a rejection is unambiguous where an escape
# scheme is one bug away from being no protection at all.
FENCE_MARKER = "<<<"


class GuidanceError(ValueError):
    """A note that must be refused. The message is safe to return to the
    caller: it never quotes the offending text (which may be unencodable —
    echoing it is how a 422 turns into a 500)."""


def parse_guidance(raw: object) -> str | None:
    """Normalize an operator note into the three states an unblock can carry.

    Returns `None` when the field was absent/null — meaning LEAVE ANY EXISTING
    NOTE ALONE — and a string otherwise: `""` is an explicit CLEAR, anything
    else is the note to store. The blank-vs-absent distinction is the only way
    an operator can retire a note they no longer mean, so it is a contract, not
    an implementation detail.

    Raises `GuidanceError` for a note that is over the ceiling, carries the
    fence marker, or cannot be encoded (lone surrogates).
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise GuidanceError("guidance must be a string")
    if len(raw) > GUIDANCE_MAX_CHARS:
        raise GuidanceError(
            f"guidance must be at most {GUIDANCE_MAX_CHARS} characters"
        )
    if FENCE_MARKER in raw:
        raise GuidanceError(f"guidance must not contain '{FENCE_MARKER}'")
    try:
        raw.encode("utf-8")
    except UnicodeEncodeError:
        raise GuidanceError("guidance must be encodable text") from None
    return raw.strip()
