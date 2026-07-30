# Prompt budget measurement

The CLAUDE.md diet was measured against the parent of implementation commit
`0965126` (the pre-refactor instruction file) on 2026-07-31.

| Measure | Before | After | Delta |
|---|---:|---:|---:|
| UTF-8 bytes | 17,816 | 5,722 | -12,094 (-67.9%) |
| whitespace-delimited words | 2,409 | 748 | -1,661 (-69.0%) |
| estimated input tokens (bytes / 4, rounded up) | 4,454 | 1,431 | -3,023 (-67.9%) |

The token number is an explicitly labelled deterministic proxy, not a claim
about a provider tokenizer. For each fresh leg that includes the full file, the
expected context reduction is approximately 3,023 input tokens. Provider
prompt caching and resumed sessions can reduce the billed delta. Routing,
direction, and non-negotiable guardrails remain in CLAUDE.md; operational and
historical detail moved to `docs/reference/`.
