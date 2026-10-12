"""Small issue transport double whose remote state survives client restarts.

Each label addition/removal commits separately, matching GitHub's partial-write
boundary. Fault hooks run outside transactions; they can emulate a lost reply
or interleave a second client without making remote writes locally atomic.
Pull request reviews are paginated like the REST list: a failing page makes
the read partial, never a shorter complete list.

`fault` raises (an infra failure or lost reply). `soft_failures` names
operations that fail the way `GitHubGateway` does when GitHub answers with an
error (403/404/422, which `gh` returns as empty output): reads raise or come
back partial exactly as the real parser makes them, writes return False or
are silently not applied. They must never read as "absent".
"""

from tests.domain.builders import observed_pr

from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import sqlite3

from issue_agent.adapters.github import ReviewReadUnavailable, parse_pr, parse_review_pages
from issue_agent.domain.labels import Label
from issue_agent.domain.review_workflow import (
    CompleteReviews, PartialReviews, SubmittedReviewFact, SubmittedReviewState, body_review_command,
)
from issue_agent.domain.checks import Observed, Unavailable, RequiredCheck
from issue_agent.domain.world import Check, Comment, IssueView, PRView, PullRequestDiff, Review, WorldState


class PersistentGitHub:
    def __init__(self, path: Path, *, fault: Callable[[str], None] = lambda _: None,
                 clock: Callable[[], datetime] | None = None):
        self.path = path
        self.fault = fault
        self.clock = clock
        self.soft_failures: set[str] = set()
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS issues (number INTEGER PRIMARY KEY, body TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS prs (number INTEGER PRIMARY KEY, body TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS calls (body TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS links (issue INTEGER PRIMARY KEY, pr INTEGER NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS clock (revision INTEGER NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS reviews (seq INTEGER PRIMARY KEY AUTOINCREMENT, pr INTEGER NOT NULL, "
                       "review_id TEXT NOT NULL UNIQUE, author TEXT NOT NULL, state TEXT NOT NULL, commit_id TEXT, "
                       "submitted_at TEXT, body TEXT NOT NULL DEFAULT '')")
            db.execute("CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS diffs (head TEXT PRIMARY KEY, text TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS commit_messages (pr INTEGER PRIMARY KEY, body TEXT NOT NULL)")
            if db.execute("SELECT revision FROM clock").fetchone() is None:
                db.execute("INSERT INTO clock VALUES (0)")

    def _connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def seed(self, issue: IssueView) -> None:
        body = asdict(issue)
        body["labels"] = [label.value for label in issue.labels]
        body["declared_paths"] = sorted(issue.declared_paths)
        body["predicted_paths"] = sorted(issue.predicted_paths)
        with self._connect() as db:
            db.execute("INSERT INTO issues VALUES (?, ?)", (issue.number, json.dumps(body)))

    def get_issue(self, number: int) -> IssueView:
        self.fault("read")
        with self._connect() as db:
            row = db.execute("SELECT body FROM issues WHERE number = ?", (number,)).fetchone()
        if row is None:
            raise KeyError(number)
        body = json.loads(row[0])
        body["labels"] = frozenset(Label(value) for value in body["labels"])
        body["comments"] = tuple(Comment(**comment) for comment in body["comments"])
        body["declared_paths"] = frozenset(body["declared_paths"])
        body["predicted_paths"] = frozenset(body["predicted_paths"])
        body["open_blocker_numbers"] = tuple(body["open_blocker_numbers"])
        return IssueView(**body)

    def edit_issue_body(self, number: int, body: str) -> None:
        self._update(number, lambda issue: issue.update(body=body))

    def reopen_issue(self, number: int) -> None:
        self._update(number, lambda issue: issue.update(state="open", state_reason="reopened"))

    def seed_pr(self, pr: PRView) -> None:
        body = asdict(pr)
        body["labels"] = sorted(label.value for label in pr.labels)
        body["changed_files"] = sorted(pr.changed_files)
        if isinstance(pr.ci, Observed):
            body["ci"]["policy"] = [asdict(r) for r in pr.ci.policy]
        with self._connect() as db:
            db.execute("INSERT INTO prs VALUES (?, ?)", (pr.number, json.dumps(body)))

    def issue_for_pr(self, number: int) -> int | None:
        from issue_agent.domain.world import issue_for_pr
        return issue_for_pr(self.get_pr(number))

    def get_pr(self, number: int, *, mergeability: bool = True, checks: bool = True) -> PRView:
        self.fault("read")
        if not checks:
            self.fault("observation_read")  # a harness workflow read, never the world observation
        if "get_pr" in self.soft_failures:
            return parse_pr(None)  # type: ignore[arg-type]  # the real gateway's parse of an empty answer
        with self._connect() as db:
            body = json.loads(db.execute("SELECT body FROM prs WHERE number = ?", (number,)).fetchone()[0])
        body["labels"] = frozenset(Label(value) for value in body["labels"])
        body["changed_files"] = frozenset(body["changed_files"])
        for key, constructor in (("comments", Comment), ("reviews", Review)):
            body[key] = tuple(constructor(**entry) for entry in body[key])
        # Reviews live in the reviews table once any exist for this PR, so a
        # submitted or dismissed review is what the next read reports.
        with self._connect() as db:
            rows = db.execute("SELECT author, state, commit_id, submitted_at, body FROM reviews "
                              "WHERE pr = ? AND state != 'PENDING' ORDER BY seq", (number,)).fetchall()
        if rows:
            body["reviews"] = tuple(Review(state, author, submitted or "", text, commit or "")
                                    for author, state, commit, submitted, text in rows)
        raw = body["ci"]
        body["ci"] = (Unavailable(raw["reason"]) if "reason" in raw else
            Observed(raw["head"], raw["tested_revision"], frozenset(RequiredCheck(**r) for r in raw["policy"]),
                     tuple(Check(**c) for c in raw["reports"])))
        if not checks:
            body["ci"] = Unavailable("checks not requested")
        return PRView(**body)

    def build_world(self) -> WorldState:
        with self._connect() as db:
            numbers = [row[0] for row in db.execute("SELECT number FROM issues ORDER BY number")]
            prs = [row[0] for row in db.execute("SELECT number FROM prs ORDER BY number")]
        return WorldState(issues=tuple(self.get_issue(number) for number in numbers), prs=tuple(self.get_pr(number) for number in prs))

    def build_review_world(self) -> WorldState:
        with self._connect() as db:
            prs = [row[0] for row in db.execute("SELECT number FROM prs ORDER BY number")]
        return WorldState(prs=tuple(self.get_pr(number, mergeability=False) for number in prs))

    def set_diff(self, head: str, text: str) -> None:
        """The compare diff GitHub serves for any base...head (not a harness call)."""
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO diffs VALUES (?, ?)", (head, text))

    def pr_diff(self, pr: int, base_oid: str, head_oid: str) -> PullRequestDiff:
        self.fault("diff")
        if "pr_diff" in self.soft_failures:
            raise RuntimeError("immutable pull-request diff is unavailable")
        with self._connect() as db:
            row = db.execute("SELECT text FROM diffs WHERE head = ?", (head_oid,)).fetchone()
        if row is None:
            raise RuntimeError("immutable pull-request diff is unavailable")
        return PullRequestDiff(base_oid, head_oid, row[0])

    def set_commit_messages(self, pr: int, *messages: str) -> None:
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO commit_messages VALUES (?, ?)", (pr, json.dumps(messages)))

    def pr_commit_messages(self, pr: int) -> tuple[str, ...]:
        self.fault("commit_messages")
        if "pr_commit_messages" in self.soft_failures:
            raise RuntimeError("commit messages were not answered")
        with self._connect() as db:
            row = db.execute("SELECT body FROM commit_messages WHERE pr = ?", (pr,)).fetchone()
        return () if row is None else tuple(json.loads(row[0]))

    def _update(self, number: int, change: Callable[[dict], None]) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            body = json.loads(db.execute("SELECT body FROM issues WHERE number = ?", (number,)).fetchone()[0])
            db.execute("UPDATE clock SET revision = revision + 1")
            revision = db.execute("SELECT revision FROM clock").fetchone()[0]
            body["updated_at"] = f"2026-09-14T00:{revision // 60:02d}:{revision % 60:02d}Z"
            change(body)
            db.execute("UPDATE issues SET body = ? WHERE number = ?", (json.dumps(body), number))

    @property
    def calls(self) -> list[list]:
        with self._connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT body FROM calls ORDER BY rowid")]

    def _call(self, *values) -> None:
        with self._connect() as db:
            db.execute("INSERT INTO calls VALUES (?)", (json.dumps(values),))

    def has_label(self, kind: str, number: int, label: str) -> bool:
        view = self.get_issue(number) if kind == "issue" else self.get_pr(number)
        return Label(label) in view.labels

    def relabel(self, kind: str, number: int, remove: str | None, add: str | None) -> None:
        if kind == "pr":
            self.fault("relabel_pr_requested")
            self._call("relabel_pr", number, remove, add)
            if "relabel_pr" in self.soft_failures:
                return  # the real gateway ignores an empty (refused) answer
            with self._connect() as db:
                body = json.loads(db.execute("SELECT body FROM prs WHERE number = ?", (number,)).fetchone()[0])
                body["labels"] = sorted((set(body["labels"]) | ({add} if add else set())) - ({remove} if remove else set()))
                db.execute("UPDATE prs SET body = ? WHERE number = ?", (json.dumps(body), number))
            self.fault("relabel_pr_applied")
            return
        assert kind == "issue"
        # A request lost before GitHub applied it: the caller cannot tell it
        # from a lost reply, but the remote never changes.
        self.fault("relabel_requested")
        self._call("relabel", number, remove, add)
        if add:
            self._update(number, lambda body: body.update(labels=sorted(set(body["labels"]) | {add})))
            self.fault("label_added")
        if remove:
            self._update(number, lambda body: body.update(labels=sorted(set(body["labels"]) - {remove})))
        self.fault("relabel_applied")

    def comment(self, kind: str, number: int, body: str) -> None:
        if kind == "pr":
            self._call("comment_pr", number, body)
            self.external_pr_change(number, comments=[
                *json.loads(self._pr_body(number))["comments"],
                {"author": "review-bot[bot]", "body": body, "created_at": "2026-09-25T12:00:00Z"},
            ])
            return
        assert kind == "issue"
        self._call("comment", number, body)
        self._update(number, lambda issue: issue["comments"].append({
            "author": getattr(self, "comment_author", "fix-bot"), "body": body, "created_at": issue["updated_at"],
        }))
        self.fault("comment_applied")

    def has_comment(self, kind: str, number: int, body: str) -> bool:
        if kind == "pr":
            return any(comment.body == body for comment in self.get_pr(number).comments)
        assert kind == "issue"
        return any(comment.body == body for comment in self.get_issue(number).comments)

    def _pr_body(self, number: int) -> str:
        with self._connect() as db:
            return db.execute("SELECT body FROM prs WHERE number = ?", (number,)).fetchone()[0]

    def close_issue(self, number: int, reason: str, comment: str) -> None:
        self._call("close", number, reason)
        self._update(number, lambda body: body.update(state="closed", state_reason=reason))
        self.fault("close_applied")

    def issue_closure(self, number: int) -> tuple[bool, str]:
        issue = self.get_issue(number)
        return issue.state.lower() == "closed", issue.state_reason

    def pr_for_issue(self, number: int, *, require_unique: bool = False) -> int | None:
        # The link's issue primary key permits only one selected PR.
        # A failed linkage read is unavailable, never an observed absence.
        self.fault("linkage")
        with self._connect() as db:
            row = db.execute("SELECT pr FROM links WHERE issue = ?", (number,)).fetchone()
        return None if row is None else int(row[0])

    def link_pr(self, issue: int, pr: int) -> None:
        """Model GitHub linkage appearing outside the harness (not a harness call)."""
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO links VALUES (?, ?)", (issue, pr))

    def external_change(
        self, number: int, *, add: tuple[str, ...] = (), remove: tuple[str, ...] = (),
        state: str | None = None, state_reason: str = "", body: str | None = None,
        comment: tuple[str, str] | None = None,
    ) -> None:
        """A human/remote edit: advances the revision but is not a harness call.

        ``comment`` is ``(author, body)`` appended as third-party conversation.
        """
        def change(issue: dict) -> None:
            issue["labels"] = sorted((set(issue["labels"]) | set(add)) - set(remove))
            if state is not None:
                issue.update(state=state, state_reason=state_reason)
            if body is not None:
                issue["body"] = body
            if comment is not None:
                author, text = comment
                issue["comments"].append({"author": author, "body": text, "created_at": issue["updated_at"]})
        self._update(number, change)

    def external_pr_change(self, number: int, **fields) -> None:
        """A remote PR edit (for example a pushed head) outside the harness."""
        with self._connect() as db:
            body = json.loads(db.execute("SELECT body FROM prs WHERE number = ?", (number,)).fetchone()[0])
            if "checks_known" in fields:
                known = fields.pop("checks_known")
                if not known:
                    fields["ci"] = {"reason": "checks not requested"}
            if "checks" in fields:
                reports = tuple(Check(**raw) if isinstance(raw, dict) else raw for raw in fields.pop("checks"))
                head = fields.get("head_ref_oid", body["head_ref_oid"])
                fields["ci"] = asdict(Observed(head, head, frozenset(), reports))
                fields["ci"]["policy"] = []
            body.update(fields)
            db.execute("UPDATE prs SET body = ? WHERE number = ?", (json.dumps(body), number))

    # -- pull request reviews ----------------------------------------------------

    def seed_review(
        self, pr: int, review_id: str, *, author: str = "review-bot[bot]", state: str = "CHANGES_REQUESTED",
        commit: str | None = None, submitted_at: str | None = "2026-09-25T11:00:00+00:00", body: str = "",
    ) -> None:
        """A review that exists on GitHub (seeded or submitted by a human): not a harness call."""
        with self._connect() as db:
            db.execute("INSERT INTO reviews(pr, review_id, author, state, commit_id, submitted_at, body) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?)", (pr, review_id, author, state, commit, submitted_at, body))

    def external_review_change(self, review_id: str, *, state: str | None = None, delete: bool = False) -> None:
        with self._connect() as db:
            if delete:
                db.execute("DELETE FROM reviews WHERE review_id = ?", (review_id,))
            elif state is not None:
                db.execute("UPDATE reviews SET state = ? WHERE review_id = ?", (state, review_id))

    def review_state(self, review_id: str) -> str | None:
        with self._connect() as db:
            row = db.execute("SELECT state FROM reviews WHERE review_id = ?", (review_id,)).fetchone()
        return None if row is None else row[0]

    def deny_dismissals(self) -> None:
        """The review identity lacks permission (or branch protection forbids dismissal): GitHub answers 403."""
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES ('dismissals', 'denied')")

    def _setting(self, name: str) -> str | None:
        with self._connect() as db:
            row = db.execute("SELECT value FROM settings WHERE name = ?", (name,)).fetchone()
        return None if row is None else row[0]

    def pull_request_reviews(self, pr: int, *, page_size: int = 2) -> CompleteReviews | PartialReviews:
        """The gateway contract: every page or a partial read; PENDING drafts are skipped."""
        if "reviews" in self.soft_failures:
            return parse_review_pages(None)
        with self._connect() as db:
            rows = db.execute("SELECT review_id, author, state, commit_id, submitted_at, body FROM reviews "
                              "WHERE pr = ? ORDER BY seq", (pr,)).fetchall()
        facts = []
        for page, start in enumerate(range(0, max(len(rows), 1), page_size)):
            try:
                self.fault(f"reviews_page:{page}")
            except Exception as exc:  # noqa: BLE001 - a failed page is a partial read
                return PartialReviews(f"review page {page} failed: {exc}")
            for review_id, author, state, commit, submitted, body in rows[start:start + page_size]:
                if state == "PENDING":
                    continue
                facts.append(SubmittedReviewFact(review_id, author, SubmittedReviewState(state), commit,
                                                 datetime.fromisoformat(submitted), body_review_command(body)))
        return CompleteReviews(tuple(facts))

    def dismiss_review(self, pr: int, review_id: str, message: str) -> bool:
        # A request lost before GitHub applied it is indistinguishable from a lost reply.
        self.fault("dismiss_requested")
        self._call("dismiss_review", pr, review_id, message)
        if self._setting("dismissals") == "denied" or "dismiss_review" in self.soft_failures:
            return False
        with self._connect() as db:
            row = db.execute("SELECT state FROM reviews WHERE pr = ? AND review_id = ?", (pr, review_id)).fetchone()
            if row is None or row[0] != "CHANGES_REQUESTED":
                return False
            db.execute("UPDATE reviews SET state = 'DISMISSED' WHERE review_id = ?", (review_id,))
        self.fault("dismiss_applied")
        return True

    def submit_review(self, pr: int, event: str, body: str, head: str) -> bool:
        state = {"APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED", "COMMENT": "COMMENTED"}[event]
        # A request lost before GitHub applied it is indistinguishable from a lost reply.
        self.fault("review_requested")
        self._call("submit_review", pr, event, head)
        if "submit_review" in self.soft_failures:
            return False
        submitted = self.clock().isoformat() if self.clock is not None else "2026-09-25T12:00:00+00:00"
        with self._connect() as db:
            count = db.execute("SELECT count(*) FROM reviews").fetchone()[0]
            db.execute("INSERT INTO reviews(pr, review_id, author, state, commit_id, submitted_at, body) "
                       "VALUES (?, ?, 'review-bot[bot]', ?, ?, ?, ?)",
                       (pr, f"submitted-{count}", state, head, submitted, body))
        self.fault("review_submitted")
        return True

    def has_review(self, pr: int, event: str, body: str, head: str) -> bool:
        state = {"APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED", "COMMENT": "COMMENTED"}[event]
        self.fault("read")
        if "has_review" in self.soft_failures:
            raise ReviewReadUnavailable(f"review read refused: pull request {pr} reviews were not answered completely")
        with self._connect() as db:
            return db.execute("SELECT 1 FROM reviews WHERE pr = ? AND state = ? AND body = ? AND commit_id = ?",
                              (pr, state, body, head)).fetchone() is not None

    def workflow_run_attempts(self, head: str, run_ids: tuple[int, ...]) -> dict[int, int] | None:
        prs = self.build_world().prs
        known = {check.run_id for pr in prs if pr.head_ref_oid == head for check in pr.checks}
        if any(run_id not in known for run_id in run_ids):
            return None
        with self._connect() as db:
            return {run_id: int((db.execute("SELECT value FROM settings WHERE name=?", (f"attempt:{head}:{run_id}",)).fetchone() or (1,))[0]) for run_id in run_ids}

    def advance_run_attempts(self, head: str, run_ids: tuple[int, ...], attempt: int) -> None:
        with self._connect() as db:
            for run_id in run_ids:
                db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (f"attempt:{head}:{run_id}", str(attempt)))

    def rerun_failed_checks(self, head: str, run_ids: tuple[int, ...], expected_attempts: dict[int, int] | None = None) -> tuple[int, ...] | None:
        attempts = self.workflow_run_attempts(head, run_ids)
        if attempts is None or (expected_attempts is not None and attempts != expected_attempts):
            return None
        self._call("rerun", head, list(run_ids))
        # Tests advance attempts explicitly after GitHub's asynchronous queue.
        return run_ids

    def set_failed_run_log(self, head: str, run_ids: tuple[int, ...], text: str) -> None:
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", ("log:" + json.dumps([head, run_ids]), text))

    def failed_run_log(self, head: str, run_ids: tuple[int, ...]) -> str:
        if self.workflow_run_attempts(head, run_ids) is None:
            raise RuntimeError("CI log request does not match the observed head/runs")
        with self._connect() as db:
            row = db.execute("SELECT value FROM settings WHERE name=?", ("log:" + json.dumps([head, run_ids]),)).fetchone()
        return row[0] if row is not None else ""
