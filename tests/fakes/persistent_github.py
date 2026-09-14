"""Small issue transport double whose remote state survives client restarts.

Each label addition/removal commits separately, matching GitHub's partial-write
boundary. Fault hooks run outside transactions; they can emulate a lost reply
or interleave a second client without making remote writes locally atomic.
"""

from collections.abc import Callable
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from issue_agent.domain.labels import Label
from issue_agent.domain.world import Check, Comment, IssueView, PRView, Review, WorldState


class PersistentGitHub:
    def __init__(self, path: Path, *, fault: Callable[[str], None] = lambda _: None):
        self.path = path
        self.fault = fault
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS issues (number INTEGER PRIMARY KEY, body TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS prs (number INTEGER PRIMARY KEY, body TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS calls (body TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS clock (revision INTEGER NOT NULL)")
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
        with self._connect() as db:
            db.execute("INSERT INTO prs VALUES (?, ?)", (pr.number, json.dumps(body)))

    def get_pr(self, number: int, *, mergeability: bool = True, checks: bool = True) -> PRView:
        self.fault("read")
        with self._connect() as db:
            body = json.loads(db.execute("SELECT body FROM prs WHERE number = ?", (number,)).fetchone()[0])
        body["labels"] = frozenset(Label(value) for value in body["labels"])
        body["changed_files"] = frozenset(body["changed_files"])
        for key, constructor in (("comments", Comment), ("reviews", Review), ("checks", Check)):
            body[key] = tuple(constructor(**entry) for entry in body[key])
        return PRView(**body)

    def build_world(self) -> WorldState:
        with self._connect() as db:
            numbers = [row[0] for row in db.execute("SELECT number FROM issues ORDER BY number")]
            prs = [row[0] for row in db.execute("SELECT number FROM prs ORDER BY number")]
        return WorldState(issues=tuple(self.get_issue(number) for number in numbers), prs=tuple(self.get_pr(number) for number in prs))

    def recently_merged_issues(self, author: str, limit: int = 30):
        return tuple(
            SimpleNamespace(issue=pr.closing_issue, pr=pr.number, title=pr.title, merged_at="2026-09-13")
            for pr in self.build_world().prs
            if pr.state == "MERGED" and pr.author == author and pr.closing_issue
        )[:limit]

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
            self._call("relabel_pr", number, remove, add)
            with self._connect() as db:
                body = json.loads(db.execute("SELECT body FROM prs WHERE number = ?", (number,)).fetchone()[0])
                body["labels"] = sorted((set(body["labels"]) | ({add} if add else set())) - ({remove} if remove else set()))
                db.execute("UPDATE prs SET body = ? WHERE number = ?", (json.dumps(body), number))
            return
        assert kind == "issue"
        self._call("relabel", number, remove, add)
        if add:
            self._update(number, lambda body: body.update(labels=sorted(set(body["labels"]) | {add})))
            self.fault("label_added")
        if remove:
            self._update(number, lambda body: body.update(labels=sorted(set(body["labels"]) - {remove})))
        self.fault("relabel_applied")

    def comment(self, kind: str, number: int, body: str) -> None:
        assert kind == "issue"
        self._call("comment", number, body)
        self._update(number, lambda issue: issue["comments"].append({
            "author": "fix-bot", "body": body, "created_at": issue["updated_at"],
        }))
        self.fault("comment_applied")

    def has_comment(self, kind: str, number: int, body: str) -> bool:
        assert kind == "issue"
        return any(comment.body == body for comment in self.get_issue(number).comments)

    def close_issue(self, number: int, reason: str, comment: str) -> None:
        self._call("close", number, reason)
        self._update(number, lambda body: body.update(state="closed", state_reason=reason))
        self.fault("close_applied")

    def issue_closure(self, number: int) -> tuple[bool, str]:
        issue = self.get_issue(number)
        return issue.state.lower() == "closed", issue.state_reason

    def pr_for_issue(self, number: int) -> None:
        return None
