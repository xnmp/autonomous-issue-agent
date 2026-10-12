import pytest

from issue_agent.domain.pr_body import complete_pr_body, issue_acceptance_criteria


@pytest.mark.parametrize("body", ["", "narrative", "  narrative", "Closes #420", "Fixes other/repo#42"])
def test_completion_preserves_narrative_and_adds_the_target_contract(body):
    completed = complete_pr_body(body, 42, "o/r", "- [ ] visible result")
    assert completed.startswith(body)
    assert "## Acceptance Criteria\n- [ ] visible result" in completed
    assert completed.endswith("Closes #42")
    assert complete_pr_body(completed, 42, "o/r", "changed contract") == completed


@pytest.mark.parametrize("link", ["Closes #42", "FIXES #42", "Resolved o/r#42", "Closes: #42"])
def test_existing_sections_and_valid_target_linkage_are_not_duplicated(link):
    body = f"## acceptance criteria\r\n- [x] current\r\n\r\n{link}\r\n"
    assert complete_pr_body(body, 42, "o/r", "- older") == body


def test_code_examples_do_not_substitute_for_the_published_contract():
    body = "```md\n## Acceptance Criteria\nCloses #42\n```"
    completed = complete_pr_body(body, 42, "o/r", "- actual")
    assert completed.endswith("## Acceptance Criteria\n- actual\n\nCloses #42")


@pytest.mark.parametrize("body", [
    "````md\nexample\n````", "```md\nexample", "~~~~~\nexample\n~~~~~~",
    "<!--\n## Acceptance Criteria\nCloses #42\n-->", "<!-- unfinished comment",
])
def test_completion_keeps_appended_sections_visible_and_is_idempotent(body):
    completed = complete_pr_body(body, 42, "o/r", "- actual")
    assert completed.endswith("## Acceptance Criteria\n- actual\n\nCloses #42")
    assert complete_pr_body(completed, 42, "o/r", "- changed") == completed


def test_fenced_payloads_inside_criteria_are_preserved():
    criteria = '- API returns exactly:\n```json\n{"success": true}\n```'
    assert issue_acceptance_criteria(f"## Acceptance Criteria\n{criteria}\n## Notes\nunrelated", []) == criteria
    comment = ("<details><summary>Technical acceptance criteria (for the engineer)"
               f"</summary>\n{criteria}\n</details>")
    assert issue_acceptance_criteria("", [comment]) == criteria


def test_unfinished_fenced_criteria_cannot_hide_issue_linkage():
    completed = complete_pr_body("narrative", 42, "o/r", "- response:\n```json\n{}")
    assert completed.endswith("{}\n```\n\nCloses #42")
    assert complete_pr_body(completed, 42, "o/r", "- changed") == completed


def test_latest_posted_contract_wins_and_stops_before_other_sections():
    assert issue_acceptance_criteria("## Acceptance Criteria\n- original", [
        "## Acceptance Criteria\n- old", "## Acceptance criteria\n- [ ] new\n## Test Plan\n- unrelated",
        "ordinary later discussion",
    ]) == "- [ ] new"


def test_triage_parsed_criteria_are_recovered_from_the_existing_comment():
    comment = ("Ready to build\n<details><summary>Technical acceptance criteria "
               "(for the engineer)</summary>\n\n- result one\n- result two\n</details>")
    assert issue_acceptance_criteria("", [comment]) == "- result one\n- result two"


@pytest.mark.parametrize("comments", [[], ["no criteria"], ["```\n## Acceptance Criteria\n- example\n```"]])
def test_absent_contract_does_not_invent_criteria(comments):
    assert issue_acceptance_criteria("no criteria", comments) == ""
    assert complete_pr_body("", 42, "o/r", "") == "## Acceptance Criteria\n\nCloses #42"


def test_large_contract_is_preserved_for_the_publication_boundary_to_bound():
    criterion = "- " + "é" * 100_000
    assert issue_acceptance_criteria(f"## Acceptance Criteria\n{criterion}", []) == criterion


@pytest.mark.parametrize("underline", ["-------------------", "===", "  ---"])
def test_a_setext_acceptance_heading_counts_as_the_existing_section(underline):
    body = f"Acceptance Criteria\n{underline}\n- [x] existing outcome\n\nCloses #42"
    assert complete_pr_body(body, 42, "o/r", "- issue contract") == body


def test_a_thematic_break_after_a_blank_line_is_not_a_heading():
    body = "narrative\n\n---\n\nmore"
    completed = complete_pr_body(body, 42, "o/r", "- actual")
    assert completed.endswith("## Acceptance Criteria\n- actual\n\nCloses #42")


@pytest.mark.parametrize("body", [
    "    ## Acceptance Criteria\n    - example only\n    Closes #42",
    "narrative\n\n    ## Acceptance Criteria\n    Closes #42\n\nmore narrative",
    "\t## Acceptance Criteria\n\tCloses #42",
])
def test_indented_code_examples_do_not_substitute_for_the_published_contract(body):
    completed = complete_pr_body(body, 42, "o/r", "- actual")
    assert completed.startswith(body)
    assert completed.endswith("## Acceptance Criteria\n- actual\n\nCloses #42")
    assert complete_pr_body(completed, 42, "o/r", "- changed") == completed


def test_an_atx_heading_may_be_indented_up_to_three_spaces():
    body = "   ## Acceptance Criteria\n- [x] current\n\nCloses #42"
    assert complete_pr_body(body, 42, "o/r", "- older") == body


def test_acceptance_subsections_are_kept_until_a_sibling_or_parent_heading():
    issue = ("## Acceptance Criteria\n### Happy path\n- [ ] Return 200\n"
             "### Failure path\n- [ ] Return 404\n## Notes\nOther text")
    assert issue_acceptance_criteria(issue, []) == (
        "### Happy path\n- [ ] Return 200\n### Failure path\n- [ ] Return 404")
    setext = "Acceptance Criteria\n===\n## Happy\n- [ ] a\nNotes\n===\nother"
    assert issue_acceptance_criteria(setext, []) == "## Happy\n- [ ] a"
