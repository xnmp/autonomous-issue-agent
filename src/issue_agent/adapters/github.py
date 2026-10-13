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

from ..domain.checks import Unavailable
from ..domain.ci import parse_timestamp
from ..domain.identity import login_eq
from ..domain.labels import Label
from ..domain.refs import harness_branch_issue
from ..domain.world import Branch, Comment, IssueView, PRView, PullRequestDiff, Push, Review, WorldState
from .github_ci import observe_checks

# Hard ceiling on any single `gh` call: `gh pr view` with lazily computed
# fields (mergeable / mergeStateStatus) once hung ~14 min on a just-merged PR
# and froze the synchronous poll loop.
_GH_TIMEOUT_S = 30.0
_LOGGED_GH_ARGV_BYTES = 2 * 1024

# Prompt-input bounds (review M13/P1R-13). Inline comments are writable by
# anyone with read access, and `tail -N` bounds lines, not bytes: a 200MB CI
# log's last 150 lines was once a 150M-char prompt.
_INLINE_COMMENT_LIMIT = 8
_FAILED_LOG_LINES = 150
_FAILED_LOG_BYTES = 20_000
_REVIEW_COMMENT_BODY_BYTES = 2_000
_REVIEW_COMMENTS_TOTAL_BYTES = 20_000


def _tail(text: str, lines: int, max_bytes: int | None = None) -> str:
    """The last `lines` lines, then the last `max_bytes` UTF-8 bytes; a cut
    multi-byte character is dropped."""
    out = "\n".join((text or "").splitlines()[-lines:])
    encoded = out.encode("utf-8", errors="ignore")
    if max_bytes is None or len(encoded) <= max_bytes:
        return out
    return encoded[-max_bytes:].decode("utf-8", errors="ignore")


def _cap_bytes(text: str, max_bytes: int) -> str:
    """The first `max_bytes` UTF-8 bytes (a comment states its point up front)."""
    encoded = text.encode("utf-8", errors="ignore")
    return text if len(encoded) <= max_bytes else encoded[:max_bytes].decode("utf-8", errors="ignore")


class GhUnavailable(RuntimeError):
    """GitHub could not ANSWER (auth, rate limit, network, 5xx, local resource
    exhaustion), as opposed to answering "nothing" (V10). Mapping every
    failure to "" once made a rate-limited poll observe an empty world and act
    on it; raising makes the daemon skip the tick, and the next poll retries.
    `kind` is one of `_HEADLINES`."""

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


def _one_line(text: str, limit: int = 200) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


# Lower-cased substrings that mark a failed `gh` call as INFRA. A non-zero exit
# is ALSO how gh reports "PR not found", "label does not exist" and a 422
# self-approval, and those must keep degrading to "". A bare HTTP 403 is not
# infra ("Resource not accessible by integration" is a permanent answer).
_AUTH_PATTERNS = ("http 401", "requires authentication", "bad credentials")
_RATE_LIMIT_PATTERNS = ("rate limit", "rate-limit", "ratelimit", "secondary rate", "abuse detection",
                        "http 429", "too many requests", "retry-after")
# "could not resolve HOST": gh's GraphQL "Could not resolve to a PullRequest"
# is an answer, not an outage.
_NETWORK_PATTERNS = ("error connecting", "could not resolve host", "could not resolve hostname", "no such host",
                     "temporary failure in name resolution", "connection refused", "connection reset",
                     "network is unreachable", "dial tcp", "tls handshake", "proxyconnect", "i/o timeout",
                     "timed out", "timeout")
_SERVER_PATTERNS = ("bad gateway", "service unavailable", "gateway timeout", "gateway time-out",
                    "internal server error")
_LOCAL_RESOURCE_PATTERNS = ("pthread_create failed", "resource temporarily unavailable",
                            "failed to create new os thread", "fatal error: newosproc", "cannot allocate memory",
                            "out of memory")
# Only next to "HTTP": a bare "502" may be an issue number.
_SERVER_RE = re.compile(r"http[/ ]?\s*5\d\d")


def classify_gh_failure(detail: str) -> str | None:
    """The infra kind of a failed `gh` call's output, or None for an ordinary
    API answer. Server before network: "504 Gateway Timeout" contains "timeout"."""
    text = (detail or "").lower()
    if any(p in text for p in _AUTH_PATTERNS):
        return "auth"
    if any(p in text for p in _RATE_LIMIT_PATTERNS):
        return "rate-limit"
    if _SERVER_RE.search(text) or any(p in text for p in _SERVER_PATTERNS):
        return "server"
    if any(p in text for p in _NETWORK_PATTERNS):
        return "network"
    if any(p in text for p in _LOCAL_RESOURCE_PATTERNS):
        return "local-resource"
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
        Comment(author=_login(c.get("author")) if isinstance(c.get("author"), dict) else c.get("author", "") or "",
                body=c.get("body", "") or "", created_at=c.get("createdAt", "") or "")
        for c in raw or [])


def _reviews(raw: list | None) -> tuple[Review, ...]:
    """GraphQL (`author`, `commit.oid`) or REST (`user`, `commit_id`) shape.
    An unknown commit is "": the ADDRESS_REVIEW supersession gate stays open."""
    out = []
    for r in raw or []:
        author = _login(r.get("author")) or _login(r.get("user"))
        commit = r.get("commit")
        commit_id = (commit.get("oid", "") if isinstance(commit, dict) else "") or r.get("commit_id", "") or ""
        out.append(Review(state=r.get("state", "") or "", author=author,
                          submitted_at=r.get("submittedAt") or r.get("submitted_at") or "",
                          body=r.get("body", "") or "", commit_id=commit_id))
    return tuple(out)


def parse_issue(d: dict) -> IssueView:
    return IssueView(
        number=int(d["number"]), title=d.get("title", "") or "", body=d.get("body", "") or "",
        labels=_labels(d.get("labels")), comments=_comments(d.get("comments")),
        updated_at=d.get("updatedAt", "") or "", author=_login(d.get("author")),
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


def format_review_comments(raw: object, limit: int = _INLINE_COMMENT_LIMIT) -> str:
    """`GET /pulls/{n}/comments` → the fixer's `path:line (author): body` block:
    top-level comments only (replies are the fixer's own thread), the newest
    `limit`, each and the whole block byte-capped (review M13, P1R-13). Any
    malformed shape degrades to a weaker prompt, never a dead dispatch."""
    if not isinstance(raw, list):
        return ""
    tops = sorted((c for c in raw if isinstance(c, dict) and c.get("in_reply_to_id") is None),
                  key=lambda c: str(c.get("created_at") or c.get("createdAt") or ""))
    out = []
    for c in tops[-limit:] if limit > 0 else tops:
        author = _login(c.get("user")) or _login(c.get("author"))
        line = c.get("line") or c.get("original_line") or 0
        body = _cap_bytes((c.get("body", "") or "").strip(), _REVIEW_COMMENT_BODY_BYTES)
        out.append(f"{c.get('path', '') or ''}:{line} ({author}): {body}")
    return _cap_bytes("\n".join(out), _REVIEW_COMMENTS_TOTAL_BYTES)


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


_FIX_MAX_PAGES = {"issues": 10, "pullRequests": 10, "refs": 10}
_FIX_NESTED_MAX = {"comments": 1000, "reviews": 500, "files": 3000, "timelineItems": 500}
_HARNESS_BRANCH_PREFIX = "fix/"
_COMMENTS = "comments(last:100{before}){{totalCount pageInfo{{hasPreviousPage startCursor}} nodes{{author{{login}} body createdAt}}}}"
_LABELS = "labels(first:100){totalCount nodes{name}}"
# Cross-references, paged in full like any nested connection: a merged harness
# PR past one page must read as "unknown" (the issue is skipped), never as
# "not merged" (which would re-RESOLVE a done issue). No totalCount: whether
# it honours `itemTypes` is undocumented, so completeness rests on paging.
_TIMELINE = ("timelineItems(itemTypes:[CROSS_REFERENCED_EVENT],last:100{before}){{pageInfo{{hasPreviousPage startCursor}} "
             "nodes{{... on CrossReferencedEvent{{source{{... on PullRequest{{number state headRefName "
             "repository{{nameWithOwner}}}}}}}}}}}}")
# The newest label applications: when `needs-human` was last applied.
_LABELED = ("labeled:timelineItems(itemTypes:[LABELED_EVENT],last:100){pageInfo{hasPreviousPage} "
            "nodes{... on LabeledEvent{createdAt label{name}}}}")
_FIX_ISSUE = (
    "id number title state author{login} " + _LABELS + " " + _LABELED + " "
    + _COMMENTS.format(before="") + " " + _TIMELINE.format(before="")
)
_REVIEWS = ("reviews(last:100{before}){{totalCount pageInfo{{hasPreviousPage startCursor}} "
            "nodes{{author{{login}} state submittedAt body commit{{oid}}}}}}")
_FILES = "files(first:100{after}){{totalCount pageInfo{{hasNextPage endCursor}} nodes{{path}}}}"
_FIX_PR = (
    "id number state headRefName headRefOid baseRefName baseRefOid title author{login} "
    "mergeable mergeStateStatus " + _LABELS + " " + _LABELED + " " + _COMMENTS.format(before="") + " "
    + _REVIEWS.format(before="") + " " + _FILES.format(after="")
    + " commits(last:1){nodes{commit{oid committedDate author{user{login}}}}}"
)
# Per branch: its newest PR (any state), its head commit's date, and how many
# commits it has that the base lacks (`compare` reads the ref as the base, so
# that count is `behindBy`). The prefix stays `refs/heads/`: under a
# `refs/heads/fix/` prefix GitHub fails `compare` (500 or NOT_FOUND for every
# base spelling), which failed every fix-role read (2026-10-13, all three
# deployments). `query` (case-insensitive, matches anywhere) narrows
# server-side; the parser keeps only names that start with the harness prefix.
_FIX_REFS = ("refs(refPrefix:\"refs/heads/\",query:\"" + _HARNESS_BRANCH_PREFIX + "\",first:100{after})"
             "{{pageInfo{{hasNextPage endCursor}} "
             "nodes{{name associatedPullRequests(last:1,orderBy:{{field:CREATED_AT,direction:ASC}})"
             "{{totalCount nodes{{state closedAt}}}} target{{... on Commit{{committedDate}}}} "
             "compare(headRef:$base){{behindBy}}}}}}")
def _escaped(fields: str) -> str:
    return fields.replace("{", "{{").replace("}", "}}")


_FIX_CONNECTIONS = {
    "issues": ("issues(states:OPEN,labels:[\"ai-ready\",\"needs-human\"],first:100{after},"
               "orderBy:{{field:CREATED_AT,direction:ASC}}){{pageInfo{{hasNextPage endCursor}} nodes{{" + _escaped(_FIX_ISSUE) + "}}}}"),
    "pullRequests": ("pullRequests(states:OPEN,first:100{after},orderBy:{{field:CREATED_AT,direction:ASC}})"
                     "{{pageInfo{{hasNextPage endCursor}} nodes{{" + _escaped(_FIX_PR) + "}}}}"),
    "refs": _FIX_REFS,
}


class FixObservationIncomplete(ValueError):
    """A required connection could not be read in full within its bound."""


def _connection(parent: object, name: str) -> tuple[list, dict]:
    if not isinstance(parent, dict) or not isinstance(parent.get(name), dict):
        raise FixObservationIncomplete(f"{name} is absent")
    conn = parent[name]
    nodes, info = conn.get("nodes"), conn.get("pageInfo")
    if not isinstance(nodes, list) or (info is not None and not isinstance(info, dict)):
        raise FixObservationIncomplete(f"{name} is malformed")
    return nodes, info or {}


def _login(raw: object) -> str:
    login = raw.get("login") if isinstance(raw, dict) else None
    return login if isinstance(login, str) else ""


def _complete_labels(node: dict) -> frozenset[Label]:
    nodes, _ = _connection(node, "labels")
    if node["labels"].get("totalCount") != len(nodes):
        raise FixObservationIncomplete("labels exceed one page")
    return _labels(nodes)


def _needs_human_labeled_at(node: dict, labels: frozenset[Label]) -> str:
    """When `needs-human` was last applied, for a labelled item ("" otherwise).
    Not found among the newest 100 label events: unknown, so the item is
    skipped rather than released on an older reply."""
    if Label.NEEDS_HUMAN not in labels:
        return ""
    nodes, info = _connection(node, "labeled")
    times = [n["createdAt"] for n in nodes if isinstance(n, dict) and isinstance(n.get("createdAt"), str)
             and isinstance(n.get("label"), dict) and n["label"].get("name") == Label.NEEDS_HUMAN.value]
    if times:
        return times[-1]
    if info.get("hasPreviousPage"):
        raise FixObservationIncomplete("needs-human label event is past the newest 100")
    return ""


def parse_fix_issue(node: object, comments: list) -> IssueView:
    """One issue node (with its fully paged comments) → IssueView."""
    if not isinstance(node, dict) or type(node.get("number")) is not int or node["number"] <= 0:
        raise FixObservationIncomplete("issue identity is malformed")
    state = node.get("state")
    if state not in ("OPEN", "CLOSED"):
        raise FixObservationIncomplete("issue state is malformed")
    labels = _complete_labels(node)
    return IssueView(
        number=node["number"], title=node.get("title") or "", labels=labels,
        comments=_comments(comments), author=_login(node.get("author")), state=state.lower(),
        needs_human_labeled_at=_needs_human_labeled_at(node, labels),
    )


def parse_merged_harness_prs(items: list, repository: str) -> tuple[PRView, ...]:
    """Merged same-repo harness PRs among an issue's (fully paged)
    cross-references. A merged harness PR means the issue is done even while
    it stays open (base ≠ default branch never auto-closes it)."""
    out = []
    for item in items:
        source = item.get("source") if isinstance(item, dict) else None
        if not isinstance(source, dict) or source.get("state") != "MERGED":
            continue
        repo = (source.get("repository") or {}).get("nameWithOwner") if isinstance(source.get("repository"), dict) else ""
        head = source.get("headRefName")
        if (not isinstance(repo, str) or repo.casefold() != repository.casefold() or type(source.get("number")) is not int
                or not isinstance(head, str) or not head.startswith(_HARNESS_BRANCH_PREFIX)):
            continue
        out.append(PRView(number=source["number"], head_ref_name=head, state="MERGED", content_known=False))
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
        pushes = (Push(_login(author.get("user")), commit.get("committedDate") or ""),)
    return PRView(
        number=node["number"], head_ref_name=node["headRefName"], head_ref_oid=head,
        base_ref_name=node.get("baseRefName") or "", base_ref_oid=node.get("baseRefOid") or "",
        title=node.get("title") or "", labels=(labels := _complete_labels(node)),
        comments=_comments(comments), reviews=_reviews(reviews),
        needs_human_labeled_at=_needs_human_labeled_at(node, labels),
        state=str(node.get("state") or ""), changed_files=_changed_files([{"path": f.get("path")} for f in files
                                                                         if isinstance(f, dict)]),
        mergeable=str(node.get("mergeable") or ""), merge_state=str(node.get("mergeStateStatus") or ""),
        author=_login(node.get("author")), pushes=pushes,
    )


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
    return ["api", f"repos/{repo}/pulls/{pr}/reviews", "--method", "POST",
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
    that end state as success."""
    if not remove and not add:
        return None
    commands: list[list[str]] = []
    if add:
        commands.append(["api", "--method", "POST", f"repos/{repo}/issues/{number}/labels", "-f", f"labels[]={add}"])
    if remove:
        commands.append(["api", "--method", "DELETE", f"repos/{repo}/issues/{number}/labels/{quote(remove, safe='')}"])
    return commands


def build_comment_args(repo: str, kind: str, number: int, body: str) -> list[str]:
    return [kind, "comment", str(number), "--repo", repo, "--body", body]


def build_close_issue_args(repo: str, number: int, reason: str, comment: str) -> list[str]:
    # `gh issue close --reason` wants "not planned", not the GraphQL enum
    # "not_planned" (keifu #193 sat labelled ai-closed but open for it).
    args = ["issue", "close", str(number), "--repo", repo, "--reason", reason.replace("_", " ")]
    return args + ["--comment", comment] if comment else args


def build_enable_auto_merge_args(repo: str, pr: int, method: str, expected_head: str) -> list[str]:
    return ["pr", "merge", str(pr), "--repo", repo, f"--{method}", "--delete-branch",
            "--match-head-commit", expected_head]


@dataclass(frozen=True)
class Identity:
    """How `gh` authenticates: app-token mint, else static token, else ambient
    `gh auth`."""

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

    def _gh(self, args: list[str]) -> str:
        try:
            return self._run(["gh", *args], self.identity.env_token())
        finally:
            if self._progress is not None:
                self._progress()

    def _gh_json(self, args: list[str]) -> object:
        out = self._gh(args)
        return json.loads(out) if out.strip() else None

    def _view(self, kind: str, number: int, fields: str) -> object:
        return self._gh_json([kind, "view", str(number), "--repo", self.repo, "--json", fields])

    def get_issue(self, number: int) -> IssueView:
        return parse_issue(self._view("issue", number, "number,title,body,labels,comments,updatedAt,author,state"))  # type: ignore[arg-type]

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
        if not isinstance(raw, dict) or raw.get("errors") or not isinstance(raw.get("data"), dict):
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
        nodes, info = _connection(node, name)
        nodes = list(nodes)
        total = node[name].get("totalCount")
        more, cursor_key = ("hasPreviousPage", "startCursor") if backward else ("hasNextPage", "endCursor")
        while info.get(more):
            cursor = info.get(cursor_key)
            if len(nodes) >= _FIX_NESTED_MAX[name] or not isinstance(cursor, str) or not cursor:
                return None
            field = template.format(**{"before" if backward else "after": f",{'before' if backward else 'after'}:$cursor"})
            data = self._graphql(
                f"query($id:ID!,$cursor:String!){{node(id:$id){{... on {on}{{{field}}}}}}}",
                id=str(node.get("id") or ""), cursor=cursor,
            )
            page, info = _connection(data.get("node"), name)
            nodes = [*page, *nodes] if backward else [*nodes, *page]
        if type(total) is int and total != len(nodes) or len(nodes) > _FIX_NESTED_MAX[name]:
            return None
        return nodes

    def build_world(self, base: str) -> WorldState:
        """The fix role's observation: open `ai-ready`/`needs-human` issues,
        open harness PRs (`fix/<issue>-`) with head, mergeability, required
        checks, reviews, comments, every changed file and the head push, and
        harness branches owed a PR against `base`. `GhUnavailable` and
        incomplete top-level reads propagate: the daemon skips the tick."""
        # The domain reads no clock: one stamp per build, so every gate in a tick judges one instant.
        now = datetime.now(timezone.utc).isoformat()
        first = self._repository_page(" ".join(t.format(after="") for t in _FIX_CONNECTIONS.values()), base=base)
        collected: dict[str, list] = {}
        for name, template in _FIX_CONNECTIONS.items():
            nodes, info = _connection(first, name)
            nodes, pages = list(nodes), 1
            while info.get("hasNextPage"):
                cursor = info.get("endCursor")
                if pages >= _FIX_MAX_PAGES[name] or not isinstance(cursor, str) or not cursor:
                    raise FixObservationIncomplete(f"{name} exceed {_FIX_MAX_PAGES[name]} pages")
                page = self._repository_page(template.format(after=",after:$after"), after=cursor,
                                             **({"base": base} if name == "refs" else {}))
                more, info = _connection(page, name)
                nodes.extend(more)
                pages += 1
            collected[name] = nodes
        issues: list[IssueView] = []
        merged: dict[int, PRView] = {}
        for node in collected["issues"]:
            number = node.get("number") if isinstance(node, dict) else None
            try:
                comments = self._nested(node, "comments", _COMMENTS, on="Issue", backward=True)
                xrefs = self._nested(node, "timelineItems", _TIMELINE, on="Issue", backward=True)
                if comments is None or xrefs is None:
                    raise FixObservationIncomplete("comments or cross-references exceed their bound")
                issues.append(parse_fix_issue(node, comments))
                merged.update((p.number, p) for p in parse_merged_harness_prs(xrefs, self.repo))
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
                          branches=branches)

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

    def open_pull_request(self, branch: str, base: str, title: str, body: str) -> None:
        self._gh(["pr", "create", "--repo", self.repo, "--head", branch, "--base", base,
                  "--title", title, "--body", body])

    def pr_diff(self, pr: int, base_oid: str, head_oid: str) -> PullRequestDiff:
        """base...head as GitHub compares it. Empty text is GitHub's answer; an
        outage raises `GhUnavailable`. The review role refuses an empty diff."""
        if not base_oid or not head_oid:
            raise ValueError("pull-request diff revisions are required")
        text = self._gh(["api", "-H", "Accept: application/vnd.github.v3.diff",
                         f"repos/{self.repo}/compare/{base_oid}...{head_oid}"])
        return PullRequestDiff(base_oid, head_oid, text)

    # The two repair-prompt inputs fail SOFT to "": a weaker prompt beats no
    # dispatch, since the agent can rediscover the failure itself (D6).
    def workflow_run_attempts(self, head: str | tuple[str, ...], run_ids: tuple[int, ...]) -> dict[int, int] | None:
        """Attempt counters for exact workflow runs, after SHA verification."""
        if not head or not run_ids or any(run_id <= 0 for run_id in run_ids):
            return None
        heads = (head,) if isinstance(head, str) else head
        attempts: dict[int, int] = {}
        for run_id in dict.fromkeys(run_ids):
            raw = self._gh_json(["api", f"repos/{self.repo}/actions/runs/{run_id}"])
            if not isinstance(raw, dict) or int(raw.get("id") or 0) != run_id or str(raw.get("head_sha") or "") not in heads:
                return None
            attempt = raw.get("run_attempt")
            if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
                return None
            attempts[run_id] = attempt
        return attempts

    def failed_run_log(self, head: str, run_ids: tuple[int, ...], *, lines: int = _FAILED_LOG_LINES) -> str:
        """Bounded failed logs of the observed runs, verified against the
        observed head (a branch's latest failed run may be an older revision)
        and pinned to the attempt so a rerun cannot swap the evidence."""
        if not head or not run_ids or any(type(n) is not int or n <= 0 for n in run_ids):
            return ""
        try:
            attempts = self.workflow_run_attempts(head, run_ids)
            if attempts is None:
                return ""
            logs = [_tail(self._gh(["run", "view", str(run_id), "--repo", self.repo, "--attempt", str(attempt),
                                    "--log-failed"]), lines, max_bytes=_FAILED_LOG_BYTES)
                    for run_id, attempt in attempts.items()]
            return _tail("\n\n".join(logs), lines, max_bytes=_FAILED_LOG_BYTES)
        except Exception:  # noqa: BLE001 — a missing log must not block the fix
            return ""

    def review_comments(self, pr: int, *, limit: int = _INLINE_COMMENT_LIMIT) -> str:
        """The newest inline review comments. `per_page=100`: the default page
        of 30 would hand the fixer the OLDEST comments on a busy PR."""
        try:
            return format_review_comments(self._gh_json(["api", f"repos/{self.repo}/pulls/{pr}/comments?per_page=100"]),
                                          limit=limit)
        except Exception:  # noqa: BLE001 — a missing comment list must not block the fix
            return ""

    def viewer_login(self) -> str:
        """This credential's login, or "". GraphQL `viewer`, not REST `/user`:
        an App installation token answers it (as `slug[bot]`)."""
        return self._gh(["api", "graphql", "-f", "query=query{viewer{login}}", "--jq", ".data.viewer.login"]).strip()

    def submit_review(self, pr: int, event: str, body: str, head: str) -> bool:
        """True iff GitHub created the review. False is a 4xx refusal (422
        self-approval, 403 without write) that retrying cannot change; an
        outage raises `GhUnavailable`."""
        return bool(self._gh(build_submit_review_args(self.repo, pr, event, body, head)).strip())

    # ── writes ──
    def relabel(self, kind: str, number: int, remove: str | None, add: str | None) -> None:
        for args in build_relabel_args(self.repo, kind, number, remove, add) or []:
            self._gh(args)

    def has_label(self, kind: str, number: int, label: str) -> bool:
        data = self._view(kind, number, "labels")
        return isinstance(data, dict) and any(isinstance(item, dict) and item.get("name") == label
                                              for item in (data.get("labels") or []))

    def has_comment(self, kind: str, number: int, body: str) -> bool:
        """Whether this exact comment exists: probed after an ambiguous failed
        write, since a timeout can arrive after GitHub accepted the POST."""
        data = self._view(kind, number, "comments")
        return isinstance(data, dict) and any(isinstance(item, dict) and item.get("body") == body
                                              for item in (data.get("comments") or []))

    def is_issue_closed(self, issue: int) -> bool:
        data = self._view("issue", issue, "state")
        return isinstance(data, dict) and str(data.get("state") or "").upper() == "CLOSED"

    def reviewed_on(self, pr: int, head: str, login: str) -> bool:
        """Whether `login` has a standing verdict on commit `head`, across every
        page. Raises unless the read was complete: the review role submits on
        "absent"."""
        data = self._gh_json(["api", f"repos/{self.repo}/pulls/{pr}/reviews?per_page=100", "--paginate", "--slurp"])
        if not isinstance(data, list) or any(not isinstance(page, list) for page in data):
            raise RuntimeError(f"pull request {pr} reviews were not answered completely")
        return any(
            isinstance(item, dict) and isinstance(item.get("user"), dict)
            and login_eq(str(item["user"].get("login") or ""), login) and item.get("commit_id") == head
            and str(item.get("state") or "").upper() in ("APPROVED", "CHANGES_REQUESTED")
            for page in data for item in page)

    def comment(self, kind: str, number: int, body: str) -> None:
        self._gh(build_comment_args(self.repo, kind, number, body))

    def close_issue(self, number: int, reason: str, comment: str) -> None:
        self._gh(build_close_issue_args(self.repo, number, reason, comment))

    def _without_auto_merge(self, pr: int, expected_head: str) -> bool:
        """Revoke any legacy GitHub auto-merge arm before a write that can
        advance the head: an armed approval survives later pushes."""
        data = self._view("pr", pr, "autoMergeRequest,headRefOid")
        if not isinstance(data, dict) or data.get("headRefOid") != expected_head:
            return False
        if data.get("autoMergeRequest") is None:
            return True
        self._gh(["pr", "merge", str(pr), "--repo", self.repo, "--disable-auto"])
        verified = self._view("pr", pr, "autoMergeRequest,headRefOid")
        return (isinstance(verified, dict) and verified.get("headRefOid") == expected_head
                and verified.get("autoMergeRequest") is None)

    def enable_auto_merge(self, pr: int, method: str, expected_head: str) -> bool:
        """Merge only the observed, review-and-CI-qualified head, now (the name
        is historical; persistent auto-merge is never armed). True iff the
        expected head is read back as merged."""
        if not self._without_auto_merge(pr, expected_head):
            return False
        self._gh(build_enable_auto_merge_args(self.repo, pr, method, expected_head))
        try:
            data = self._view("pr", pr, "state,headRefOid")
        except Exception:  # noqa: BLE001 — unreadable is not confirmed
            return False
        return (isinstance(data, dict) and data.get("headRefOid") == expected_head
                and str(data.get("state") or "").upper() == "MERGED")

    def update_branch(self, pr: int, expected_head: str) -> bool:
        """Merge base into a BEHIND head. Fail-soft: an 'already up to date' or
        conflict race is re-evaluated next tick."""
        try:
            if not self._without_auto_merge(pr, expected_head):
                return False
            if not self._gh(["api", f"repos/{self.repo}/pulls/{pr}/update-branch", "--method", "PUT",
                             "-f", f"expected_head_sha={expected_head}"]).strip():
                return False
            data = self._view("pr", pr, "headRefOid,mergeStateStatus")
            if not isinstance(data, dict):
                return False
            current = str(data.get("headRefOid") or "")
            return (bool(current) and current != expected_head
                    and str(data.get("mergeStateStatus") or "").upper() != "BEHIND"
                    and self._without_auto_merge(pr, current))
        except Exception:  # noqa: BLE001
            return False


def _subprocess_runner(argv: list[str], token: str | None) -> str:
    """Run gh (with GH_TOKEN when given) under `_GH_TIMEOUT_S`. A non-zero exit
    returns "" (callers such as `submit_review` read the empty answer) and is
    written to stderr, since a silently swallowed 422 once made the reviewer
    look idle. Infra failures (`classify_gh_failure`) raise `GhUnavailable`
    instead (V10)."""
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
    kind = classify_gh_failure(detail)
    if kind is not None:
        raise GhUnavailable(kind, detail, argv=argv, retry_after_s=_retry_after(detail))
    return ""


def _retry_after(detail: str) -> float | None:
    match = re.search(r"retry[- ]after\s*[:=]?\s*(\d+(?:\.\d+)?)", detail or "", re.IGNORECASE)
    return float(match.group(1)) if match else None
