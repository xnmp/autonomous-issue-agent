"""Tool calls the fixer may not make: credential hygiene only.

These rules feed the SDK's `disallowed_tools`, the one policy that holds under
bypassPermissions (a deny rule beats bypass). They are rarely hit and the SDK's
bare "Permission to use ... has been denied" explains itself. Network tools are
allowed: agents fetch docs, research and probe local servers like an engineer,
and the supported posture is trusted inbound content only (CLAUDE.md).
"""

from __future__ import annotations

FIXER_DENY: tuple[str, ...] = (
    "Bash(gh auth:*)",
    "Bash(gh secret:*)",
    "Bash(gh api:*)",
    # #1128: a FIX_CI leg watched a 50-minute run for 40 minutes, holding a slot
    # and the hot-reload drain, so nothing else dispatched.
    "Bash(gh run watch:*)",
    "Read(.env)",
    "Read(.env.*)",
    "Read(**/.env)",
    "Read(**/.env.*)",
    "Read(**/*.pem)",
    "Read(**/id_rsa*)",
)
