"""Shared operator-control wire helpers (the resolve projection itself was
deleted with the fix workflow in refactor slice 5b)."""


from . import resolve_contracts as wire


def control_error(error: BaseException) -> str:
    return str(error).replace("\x00", "").encode("utf-8", errors="replace").decode("utf-8")[:16384] or type(error).__name__


def unavailable_control(error: BaseException) -> wire.UnavailableControl:
    """A failed authority read is unavailable, never an empty or absent gate set."""
    return wire.UnavailableControl(status="unavailable", reason=control_error(error))


def unavailable_command(error: BaseException) -> wire.CommandResult:
    """The stable request identity makes an uncertain response safe to replay."""
    return wire.CommandResult(outcome="unavailable", reason=control_error(error),
                              control=wire.UnavailableControl(status="unavailable", reason="control read unavailable"))


