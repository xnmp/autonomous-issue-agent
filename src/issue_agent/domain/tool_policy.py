"""Tool calls the fixer may not make: credential hygiene.

These rules feed the SDK's `disallowed_tools`, the one policy that holds under
bypassPermissions (a deny rule beats bypass). They get the SDK's bare
"Permission to use ... has been denied"; AGENT.md says not to retry it.
Network tools are allowed: agents fetch docs, research and probe local servers
like an engineer, and the supported posture is trusted inbound content only
(CLAUDE.md).
"""

from __future__ import annotations

FIXER_DENY: tuple[str, ...] = (
    "Bash(gh auth:*)",
    "Bash(gh secret:*)",
    "Read(.env)",
    "Read(.env.*)",
    "Read(**/.env)",
    "Read(**/.env.*)",
    "Read(**/*.pem)",
    "Read(**/id_rsa*)",
)
