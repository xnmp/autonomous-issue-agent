#!/usr/bin/env bash
# Create the demo's seed issues on a target GitHub repo, labelled ai-ready so the
# fix watcher picks them up. Run AFTER bin/bootstrap.sh has created the labels.
#
#   REPO=you/calc-demo bash examples/demo-target/seed-issues.sh
set -uo pipefail
REPO="${REPO:?set REPO=owner/repo}"

mk() {  # mk <title> <labels> <body>
  local title="$1" labels="$2" body="$3"
  # skip if a same-title issue already exists (idempotent)
  if gh issue list --repo "$REPO" --search "$title in:title" --state all --json number --jq 'length' 2>/dev/null | grep -qv '^0$'; then
    echo "  ⊘ exists: $title"; return 0
  fi
  gh issue create --repo "$REPO" --title "$title" --label "$labels" --body "$body" >/dev/null \
    && echo "  ✓ created: $title"
}

mk "divide() must reject division by zero" "ai-ready,P1" \
'## Problem
`calc.divide(a, 0)` raises a bare `ZeroDivisionError` (see src/calc/operations.py).

## Expected
Per INVARIANTS.md #2 and ADR 0001, `divide(a, 0)` must raise `ValueError` naming
the operation (e.g. "divide: cannot divide by zero"). No inf/nan/None.

## Notes
This is a `fix/` — add a regression test that fails before and passes after.'

mk "Add a modulo operation" "ai-ready,P2" \
'## Request
Add `modulo(a, b)` to `calc` returning `a % b`, with the same zero-divisor
contract as divide (ValueError on b == 0).

## Acceptance
- `modulo(7, 3) == 1`
- `modulo(5, 0)` raises ValueError.
This is a `feat/` — add an integration test exercising it through the package API.'

echo "Done. Move issues to ai-ready (already labelled) and start the fix watcher."
