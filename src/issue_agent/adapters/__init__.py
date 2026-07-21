"""Impure adapter layer.

Each module here wraps exactly one external surface (the Claude Agent SDK, git
worktrees, the filesystem, GitHub) and is the ONLY place that surface is touched.
Adapters speak the domain's value objects (AgentTask/AgentResult, WorldState,
Action) so the domain stays dependency-free and the adapters are fakeable in
tests. Importing this package does NOT import the SDK — only `agent_runner`
does, lazily — so domain-only consumers never pay for it.
"""
