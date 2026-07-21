"""FIX_MCP_CONFIG → MCP servers for the CI-fix leg (Python port of
agent/lib.sh:ci_mcp_for_pr).

FIX_MCP_CONFIG is a JSON map keyed by a check-name extended-regex:

    {"SonarQube": {"config": "config/mcp/sonarqube.json", "tools": "mcp__sonarqube__*"}}

For each entry whose regex matches ANY of the PR's check names, the referenced
mcp-config file (`{"mcpServers": {<name>: <spec>}}`) is loaded and each server
becomes an `McpServer(name, spec, tools)` attached to the fixer — exactly the
bash behaviour (match against all checks, not just failing ones).

`mcp_servers_for_checks` is pure except for the injected `load` callable (the
file read), so it is unit-tested with a fake loader and no disk.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable

from ..domain.agent_task import McpServer


def _default_load(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def mcp_servers_from_file(
    path: str,
    tools: str,
    harness_root: str = "",
    load: Callable[[str], dict] = _default_load,
    log: Callable[[str], None] = lambda _m: None,
) -> tuple[McpServer, ...]:
    """Load an MCP config file (`{"mcpServers": {<name>: <spec>}}`) and build an
    `McpServer(name, spec, tools)` per server, all sharing the `tools` allow-glob.
    Relative `path` resolves against `harness_root`. Blank path or a missing/garbled
    file → () with a log line (fail-soft; never raises). Shared by the CI-fix
    check-matching path and the steward's Jira (Atlassian) MCP."""
    if not path:
        return ()
    if not os.path.isabs(path) and harness_root:
        path = os.path.join(harness_root, path)
    try:
        data = load(path)
    except (FileNotFoundError, OSError, ValueError):
        log(f"MCP config not found/invalid: {path} — skipping")
        return ()
    defs = data.get("mcpServers", {}) if isinstance(data, dict) else {}
    return tuple(
        McpServer(name=name, config=spec, tools=tools) for name, spec in defs.items()
    )


def mcp_servers_for_checks(
    fix_mcp_config: str,
    check_names: list[str],
    harness_root: str = "",
    load: Callable[[str], dict] = _default_load,
    log: Callable[[str], None] = lambda _m: None,
) -> tuple[McpServer, ...]:
    """Attach the MCP servers whose check-name regex matches a check on the PR.

    `fix_mcp_config` is the raw JSON string; blank/invalid → (). Relative `config`
    paths resolve against `harness_root`. A missing/garbled config file is skipped
    with a log line (never raises), mirroring ci_mcp_for_pr's fail-soft behaviour.
    """
    if not fix_mcp_config.strip():
        return ()
    try:
        mapping = json.loads(fix_mcp_config)
    except (ValueError, TypeError):
        log(f"FIX_MCP_CONFIG is not valid JSON; ignoring: {fix_mcp_config[:80]}")
        return ()
    if not isinstance(mapping, dict):
        return ()

    servers: list[McpServer] = []
    for key, entry in mapping.items():
        if not isinstance(entry, dict):
            continue
        try:
            matches = any(re.search(key, name) for name in check_names)
        except re.error:
            log(f"FIX_MCP_CONFIG key '{key}' is not a valid regex; skipping")
            continue
        if not matches:
            continue
        path = entry.get("config", "")
        tools = entry.get("tools", "")
        if not path:
            continue
        for m in mcp_servers_from_file(path, tools, harness_root, load, log):
            servers.append(m)
            log(f"CI check matches '{key}' → attaching MCP {m.name} (tools: {tools or '<none>'})")
    return tuple(servers)
