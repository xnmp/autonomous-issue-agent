"""Denied tool calls and the reason each carries.

One table feeds both enforcement points: the SDK's `disallowed_tools` (the
boundary; a deny rule beats bypassPermissions) and a PreToolUse hook that
returns the reason, because the bare deny tells the agent nothing and #1078
read a denied curl probe as "dev server denied". The hook is advisory: it may
only be narrower than the rules, never the sole enforcement.
"""

from __future__ import annotations

import fnmatch
import re
import shlex
from dataclasses import dataclass
from typing import Any

_FETCH = "network fetch is denied by policy; to probe or screenshot a local dev server use `agent-browser` or Playwright `page.screenshot`"
_GH = "raw/credential gh commands are denied by policy; use the `gh` subcommands the task needs (gh pr, gh issue, gh run)"
_SECRET = "reading secret files (.env, *.pem, id_rsa*) is denied by policy; ask for the needed value in your result instead"


@dataclass(frozen=True)
class DeniedTool:
    rule: str  # Claude permission-rule syntax: "Tool" or "Tool(specifier)"
    reason: str


DENIED_TOOLS: tuple[DeniedTool, ...] = (
    DeniedTool("WebFetch", "WebFetch is denied by policy (outbound network is closed); work from the repository and the issue text"),
    DeniedTool("WebSearch", "WebSearch is denied by policy (outbound network is closed); work from the repository and the issue text"),
    DeniedTool("Bash(curl:*)", "`curl`: " + _FETCH),
    DeniedTool("Bash(wget:*)", "`wget`: " + _FETCH),
    DeniedTool("Bash(gh auth:*)", "`gh auth` is denied by policy; credentials are managed by the harness"),
    DeniedTool("Bash(gh secret:*)", "`gh secret` is denied by policy; credentials are managed by the harness"),
    DeniedTool("Bash(gh api:*)", "`gh api` " + _GH),
    DeniedTool("Read(.env)", _SECRET),
    DeniedTool("Read(.env.*)", _SECRET),
    DeniedTool("Read(**/.env)", _SECRET),
    DeniedTool("Read(**/.env.*)", _SECRET),
    DeniedTool("Read(**/*.pem)", _SECRET),
    DeniedTool("Read(**/id_rsa*)", _SECRET),
)

FIXER_DENY: tuple[str, ...] = tuple(d.rule for d in DENIED_TOOLS)
_REASONS: dict[str, str] = {d.rule: d.reason for d in DENIED_TOOLS}

_RULE = re.compile(r"^(?P<tool>[A-Za-z_][\w]*)(?:\((?P<spec>.*)\))?$", re.S)
_SEGMENT_SPLIT = re.compile(r"&&|\|\||[;|&\n]")
_ASSIGN = re.compile(r"^[A-Za-z_]\w*=")
# Prefixes that run their argument: `env curl x` is still curl.
_WRAPPERS = frozenset({"env", "command", "exec", "nohup", "time", "sudo", "nice", "builtin"})
_SHELLS = frozenset({"bash", "sh", "zsh", "dash"})
_MAX_CMD = 100_000  # beyond this the hook stays silent; the backstop still denies
_MAX_DEPTH = 4


def _argvs(command: str, depth: int = 0) -> list[list[str]]:
    """Every simple command in `command`: compound segments, wrapper-stripped,
    plus the body of `sh -c '...'`."""
    out: list[list[str]] = []
    for seg in _SEGMENT_SPLIT.split(command):
        try:
            words = shlex.split(seg)
        except ValueError:
            words = seg.split()
        while words and (_ASSIGN.match(words[0]) or words[0] in _WRAPPERS):
            words = words[1:]
        if not words:
            continue
        out.append(words)
        if depth < _MAX_DEPTH and words[0].rsplit("/", 1)[-1] in _SHELLS and "-c" in words[1:-1]:
            out.extend(_argvs(words[words.index("-c") + 1], depth + 1))
    return out


def _bash_match(spec: str, command: str) -> bool:
    prefix = spec[:-2] if spec.endswith(":*") else spec
    want = prefix.split()
    return any(
        len(a) >= len(want) and [a[0].rsplit("/", 1)[-1], *a[1:len(want)]] == want
        for a in _argvs(command)
    )


def _read_match(spec: str, path: str) -> bool:
    # Basename match: the union of the root-relative and `**/` rules, so the
    # hook explains at least what the rules deny.
    return fnmatch.fnmatch(path.rsplit("/", 1)[-1], spec.removeprefix("**/"))


def denial_reason(tool_name: str, tool_input: Any, rules: tuple[str, ...]) -> str | None:
    """The reason a call matches a deny rule in `rules`, or None. Total: any
    malformed input yields None (the rules themselves still deny)."""
    if not isinstance(tool_name, str):
        return None
    arg = tool_input if isinstance(tool_input, dict) else {}
    for rule in rules:
        reason = _REASONS.get(rule)
        m = _RULE.match(rule)
        if reason is None or m is None or m["tool"] != tool_name:
            continue
        spec = m["spec"]
        if spec is None:
            return reason
        if tool_name == "Bash":
            cmd = arg.get("command")
            if isinstance(cmd, str) and len(cmd) <= _MAX_CMD and _bash_match(spec, cmd):
                return reason
        elif tool_name == "Read":
            path = arg.get("file_path")
            if isinstance(path, str) and len(path) <= _MAX_CMD and _read_match(spec, path):
                return reason
    return None
