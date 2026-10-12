"""Empty-policy outcomes captured against origin/dev 807aa508, without ignores.

Slice 1 (2026-10-12) deliberately diverges on "no checks reported": a head
with no CI result never automerges, since required CI is the only test
evidence once review-time operator tests are gone.

ci_empty_policy_probe.py also runs unchanged with that checkout's src on
PYTHONPATH, so the compatibility contract can be checked against real code.
"""

import pytest

from tests.adapters.ci_empty_policy_probe import cases


@pytest.fixture(scope="module")
def observed_cases():
    return {case["case"]: case for case in cases()}


@pytest.mark.parametrize("expected", [
    {"case": "empty-policy-red-head-green-merge", "ci": "failure", "checks": [["unit", "fail", 10]], "actions": ["RerunFailedChecks"]},
    {"case": "same-name-distinct-workflows", "ci": "ok", "checks": [["unit", "pass", 11]], "actions": ["EnableAutoMerge"]},
    {"case": "same-name-distinct-events", "ci": "ok", "checks": [["unit", "pass", 11]], "actions": ["EnableAutoMerge"]},
    {"case": "all-queued", "ci": "pending", "checks": [["unit", "pending", 10]], "actions": []},
    {"case": "latest-rerun", "ci": "ok", "checks": [["unit", "pass", 11]], "actions": ["EnableAutoMerge"]},
    {"case": "empty-reports", "ci": "none", "checks": [], "actions": []},
    {"case": "empty-reports-with-retry-debt", "ci": "none", "checks": [], "actions": []},
    {"case": "empty-reports-check-blocker", "ci": "none", "checks": [], "actions": []},
    {"case": "empty-policy-hooks", "ci": "none", "checks": [], "actions": []},
    {"case": "empty-policy-unstable", "ci": "ok", "checks": [["unit", "pass", 10]], "actions": []},
    {"case": "external-status-stalled", "ci": "pending", "checks": [["external-CI", "pending", None]], "actions": ["EscalateCiPending"]},
], ids=lambda case: case["case"])
def test_empty_policy_matches_dev(observed_cases, expected):
    assert observed_cases[expected["case"]] == expected
