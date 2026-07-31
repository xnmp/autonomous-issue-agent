# Pyrefly default-preset backlog

**Owner:** harness maintainers

**Opened:** 2026-07-31

**Exit condition:** every rule below is removed from
`[tool.pyrefly.errors]` and `make typecheck` remains green.

The project now runs Pyrefly's `default` preset. The pre-existing annotation
backlog is acknowledged by rule instead of hidden behind `preset = "basic"`.
Authority-bearing `WorkflowState`, `ControlStore`, and `FixWorktreePort`
conformance is checked separately with the suppressed seam-sensitive rules
forced back to error severity.

Each heading is the removal issue for the matching suppression:

## bad-argument-type

Narrow loosely typed configuration/test builders and adapter payload parsing.

## missing-attribute

Replace inferred dynamic attributes with declared fields or narrow protocols.

## bad-index

Narrow JSON/object unions before indexing.

## unsupported-operation

Narrow operand unions before arithmetic and collection operations.

## bad-assignment

Annotate collection/value boundaries and remove incompatible inferred unions.

## not-iterable

Validate external payload containers before iteration.

## bad-override

Align test doubles and adapter subclasses with their declared return contracts.

## bad-specialization

Correct invalid generic specializations at their owning seam.

## bad-return

Make return annotations match observable fallback behavior.

## unsafe-overlap

Disambiguate overlapping runtime-checkable protocols.

## no-matching-overload

Narrow arguments before overloaded standard-library calls.
