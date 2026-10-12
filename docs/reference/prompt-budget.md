# Prompt budget measurement

The CLAUDE.md diet was measured against the parent of implementation commit
`0965126` (the pre-refactor instruction file) on 2026-07-31.

| Measure | Before | After | Delta |
|---|---:|---:|---:|
| UTF-8 bytes | 17,816 | 6,380 | -11,436 (-64.2%) |
| whitespace-delimited words | 2,409 | 835 | -1,574 (-65.3%) |
| estimated input tokens (bytes / 4, rounded up) | 4,454 | 1,595 | -2,859 (-64.2%) |

The token number is an explicitly labelled deterministic proxy, not a claim
about a provider tokenizer. For each fresh leg that includes the full file, the
expected context reduction is approximately 2,859 input tokens. Provider
prompt caching and resumed sessions can reduce the billed delta. Routing,
direction, and non-negotiable guardrails remain in CLAUDE.md; operational and
historical detail moved to `docs/reference/`.

Re-measured 2026-08-05 after the ADR 0012 provenance/liveness rule and the
explicit typed-variant rule were added; the diet's guardrail (routing,
direction, non-negotiables only) still holds.

Re-measured 2026-10-02 after the private test scratch instructions: 6,716
UTF-8 bytes and 885 whitespace-delimited words.

Re-measured 2026-10-10 after the PR-repair acknowledgment constraint: 7,299
UTF-8 bytes and 959 whitespace-delimited words.

Re-measured 2026-10-11 after the surface-area rule: 8,126
UTF-8 bytes and 1,082 whitespace-delimited words.

Re-measured 2026-10-12 after the estimated-settlement rule: 8,260
UTF-8 bytes and 1,099 whitespace-delimited words.

Re-measured 2026-10-13 after machine failures stopped counting as attempt
debt: 8,339 UTF-8 bytes and 1,110 whitespace-delimited words.

Re-measured 2026-10-13 after slice 6a deleted the fixer gates and governor
and slice 8a deleted the telemetry layer: 7,895 UTF-8 bytes and 1,059
whitespace-delimited words.
