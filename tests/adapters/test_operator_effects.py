from __future__ import annotations

import json
from types import SimpleNamespace

from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.operator_effects import GitHubOperatorEffects


class Remote:
    def __init__(self):
        self.calls = []
        self.catalog = set()
        self.issue_labels = {"backlog"}
        self.issue_revision = 1
        self.auto_merge = False
        self.reject_auto_merge = False
        self.repo_revision = 1
        self.hooks = []
        self.next_hook = 100
        self.remote_branches = {}
        self.local_head = "a" * 40
        self.lose_issue_reply = False
        self.before_mutation = None
        # PR reviews, keyed by review id: {id: state}
        self.reviews = {}
        self.reject_dismissal = False

    def result(self, stdout="", returncode=0, stderr=""):
        return SimpleNamespace(
            stdout=stdout,
            stderr=stderr,
            returncode=returncode,
        )

    def run(self, argv, **_kwargs):
        self.calls.append(tuple(argv))
        if argv[:3] == ["gh", "label", "list"]:
            return self.result(
                json.dumps([{"name": label} for label in sorted(self.catalog)])
            )
        if argv[:3] == ["gh", "label", "create"]:
            self._before_mutation()
            self.catalog.add(argv[3])
            return self.result()
        if argv[:3] == ["gh", "issue", "view"]:
            return self.result(
                json.dumps(
                    {
                        "labels": [
                            {"name": label} for label in sorted(self.issue_labels)
                        ],
                        "updatedAt": f"revision-{self.issue_revision}",
                    }
                )
            )
        if (
            argv[:3] == ["gh", "api", "--method"]
            and "/issues/" in argv[4]
            and "/labels" in argv[4]
        ):
            self._before_mutation()
            if argv[3] == "POST":
                for index, value in enumerate(argv):
                    if value == "-f":
                        self.issue_labels.add(argv[index + 1].split("=", 1)[1])
            elif argv[3] == "DELETE":
                from urllib.parse import unquote

                self.issue_labels.discard(unquote(argv[4].rsplit("/", 1)[-1]))
            self.issue_revision += 1
            if self.lose_issue_reply:
                self.lose_issue_reply = False
                raise OSError("reply lost")
            return self.result()
        if argv[:3] == ["gh", "api", "repos/o/r"]:
            return self.result(
                json.dumps(
                    {
                        "allow_auto_merge": self.auto_merge,
                        "updated_at": f"repo-{self.repo_revision}",
                    }
                )
            )
        if argv[:4] == ["gh", "api", "-X", "PATCH"]:
            self._before_mutation()
            if not self.reject_auto_merge:
                self.auto_merge = True
                self.repo_revision += 1
            return self.result()
        if argv[:3] == ["gh", "api", "--paginate"] and argv[3].endswith("/reviews"):
            return self.result(
                json.dumps(
                    [
                        {
                            "id": int(review_id),
                            "state": state,
                            "user": {"login": "review-bot"},
                            "submitted_at": f"2026-08-02T0{review_id}:00:00Z",
                        }
                        for review_id, state in sorted(self.reviews.items())
                    ]
                )
            )
        if argv[:4] == ["gh", "api", "--method", "PUT"] and "/dismissals" in argv[4]:
            self._before_mutation()
            if self.reject_dismissal:
                return self.result(returncode=1, stderr="422 not permitted")
            self.reviews[argv[4].split("/reviews/")[1].split("/")[0]] = "DISMISSED"
            return self.result()
        if argv[:3] == ["gh", "api", "repos/o/r/hooks"]:
            return self.result(json.dumps(self.hooks))
        if argv[:4] == ["gh", "api", "-X", "DELETE"]:
            self._before_mutation()
            hook_id = argv[4].rsplit("/", 1)[-1]
            self.hooks = [
                hook for hook in self.hooks if str(hook["id"]) != hook_id
            ]
            return self.result()
        if argv[:3] == ["git", "-C", "/repo"] and argv[3] == "rev-parse":
            return self.result(self.local_head + "\n")
        if argv[:3] == ["git", "-C", "/repo"] and argv[3] == "ls-remote":
            branch = argv[-1].rsplit("/", 1)[-1]
            head = self.remote_branches.get(branch, "")
            return self.result(f"{head}\trefs/heads/{branch}\n" if head else "")
        if argv[:3] == ["git", "-C", "/repo"] and argv[3] == "push":
            self._before_mutation()
            self.remote_branches[argv[-1]] = self.local_head
            return self.result()
        raise AssertionError(f"unexpected command: {argv}")

    def spawn(self, argv, **_kwargs):
        self._before_mutation()
        self.calls.append(tuple(argv))
        self.next_hook += 1
        self.hooks.append(
            {
                "id": self.next_hook,
                "active": True,
                "created_at": f"hook-{self.next_hook}",
                "updated_at": f"hook-{self.next_hook}",
            }
        )
        return SimpleNamespace(stdout=None, wait=lambda: 1)

    def _before_mutation(self):
        if self.before_mutation is not None:
            self.before_mutation()


def _effects(tmp_path):
    store = SQLiteControlStore(str(tmp_path / "control.db"))
    store.initialize()
    remote = Remote()
    effects = GitHubOperatorEffects(
        "o/r",
        store,
        identity="o/r:operator:test",
        run=remote.run,
    )
    return effects, remote, store


def test_issue_label_mutation_is_committed_before_gh_and_deduplicated(
    tmp_path,
) -> None:
    effects, remote, store = _effects(tmp_path)
    observed = []
    remote.before_mutation = lambda: observed.append(
        store.pending_commands()[0].state
    )

    assert effects.set_issue_labels(
        9,
        add=("P1",),
        remove=("P2", "P3"),
    ) == (True, "ok")
    assert effects.set_issue_labels(
        9,
        add=("P1",),
        remove=("P2", "P3"),
    ) == (True, "ok")

    edits = [
        call for call in remote.calls
        if call[:3] == ("gh", "api", "--method") and "/labels" in call[4]
    ]
    assert observed == ["executing"]
    # One POST for the add; P2/P3 were never present, so no DELETEs are
    # issued (gh api raises on the 404 an absent label earns).
    assert len(edits) == 1
    assert store.pending_commands() == ()


def test_lost_mutation_reply_is_reconciled_without_a_blind_retry(tmp_path) -> None:
    effects, remote, store = _effects(tmp_path)
    remote.lose_issue_reply = True

    assert effects.set_issue_labels(9, add=("ai-ready",)) == (True, "ok")

    edits = [
        call for call in remote.calls
        if call[:3] == ("gh", "api", "--method") and "/labels" in call[4]
    ]
    assert len(edits) == 1
    assert "ai-ready" in remote.issue_labels
    assert store.pending_commands() == ()


def test_conditional_fallback_uses_the_command_revision_label_read(tmp_path) -> None:
    effects, remote, _store = _effects(tmp_path)
    # This is the state after an add-first escalator removes ai-working in the
    # dashboard's observation-to-command window.  The durable adapter must
    # repair the now-missing lifecycle arm from its own fresh read.
    remote.issue_labels = {"needs-human-review"}

    assert effects.set_issue_labels(
        9,
        add=("ai-ready",),
        remove=("needs-human-review",),
        add_unless_present=("ai-ready", "ai-working", "ai-pr-open"),
    ) == (True, "ok")

    assert remote.issue_labels == {"ai-ready"}


def test_conditional_fallback_preserves_a_surviving_lifecycle_arm(tmp_path) -> None:
    effects, remote, _store = _effects(tmp_path)
    remote.issue_labels = {"needs-human-review", "ai-working"}

    assert effects.set_issue_labels(
        9,
        add=("ai-ready",),
        remove=("needs-human-review",),
        add_unless_present=("ai-ready", "ai-working", "ai-pr-open"),
    ) == (True, "ok")

    assert remote.issue_labels == {"ai-working"}


def test_remote_bootstrap_mutations_all_receive_terminal_receipts(tmp_path) -> None:
    effects, remote, store = _effects(tmp_path)

    assert effects.ensure_label("P1", color="b60205") == (True, "ok")
    assert effects.set_repository_auto_merge(True) == (True, "ok")
    assert effects.publish_base_branch("/repo", "dev") == (True, "ok")

    assert remote.catalog == {"P1"}
    assert remote.auto_merge is True
    assert remote.remote_branches == {"dev": remote.local_head}
    assert store.pending_commands() == ()


def test_confirmed_repository_setting_rejection_is_terminal_until_reinvoked(
    tmp_path,
) -> None:
    effects, remote, store = _effects(tmp_path)
    remote.reject_auto_merge = True

    first = effects.set_repository_auto_merge(True)
    assert first[0] is False
    assert "rejected" in first[1]
    assert store.pending_commands() == ()

    second = effects.set_repository_auto_merge(True)
    patches = [
        call
        for call in remote.calls
        if call[:4] == ("gh", "api", "-X", "PATCH")
    ]
    assert second[0] is False
    assert len(patches) == 2
    assert store.pending_commands() == ()


def test_relay_creation_and_gc_use_commands_and_receipts(tmp_path) -> None:
    effects, remote, store = _effects(tmp_path)

    first = effects.start_relay_forward("issues", spawn=remote.spawn)
    second = effects.start_relay_forward("issues", spawn=remote.spawn)

    assert first is not second
    assert len(remote.hooks) == 1
    deletes = [
        call
        for call in remote.calls
        if call[:4] == ("gh", "api", "-X", "DELETE")
    ]
    forwards = [
        call
        for call in remote.calls
        if call[:3] == ("gh", "webhook", "forward")
    ]
    assert len(deletes) == 1
    assert len(forwards) == 2
    assert store.pending_commands() == ()


def test_unobserved_relay_creation_stays_unknown_until_readback_proves_absence(
    tmp_path,
) -> None:
    effects, remote, store = _effects(tmp_path)
    spawns = []

    def invisible_spawn(argv, **_kwargs):
        spawns.append(tuple(argv))
        return SimpleNamespace(stdout=None, wait=lambda: 1)

    effects.start_relay_forward("issues", spawn=invisible_spawn)
    assert store.pending_commands()[0].state == "unknown_outcome"

    effects.start_relay_forward("issues", spawn=invisible_spawn)

    assert len(spawns) == 2
    assert store.pending_commands()[0].state == "unknown_outcome"


class Journal:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


def _journalled_effects(tmp_path):
    store = SQLiteControlStore(str(tmp_path / "control.db"))
    store.initialize()
    remote = Remote()
    journal = Journal()
    effects = GitHubOperatorEffects(
        "o/r",
        store,
        identity="o/r:dashboard",
        run=remote.run,
        journal=journal,
    )
    return effects, remote, journal


def test_an_applied_label_change_is_journalled_as_an_operator_action(tmp_path):
    effects, remote, journal = _journalled_effects(tmp_path)
    remote.issue_labels = {"needs-human-review", "ai-blocked"}

    assert effects.set_issue_labels(
        9, remove=("needs-human-review", "ai-blocked")
    ) == (True, "ok")

    (event,) = journal.events
    payload = event.payload
    assert payload.kind == "operator_relabel"
    assert payload.number == 9 and event.correlation.issue == 9
    assert payload.attributes["remove"] == ["needs-human-review", "ai-blocked"]
    assert payload.attributes["operator"] == "o/r:dashboard"
    assert "#9" in payload.display


def test_a_caller_may_name_the_operator_action_it_is_performing(tmp_path):
    effects, remote, journal = _journalled_effects(tmp_path)
    remote.issue_labels = {"ai-blocked"}

    effects.set_issue_labels(
        9,
        remove=("ai-blocked",),
        action_kind="operator_unblock",
        action_attributes={"retries_cleared": True},
    )

    (event,) = journal.events
    assert event.payload.kind == "operator_unblock"
    assert event.payload.attributes["retries_cleared"] is True


def test_a_label_change_that_changed_nothing_is_not_journalled(tmp_path):
    effects, remote, journal = _journalled_effects(tmp_path)
    remote.issue_labels = {"P1"}

    assert effects.set_issue_labels(9, add=("P1",)) == (True, "ok")

    assert journal.events == []


def test_a_failed_label_change_is_not_journalled_as_applied(tmp_path):
    effects, remote, journal = _journalled_effects(tmp_path)
    remote.issue_labels = {"ai-blocked"}
    remote.before_mutation = lambda: (_ for _ in ()).throw(OSError("no network"))

    applied, _detail = effects.set_issue_labels(9, remove=("ai-blocked",))

    assert applied is False and journal.events == []


# ── review dismissal: the only true re-arm of the rejection ceiling ──────────
def _rejection(review_id, state="CHANGES_REQUESTED"):
    from issue_agent.domain.review import SubmittedReview

    return SubmittedReview(review_id, state, "review-bot")


def test_a_dismissal_is_committed_before_gh_and_leaves_the_review_dismissed(
    tmp_path,
) -> None:
    effects, remote, store = _effects(tmp_path)
    remote.reviews = {"41": "CHANGES_REQUESTED"}
    observed = []
    remote.before_mutation = lambda: observed.append(
        store.pending_commands()[0].state
    )

    assert effects.dismiss_review(7, _rejection("41"), "operator re-arm") == (
        True, "ok",
    )

    assert observed == ["executing"]
    assert remote.reviews == {"41": "DISMISSED"}
    assert store.pending_commands() == ()
    put = [c for c in remote.calls if "/dismissals" in " ".join(c)]
    assert put[0][:4] == ("gh", "api", "--method", "PUT")
    assert "repos/o/r/pulls/7/reviews/41/dismissals" in put[0][4]
    assert "message=operator re-arm" in put[0]


def test_replaying_a_dismissal_does_not_re_issue_it(tmp_path) -> None:
    effects, remote, _store = _effects(tmp_path)
    remote.reviews = {"41": "CHANGES_REQUESTED"}

    effects.dismiss_review(7, _rejection("41"), "m")
    effects.dismiss_review(7, _rejection("41"), "m")

    assert len([c for c in remote.calls if "/dismissals" in " ".join(c)]) == 1


def test_a_refused_dismissal_is_reported_not_raised(tmp_path) -> None:
    effects, remote, _store = _effects(tmp_path)
    remote.reviews = {"41": "CHANGES_REQUESTED"}
    remote.reject_dismissal = True

    ok, detail = effects.dismiss_review(7, _rejection("41"), "m")

    assert ok is False and "422" in detail
    assert remote.reviews == {"41": "CHANGES_REQUESTED"}


def test_an_unreadable_review_list_is_an_error_not_an_empty_history(
    tmp_path,
) -> None:
    """"No reviews" and "GitHub would not say" lead to opposite operator
    decisions, so they must not collapse into the same answer."""
    effects, remote, _store = _effects(tmp_path)
    effects.run = lambda argv, **_kw: remote.result("not json")

    try:
        effects.pr_reviews(7)
    except Exception as exc:
        assert "malformed" in str(exc)
    else:
        raise AssertionError("an unreadable review list must raise")


def test_reviews_are_parsed_into_the_domains_value_object(tmp_path) -> None:
    effects, remote, _store = _effects(tmp_path)
    remote.reviews = {"41": "CHANGES_REQUESTED", "42": "APPROVED"}

    reviews = effects.pr_reviews(7)

    assert [(r.review_id, r.state, r.author) for r in reviews] == [
        ("41", "CHANGES_REQUESTED", "review-bot"),
        ("42", "APPROVED", "review-bot"),
    ]


def test_a_dismissal_without_a_review_id_never_reaches_github(tmp_path) -> None:
    effects, remote, _store = _effects(tmp_path)

    assert effects.dismiss_review(7, _rejection(""), "m")[0] is False
    assert effects.dismiss_review(0, _rejection("41"), "m")[0] is False
    assert remote.calls == []


def test_an_error_object_from_gh_is_not_read_as_an_empty_review_list(
    tmp_path,
) -> None:
    """`gh api` answers a 404 with a JSON OBJECT. Parsed as "no reviews" it
    would report a PR as needing no re-arm at all."""
    effects, remote, _store = _effects(tmp_path)
    effects.run = lambda argv, **_kw: remote.result(
        json.dumps({"message": "Not Found"})
    )

    try:
        effects.pr_reviews(7)
    except Exception as exc:
        assert "invalid payload" in str(exc)
    else:
        raise AssertionError("an error object must not read as no reviews")


def test_a_review_with_no_user_is_attributed_to_nobody_not_crashed_on(
    tmp_path,
) -> None:
    effects, remote, _store = _effects(tmp_path)
    effects.run = lambda argv, **_kw: remote.result(
        json.dumps([{"id": 1, "state": "CHANGES_REQUESTED"}, {"state": "X"}])
    )

    reviews = effects.pr_reviews(7)

    assert [(r.review_id, r.author) for r in reviews] == [("1", "")]
