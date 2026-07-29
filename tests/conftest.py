"""Suite-wide environment sanitization (the #63 class, python tier).

The bash suites unset the deployment knobs at their top; the pytest tiers had
no equivalent, so a developer's (or a self-hosted fixer's) exported LINT_CMD /
TYPECHECK_CMD / GH_APP_ID flipped adapter-test verdicts — verified live on
2026-07-29. One source of truth for the list: the scrub the gate itself applies.

HARNESS_GATE_DEPTH is deliberately NOT cleared: it is the fork-bomb
re-entrancy counter, and clearing it inside a nested (gate-launched) suite run
would re-arm the bomb.
"""

import os

from issue_agent.adapters.governor import target_cmd_scrub_set

for _knob in target_cmd_scrub_set():
    os.environ.pop(_knob, None)
