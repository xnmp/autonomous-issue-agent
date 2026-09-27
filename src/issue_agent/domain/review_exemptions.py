"""Grant evidence shared by review execution and offline authority import."""

from .exemptions import (
    ExemptionDeclaration, MAX_DECLARATIONS_PER_CHANGE, RULE_REQUIRED_TEST, RULE_TESTS_IMMUTABLE,
    authenticated_grant_pairs, parse_exemption_declarations, parse_exemption_grants, valid_subject,
)
from .gate import modified_existing_tests_from_diff
from .identity import login_eq
from .world import Review


def review_declarations(diff: str, messages: tuple[str, ...]) -> tuple[ExemptionDeclaration, ...]:
    """The runtime's bounded declaration set from revision-bound evidence."""
    subjects, _undecidable = modified_existing_tests_from_diff(diff)
    declared = dict.fromkeys(ExemptionDeclaration(RULE_TESTS_IMMUTABLE, path)
                             for path in subjects if valid_subject(path))
    for message in messages:
        for declaration in parse_exemption_declarations(message):
            if declaration.rule == RULE_REQUIRED_TEST:
                declared.setdefault(declaration)
    return tuple(declared)[:MAX_DECLARATIONS_PER_CHANGE]


def approval_grants_cover(
    reviews: tuple[Review, ...], *, review_bot: str, declarations: tuple[ExemptionDeclaration, ...],
    issue_number: int | None, patch_id: str, mac_key: str,
) -> bool:
    """Only authenticated grants from bot approvals cover a declaration."""
    grants = tuple(grant for review in reviews
                   if review.state == "APPROVED" and login_eq(review.author, review_bot)
                   for grant in parse_exemption_grants(review.body))
    granted = authenticated_grant_pairs(grants, issue_number=issue_number, patch_id=patch_id, mac_key=mac_key)
    return all((declaration.rule, declaration.subject) in granted for declaration in declarations)
