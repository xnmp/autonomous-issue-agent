"""The agent runtimes a role can run on."""

from __future__ import annotations

from enum import Enum


class RuntimeName(str, Enum):
    CLAUDE = "claude"
    CODEX = "codex"
