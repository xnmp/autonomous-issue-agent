"""launchd entrypoint that suppresses restarts for permanent exit classes."""

from __future__ import annotations

import sys
from collections.abc import Callable

from ..domain.supervision import ExitCode
from .main import main as daemon_main

_PERMANENT = frozenset(
    {
        ExitCode.INVALID_CONFIG,
        ExitCode.DUPLICATE_INSTANCE,
        ExitCode.INCOMPATIBLE_SCHEMA,
        ExitCode.STORE_CORRUPTION,
        ExitCode.OPERATOR_REPAIR_REQUIRED,
    }
)


def run(entrypoint: Callable[[], None] = daemon_main) -> None:
    try:
        entrypoint()
    except SystemExit as exc:
        try:
            code = ExitCode.CLEAN if exc.code is None else ExitCode(int(exc.code))
        except (TypeError, ValueError):
            raise
        if code == ExitCode.CLEAN:
            # SuccessfulExit=false reserves wrapper success for permanent errors.
            raise SystemExit(ExitCode.TRANSIENT) from None
        if code not in _PERMANENT:
            raise
        sys.stderr.write(
            f"launchd: permanent issue-agent exit {int(code)}; "
            "leaving service stopped for operator repair\n"
        )
        return
    raise SystemExit(ExitCode.TRANSIENT)


if __name__ == "__main__":
    run()
