"""Pure domain model.

No module here may import the Claude Agent SDK, `gh`, git, or perform any I/O.
Everything is a pure function over frozen value objects, so the behavioural
tests construct a `WorldState` (or a command string) and assert on the returned
`Action` list (or `GateResult`) with no external dependencies.
"""
