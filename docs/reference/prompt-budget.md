# Prompt budget measurement

The CLAUDE.md diet was measured against the parent of implementation commit
`0965126` (the pre-refactor instruction file) on 2026-07-31.

| Measure | Before | After | Delta |
|---|---:|---:|---:|
| UTF-8 bytes | 17,816 | 6,038 | -11,778 (-66.1%) |
| whitespace-delimited words | 2,409 | 794 | -1,615 (-67.0%) |
| estimated input tokens (bytes / 4, rounded up) | 4,454 | 1,510 | -2,944 (-66.1%) |

The token number is an explicitly labelled deterministic proxy, not a claim
about a provider tokenizer. For each fresh leg that includes the full file, the
expected context reduction is approximately 2,944 input tokens. Provider
prompt caching and resumed sessions can reduce the billed delta. Routing,
direction, and non-negotiable guardrails remain in CLAUDE.md; operational and
historical detail moved to `docs/reference/`.

Re-measured 2026-08-01 after the ADR 0012 provenance/liveness rule was
added to CLAUDE.md (+~80 words); the diet's guardrail (routing, direction,
non-negotiables only) still holds.
