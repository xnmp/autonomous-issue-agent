"""#441: how an item's next leg starts, and whether a cut-off end counts. Pure."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from issue_agent.domain.actions import AgentKind
from issue_agent.domain.attempt import AnswerResume, Continue, Fresh, cut_off_counts, latest_session, next_leg
from issue_agent.domain.backoff import LegEnd

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
RESOLVE, FIX_CI, REVIEW = AgentKind.RESOLVE, AgentKind.FIX_CI, AgentKind.REVIEW


def end(minute: int, *, kind: AgentKind = RESOLVE, machine: bool = False, session: str = "",
        runtime: str = "claude", question: str | None = None) -> LegEnd:
    return LegEnd(kind, T0 + timedelta(minutes=minute), machine, question=question, session=session, runtime=runtime)


def test_an_item_with_no_legs_starts_fresh_and_answers_nothing():
    assert next_leg((), "claude").start == Fresh() and next_leg((), "claude").answering is None


def test_the_newest_machine_end_with_a_session_since_the_last_agent_end_is_continued():
    ends = (end(0, session="s-old"), end(1, machine=True, session="s-a"), end(2, machine=True),
            end(3, machine=True, session="s-b"), end(4, machine=True))
    assert next_leg(ends, "claude").start == Continue("s-b")


def test_an_agent_end_spends_the_free_resume():
    ends = (end(0, machine=True, session="s-a"), end(1, session="s-a"))
    assert next_leg(ends, "claude").start == Fresh()


def test_a_session_on_another_runtime_is_never_continued():
    ends = (end(0, machine=True, session="s-a", runtime="codex"),)
    assert next_leg(ends, "claude").start == Fresh()
    assert next_leg(ends, "codex").start == Continue("s-a")


def test_legs_of_kinds_that_never_resume_neither_spend_nor_offer_a_resume():
    ends = (end(0, kind=FIX_CI, machine=True, session="s-a"), end(1, kind=REVIEW, session="s-r"),
            end(2, kind=REVIEW, machine=True, session="s-r2"))
    assert next_leg(ends, "claude").start == Continue("s-a")


def test_an_answered_question_resumes_the_session_that_asked_it():
    asked = end(0, session="s-q", question="404 or 410?")
    plan = next_leg((asked,), "claude")
    assert (plan.start, plan.answering) == (AnswerResume("s-q"), asked)


def test_a_machine_end_after_a_question_keeps_the_question_and_is_continued():
    asked = end(0, session="s-q", question="404 or 410?")
    plan = next_leg((asked, end(1, machine=True, session="s-q"), end(2, machine=True)), "claude")
    assert (plan.start, plan.answering) == (Continue("s-q"), asked)


def test_a_question_asked_on_another_runtime_is_answered_in_a_fresh_session():
    asked = end(0, session="s-q", runtime="codex", question="which API?")
    plan = next_leg((asked,), "claude")
    assert (plan.start, plan.answering) == (Fresh(), asked)


@pytest.mark.parametrize("kind", [AgentKind.STEWARD, AgentKind.TRIAGE, AgentKind.REVIEW])
def test_a_cut_off_of_a_kind_that_never_resumes_counts_at_once(kind):
    assert cut_off_counts(kind, "claude", ())


@pytest.mark.parametrize("kind", [AgentKind.RESOLVE, AgentKind.FIX_CI, AgentKind.ADDRESS_REVIEW, AgentKind.REBASE])
def test_a_routed_cut_off_counts_only_as_the_free_resume(kind):
    assert not cut_off_counts(kind, "claude", ())
    assert not cut_off_counts(kind, "claude", (end(0, kind=kind, session="s"),))
    assert cut_off_counts(kind, "claude", (end(0, kind=kind, machine=True, session="s"),))
    assert not cut_off_counts(kind, "codex", (end(0, kind=kind, machine=True, session="s"),))


def test_latest_session_reads_only_nonempty_sessions_on_the_runtime():
    ends = (end(0, session="a"), end(1, session="b", runtime="codex"), end(2))
    assert latest_session(ends, "claude") == "a" and latest_session(ends, "codex") == "b"
    assert latest_session((), "claude") == "" and latest_session(ends, "") == ""


@pytest.mark.parametrize("session", ["x" * 129, 7, None])
def test_a_leg_end_refuses_an_unbounded_or_non_text_session(session):
    with pytest.raises(ValueError):
        LegEnd(RESOLVE, T0, False, session=session)  # type: ignore[arg-type]
