"""GitHubGateway — the only module that runs `gh`.

Pure parsers (`parse_issue`, `parse_pr`, `parse_fix_*`) turn gh JSON into
domain value objects and are unit-tested against the documented shapes (the
fake `gh` drifting from the real one was the bash harness's recurring bug).
The gateway runs `gh` as one identity (ambient, static PAT or GitHub-App
installation token) and applies the side effects.
"""

from __future__ import annotations

import errno
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from urllib.parse import quote

from ..domain.checks import BaseHealth, Unavailable
from ..domain.architecture import ArchitectureObservation, NoPass, PassEnded, PassInFlight, architecture_pass
from ..domain.ci import parse_timestamp
from ..domain.identity import account_eq, configured_account
from .github_identity import parse_account
from ..domain.labels import Label
from ..domain.refs import harness_branch_issue
from ..domain.world import BlockerRef, Branch, Comment, IssueView, Pickup, PRView, Push, Review, WorldState
from .github_ci import (
    active_workflows, base_issues, base_reports, base_runs, observe_checks, required_policy, required_workflows,
)

# Hard ceiling on any single `gh` call: `gh pr view` with lazily computed
# fields (mergeable / mergeStateStatus) once hung ~14 min on a just-merged PR
# and froze the synchronous poll loop.
_GH_TIMEOUT_S = 30.0
_LOGGED_GH_ARGV_BYTES = 2 * 1024

# Prompt-input bounds (review M13/P1R-13): PR comments are writable by anyone
# with read access.
_REVIEW_COMMENT_BODY_BYTES = 2_000
_REVIEW_COMMENTS_TOTAL_BYTES = 20_000


def _cap_bytes(text: str, max_bytes: int) -> str:
    """The first `max_bytes` UTF-8 bytes (a comment states its point up front)."""
    encoded = text.encode("utf-8", errors="ignore")
    return text if len(encoded) <= max_bytes else encoded[:max_bytes].decode("utf-8", errors="ignore")


# `gh run list` returns at most this many runs; a branch with more is cancelled over later ticks' observations.
_RUN_SCAN_LIMIT = 100


class GhUnavailable(RuntimeError):
    """GitHub could not ANSWER (auth, rate limit, network, 5xx, local resource
    exhaustion), as opposed to answering "nothing" (V10). Mapping every
    failure to "" once made a rate-limited poll observe an empty world and act
    on it; raising makes the daemon skip the tick, and the next poll retries.
    To a write it is NoAnswer: the write may have landed. `kind` is one of
    `_HEADLINES`."""

    _HEADLINES = {
        "auth": "unavailable (authentication)",
        "rate-limit": "rate-limited",
        "network": "unreachable (network)",
        "server": "server error (5xx)",
        "local-resource": "unavailable (local resource exhaustion)",
    }
    _MAX_DETAIL_BYTES = 8 * 1024

    def __init__(self, kind: str, detail: str, argv: list[str] | None = None,
                 retry_after_s: float | None = None):
        self.kind = kind
        self.detail = _cap_bytes(_one_line(detail), self._MAX_DETAIL_BYTES)
        self.argv = tuple(argv or ())
        self.retry_after_s = retry_after_s
        super().__init__(f"GitHub {self.headline}: {self.detail}")

    @property
    def headline(self) -> str:
        return self._HEADLINES.get(self.kind, self.kind)


@dataclass(frozen=True)
class Accepted:
    """GitHub's 2xx answer to a write, the receipt (#433, #440): `id` is the
    created comment's, issue's number or review's, when the answer has one."""

    id: int | None = None


@dataclass(frozen=True)
class Refused:
    """GitHub answered (a 4xx, or GraphQL errors) and did not write: no retry changes it."""

    status: int
    message: str


_HTTP_STATUS = re.compile(r"HTTP/\S+ (\d{3})\b")


def parse_write_answer(out: str, receipt: str = "", argv: list[str] | None = None) -> Accepted | Refused:
    """`gh api --include` output as GitHub's answer, read from its status
    line and body, never gh's error text. No answer raises GhUnavailable: no
    status line, a 5xx or an untyped GraphQL error (the write may have
    landed); a 401 or a rate limit
    (429, a 403 with `retry-after`, or an error saying "rate limit", as GitHub
    documents), after which it did not land but the next tick may."""
    head, _, body = out.replace("\r\n", "\n").partition("\n\n")
    if (line := _HTTP_STATUS.match(head)) is None:
        raise GhUnavailable("network", "the write got no HTTP answer", argv)
    status = int(line[1])
    try:
        data = json.loads(body) if body.strip() else {}
    except ValueError:
        data = {}
    data = data if isinstance(data, dict) else {}
    errors = data.get("errors") if isinstance(data.get("errors"), list) else []
    first = errors[0] if errors and isinstance(errors[0], dict) else {}
    message = _one_line(str(data.get("message") or first.get("message") or f"HTTP {status}"), 300)
    if status == 401:
        raise GhUnavailable("auth", message, argv)
    retry_after = _retry_after(head)
    if (status >= 400 or errors) and (status == 429 or any(p in message.lower().replace("-", " ") for p in ("rate limit", "ratelimit remaining: 0", "abuse detection"))
                                      or status == 403 and retry_after is not None):
        raise GhUnavailable("rate-limit", message, argv, retry_after_s=retry_after)
    # GitHub types a GraphQL refusal (NOT_FOUND, FORBIDDEN, ...); an untyped
    # error is its own failure ("Something went wrong"), which may have landed.
    if not 200 <= status < 500 or status < 300 and not all(isinstance(e, dict) and e.get("type") for e in errors):
        raise GhUnavailable("server", message, argv)
    if status >= 300 or errors:
        return Refused(status, message)
    raw = data.get(receipt)
    return Accepted(raw if type(raw) is int and raw > 0 else None)


def _one_line(text: str, limit: int = 200) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def classify_gh_failure(detail: str) -> str | None:
    """Fallback for gh commands without HTTP headers; an HTTP answer uses
    the same classifier as typed writes, ahead of diagnostic text."""
    if status := re.search(r"\bHTTP(?:/\S+)? (\d{3})\b", detail):
        try:
            headers = f"Retry-After: {delay}\n" if (delay := _retry_after(detail)) is not None else ""
            parse_write_answer(f"HTTP/2.0 {status[1]}\n{headers}\n" + json.dumps({"message": detail}))
        except GhUnavailable as exc:
            return exc.kind
        return None
    text = (detail or "").lower()
    if any(p in text for p in ("bad credentials", "requires authentication")):
        return "auth"
    if any(p in text for p in ("rate limit", "rate-limit", "ratelimit", "secondary rate", "abuse detection",
                               "too many requests", "retry-after")):
        return "rate-limit"
    if any(p in text for p in ("pthread_create failed", "resource temporarily unavailable", "newosproc",
                               "failed to create new os thread", "cannot allocate memory", "out of memory")):
        return "local-resource"
    if any(p in text for p in ("bad gateway", "service unavailable", "gateway timeout", "gateway time-out",
                               "internal server error")):
        return "server"
    if any(p in text for p in ("error connecting", "could not resolve host", "no such host", "connection refused",
                               "connection reset", "network is unreachable", "dial tcp", "tls handshake",
                               "proxyconnect", "timeout", "timed out", "temporary failure in name resolution")):
        return "network"
    return None


def _printable_gh_argv(argv: list[str]) -> str:
    """Bound command diagnostics without logging untrusted publication bodies."""
    rendered: list[str] = []
    redact_next = field_value = False
    for arg in argv:
        if redact_next:
            rendered.append(f"<body redacted: {len(arg.encode())} bytes>")
            redact_next = False
            continue
        if field_value and arg.startswith("body="):
            rendered.append(f"body=<redacted: {len(arg.removeprefix('body=').encode())} bytes>")
            field_value = False
            continue
        rendered.append(arg)
        redact_next = arg in {"--body", "-b"}
        field_value = arg in {"-f", "-F", "--field", "--raw-field"}
    return _cap_bytes(" ".join(rendered), _LOGGED_GH_ARGV_BYTES)


# ── pure parsers ──────────────────────────────────────────────────────────────
def _labels(raw: list | None) -> frozenset[Label]:
    """Known label names from [{"name": …}] or bare strings; unknown repo
    labels are dropped, never a parse failure."""
    out = set()
    for item in raw or []:
        name = item.get("name") if isinstance(item, dict) else item
        try:
            out.add(Label(name))
        except ValueError:
            continue
    return frozenset(out)


def _comments(raw: list | None) -> tuple[Comment, ...]:
    return tuple(
        Comment(author=_login(c.get("author")),
                body=c.get("body", "") or "", created_at=c.get("createdAt", "") or "",
                url=c.get("url", "") if isinstance(c.get("url"), str) else "")
        for c in raw or [])


def _reviews(raw: list | None) -> tuple[Review, ...]:
    """GraphQL (`author`, `commit.oid`) or REST (`user`, `commit_id`) shape.
    An unknown commit is "": the ADDRESS_REVIEW supersession gate stays open."""
    out = []
    for r in raw or []:
        author = _login(r.get("author")) or _login(r.get("user"))
        commit = r.get("commit")
        commit_id = (commit.get("oid", "") if isinstance(commit, dict) else "") or r.get("commit_id", "") or ""
        # REST `id` is the review id; a GraphQL `id` is a node id, so only `databaseId` there.
        review_id = r.get("databaseId", r.get("id"))
        out.append(Review(state=r.get("state", "") or "", author=author,
                          submitted_at=r.get("submittedAt") or r.get("submitted_at") or "",
                          body=r.get("body", "") or "", commit_id=commit_id,
                          id=review_id if type(review_id) is int and review_id > 0 else None))
    return tuple(out)


def parse_issue(d: dict) -> IssueView:
    return IssueView(
        number=int(d["number"]), title=d.get("title", "") or "", body=d.get("body", "") or "",
        labels=_labels(d.get("labels")), comments=_comments(d.get("comments")),
        updated_at=d.get("updatedAt", "") or "", author=parse_account(d.get("author")),
        state=(d.get("state", "open") or "open").lower(),
    )


def parse_pr(d: dict) -> PRView:
    return PRView(
        number=int(d["number"]), head_ref_name=d.get("headRefName", "") or "",
        head_ref_oid=d.get("headRefOid", "") or "", base_ref_name=d.get("baseRefName", "") or "",
        base_ref_oid=d.get("baseRefOid", "") or "", title=d.get("title", "") or "", body=d.get("body", "") or "",
        labels=_labels(d.get("labels")), comments=_comments(d.get("comments")), reviews=_reviews(d.get("reviews")),
        state=(d.get("state", "OPEN") or "OPEN").upper(), changed_files=_changed_files(d.get("files")),
        mergeable=(d.get("mergeable", "") or "").upper(), merge_state=(d.get("mergeStateStatus", "") or "").upper(),
        author=_login(d.get("author")),
    )


def _changed_files(files: object) -> frozenset[str]:
    if not isinstance(files, list):
        return frozenset()
    return frozenset(str(f["path"]) for f in files if isinstance(f, dict) and f.get("path"))


def _marked_cap(text: str, max_bytes: int) -> str:
    capped = _cap_bytes(text, max_bytes)
    return capped if capped == text else f"{capped} […comment truncated]"


def format_pr_discussion(
    comments: tuple[Comment, ...], *, body_bytes: int = _REVIEW_COMMENT_BODY_BYTES,
    total_bytes: int = _REVIEW_COMMENTS_TOTAL_BYTES,
) -> str:
    """The PR conversation as the reviewer's DATA block, NEWEST FIRST: the byte
    ceiling cuts the tail, and whether the author already answered a demand is
    at the recent end (tauri #613/#614: the reviewer re-demanded evidence the
    thread had answered). Bot comments are kept (the fixer's blocked-reason
    comment is one), and both cuts are marked so a bounded view never reads as
    the whole conversation."""
    rendered = [f"@{c.author or '(unknown)'}: {_marked_cap(c.body.strip(), body_bytes)}"
                for c in reversed(comments) if c.body.strip()]
    if not rendered:
        return ""
    joined = "\n\n---\n\n".join(rendered)
    capped = _cap_bytes(joined, total_bytes)
    return capped if capped == joined else capped + "\n\n---\n\n[older comments truncated]"


# #425: one request carrying every connection grew with repository activity
# past GitHub's GraphQL server timeout (502/504; the tick was skipped). Each
# connection is its own request now, paged so that one page stays near a
# second however busy the repository gets. Measured on xnmp/tauri-explorer
# (2026-10-15): 100 issues with their nested reads 4.7s, 25 1.7s; 50 PRs
# 5.3s, 10 1.1s; 100 refs 2.2-4.1s (per-ref PR association and compare), 50
# 1.7s. The item ceilings are those of the old 10 pages of 100.
_FIX_PAGE_SIZE = {"issues": 25, "pullRequests": 10, "refs": 50}
FIX_MAX_ITEMS = 1000
_FIX_MAX_PAGES = {name: -(-FIX_MAX_ITEMS // size) for name, size in _FIX_PAGE_SIZE.items()}
_FIX_NESTED_MAX = {"comments": 1000, "reviews": 500, "files": 3000, "timelineItems": 500, "contexts": 1000}
_HARNESS_BRANCH_PREFIX = "fix/"
_COMMENTS = "comments(last:100{before}){{totalCount pageInfo{{hasPreviousPage startCursor}} nodes{{author{{__typename login}} body createdAt url}}}}"
_LABELS = "labels(first:100){totalCount nodes{name}}"
# Cross-references, paged in full like any nested connection: a merged harness
# PR past one page must read as "unknown" (the issue is skipped), never as
# "not merged" (which would re-RESOLVE a done issue). No totalCount: whether
# it honours `itemTypes` is undocumented, so completeness rests on paging.
_TIMELINE = ("timelineItems(itemTypes:[CROSS_REFERENCED_EVENT],last:100{before}){{pageInfo{{hasPreviousPage startCursor}} "
             "nodes{{... on CrossReferencedEvent{{source{{... on PullRequest{{number state mergedAt headRefName "
             "repository{{nameWithOwner}}}}}}}}}}}}")
# The newest label applications: when `needs-human` was last applied.
_LABELED = ("labeled:timelineItems(itemTypes:[LABELED_EVENT],last:100){pageInfo{hasPreviousPage} "
            "nodes{... on LabeledEvent{createdAt label{name}}}}")
# GitHub's native "blocked by" issue dependencies, in the same query (no
# per-issue round-trip). One page: GitHub caps a relationship well below 100.
_BLOCKED_BY = "blockedBy(first:100){totalCount nodes{number state repository{nameWithOwner}}}"
_FIX_ISSUE = (
    "id number title state author{__typename login} " + _LABELS + " " + _LABELED + " "
    + _COMMENTS.format(before="") + " " + _TIMELINE.format(before="") + " " + _BLOCKED_BY
)
_REVIEWS = ("reviews(last:100{before}){{totalCount pageInfo{{hasPreviousPage startCursor}} "
            "nodes{{databaseId author{{__typename login}} state submittedAt body commit{{oid}}}}}}")
_FILES = "files(first:100{after}){{totalCount pageInfo{{hasNextPage endCursor}} nodes{{path}}}}"
_FIX_PR = (
    "id number state headRefName headRefOid baseRefName baseRefOid title author{__typename login} lastEditedAt "
    "mergeable mergeStateStatus " + _LABELS + " " + _LABELED + " " + _COMMENTS.format(before="") + " "
    + _REVIEWS.format(before="") + " " + _FILES.format(after="")
    + " commits(last:1){nodes{commit{oid committedDate author{user{__typename login}}}}}"
)
# Per branch: its newest PR (any state), its head commit's date, and how many
# commits it has that the base lacks (`compare` reads the ref as the base, so
# that count is `behindBy`). The prefix stays `refs/heads/`: under a
# `refs/heads/fix/` prefix GitHub fails `compare` (500 or NOT_FOUND for every
# base spelling), which failed every fix-role read (2026-10-13, all three
# deployments). `query` (case-insensitive, matches anywhere) narrows
# server-side; the parser keeps only names that start with the harness prefix.
_FIX_REFS = ("refs(refPrefix:\"refs/heads/\",query:\"" + _HARNESS_BRANCH_PREFIX + "\","
             f"first:{_FIX_PAGE_SIZE['refs']}" "{after})"
             "{{pageInfo{{hasNextPage endCursor}} "
             "nodes{{name associatedPullRequests(last:1,orderBy:{{field:CREATED_AT,direction:ASC}})"
             "{{totalCount nodes{{state closedAt}}}} target{{... on Commit{{committedDate}}}} "
             "compare(headRef:$base){{behindBy}}}}}}")
# The base tip's checks, a request of their own (#313, #425): a PR check red here too is
# the base's, not the PR's. GitHub's rollup lists a rerun job's latest attempt. An
# Actions check suite is its workflow run: its conclusion is the run's (#442).
_BASE_CONTEXTS = ("contexts(first:100{after}){{totalCount pageInfo{{hasNextPage endCursor}} nodes{{__typename "
                  "... on CheckRun{{id databaseId name status conclusion startedAt detailsUrl "
                  "checkSuite{{conclusion app{{databaseId}} workflowRun{{event workflow{{name}}}}}}}} "
                  "... on StatusContext{{id createdAt context state targetUrl}}}}}}")
_BASE_TIP = ("baseTip:ref(qualifiedName:$base){name target{... on Commit{oid statusCheckRollup{id "
             + _BASE_CONTEXTS.format(after="") + "}}}}")

_BASE_ISSUES = ("query($q:String!){search(query:$q,type:ISSUE,first:100){issueCount "
                "nodes{... on Issue{number title state closedAt author{__typename login} "
                "timelineItems(itemTypes:[ISSUE_COMMENT,CLOSED_EVENT,REOPENED_EVENT],last:2){nodes{__typename "
                "... on IssueComment{author{__typename login} body} "
                "... on ClosedEvent{closer{... on PullRequest{mergeCommit{oid}} ... on Commit{oid}}}}}}}}}")

_PASS_ISSUES = ('query($q:String!){search(query:$q,type:ISSUE,first:20){issueCount nodes{... on Issue{'
                'number title state closedAt author{__typename login} ' + _LABELS + ' '
                + _TIMELINE.format(before="") + '}}}}')
_MERGED_COUNT = 'query($q:String!){search(query:$q,type:ISSUE,first:1){issueCount}}'


def _escaped(fields: str) -> str:
    return fields.replace("{", "{{").replace("}", "}}")


def _issues_connection(labels: str, fields: str) -> str:
    return ("issues(states:OPEN," + labels + f"first:{_FIX_PAGE_SIZE['issues']}" "{after},"
            "orderBy:{{field:CREATED_AT,direction:ASC}}){{pageInfo{{hasNextPage endCursor}} nodes{{"
            + _escaped(fields) + "}}}}")


_FIX_CONNECTIONS = {
    "issues": _issues_connection("labels:[\"ai-ready\",\"needs-human\"],", _FIX_ISSUE),
    "pullRequests": (f"pullRequests(states:OPEN,first:{_FIX_PAGE_SIZE['pullRequests']}"
                     "{after},orderBy:{{field:CREATED_AT,direction:ASC}})"
                     "{{pageInfo{{hasNextPage endCursor}} nodes{{" + _escaped(_FIX_PR) + "}}}}"),
    "refs": _FIX_REFS,
}


# ISSUE_PICKUP=open: every open issue (derive leaves out the harness's own gap reports).
_FIX_OPEN_ISSUES = _issues_connection("", _FIX_ISSUE)


class FixObservationIncomplete(ValueError):
    """A required connection could not be read in full within its bound."""


def _connection(parent: object, name: str, more: str = "") -> tuple[list, dict]:
    """A connection's nodes and pageInfo. A paged read names its `more` flag:
    without a boolean one, whether more pages follow is unknown, never "no"."""
    if not isinstance(parent, dict) or not isinstance(parent.get(name), dict):
        raise FixObservationIncomplete(f"{name} is absent")
    conn = parent[name]
    nodes, info = conn.get("nodes"), conn.get("pageInfo")
    if not isinstance(nodes, list) or (info is not None and not isinstance(info, dict)):
        raise FixObservationIncomplete(f"{name} is malformed")
    if more and (info is None or type(info.get(more)) is not bool):
        raise FixObservationIncomplete(f"{name} page info is malformed")
    return nodes, info or {}


def _login(raw: object) -> str:
    account = parse_account(raw)
    return str(account) if account is not None else ""


def _complete_labels(node: dict) -> frozenset[Label]:
    nodes, _ = _connection(node, "labels")
    if node["labels"].get("totalCount") != len(nodes):
        raise FixObservationIncomplete("labels exceed one page")
    return _labels(nodes)


def _last_labeled(node: dict, label: Label) -> tuple[str, bool]:
    """When `label` was last applied among the newest 100 label events ("" if
    not among them), and whether older events were cut off."""
    nodes, info = _connection(node, "labeled")
    times = [n["createdAt"] for n in nodes if isinstance(n, dict) and isinstance(n.get("createdAt"), str)
             and isinstance(n.get("label"), dict) and n["label"].get("name") == label.value]
    return (times[-1] if times else ""), bool(info.get("hasPreviousPage"))


def _needs_human_labeled_at(node: dict, labels: frozenset[Label]) -> str:
    """When `needs-human` was last applied, for a labelled item ("" otherwise).
    Not found among the newest 100 label events: unknown, so the item is
    skipped rather than released on an older reply."""
    if Label.NEEDS_HUMAN not in labels:
        return ""
    at, cut = _last_labeled(node, Label.NEEDS_HUMAN)
    if not at and cut:
        raise FixObservationIncomplete("needs-human label event is past the newest 100")
    return at


def _ai_ready_labeled_at(node: dict, labels: frozenset[Label]) -> str:
    """When `ai-ready` was last applied, for a ready issue; "" when unknown.
    Display only (the board dates a queued wait from it), so it never fails
    the observation."""
    if Label.AI_READY not in labels:
        return ""
    try:
        return _last_labeled(node, Label.AI_READY)[0]
    except FixObservationIncomplete:
        return ""


def parse_blockers(raw: object, repository: str) -> tuple[tuple[BlockerRef, ...], str]:
    """An issue's `blockedBy` connection → its still-open blockers (sorted) and
    what could not be read ("" when all of it could). A closed blocker is
    resolved whatever its reason. Unreadable data never blocks: an absent or
    malformed connection is no blockers, a malformed node is dropped, and a
    blocker GitHub counts but does not list (past the page, or a repository
    this token cannot see) is unseen."""
    if not isinstance(raw, dict) or not isinstance(raw.get("nodes"), list):
        return (), "blockedBy is absent or malformed"
    nodes = raw["nodes"]
    found: set[BlockerRef] = set()
    dropped = 0
    for node in nodes:
        try:
            ref = _open_blocker(node, repository)
        except ValueError:
            dropped += 1
            continue
        if ref is not None:
            found.add(ref)
    problems = [f"{dropped} malformed blocker(s) dropped"] if dropped else []
    total = raw.get("totalCount")
    if type(total) is int and total > len(nodes):
        problems.append(f"{total - len(nodes)} blocker(s) unseen")
    return tuple(sorted(found, key=lambda b: b.sort_key)), "; ".join(problems)


def _blockers_only(errors: object) -> bool:
    """No errors, or only errors inside a `blockedBy` connection (e.g. a blocker
    in a repository this token cannot see): GitHub nulls that part, which
    `parse_blockers` reads as unreadable. A link must never skip the tick."""
    if not errors:
        return True
    return isinstance(errors, list) and all(
        isinstance(e, dict) and isinstance(e.get("path"), list) and "blockedBy" in e["path"] for e in errors)


def _open_blocker(node: object, repository: str) -> BlockerRef | None:
    """One blocker node → its ref while open, None once closed; ValueError if malformed."""
    if not isinstance(node, dict) or node.get("state") not in ("OPEN", "CLOSED"):
        raise ValueError("blocker state is malformed")
    repo = node.get("repository")
    name = repo.get("nameWithOwner") if isinstance(repo, dict) else None
    number = node.get("number")
    if not isinstance(name, str) or not name or type(number) is not int:
        raise ValueError("blocker identity is malformed")
    ref = BlockerRef(number, "" if name.casefold() == repository.casefold() else name)
    return ref if node["state"] == "OPEN" else None


@dataclass(frozen=True)
class BlockerLinkFacts:
    """A fresh read of both ends of a proposed "blocked by" link."""

    issue_id: str
    blocker_id: str
    blocker_open: bool
    linked: bool  # the issue is already blocked by the blocker
    blocker_blocked_by: tuple[int, ...]  # this repository's open issues blocking the blocker


def parse_blocker_link(raw: object, repository: str, blocker: int) -> BlockerLinkFacts:
    """Both issues of a link read in one query. Unreadable identities, or a
    blocker whose own blockers cannot all be read (the cycle check would be a
    guess), raise; act refuses the link then. The issue's own list only says
    whether the link already exists: a gap there can at worst make
    addBlockedBy answer an error, which act also refuses."""
    subject = raw.get("subject") if isinstance(raw, dict) else None
    target = raw.get("blocker") if isinstance(raw, dict) else None
    if (not isinstance(subject, dict) or not isinstance(target, dict) or target.get("state") not in ("OPEN", "CLOSED")
            or not all(isinstance(n.get("id"), str) and n["id"] for n in (subject, target))):
        raise FixObservationIncomplete("a blocker link's issues are unreadable")
    mine, _ = parse_blockers(subject.get("blockedBy"), repository)
    theirs, problem = parse_blockers(target.get("blockedBy"), repository)
    if problem:
        raise FixObservationIncomplete(f"#{blocker}'s own blockers are unreadable: {problem}")
    return BlockerLinkFacts(subject["id"], target["id"], target["state"] == "OPEN", BlockerRef(blocker) in mine,
                            tuple(b.number for b in theirs if not b.repository))


def parse_fix_issue(node: object, comments: list, blocked_by: tuple[BlockerRef, ...] = ()) -> IssueView:
    """One issue node (with its fully paged comments and parsed blockers) → IssueView."""
    if not isinstance(node, dict) or type(node.get("number")) is not int or node["number"] <= 0:
        raise FixObservationIncomplete("issue identity is malformed")
    state = node.get("state")
    if state not in ("OPEN", "CLOSED"):
        raise FixObservationIncomplete("issue state is malformed")
    labels = _complete_labels(node)
    return IssueView(
        number=node["number"], title=node.get("title") or "", labels=labels,
        comments=_comments(comments), author=parse_account(node.get("author")), state=state.lower(),
        needs_human_labeled_at=_needs_human_labeled_at(node, labels), blocked_by=blocked_by,
        ai_ready_labeled_at=_ai_ready_labeled_at(node, labels),
    )


def parse_merged_harness_prs(items: list, repository: str) -> tuple[PRView, ...]:
    """Merged same-repo harness PRs among an issue's (fully paged)
    cross-references. A merged harness PR means the issue is done even while
    it stays open (base ≠ default branch never auto-closes it)."""
    out = []
    for item in items:
        source = item.get("source") if isinstance(item, dict) else None
        if source == {}:  # A non-PR source has no fields in the PullRequest fragment.
            continue
        if not isinstance(source, dict):
            raise FixObservationIncomplete("cross-reference source is unreadable")
        repo = (source.get("repository") or {}).get("nameWithOwner") if isinstance(source.get("repository"), dict) else ""
        head = source.get("headRefName")
        if (source.get("state") not in ("OPEN", "CLOSED", "MERGED") or not isinstance(repo, str) or not repo
                or type(source.get("number")) is not int or not 0 < source["number"] <= 2**63 - 1
                or not isinstance(head, str) or not head):
            raise FixObservationIncomplete("cross-reference PR facts are malformed")
        if source["state"] != "MERGED" or repo.casefold() != repository.casefold() or not head.startswith(_HARNESS_BRANCH_PREFIX):
            continue
        merged_at = source.get("mergedAt")
        out.append(PRView(number=source["number"], head_ref_name=head, state="MERGED", content_known=False,
                          merged_at=merged_at if isinstance(merged_at, str) else ""))
    return tuple(out)


def parse_fix_pr(node: object, comments: list, reviews: list, files: list) -> PRView:
    """One open PR node (with fully paged nested connections) → PRView. CI is
    attached separately by the revision-bound required-check observer."""
    if not isinstance(node, dict) or type(node.get("number")) is not int or node["number"] <= 0:
        raise FixObservationIncomplete("PR identity is malformed")
    head = node.get("headRefOid")
    if not isinstance(head, str) or not isinstance(node.get("headRefName"), str):
        raise FixObservationIncomplete("PR head is malformed")
    commits, _ = _connection(node, "commits")
    pushes: tuple[Push, ...] = ()
    if commits:
        commit = commits[-1].get("commit") if isinstance(commits[-1], dict) else None
        if not isinstance(commit, dict) or commit.get("oid") != head:
            raise FixObservationIncomplete("PR head commit disagrees with its head")
        author = commit.get("author") if isinstance(commit.get("author"), dict) else {}
        # CommitAuthor.user is concrete User, even for an app's suffixed login.
        user = author.get("user")
        account = configured_account(user.get("login")) if isinstance(user, dict) else None
        pushes = (Push(str(account) if account is not None else "", commit.get("committedDate") or ""),)
    return PRView(
        number=node["number"], head_ref_name=node["headRefName"], head_ref_oid=head,
        base_ref_name=node.get("baseRefName") or "", base_ref_oid=node.get("baseRefOid") or "",
        title=node.get("title") or "", labels=(labels := _complete_labels(node)),
        comments=_comments(comments), reviews=_reviews(reviews),
        needs_human_labeled_at=_needs_human_labeled_at(node, labels),
        state=str(node.get("state") or ""), changed_files=_changed_files([{"path": f.get("path")} for f in files
                                                                         if isinstance(f, dict)]),
        mergeable=str(node.get("mergeable") or ""), merge_state=str(node.get("mergeStateStatus") or ""),
        author=_login(node.get("author")), pushes=pushes, body_edited_at=_edited_at(node),
    )


def _edited_at(node: dict) -> str:
    """GraphQL `lastEditedAt`: null (never edited) or malformed reads as "",
    which leaves review owed on the head alone (#472)."""
    edited = node.get("lastEditedAt")
    return edited if isinstance(edited, str) else ""


def parse_fix_refs(nodes: list) -> tuple[tuple[Branch, ...], dict[int, str]]:
    """Harness branches owed a PR, and the issues whose branch could not be
    judged (with why). A branch is owed when it is ahead of base and either
    never had a PR or was pushed after its newest PR was closed unmerged (a
    human closed it and the issue went back to RESOLVE). A branch with nothing
    ahead of base is owed nothing: opening its PR could only fail, every tick.
    An unjudgeable branch (a hidden PR, a comparison GitHub cannot make, e.g.
    a missing base) makes only its issue unknown, never the whole read."""
    owed: list[Branch] = []
    unknown: dict[int, str] = {}
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("name"), str):
            raise FixObservationIncomplete("branch ref is malformed")
        branch = node["name"]
        if not branch.startswith(_HARNESS_BRANCH_PREFIX):
            continue  # `query` matches anywhere in the name (e.g. `hotfix/`)
        try:
            if _owed(node):
                target = node.get("target") if isinstance(node.get("target"), dict) else {}
                committed = target.get("committedDate")
                owed.append(Branch(branch, committed if isinstance(committed, str) else ""))
        except FixObservationIncomplete as exc:
            if (issue := harness_branch_issue(branch)) is not None:
                unknown[issue] = str(exc)
    return tuple(owed), unknown


def _owed(node: dict) -> bool:
    prs = node.get("associatedPullRequests")
    if not isinstance(prs, dict) or type(prs.get("totalCount")) is not int or not isinstance(prs.get("nodes"), list):
        raise FixObservationIncomplete("branch PR association is unreadable")
    compare = node.get("compare")
    if not isinstance(compare, dict) or type(compare.get("behindBy")) is not int:
        raise FixObservationIncomplete("branch cannot be compared with the base")
    if compare["behindBy"] <= 0:
        return False
    if not prs["totalCount"]:
        return True
    newest = prs["nodes"][-1] if prs["nodes"] else None
    if not isinstance(newest, dict) or newest.get("state") not in ("OPEN", "CLOSED", "MERGED"):
        raise FixObservationIncomplete("branch PR state is unreadable")
    target = node.get("target") if isinstance(node.get("target"), dict) else {}
    closed_at = parse_timestamp(newest.get("closedAt") or "")
    pushed_at = parse_timestamp(target.get("committedDate") or "")
    return newest["state"] == "CLOSED" and closed_at is not None and pushed_at is not None and pushed_at > closed_at


# ── write command builders ──────────────────────────────────────────────────
def build_submit_review_args(repo: str, pr: int, event: str, body: str, head: str) -> list[str]:
    return [f"repos/{repo}/pulls/{pr}/reviews", "--method", "POST",
            "-f", f"event={event}", "-f", f"body={body}", "-f", f"commit_id={head}"]


def build_relabel_args(
    repo: str, kind: str, number: int, remove: str | None, add: str | None,
) -> list[list[str]] | None:
    """REST label commands, never `gh <kind> edit`: its GraphQL metadata query
    demands `read:org`, which a repo-scoped bot PAT lacks (review daemon wedged
    on PR #171, 2026-08-02). The issues endpoint covers PRs too.

    ADD BEFORE REMOVE: if the second call fails permanently, "both labels"
    stays visible to the world build, while "no label" would be invisible
    forever. A DELETE of an absent label 404s; membership verification treats
    the pre-read avoids that write."""
    if not remove and not add:
        return None
    commands: list[list[str]] = []
    if add:
        commands.append(["--method", "POST", f"repos/{repo}/issues/{number}/labels", "-f", f"labels[]={add}"])
    if remove:
        commands.append(["--method", "DELETE", f"repos/{repo}/issues/{number}/labels/{quote(remove, safe='')}"])
    return commands


@dataclass(frozen=True)
class Identity:
    """How `gh` authenticates: app-token mint, else static token, else ambient
    `gh auth`, the operator's own login (operator CLI writes only; a daemon
    never runs ambient: `app/identity.py`)."""

    static_token: str | None = None
    token_provider: Callable[[], str] | None = None  # e.g. AppTokenMinter.get

    def env_token(self) -> str | None:
        return self.token_provider() if self.token_provider is not None else self.static_token


class GitHubGateway:
    """Runs `gh` for one repo as one identity. `runner` is injected so tests
    record argv and return canned JSON."""

    def __init__(self, repo: str, identity: Identity | None = None,
                 runner: Callable[[list[str], str | None], str] | None = None,
                 progress: Callable[[], None] | None = None):
        self.repo = repo
        self.identity = identity or Identity()
        self._run = runner or _subprocess_runner
        self._progress = progress

    def for_repo(self, repo: str) -> "GitHubGateway":
        """The same identity on another repository: the fix role files harness
        gaps into the harness's own repository with its credential (ADR 0001)."""
        return GitHubGateway(repo, self.identity, self._run, self._progress)

    def filing_facts(self) -> tuple[str, bool]:
        """The repository's visibility ("public", "internal" or "private") and
        whether this credential can file issues there (push permission,
        issues enabled, not archived). An unreadable answer raises."""
        data = self._gh_json(["api", f"repos/{self.repo}"])
        if not isinstance(data, dict) or data.get("visibility") not in ("public", "internal", "private"):
            raise FixObservationIncomplete(f"{self.repo}: visibility unreadable")
        permissions = data.get("permissions")
        push = isinstance(permissions, dict) and permissions.get("push") is True
        return str(data["visibility"]), push and data.get("has_issues") is True and data.get("archived") is False

    def _gh(self, args: list[str]) -> str:
        try:
            return self._run(["gh", *args], self.identity.env_token())
        finally:
            if self._progress is not None:
                self._progress()

    def _write(self, args: list[str], receipt: str = "") -> Accepted | Refused:
        """One `gh api` write (`args` after `api`); GitHub's answer."""
        argv = ["api", "--include", *args]
        return parse_write_answer(self._gh(argv), receipt, argv)

    def _gh_json(self, args: list[str]) -> object:
        out = self._gh(args)
        return json.loads(out) if out.strip() else None

    def _view(self, kind: str, number: int, fields: str) -> object:
        requested = fields.split(",")
        if not {"author", "comments", "reviews"} & set(requested):
            return self._gh_json([kind, "view", str(number), "--repo", self.repo, "--json", fields])
        # gh view drops author type from reviews; bare Bot and User logins collide.
        connections = {"comments": _COMMENTS, "reviews": _REVIEWS, "files": _FILES}
        selections = {name: template.format(before="", after="") for name, template in connections.items()}
        selections |= {"author": "author{__typename login}", "labels": _LABELS}
        selection = "id " + " ".join(selections.get(f, f) for f in requested)
        field, on = ("issue", "Issue") if kind == "issue" else ("pullRequest", "PullRequest")
        node = self._repository_page(f"{field}(number:{int(number)}){{{selection}}}").get(field)
        if not isinstance(node, dict):
            raise FixObservationIncomplete(f"{kind} #{number} was not answered")
        result = dict(node)
        for name, template in connections.items():
            if name in requested:
                rows = self._nested(node, name, template, on=on, backward=name != "files")
                if rows is None:
                    raise FixObservationIncomplete(f"{kind} #{number} {name} exceeded its bound")
                result[name] = rows
        if "labels" in requested:
            _complete_labels(node)
            result["labels"] = node["labels"]["nodes"]
        return result

    def get_issue(self, number: int) -> IssueView:
        return parse_issue(self._view("issue", number, "number,title,body,labels,comments,updatedAt,author,state"))  # type: ignore[arg-type]

    def pr_body(self, number: int) -> tuple[str, str]:
        """The PR body and when it was last edited ("" never), in one read: a
        review is pinned to the description it judged (#472). `gh pr view`
        cannot read the edit time."""
        data = self._repository_page(f"pullRequest(number:{int(number)}){{body lastEditedAt}}")
        node = data.get("pullRequest")
        if not isinstance(node, dict) or not isinstance(node.get("body"), str):
            raise FixObservationIncomplete(f"pull request {number} body is unreadable")
        return node["body"], _edited_at(node)

    def get_pr(self, number: int, *, mergeability: bool = True, checks: bool = True) -> PRView:
        """One PR. `mergeability=False` leaves out the lazily computed fields
        that can hang on a just-merged PR. `checks=False` marks CI Unavailable
        rather than empty: a PR re-opened between list and view must not read
        as Clear (#48 review F3)."""
        fields = ("number,title,body,headRefName,headRefOid,baseRefName,baseRefOid,labels,comments,reviews,state,"
                  "files,author" + (",mergeable,mergeStateStatus" if mergeability else ""))
        pr = parse_pr(self._view("pr", number, fields))  # type: ignore[arg-type]
        if not checks:
            return replace(pr, ci=Unavailable("checks not requested"))
        return replace(pr, ci=observe_checks(self.repo, pr, self._gh_json))

    @staticmethod
    def _skip_item(kind: str, number: int, exc: BaseException) -> None:
        sys.stderr.write(f"skip {kind} #{number} this poll ({type(exc).__name__}: {str(exc)[:120]}) "
                         "— re-derived next tick\n")

    def _graphql(self, query: str, **variables: str) -> dict:
        """One query; `variables` must be exactly the ones it declares (GitHub
        rejects a declared-but-unused variable)."""
        args = ["api", "graphql", "-f", f"query={query}"]
        for key, value in variables.items():
            args += ["-f", f"{key}={value}"]
        raw = self._gh_json(args)
        if not isinstance(raw, dict) or not isinstance(raw.get("data"), dict) or not _blockers_only(raw.get("errors")):
            raise FixObservationIncomplete("GraphQL answered with errors or no data")
        return raw["data"]

    def _repository_page(self, fields: str, **variables: str) -> dict:
        owner, name = self.repo.split("/", 1)
        declared = "".join(f",${key}:String!" for key in variables)
        data = self._graphql(
            f"query($owner:String!,$repo:String!{declared}){{repository(owner:$owner,name:$repo){{{fields}}}}}",
            owner=owner, repo=name, **variables,
        )
        if not isinstance(data.get("repository"), dict):
            raise FixObservationIncomplete("repository is unreadable")
        return data["repository"]

    def _nested(self, node: dict, name: str, template: str, *, on: str, backward: bool) -> list | None:
        """A nested connection of an `on`-typed node read in full, or None past its bound."""
        more, cursor_key = ("hasPreviousPage", "startCursor") if backward else ("hasNextPage", "endCursor")
        nodes, info = _connection(node, name, more)
        nodes = list(nodes)
        total = node[name].get("totalCount")
        while info[more]:
            cursor = info.get(cursor_key)
            if len(nodes) >= _FIX_NESTED_MAX[name] or not isinstance(cursor, str) or not cursor:
                return None
            field = template.format(**{"before" if backward else "after": f",{'before' if backward else 'after'}:$cursor"})
            data = self._graphql(
                f"query($id:ID!,$cursor:String!){{node(id:$id){{... on {on}{{{field}}}}}}}",
                id=str(node.get("id") or ""), cursor=cursor,
            )
            page, info = _connection(data.get("node"), name, more)
            nodes = [*page, *nodes] if backward else [*nodes, *page]
        if type(total) is int and total != len(nodes) or len(nodes) > _FIX_NESTED_MAX[name]:
            return None
        return nodes

    def build_world(self, base: str, *, filed_by: str = "", pickup: Pickup = Pickup.READY) -> WorldState:
        """The fix role's observation: open `ai-ready`/`needs-human` issues
        (with `pickup` OPEN, every open issue and its body),
        open harness PRs (`fix/<issue>-`) with head, mergeability, required
        checks, reviews, comments, every changed file and the head push, and
        harness branches owed a PR against `base`; with `filed_by` (the fixer's
        login), also the base's runs and base-CI issues. `GhUnavailable` and incomplete
        top-level reads propagate: the daemon skips the tick."""
        # The domain reads no clock: one stamp per build, so every gate in a tick judges one instant.
        now = datetime.now(timezone.utc).isoformat()
        templates = {**_FIX_CONNECTIONS, **({"issues": _FIX_OPEN_ISSUES} if pickup is Pickup.OPEN else {})}
        collected = {name: self._top_level(name, base, template) for name, template in templates.items()}
        issues: list[IssueView] = []
        merged: dict[int, PRView] = {}
        for node in collected["issues"]:
            number = node.get("number") if isinstance(node, dict) else None
            try:
                comments = self._nested(node, "comments", _COMMENTS, on="Issue", backward=True)
                xrefs = self._nested(node, "timelineItems", _TIMELINE, on="Issue", backward=True)
                if comments is None or xrefs is None:
                    raise FixObservationIncomplete("comments or cross-references exceed their bound")
                blockers, problem = parse_blockers(node.get("blockedBy"), self.repo)
                if problem:
                    sys.stderr.write(f"issue #{number}: {problem}; unreadable blockers never block\n")
                merged.update((p.number, p) for p in parse_merged_harness_prs(xrefs, self.repo))
                issues.append(parse_fix_issue(node, comments, blockers))
            except FixObservationIncomplete as exc:
                self._skip_item("issue", number if type(number) is int else 0, exc)
        prs: list[PRView] = []
        for node in collected["pullRequests"]:
            if not isinstance(node, dict) or type(node.get("number")) is not int or not isinstance(node.get("headRefName"), str):
                raise FixObservationIncomplete("open PR identity is unreadable")
            if not node["headRefName"].startswith(_HARNESS_BRANCH_PREFIX):
                continue
            try:
                comments = self._nested(node, "comments", _COMMENTS, on="PullRequest", backward=True)
                reviews = self._nested(node, "reviews", _REVIEWS, on="PullRequest", backward=True)
                files = self._nested(node, "files", _FILES, on="PullRequest", backward=False)
                if comments is None or reviews is None or files is None:
                    raise FixObservationIncomplete("a nested connection exceeds its bound")
                pr = parse_fix_pr(node, comments, reviews, files)
                prs.append(replace(pr, ci=observe_checks(self.repo, pr, self._gh_json)))
            except FixObservationIncomplete as exc:
                # Still the item (it shadows its issue), but with no head to act on.
                self._skip_item("PR", node["number"], exc)
                prs.append(PRView(number=node["number"], head_ref_name=node["headRefName"], state="OPEN",
                                  content_known=False))
        prs.extend(p for n, p in sorted(merged.items()) if n not in {o.number for o in prs})
        branches, unknown = parse_fix_refs(collected["refs"])
        for number, why in sorted(unknown.items()):
            self._skip_item("issue", number, FixObservationIncomplete(why))
        return WorldState(issues=tuple(i for i in issues if i.number not in unknown), prs=tuple(prs), now=now,
                          branches=branches, base=self._base_health(base, filed_by), pickup=pickup,
                          architecture=self._architecture(base, filed_by) if filed_by else None)

    def _architecture(self, base: str, filer: str) -> ArchitectureObservation | None:
        """Two bounded reads at most; unknown pass facts defer filing (ADR 0017)."""
        try:
            search = self._graphql(_PASS_ISSUES, q=(
                f'repo:{self.repo} is:issue author:{filer} in:title "Architecture pass" sort:created-desc')).get("search")
            if (not isinstance(search, dict) or not isinstance(search.get("nodes"), list)
                    or type(search.get("issueCount")) is not int or not 0 <= search["issueCount"] <= 2**63 - 1
                    or len(search["nodes"]) != min(20, search["issueCount"])):
                return None
            for node in search["nodes"]:
                if (not isinstance(node, dict) or not isinstance(node.get("title"), str) or len(node["title"]) > 256
                        or type(node.get("number")) is not int or not 0 < node["number"] <= 2**63 - 1
                        or parse_account(node.get("author")) is None):
                    return None
                issue = IssueView(node["number"], title=node["title"], author=parse_account(node.get("author")))
                if architecture_pass(issue, filer):
                    break
            else:
                if search["issueCount"] >= 20:
                    raise FixObservationIncomplete("no genuine pass among 20 search hits")
                return NoPass()
            if node.get("state") not in ("OPEN", "CLOSED"):
                return None
            issue = replace(issue, state=node["state"].lower(), labels=_complete_labels(node))
            xrefs, info = _connection(node, "timelineItems", "hasPreviousPage")
            if info["hasPreviousPage"] or len(xrefs) > 100:
                return None
            merged = tuple(p for p in parse_merged_harness_prs(xrefs, self.repo)
                           if harness_branch_issue(p.head_ref_name) == issue.number)
            if not merged and issue.state == "open":
                return PassInFlight(issue)
            stamps = tuple(p.merged_at for p in merged) if merged else (node["closedAt"],)
            if any(not isinstance(s, str) or len(s) > 64 for s in stamps):
                return None
            ends = tuple(datetime.fromisoformat(s.replace("Z", "+00:00")) for s in stamps)
            if any(end.utcoffset() is None for end in ends):
                return None
            count = self._graphql(_MERGED_COUNT, q=(
                f'repo:{self.repo} is:pr is:merged base:{json.dumps(base)} merged:>{max(ends).isoformat()}'
            )).get("search", {}).get("issueCount")
            return PassEnded(issue.number, count)
        except (ValueError, TypeError, KeyError, AttributeError, GhUnavailable) as exc:
            sys.stderr.write(f"architecture unread this poll ({type(exc).__name__}: {str(exc)[:120]})\n")
            return None

    def _top_level(self, name: str, base: str, template: str) -> list:
        """One top-level connection, its own request per page (#425), read in
        full within its page bound; otherwise the read raises and the tick is
        skipped. At most `_FIX_MAX_PAGES[name]` requests."""
        fixed = {"base": base} if name == "refs" else {}
        nodes: list = []
        cursor: str | None = None
        for _ in range(_FIX_MAX_PAGES[name]):
            page = (self._repository_page(template.format(after=""), **fixed) if cursor is None
                    else self._repository_page(template.format(after=",after:$after"), after=cursor, **fixed))
            more, info = _connection(page, name, "hasNextPage")
            nodes.extend(more)
            if not info["hasNextPage"]:
                return nodes
            cursor = info.get("endCursor")
            if not isinstance(cursor, str) or not cursor:
                raise FixObservationIncomplete(f"{name} page has no cursor")
        raise FixObservationIncomplete(f"{name} exceed {_FIX_MAX_PAGES[name]} pages")

    def _base_health(self, base: str, filer: str) -> BaseHealth:
        """The base's health, its parts read apart (#442). The tip's checks,
        None when unreadable: a red PR check is then the PR's, as before #313
        (never a silent wait). A failed request skips the tick, as it did when
        the tip rode the combined read: a timeout answered as GraphQL errors
        must not charge the base's red to every PR. With `filer`: the base's
        push runs and the base-CI issues `filer` opened, open and closed, read
        together (nothing is filed or changed unless both are); the required
        workflows, from the branch policy and the tip's check runs (#402), and
        the repository's active workflows (#449), each None when unread."""
        raw = self._repository_page(_BASE_TIP, base=base).get("baseTip")
        try:
            if (not isinstance(raw, dict) or raw.get("name") != base or not isinstance(raw.get("target"), dict)
                    or not isinstance(raw["target"].get("oid"), str) or not raw["target"]["oid"]):
                raise FixObservationIncomplete("base ref is absent or not the configured base")
            rollup = raw["target"].get("statusCheckRollup")
            nodes = [] if rollup is None else self._nested(
                rollup, "contexts", _BASE_CONTEXTS, on="StatusCheckRollup", backward=False)
            if nodes is None:
                raise FixObservationIncomplete("base checks exceed their bound")
            reports = base_reports(nodes)
        except (ValueError, KeyError, TypeError) as exc:
            sys.stderr.write(f"base tip unread this poll ({type(exc).__name__}: {str(exc)[:120]}) "
                             "— red PR checks are charged to the PR\n")
            nodes = reports = None
        if not filer:
            return BaseHealth(base, reports)
        try:
            listing = self._gh_json([
                "api", f"repos/{self.repo}/actions/runs?branch={quote(base, safe='')}&event=push"
                       f"&exclude_pull_requests=true&per_page=100"])
            runs = base_runs(listing, base)
            issues = base_issues(self._graphql(_BASE_ISSUES, q=(
                f'repo:{self.repo} is:issue author:{filer} in:title "failing on {base}" sort:created-desc'))
                .get("search"), filer)
        except (ValueError, KeyError, TypeError, GhUnavailable) as exc:
            # An optional read: GitHub not answering it must not cost the tick its merges and dispatches.
            sys.stderr.write(f"base CI unread this poll ({type(exc).__name__}: {str(exc)[:120]})\n")
            return BaseHealth(base, reports)
        try:
            required = None if nodes is None else required_workflows(required_policy(self.repo, base, self._gh_json), nodes)
        except Exception as exc:  # noqa: BLE001 - an unread policy readies nothing, as in observe_checks
            sys.stderr.write(f"base required workflows unread this poll ({type(exc).__name__}: {str(exc)[:120]})\n")
            required = None
        try:
            workflows = active_workflows(self._gh_json(["api", f"repos/{self.repo}/actions/workflows?per_page=100"]),
                                         listing)
        except Exception as exc:  # noqa: BLE001 - an optional read: an unread listing closes nothing
            sys.stderr.write(f"workflows unread this poll ({type(exc).__name__}: {str(exc)[:120]})\n")
            workflows = None
        return BaseHealth(base, reports, runs, issues, required, workflows)

    def create_issue(self, title: str, body: str) -> Accepted | Refused:
        """Open an unlabelled issue; the receipt carries its number."""
        return self._write(["--method", "POST", f"repos/{self.repo}/issues", "-f", f"title={title}",
                            "-f", f"body={body}"], "number")

    def reopen_issue(self, number: int) -> Accepted | Refused:
        return self._write(["--method", "PATCH", f"repos/{self.repo}/issues/{number}", "-f", "state=open"])

    def issue_index(self, limit: int = 300, author: str = "") -> tuple[tuple[int, str, str, frozenset[Label]], ...]:
        """(number, state, title, harness labels) of the repository's most
        recent issues, open and closed (by `author`, `@me` for this token's
        login, if given; gh reads it as `createdBy`, not the search index):
        the candidates a STEWARD leg may name as a duplicate or a blocker. An
        unreadable listing raises; the STEWARD leg is then refused (a machine end)."""
        data = self._gh_json(["issue", "list", "--repo", self.repo, "--state", "all", "--json",
                              "number,state,title,labels", "--limit", str(limit),
                              *(["--author", author] if author else [])])
        if not isinstance(data, list):
            raise FixObservationIncomplete("issue listing is unreadable")
        return tuple((row["number"], str(row.get("state") or "").lower(), str(row.get("title") or ""),
                      _labels(row.get("labels") if isinstance(row.get("labels"), list) else None))
                     for row in data if isinstance(row, dict) and type(row.get("number")) is int
                     and row["number"] > 0)

    def blocker_link(self, issue: int, blocker: int) -> BlockerLinkFacts:
        """Both ends of a proposed "#issue blocked by #blocker" link, fresh."""
        if type(issue) is not int or type(blocker) is not int or issue <= 0 or blocker <= 0:
            raise ValueError("a blocker link joins two issue numbers")
        fields = "id state " + _BLOCKED_BY
        page = self._repository_page(f"subject:issue(number:{issue}){{{fields}}} blocker:issue(number:{blocker}){{{fields}}}")
        return parse_blocker_link(page, self.repo, blocker)

    def add_blocked_by(self, issue_id: str, blocker_id: str) -> Accepted | Refused:
        """GitHub's addBlockedBy."""
        return self._write(["graphql", "-f", "query=mutation($issue:ID!,$blocker:ID!){addBlockedBy(input:"
                            "{issueId:$issue,blockingIssueId:$blocker}){issue{number}}}",
                            "-f", f"issue={issue_id}", "-f", f"blocker={blocker_id}"])

    def pull_request_for_branch(self, branch: str) -> int | None:
        """The open PR whose head is `branch` in this repository. A closed one
        does not count: a branch pushed after its PR was closed is reopened
        with a new PR."""
        data = self._gh_json(["pr", "list", "--repo", self.repo, "--head", branch, "--state", "open",
                              "--json", "number,headRefName", "--limit", "10"])
        if not isinstance(data, list):
            raise FixObservationIncomplete("PR lookup by branch is unreadable")
        numbers = [row["number"] for row in data if isinstance(row, dict) and row.get("headRefName") == branch
                   and type(row.get("number")) is int]
        return min(numbers) if numbers else None

    def open_pull_request(self, branch: str, base: str, title: str, body: str) -> Accepted | Refused:
        return self._write(["--method", "POST", f"repos/{self.repo}/pulls", "-f", f"head={branch}", "-f", f"base={base}",
                            "-f", f"title={title}", "-f", f"body={body}"], "number")

    def viewer_login(self) -> str:
        """This credential's login, or "". GraphQL `viewer`, not REST `/user`:
        an App installation token answers it (as `slug[bot]`)."""
        return self._gh(["api", "graphql", "-f", "query=query{viewer{login}}", "--jq", ".data.viewer.login"]).strip()

    def submit_review(self, pr: int, event: str, body: str, head: str) -> Accepted | Refused:
        """Refused is a 4xx (422 self-approval, 403 without write) that
        retrying cannot change; the receipt carries the review id."""
        return self._write(build_submit_review_args(self.repo, pr, event, body, head), "id")

    # ── writes ──
    def relabel(self, kind: str, number: int, remove: str | None, add: str | None) -> Accepted | Refused:
        answer: Accepted | Refused = Accepted()
        for args in build_relabel_args(self.repo, kind, number, remove, add) or []:
            answer = self._write(args)
            if isinstance(answer, Refused):
                break
        return answer

    def has_label(self, kind: str, number: int, label: str) -> bool:
        data = self._view(kind, number, "labels")
        return isinstance(data, dict) and any(isinstance(item, dict) and item.get("name") == label
                                              for item in (data.get("labels") or []))

    def has_comment(self, kind: str, number: int, body: str, author: str) -> bool:
        """Whether this exact comment exists under `author`: probed after an ambiguous failed
        write, since a timeout can arrive after GitHub accepted the POST, and
        before a settled comment is posted again. A reply that is not a comment
        list is no answer: absent would post a duplicate."""
        data = self._view(kind, number, "comments")
        if not isinstance(data, dict) or not isinstance(data.get("comments"), list):
            raise FixObservationIncomplete(f"{kind} #{number}: comments were not answered")
        return any(isinstance(item, dict) and item.get("body") == body
                   and account_eq(_login(item.get("author")), author) for item in data["comments"])

    def is_issue_closed(self, issue: int) -> bool:
        data = self._view("issue", issue, "state")
        return isinstance(data, dict) and str(data.get("state") or "").upper() == "CLOSED"

    def reviewed_on(self, pr: int, head: str, login: str, since: str = "") -> bool:
        """Whether `login` has a standing verdict on commit `head` submitted no
        earlier than `since` (the body edit the review judged, #472; "" or
        unparseable: any), across every page. Raises unless the read was
        complete: the review role submits on "absent"."""
        after = parse_timestamp(since)

        def current(raw: object) -> bool:
            at = parse_timestamp(raw) if isinstance(raw, str) else None
            return after is None or at is None or at >= after

        data = self._gh_json(["api", f"repos/{self.repo}/pulls/{pr}/reviews?per_page=100", "--paginate", "--slurp"])
        if not isinstance(data, list) or any(not isinstance(page, list) for page in data):
            raise RuntimeError(f"pull request {pr} reviews were not answered completely")
        return any(
            isinstance(item, dict) and isinstance(item.get("user"), dict)
            and account_eq(_login(item.get("user")), login) and item.get("commit_id") == head
            and str(item.get("state") or "").upper() in ("APPROVED", "CHANGES_REQUESTED")
            and current(item.get("submitted_at"))
            for page in data for item in page)

    def comment(self, kind: str, number: int, body: str) -> Accepted | Refused:
        """On an issue or PR (one REST endpoint); the receipt carries its id."""
        return self._write(["--method", "POST", f"repos/{self.repo}/issues/{number}/comments", "-f", f"body={body}"],
                           "id")

    def close_issue(self, number: int, reason: str) -> Accepted | Refused:
        return self._write(["--method", "PATCH", f"repos/{self.repo}/issues/{number}",
                            "-f", "state=closed", "-f", f"state_reason={reason.replace(' ', '_')}"])

    def _without_auto_merge(self, pr: int, expected_head: str) -> dict | None:
        """Revoke any legacy GitHub auto-merge arm before a write that can
        advance the head: an armed approval survives later pushes."""
        data = self._view("pr", pr, "id,autoMergeRequest,headRefOid,headRefName,isCrossRepository")
        if not isinstance(data, dict) or data.get("headRefOid") != expected_head:
            return None
        if data.get("autoMergeRequest") is not None and isinstance(self._write(["graphql", "-f",
            "query=mutation($pr:ID!){disablePullRequestAutoMerge(input:{pullRequestId:$pr}){pullRequest{id}}}",
            "-f", f"pr={data.get('id', '')}"]), Refused):
            return None
        return data

    def enable_auto_merge(self, pr: int, method: str, expected_head: str) -> bool:
        """Merge only the observed, review-and-CI-qualified head, now (the name
        is historical; persistent auto-merge is never armed). GitHub's answer
        is the receipt; an unknown outcome is observed on the next tick."""
        if not (target := self._without_auto_merge(pr, expected_head)):
            return False
        if isinstance(self._write(["--method", "PUT", f"repos/{self.repo}/pulls/{pr}/merge",
                                   "-f", f"sha={expected_head}", "-f", f"merge_method={method}"]), Refused):
            return False
        # As with gh pr merge --delete-branch, cleanup failure cannot undo a
        # merge. Leaving every fix ref eventually exhausts the observation bound.
        if target.get("isCrossRepository") is False and (branch := target.get("headRefName")):
            try:
                answer = self._write(["--method", "DELETE", f"repos/{self.repo}/git/refs/heads/{quote(branch, safe='')}"])
                if isinstance(answer, Refused):
                    sys.stderr.write(f"PR #{pr}: merged; branch deletion refused: {answer.message}\n")
            except GhUnavailable as exc:
                sys.stderr.write(f"PR #{pr}: merged; branch deletion unavailable: {exc}\n")
        return True

    def branch_runs(self, branch: str) -> tuple[tuple[int, str, str], ...]:
        """(id, status, created-at) of the newest workflow runs on
        `branch` that were built from THIS repository, at most
        `_RUN_SCAN_LIMIT`. REST, not `gh run list`: only the REST items carry
        `head_repository`, and a fork's same-named branch must never be
        cancelled, so a run with an absent or malformed head repository is
        skipped. Raises ValueError on a payload that is not a run list. Works
        after the branch is deleted."""
        data = self._gh_json(["api", f"repos/{self.repo}/actions/runs?branch={quote(branch, safe='')}"
                              f"&per_page={_RUN_SCAN_LIMIT}"])
        rows = data.get("workflow_runs") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise ValueError("workflow run list is unreadable")
        out = []
        for row in rows[:_RUN_SCAN_LIMIT]:
            if not isinstance(row, dict):
                continue
            head_repo = row.get("head_repository")
            name = head_repo.get("full_name") if isinstance(head_repo, dict) else None
            if (not isinstance(name, str) or name.casefold() != self.repo.casefold()
                    or type(row.get("id")) is not int or row["id"] <= 0
                    or not isinstance(row.get("status"), str) or not isinstance(row.get("created_at"), str)):
                continue
            out.append((row["id"], row["status"], row["created_at"]))
        return tuple(out)

    def cancel_run(self, run_id: int) -> Accepted | Refused:
        """Request the cancel: 202, or refused (403 without actions:write,
        409 on a finished run)."""
        return self._write(["--method", "POST", f"repos/{self.repo}/actions/runs/{run_id}/cancel"])

    def run_attempt(self, run_id: int) -> int:
        """A workflow run's current attempt: the read-back of a re-run. Raises
        on an answer that is not a run."""
        data = self._gh_json(["api", f"repos/{self.repo}/actions/runs/{run_id}"])
        attempt = data.get("run_attempt") if isinstance(data, dict) else None
        if type(attempt) is not int or attempt <= 0:
            raise ValueError(f"run {run_id} is unreadable")
        return attempt

    def rerun_failed_checks(self, run_id: int) -> Accepted | Refused:
        """Request a completed run's failed-jobs re-run (a new run attempt),
        or refused (403, a run still going). The run read lags the POST, so
        the answer is the receipt, not a read-back (#433)."""
        return self._write(["--method", "POST", f"repos/{self.repo}/actions/runs/{run_id}/rerun-failed-jobs"])

    def update_branch(self, pr: int, expected_head: str) -> bool:
        """Merge base into a BEHIND head. True iff GitHub accepted it (the
        merge runs after the 202, so a head read would lag it, #433); False if
        refused (422: not behind, the head moved) or a legacy auto-merge arm
        could not be revoked first. No answer raises."""
        if not self._without_auto_merge(pr, expected_head):
            return False
        return isinstance(self._write([f"repos/{self.repo}/pulls/{pr}/update-branch", "--method", "PUT",
                                       "-f", f"expected_head_sha={expected_head}"]), Accepted)


def _subprocess_runner(argv: list[str], token: str | None) -> str:
    """Run gh (with GH_TOKEN when given) under `_GH_TIMEOUT_S`. A non-zero exit
    is written to stderr, since a silently swallowed 422 once made the
    reviewer look idle. An `--include` write that GitHub answered returns its
    output whatever the exit, for `parse_write_answer` to read the status.
    Otherwise infra failures (`classify_gh_failure`) raise `GhUnavailable`
    (V10) and any other non-zero exit returns ""."""
    env = dict(os.environ)
    if token:
        env["GH_TOKEN"] = token
    try:
        res = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=_GH_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        # A killed call produced no data; it must not read as an empty answer.
        sys.stderr.write(f"gh timed out (>{_GH_TIMEOUT_S:.0f}s): {' '.join(argv)}\n"
                         "  killed; treating as INFRA failure (poll re-derives next tick)\n")
        raise GhUnavailable("network", f"gh timed out after {_GH_TIMEOUT_S:.0f}s: {' '.join(argv[:4])}",
                            argv=argv) from exc
    except OSError as exc:
        if exc.errno not in {errno.EAGAIN, errno.ENOMEM}:
            raise
        detail = str(exc) or "process creation failed"
        sys.stderr.write(f"gh failed to start: {_printable_gh_argv(argv)}\n"
                         f"  {_cap_bytes(detail, GhUnavailable._MAX_DETAIL_BYTES)}\n")
        raise GhUnavailable("local-resource", detail, argv=argv) from exc
    if res.returncode == 0:
        return res.stdout
    detail = (res.stderr or res.stdout or "").strip()
    # A private repo on a free plan has no rulesets API: that 403 means "no
    # rules" (an empty page), not a failure.
    capability = "Upgrade to GitHub Pro or make this repository public to enable this feature."
    if any("/rules/branches/" in arg for arg in argv) and detail.removeprefix("gh: ") == f"{capability} (HTTP 403)":
        try:
            error = json.loads(res.stdout) if res.stdout.strip() else None
            if isinstance(error, list) and len(error) == 1:
                error = error[0]
            if error is None or (isinstance(error, dict) and error.get("status") == "403"
                                 and error.get("message") == capability):
                return "[[]]"
        except ValueError:
            pass
    logged = _cap_bytes(detail, GhUnavailable._MAX_DETAIL_BYTES)
    truncated = "\n  [gh stderr truncated]" if logged != detail else ""
    # Written on the raise path too: some callers swallow GhUnavailable.
    sys.stderr.write(f"gh failed (exit {res.returncode}): {_printable_gh_argv(argv)}\n  {logged}{truncated}\n")
    if "--include" in argv and _HTTP_STATUS.match(res.stdout):
        return res.stdout
    kind = classify_gh_failure(detail)
    if kind is not None:
        raise GhUnavailable(kind, detail, argv=argv, retry_after_s=_retry_after(detail))
    return ""


def _retry_after(detail: str) -> float | None:
    match = re.search(r"retry[- ]after\s*[:=]?\s*(\d+(?:\.\d+)?)", detail or "", re.IGNORECASE)
    return float(match.group(1)) if match else None
