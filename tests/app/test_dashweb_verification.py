"""The field-verification ledger parser — tolerant by design.

The ledger is hand-edited prose in a markdown table whose columns have already
changed once, so these tests pin the tolerance, not the current column order.
"""

import os

from issue_agent.adapters.dashweb import verification

HARNESS_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

LEDGER = """\
# Field-verification ledger

Prose that is not a table.

| change | landed | evidence spec | status |
|---|---|---|---|
| #16 escalation ladder | be49b1d 2026-07-27 | `route_escalation` journal event | **pending** — never fired |
| codex per-leg usage deltas | 27565d9 | repair legs single-digit | VERIFIED 2026-07-27 (fifth) |
| pristine-base gate probe | 2b87a2e | red base blocks launch | MISBEHAVED 2026-07-28 → #49 |
| retired knob | old | n/a | RETIRED |
"""


def rows():
    return verification.parse_verification(LEDGER)


def test_every_data_row_is_parsed_and_the_header_is_not():
    assert [r.change for r in rows()][:1] == ["#16 escalation ladder"]
    assert len(rows()) == 4
    assert all(r.change != "change" for r in rows())


def test_status_is_read_from_the_verdict_column():
    assert [r.status for r in rows()] == ["pending", "VERIFIED", "MISBEHAVED", "RETIRED"]


def test_an_id_is_extracted_when_the_change_names_an_issue():
    assert rows()[0].id == "#16"
    assert rows()[1].id == "codex per-leg usage deltas"


def test_the_raw_row_is_always_kept_for_what_the_parser_could_not_classify():
    assert rows()[2].raw.startswith("| pristine-base gate probe |")


def test_a_verified_row_that_later_misbehaved_still_reads_verified():
    text = "|---|\n| x | y | z | VERIFIED 2026-07-28 — but MISBEHAVED on reversal → #54 |"
    assert verification.parse_verification(text)[0].status == "VERIFIED"


def test_column_drift_keeps_the_first_column_and_a_status_found_anywhere():
    text = "| #99 something | VERIFIED yesterday |"
    row = verification.parse_verification(text)[0]
    assert (row.change, row.status) == ("#99 something", "VERIFIED")


def test_a_row_with_no_verdict_keyword_defaults_to_pending():
    assert verification.parse_verification("| a | b | c | ??? |")[0].status == "pending"


def test_a_document_with_no_table_yields_no_rows():
    assert verification.parse_verification("# just prose\n\nnothing here") == []


def test_a_missing_ledger_is_a_warning_not_an_exception(tmp_path):
    payload = verification.verification_payload(str(tmp_path / "absent.md"))
    assert payload["rows"] == [] and payload["warnings"]


def test_the_real_ledger_parses_with_verdicts_on_both_sides(tmp_path):
    payload = verification.verification_payload(verification.default_path(HARNESS_ROOT))
    statuses = {r["status"] for r in payload["rows"]}
    assert payload["warnings"] == []
    assert {"VERIFIED", "pending"} <= statuses
    assert all(r["change"] for r in payload["rows"])


def test_status_indexes_distinguish_issue_and_pr_references():
    issue_status, pr_status = verification.status_indexes([
        {"change": "#62 capability ceiling", "shipped": "PR #76", "status": "pending"},
        {"change": "#62 capability ceiling — verdict", "shipped": None, "status": "VERIFIED"},
    ])
    assert issue_status == {62: "VERIFIED"}
    assert pr_status == {76: "pending"}


def test_reason_is_the_verdict_prose_with_the_keyword_removed():
    assert [r.reason for r in rows()] == [
        "never fired",
        "2026-07-27 (fifth)",
        "2026-07-28 → #49",
        "n/a",
    ]


def test_a_bare_verdict_falls_back_to_the_evidence_column():
    row = verification.parse_verification(
        "| change | landed | why it matters | status |\n"
        "|---|---|---|---|\n"
        "| #9 thing | abc | probe never fired again | VERIFIED |\n"
    )[0]
    assert row.reason == "probe never fired again"


def test_a_row_with_nothing_to_say_reports_no_reason():
    row = verification.parse_verification(
        "| change | landed | evidence | status |\n"
        "|---|---|---|---|\n"
        "| #9 thing |  |  | VERIFIED |\n"
    )[0]
    assert row.reason is None


def test_a_three_column_table_reports_its_reason_without_the_verdict_word():
    """The badge beside the reason already says VERIFIED; repeating it in the
    tooltip is noise whichever column the ledger happened to put it in."""
    row = verification.parse_verification(
        "| #9 thing | abc | VERIFIED — probe stayed green |\n"
    )[0]
    assert (row.status, row.reason) == ("VERIFIED", "probe stayed green")


def test_a_reason_longer_than_a_tooltip_is_bounded():
    row = verification.parse_verification(
        f"| #9 thing | abc | evidence | VERIFIED — {'y' * 5000} |\n"
    )[0]
    assert row.reason is not None and len(row.reason) == 240


def test_a_bounded_reason_is_taken_from_the_fallback_column_too():
    row = verification.parse_verification(
        f"| #9 thing | abc | {'z' * 5000} |\n"
    )[0]
    assert row.reason is not None and len(row.reason) == 240


def test_reason_indexes_distinguish_issue_and_pr_references():
    issue_reason, pr_reason = verification.reason_indexes([
        {"change": "#62 capability ceiling", "shipped": "PR #76",
         "status": "pending", "reason": "never fired"},
        {"change": "#63 relabel", "shipped": None, "status": "VERIFIED",
         "reason": None},
    ])
    assert issue_reason == {62: "never fired", 63: None}
    assert pr_reason == {76: "never fired"}
