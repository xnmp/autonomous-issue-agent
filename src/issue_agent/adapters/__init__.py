"""Impure adapter layer.

Each module here wraps exactly one external surface (the Claude Agent SDK, git
worktrees, the filesystem, GitHub) and is the ONLY place that surface is touched.
Adapters translate domain intent/results and world state into runtime plans and
actions, so the domain stays dependency-free and the adapters are fakeable in
tests. Importing this package does NOT import the SDK — only `agent_runner`
does, lazily — so domain-only consumers never pay for it.
"""
