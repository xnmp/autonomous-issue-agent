"""Autonomous issue-resolving harness.

Layering:
  domain/   — pure: no I/O, no SDK, no gh, no subprocess. 100% unit-tested.
  adapters/ — impure, thin, fakeable integrations with gh, git, runners, hooks.
  app/      — Python-owned role loops, workflow dispatch, config, and planners.

Shell remains where the operating system or Git requires shell glue, and legacy
watcher scripts remain explicit compatibility entrypoints. Python owns the
default watcher runtime and architectural seams.
"""
