"""GitHubGateway — the only module that runs `gh`.

Two halves, the usual pure/impure split:

  * PURE PARSERS (`parse_issue`, `parse_pr`, `parse_world`) — gh `--json` dicts →
    domain value objects (IssueView/PRView/Review/Check/Comment). These are the
    real-vs-fake seam the bash kept getting wrong (the fake `gh` drifting from
    real `gh`), so they are unit-tested against the documented JSON shapes.
  * IMPURE GATEWAY — runs `gh` as a chosen identity (ambient / static PAT /
    GitHub-App installation token) and applies side-effects (relabel, comment,
    close, enable-auto-merge). The identity selection reproduces the bash
    gh_fix / gh_review identity model.

Field names match what the bash queries (issue: number,title,body,labels,
comments,updatedAt,author,state; pr: number,title,body,headRefName,headRefOid,
baseRefName,labels,reviews,state,closingIssuesReferences; checks: name,state,
bucket,startedAt,link) so the mapping is mechanical and auditable.
"""

from __future__ import annotations

import errno
import json
import re
import subprocess
from .github_ci import observe_checks
from ..domain.checks import Unavailable
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from urllib.parse import quote

from ..domain.ci import parse_timestamp
from ..domain.identity import login_eq
from ..domain.labels import Label
from ..domain.refs import harness_branch_issue
from ..domain.world import (
    Branch,
    Comment,
    IssueView,
    PRView,
    PullRequestDiff,
    Push,
    Review,
    WorldState,
    extract_declared_paths,
)


# Hard ceiling on any single `gh` subprocess. A `gh pr view` requesting lazily
# computed fields (mergeable / mergeStateStatus) can hang indefinitely on a
# just-merged PR; without a timeout that wedges the synchronous poll loop. 120s is
# far above a healthy call (<2s), below the service watchdog interval, and
# paired with a scheduler-progress callback after every call.
_GH_TIMEOUT_S = 30.0
_LOGGED_GH_ARGV_BYTES = 2 * 1024

# Cap on `gh issue list` / `gh pr list` world-building calls. gh has no
# "there were more" signal short of paginating (cursor-following, unshipped
# here) — a returned count that HITS this ceiling is the only observable proxy
# for truncation, and it means the OLDEST issues/PRs silently vanished from
# consideration this poll, no error, no signal (suspected root cause of
# FAILURE-MODES #66 — triage candidates going missing). 500 covers real-world
# open-issue/PR counts with headroom over the old 200; _warn_if_list_truncated
# below makes a hit LOUD instead of silent (P0.7).
_LIST_LIMIT = 500

# Cap on the CLOSED `ai-pr-open` slice the fix world adds for merge cleanup
# (#28). Deliberately much smaller than _LIST_LIMIT: every number here costs a
# `gh issue view`, and on a repo that ran the harness before the fix landed the
# set is a BACKLOG of stuck issues, not a working set. Newest-first (gh's
# default order), so a backlog deeper than the cap is worked off newest-first
# over consecutive polls. `domain.fix.orphaned_cleanup_actions` is what makes
# that terminate: it relabels each observed issue to ai-done, which removes
# ai-pr-open and drops it out of this query for good. Without that arm this
# slice would re-fetch the same stuck issues every poll forever. Steady state
# is an empty set and one cheap list call.
_CLOSED_CLEANUP_LIMIT = 100

# Inline review comments pulled per PR (port of agent/address-review.sh:41's
# `.[-8:]`) — the NEWEST 8. A bound is required: the whole list is pasted into a
# prompt, and an unbounded paste is how a long review thread blows the context
# window on the one leg that must not fail.
_INLINE_COMMENT_LIMIT = 8

# Tail of the failed-CI log handed to the fixer (port of `tail -150` in
# agent/fix-ci.sh:42). The failure is almost always in the last frames; the head
# of a 50k-line Actions log is pure setup noise.
_FAILED_LOG_LINES = 150

# Byte ceiling on the SAME tail, independent of the line count (review P1R-13).
# `tail -150` bounds LINES, not bytes: a log whose lines are individually huge —
# one minified bundle, a base64 blob, a per-line JSON dump — sails straight
# through it. Measured: a 200MB CI log's last 150 lines was still a
# 150,001,711-char prompt. 20_000 bytes (~5-6k tokens at a typical ~3.5
# bytes/token) comfortably fits a real multi-hundred-line stack trace (a
# genuine failure line runs 80-150 bytes) while capping the fix-ci leg's
# worst-case prompt to a few cents of context on even the cheapest model —
# compare `_one_line`'s 200-char bound for a single gh error line.
_FAILED_LOG_BYTES = 20_000

# Per-comment + overall byte ceilings on the inline-review-comments block
# (review P1R-13). Unlike an issue/PR body, an inline review comment is
# writable by ANY user with read access to the repo — not just the PR author —
# so it is a wider attack surface than most prompt inputs. 2_000 bytes/comment
# is generous for an actual review remark (a paragraph is well under 500
# bytes) while making a weaponized paste (measured repro: 10 x 500KB comments
# -> ~4MB) harmless; the 20_000-byte overall ceiling matches the CI-log budget
# above and is defense-in-depth beyond the per-comment cap (it also bounds a
# future caller that raises `limit` past `_INLINE_COMMENT_LIMIT`).
_REVIEW_COMMENT_BODY_BYTES = 2_000
_REVIEW_COMMENTS_TOTAL_BYTES = 20_000


class MissingListLimit(ValueError):
    """Programmer error: a `gh … list` argv was built without `--limit`.

    Not a runtime condition — a bug in this module, raised eagerly so it can
    never reach production as a silent truncation. gh's default page size is 30,
    and a `list` call without `--limit` therefore answers with the 30 NEWEST
    items while looking exactly like a complete answer: past 30 ai-ready issues
    the oldest were never dispatched, past 30 open PRs their CI was never fixed —
    every poll, no error (review C2; FAILURE-MODES #66's signature). Subclasses
    ValueError so it reads as the programmer error it is, but is distinguishable
    from a JSONDecodeError in the fail-soft methods that swallow everything else.
    """


def _require_list_limit(args: list[str]) -> None:
    """Reject a `gh <thing> list …` argv that carries no `--limit` (review C2).

    Every list call in this module must state its ceiling so `_warn_if_list_truncated`
    has something to compare against; this guard makes the omission impossible to
    reintroduce rather than merely unlikely."""
    if len(args) >= 2 and args[1] == "list" and "--limit" not in args:
        raise MissingListLimit(
            f"gh {args[0]} list built without --limit: {' '.join(args)} — "
            "gh would silently return only the 30 newest items. Pass "
            "--limit str(_LIST_LIMIT) (and call _warn_if_list_truncated on the count)."
        )


def _tail(text: str, lines: int, max_bytes: int | None = None) -> str:
    """Last `lines` lines of `text` (port of `tail -N`), ADDITIONALLY capped to
    the last `max_bytes` UTF-8 bytes when given (review P1R-13). The two caps
    are independent: `tail -N` bounds line COUNT, not size, so a log with a
    handful of enormous lines needs a separate byte cut to stay bounded.
    Slicing bytes can land mid multi-byte character at the cut point;
    `errors="ignore"` drops that partial character rather than raising.
    Tolerates ""/None."""
    out = "\n".join((text or "").splitlines()[-lines:])
    if max_bytes is None:
        return out
    encoded = out.encode("utf-8", errors="ignore")
    if len(encoded) <= max_bytes:
        return out
    return encoded[-max_bytes:].decode("utf-8", errors="ignore")


def _cap_bytes(text: str, max_bytes: int) -> str:
    """Truncate `text` to at most `max_bytes` of UTF-8 encoded content, keeping
    the FRONT (review P1R-13) — unlike `_tail`'s from-the-end truncation for a
    CI log, a review comment's substance is normally stated up front, so
    keeping the start preserves whatever legitimate point the comment opens
    with. `errors="ignore"` drops a partial multi-byte character at the cut
    point rather than raising."""
    encoded = text.encode("utf-8", errors="ignore")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


# ── infra-failure detection (V10) ─────────────────────────────────────────────
class GhUnavailable(RuntimeError):
    """GitHub could not ANSWER — the `gh` call failed for infrastructure reasons
    (auth outage, rate limit, network, 5xx), not because the world is empty.

    The lesson: `_subprocess_runner` used to map every failure to `""`, so a
    rate-limited poll observed an EMPTY world — zero PRs, zero issues — and the
    domain, being honest about what it saw, acted on it (nothing to review,
    nothing in flight, cleanup fair game). "Nothing to do" and "I could not
    look" must never be the same value. Raising is the entire fix: a world built
    on a failed read is a lie, so the daemon skips the tick and the poll loop —
    which IS the retry — looks again next interval. No backoff lives here.

    `kind` is one of "auth" | "rate-limit" | "network" | "server" |
    "local-resource";
    `timed_out` marks the
    one sub-case that is per-CALL rather than systemic (the `_GH_TIMEOUT_S` kill
    of a single hung `gh pr view`), which `_safe_get_pr` still isolates per PR.
    """

    _HEADLINES = {
        "auth": "unavailable (authentication)",
        "rate-limit": "rate-limited",
        "network": "unreachable (network)",
        "server": "server error (5xx)",
        "local-resource": "unavailable (local resource exhaustion)",
    }
    _MAX_DETAIL_BYTES = 8 * 1024

    def __init__(
        self,
        kind: str,
        detail: str,
        argv: list[str] | None = None,
        timed_out: bool = False,
        retry_after_s: float | None = None,
    ):
        self.kind = kind
        self.detail = _cap_bytes(
            _one_line(detail), self._MAX_DETAIL_BYTES
        )
        self.argv = tuple(argv or ())
        self.timed_out = timed_out
        self.retry_after_s = retry_after_s
        super().__init__(f"GitHub {self.headline}: {self.detail}")

    @property
    def headline(self) -> str:
        """Operator-facing phrase for the kind (the loud loop line reads it)."""
        return self._HEADLINES.get(self.kind, self.kind)


def _one_line(text: str, limit: int = 200) -> str:
    """Collapse a multi-line gh error into ONE bounded line. Daemon logs are
    scanned by eye and grepped by line; a 40-line curl dump buries the signal."""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


# Lower-cased substrings that mark a failed `gh` call as INFRA rather than as an
# ordinary API answer. Pattern-matching is deliberate — a non-zero exit is ALSO
# how gh reports "PR not found", "label does not exist", and a 422 self-approval,
# and callers depend on those still degrading to "" (see `_safe_get_pr`,
# `build_review_world`'s missing-label tolerance, `submit_review`'s False). A
# blanket raise would trade a silent-empty-world bug for a wedged daemon.
_RATE_LIMIT_PATTERNS = (
    "rate limit",        # "API rate limit exceeded for user ID …" (arrives as HTTP 403)
    "rate-limit",
    "ratelimit",         # header echo, e.g. "x-ratelimit-remaining: 0"
    "secondary rate",    # secondary/abuse limits (short-lived but real)
    "abuse detection",
    "http 429",
    "too many requests",
    "retry-after",
)
_AUTH_PATTERNS = (
    "http 401",
    "requires authentication",
    "bad credentials",
)
# NB: a bare HTTP 403 is NOT infra — "Resource not accessible by integration"
# (App token missing a scope) is a permanent, benign permission answer that the
# reviewer already tolerates. Only 403s carrying rate-limit text match above.

_NETWORK_PATTERNS = (
    "error connecting",                     # gh: "error connecting to api.github.com"
    # DNS — the "host" is load-bearing: gh's GraphQL 404 reads "Could not resolve
    # to a PullRequest with the number of 999", a perfectly good ANSWER that a
    # bare "could not resolve" would misfile as an outage and skip ticks over.
    "could not resolve host",
    "could not resolve hostname",
    "no such host",
    "temporary failure in name resolution",
    "connection refused",
    "connection reset",
    "network is unreachable",
    "dial tcp",
    "tls handshake",
    "proxyconnect",                         # broken/absent outbound proxy (see CLAUDE.md)
    "i/o timeout",
    "timed out",
    "timeout",
)

_SERVER_PATTERNS = (
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    "gateway time-out",
    "internal server error",
)
_LOCAL_RESOURCE_PATTERNS = (
    "pthread_create failed",
    "resource temporarily unavailable",
    "failed to create new os thread",
    "fatal error: newosproc",
    "cannot allocate memory",
    "out of memory",
)
# Match the STATUS TOKEN only next to an explicit "HTTP": gh prints "HTTP 503
# (https://api.github.com/…)". A bare "502" is not enough — it could be an issue
# number in "issue #502 not found", and misreading that as an outage would skip
# ticks forever.
_SERVER_RE = re.compile(r"http[/ ]?\s*5\d\d")


def classify_gh_failure(detail: str) -> str | None:
    """Map a failed `gh` invocation's stderr/stdout to an infra kind
    ("rate-limit" | "network" | "server" | "local-resource"), or None when
    the failure is an ordinary API answer the caller should keep treating as
    "" (fail-soft).

    Order matters: auth → rate-limit → server → network. "504 Gateway Timeout" contains
    "timeout", so the 5xx test must run before the network test or a GitHub
    outage would be misfiled as a local network fault (same tick-skip either way,
    but the operator reads the kind to decide whether to fix their box)."""
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
    redact_next = False
    field_value = False
    for arg in argv:
        if redact_next:
            rendered.append(f"<body redacted: {len(arg.encode())} bytes>")
            redact_next = False
            continue
        if field_value and arg.startswith("body="):
            body = arg.removeprefix("body=")
            rendered.append(f"body=<redacted: {len(body.encode())} bytes>")
            field_value = False
            continue
        rendered.append(arg)
        redact_next = arg in {"--body", "-b"}
        field_value = arg in {"-f", "-F", "--field", "--raw-field"}
    return _cap_bytes(" ".join(rendered), _LOGGED_GH_ARGV_BYTES)


# ── pure parsers ──────────────────────────────────────────────────────────────
def _label_names(raw: list | None) -> frozenset[str]:
    """Raw label names, in gh's two shapes: [{"name": "ai-ready"}, …] (issue/pr
    view) or bare strings (some fakes). The name-level twin of `_labels`, for
    callers that must ask about a label the `Label` enum does not know — the
    reviewer's configurable trigger label. ONE extraction so a shape the two
    read differently cannot exist (adversarial review F3)."""
    out = set()
    for item in raw or []:
        name = item.get("name") if isinstance(item, dict) else item
        if isinstance(name, str) and name:
            out.add(name)
    return frozenset(out)


def _labels(raw: list | None) -> frozenset[Label]:
    """gh returns labels as [{"name": "ai-ready"}, …] (issue/pr view) or as bare
    strings (some fakes). Map known names to Label; silently drop unknown ones so
    a stray repo label never crashes the parse."""
    out = set()
    for name in _label_names(raw):
        try:
            out.add(Label(name))
        except ValueError:
            continue
    return frozenset(out)


def _comments(raw: list | None) -> tuple[Comment, ...]:
    out = []
    for c in raw or []:
        author = (c.get("author") or {}).get("login", "") if isinstance(c.get("author"), dict) else c.get("author", "")
        out.append(
            Comment(author=author or "", body=c.get("body", "") or "", created_at=c.get("createdAt", "") or "")
        )
    return tuple(out)


def _reviews(raw: list | None) -> tuple[Review, ...]:
    out = []
    for r in raw or []:
        author = ""
        if isinstance(r.get("author"), dict):
            author = r["author"].get("login", "")
        elif isinstance(r.get("user"), dict):  # API shape
            author = r["user"].get("login", "")
        # `commit_id` feeds the ADDRESS_REVIEW supersession gate (Review docstring
        # in domain/world.py). Two incompatible gh shapes carry it: the GraphQL-backed
        # `gh pr view --json reviews` nests it under commit.oid; the plain REST
        # reviews API (`/pulls/{n}/reviews`) has it flat as commit_id. Try both,
        # defensively (isinstance guards a malformed/absent "commit"); "" (unknown)
        # if neither is present — the gate stays open and the attempt-counting
        # breaker is the backstop, never a crash on an unrecognized shape.
        commit_id = ""
        if isinstance(r.get("commit"), dict):  # GraphQL shape
            commit_id = r["commit"].get("oid", "") or ""
        if not commit_id:
            commit_id = r.get("commit_id", "") or ""  # REST shape
        out.append(
            Review(
                state=r.get("state", "") or "",
                author=author or "",
                submitted_at=r.get("submittedAt") or r.get("submitted_at") or "",
                body=r.get("body", "") or "",
                commit_id=commit_id,
            )
        )
    return tuple(out)


def _check_run_observation(raw: dict) -> dict[str, object]:
    status = str(raw.get("status") or "").lower()
    conclusion = str(raw.get("conclusion") or "").lower()
    if status != "completed":
        bucket = "pending"
    elif conclusion in {"success", "neutral", "skipped"}:
        bucket = "pass"
    else:
        bucket = "fail"
    return {
        "name": str(raw.get("name") or ""),
        "state": status.upper(),
        "bucket": bucket,
        "startedAt": str(
            raw.get("started_at") or raw.get("created_at") or ""
        ),
        "link": str(raw.get("details_url") or ""),
    }


def _commit_status_observation(raw: dict) -> dict[str, object]:
    state = str(raw.get("state") or "").lower()
    return {
        "name": str(raw.get("context") or ""),
        "state": state.upper(),
        "bucket": (
            "pending"
            if state == "pending"
            else "pass"
            if state == "success"
            else "fail"
        ),
        "startedAt": str(
            raw.get("created_at") or raw.get("updated_at") or ""
        ),
        "link": str(raw.get("target_url") or ""),
    }


def parse_issue(d: dict) -> IssueView:
    author = ""
    if isinstance(d.get("author"), dict):
        author = d["author"].get("login", "")
    body = d.get("body", "") or ""
    return IssueView(
        number=int(d["number"]),
        title=d.get("title", "") or "",
        body=body,
        labels=_labels(d.get("labels")),
        comments=_comments(d.get("comments")),
        updated_at=d.get("updatedAt", "") or "",
        author=author or "",
        # gh issue state is "OPEN"/"CLOSED"; WorldState uses lower-case open/closed.
        state=(d.get("state", "open") or "open").lower(),
        # GraphQL serves REOPENED; compare against one casing.
        state_reason=str(d.get("stateReason") or "").lower(),
        # File paths the body names in backticks → the file-overlap gate.
        declared_paths=extract_declared_paths(body),
    )


def parse_pr(d: dict) -> PRView:
    closing = None
    refs = d.get("closingIssuesReferences") or []
    if refs and isinstance(refs[0], dict) and refs[0].get("number") is not None:
        closing = int(refs[0]["number"])
    return PRView(
        number=int(d["number"]),
        head_ref_name=d.get("headRefName", "") or "",
        head_ref_oid=d.get("headRefOid", "") or "",
        base_ref_name=d.get("baseRefName", "") or "",
        base_ref_oid=d.get("baseRefOid", "") or "",
        title=d.get("title", "") or "",
        body=d.get("body", "") or "",
        labels=_labels(d.get("labels")),
        comments=_comments(d.get("comments")),
        reviews=_reviews(d.get("reviews")),

        state=(d.get("state", "OPEN") or "OPEN").upper(),
        closing_issue=closing,
        changed_files=_changed_files(d.get("files")),
        mergeable=(d.get("mergeable", "") or "").upper(),
        merge_state=(d.get("mergeStateStatus", "") or "").upper(),
        is_draft=bool(d.get("isDraft", False)),
        author=((d.get("author") or {}).get("login", "") or ""),
    )


# Every DECISION field a terminal (MERGED/CLOSED) PRView needs, in one labelled
# list call. Kept in lockstep with `parse_terminal_pr`'s required-key check: a
# field this list stops carrying must make the parse FAIL (→ full `gh pr view`),
# never silently default.
#
# Deliberately ABSENT, and therefore never defaulted-in silently either: the
# connection-shaped fields (body, comments, reviews, files), whose GraphQL cost
# scales with page size. A view built from this list carries
# `content_known=False` so telemetry can tell "unread" from "empty"; no
# lifecycle decision reads any of them off a non-OPEN PR.
_PR_LIST_FIELDS = (
    "number,state,labels,headRefName,headRefOid,baseRefName,baseRefOid,"
    "title,isDraft,author,closingIssuesReferences"
)

_TERMINAL_STATES = ("MERGED", "CLOSED")


def _changed_files(files: object) -> frozenset[str]:
    """`gh pr view --json files` → [{path, additions, ...}]. Pull the paths for
    the file-overlap gate. Tolerant of a missing/oddly-shaped field (→ empty)."""
    if not isinstance(files, list):
        return frozenset()
    out = set()
    for f in files:
        if isinstance(f, dict) and f.get("path"):
            out.add(str(f["path"]))
    return frozenset(out)


def format_review_comments(raw: object, limit: int = _INLINE_COMMENT_LIMIT) -> str:
    """`GET /repos/{repo}/pulls/{n}/comments` JSON → the prompt block the fixer
    reads, one comment per entry as `path:line (author): body` (review M13, D6).

    Port of agent/address-review.sh:41 with the formatting moved OUT of the jq
    filter and into Python: jq inside an argv is invisible to the tests, and this
    text is the fixer's only view of the file:line asks reviewers actually make.

    Selection mirrors the bash exactly: top-level comments only (`in_reply_to_id
    == null` — replies are the fixer's own thread, not new asks), oldest→newest,
    the last `limit`. Tolerant of every shape it might be handed (a non-list error
    object, a missing path/line/user) — this feeds a prompt, so a malformed field
    degrades to a weaker prompt, never to a dead dispatch.

    Each comment body is capped at `_REVIEW_COMMENT_BODY_BYTES` and the whole
    formatted block at `_REVIEW_COMMENTS_TOTAL_BYTES` (review P1R-13): unlike an
    issue/PR body, an inline comment is writable by ANY user with read access to
    the repo, so `limit` alone (item COUNT) does not bound the paste — a handful
    of huge comments still blow the prompt."""
    if not isinstance(raw, list):
        return ""
    tops = [
        c for c in raw
        if isinstance(c, dict) and c.get("in_reply_to_id") is None
    ]
    tops.sort(key=lambda c: str(c.get("created_at") or c.get("createdAt") or ""))
    out = []
    for c in tops[-limit:] if limit > 0 else tops:
        author = ""
        for key in ("user", "author"):  # REST shape, then GraphQL shape
            if isinstance(c.get(key), dict):
                author = c[key].get("login", "") or ""
                break
        line = c.get("line") or c.get("original_line") or 0
        body = _cap_bytes((c.get("body", "") or "").strip(), _REVIEW_COMMENT_BODY_BYTES)
        out.append(f"{c.get('path', '') or ''}:{line} ({author}): {body}")
    return _cap_bytes("\n".join(out), _REVIEW_COMMENTS_TOTAL_BYTES)


def _marked_cap(text: str, max_bytes: int) -> str:
    """`_cap_bytes` that says so when it cut. An unmarked truncation reads as a
    complete statement that happens to trail off mid-sentence."""
    capped = _cap_bytes(text, max_bytes)
    return capped if capped == text else f"{capped} […comment truncated]"


def format_pr_discussion(
    comments: tuple[Comment, ...],
    *,
    body_bytes: int = _REVIEW_COMMENT_BODY_BYTES,
    total_bytes: int = _REVIEW_COMMENTS_TOTAL_BYTES,
) -> str:
    """The PR's conversation thread → the block the reviewer reads as DATA.

    NEWEST FIRST, unlike the triage thread's oldest-first ordering: the byte
    ceiling truncates the tail, and on a PR the answer to "did the author
    already respond to this demand?" is at the recent end (2026-08-08 tauri
    #613/#614 — the reviewer re-demanded evidence the thread had answered two
    rounds earlier). Truncation is marked in-band so a bounded view is never
    mistaken for the whole conversation.

    Bot comments are KEPT: the harness's own fixer posts its blocked-reason
    comment as a bot, and that comment is the one this exists to deliver.

    Same ceilings as the inline-review-comment block — a PR comment is writable
    by anyone with read access to the repo, so the comment COUNT does not bound
    the paste. Both cuts (per comment, whole block) are MARKED: a reviewer
    reading a silently amputated answer would re-raise the point it made, which
    is the loop this whole feature exists to break."""
    rendered = [
        f"@{c.author or '(unknown)'}: {_marked_cap(c.body.strip(), body_bytes)}"
        for c in reversed(comments)
        if c.body.strip()
    ]
    if not rendered:
        return ""
    joined = "\n\n---\n\n".join(rendered)
    capped = _cap_bytes(joined, total_bytes)
    if capped != joined:
        capped += "\n\n---\n\n[older comments truncated]"
    return capped


def build_time() -> str:
    """The stamp every world builder puts in `WorldState.now`. The domain may not
    read a clock, so this is the one place a poll's notion of "now" is taken —
    and it is taken once per build, so every time-based gate in a tick judges
    against the same instant."""
    return datetime.now(timezone.utc).isoformat()


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
_FIX_ISSUE = (
    "id number title state author{login} " + _LABELS + " "
    + _COMMENTS.format(before="") + " " + _TIMELINE.format(before="")
)
_REVIEWS = ("reviews(last:100{before}){{totalCount pageInfo{{hasPreviousPage startCursor}} "
            "nodes{{author{{login}} state submittedAt body commit{{oid}}}}}}")
_FILES = "files(first:100{after}){{totalCount pageInfo{{hasNextPage endCursor}} nodes{{path}}}}"
_FIX_PR = (
    "id number state headRefName headRefOid baseRefName baseRefOid isDraft title author{login} "
    "mergeable mergeStateStatus " + _LABELS + " " + _COMMENTS.format(before="") + " "
    + _REVIEWS.format(before="") + " " + _FILES.format(after="")
    + " commits(last:1){nodes{commit{oid committedDate author{user{login}}}}}"
)
# Per branch: its newest PR (any state), its head commit's date, and how many
# commits it has that the base lacks (`compare` reads the ref as the base, so
# that count is `behindBy`).
_FIX_REFS = ("refs(refPrefix:\"refs/heads/" + _HARNESS_BRANCH_PREFIX + "\",first:100{after}){{pageInfo{{hasNextPage endCursor}} "
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


def parse_fix_issue(node: object, comments: list) -> IssueView:
    """One issue node (with its fully paged comments) → IssueView."""
    if not isinstance(node, dict) or type(node.get("number")) is not int or node["number"] <= 0:
        raise FixObservationIncomplete("issue identity is malformed")
    state = node.get("state")
    if state not in ("OPEN", "CLOSED"):
        raise FixObservationIncomplete("issue state is malformed")
    return IssueView(
        number=node["number"], title=node.get("title") or "", labels=_complete_labels(node),
        comments=_comments(comments), author=_login(node.get("author")), state=state.lower(),
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
        title=node.get("title") or "", labels=_complete_labels(node),
        comments=_comments(comments), reviews=_reviews(reviews),
        state=str(node.get("state") or ""), changed_files=_changed_files([{"path": f.get("path")} for f in files
                                                                         if isinstance(f, dict)]),
        mergeable=str(node.get("mergeable") or ""), merge_state=str(node.get("mergeStateStatus") or ""),
        is_draft=node.get("isDraft") is True, author=_login(node.get("author")), pushes=pushes,
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
        branch = _HARNESS_BRANCH_PREFIX + node["name"]
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
def build_submit_review_args(
    repo: str,
    pr: int,
    event: str,
    body: str,
    head: str,
) -> list[str]:
    return [
        "api", f"repos/{repo}/pulls/{pr}/reviews", "--method", "POST",
        "-f", f"event={event}", "-f", f"body={body}",
        "-f", f"commit_id={head}",
    ]


def build_relabel_args(
    repo: str, kind: str, number: int, remove: str | None, add: str | None,
) -> list[list[str]] | None:
    """REST label commands (one per mutation), never `gh <kind> edit`.

    `gh pr edit --add-label` runs a GraphQL metadata-resolution query (it
    fetches assignable users/teams alongside labels) that demands `read:org`
    — a classic PAT scoped to `repo` alone (the supported cross-account bot
    credential) fails every relabel with a scope error (observed live: review
    daemon wedged on PR #171, 2026-08-02). Plain `--json` reads are unaffected.
    The REST issues/labels endpoints accept plain `repo` scope and cover PRs
    too (a PR is an issue to this endpoint), so `kind` does not select a route.

    ADD BEFORE REMOVE (deliberate, adversarial F1): the pair is no longer one
    atomic edit, and a permanently failing second call must not strand the
    target label-less — an unlabeled issue/PR is invisible to every daemon's
    world build, so nothing would ever repair it. Add-first leaves "both
    labels" as the stuck state, which the world still sees and re-derives.
    A DELETE for an already-absent label 404s; the executor's membership
    verification treats that end state as success, so the failure is benign.
    """
    if not remove and not add:
        return None
    commands: list[list[str]] = []
    if add:
        commands.append([
            "api", "--method", "POST",
            f"repos/{repo}/issues/{number}/labels",
            "-f", f"labels[]={add}",
        ])
    if remove:
        commands.append([
            "api", "--method", "DELETE",
            f"repos/{repo}/issues/{number}/labels/{quote(remove, safe='')}",
        ])
    return commands


def build_comment_args(repo: str, kind: str, number: int, body: str) -> list[str]:
    return [kind, "comment", str(number), "--repo", repo, "--body", body]


def build_close_issue_args(repo: str, number: int, reason: str, comment: str) -> list[str]:
    # The domain speaks GitHub's GraphQL stateReason enum ("not_planned"), but
    # `gh issue close --reason` wants the CLI spelling ("not planned") and exits
    # 1 on the enum token — keifu #193 sat labelled ai-closed but open for it.
    args = ["issue", "close", str(number), "--repo", repo,
            "--reason", reason.replace("_", " ")]
    if comment:
        args += ["--comment", comment]
    return args


def build_enable_auto_merge_args(
    repo: str,
    pr: int,
    method: str,
    expected_head: str,
) -> list[str]:
    return [
        "pr",
        "merge",
        str(pr),
        "--repo",
        repo,
        f"--{method}",
        "--delete-branch",
        "--match-head-commit",
        expected_head,
    ]


def build_update_branch_args(
    repo: str, pr: int, expected_head: str
) -> list[str]:
    return [
        "api",
        f"repos/{repo}/pulls/{pr}/update-branch",
        "--method",
        "PUT",
        "-f",
        f"expected_head_sha={expected_head}",
    ]


# ── identity ────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Identity:
    """How to authenticate `gh`. Exactly one of these applies, in the bash
    priority order: app-token mint → static token → ambient `gh auth`."""

    static_token: str | None = None
    token_provider: Callable[[], str] | None = None  # e.g. AppTokenMinter.get

    def env_token(self) -> str | None:
        if self.token_provider is not None:
            return self.token_provider()
        return self.static_token


# ── gateway ───────────────────────────────────────────────────────────────────
class GitHubGateway:
    """Runs `gh` for one repo as one identity. `runner` is injected
    (subprocess by default) so tests drive it with a fake that records argv +
    returns canned JSON — the same real-vs-fake contract the bash test `gh` had,
    but now the PARSE is also unit-tested directly against JSON shapes."""

    def __init__(
        self,
        repo: str,
        identity: Identity | None = None,
        runner: Callable[[list[str], str | None], str] | None = None,
        progress: Callable[[], None] | None = None,
    ):
        self.repo = repo
        self.identity = identity or Identity()
        self._run = runner or _subprocess_runner
        self._progress = progress

    def _gh(self, args: list[str]) -> str:
        # Guard EVERY call, not just the _gh_json ones: a list subcommand read as
        # raw text (`--jq`) truncates just as silently (review C2).
        _require_list_limit(args)
        try:
            return self._run(["gh", *args], self.identity.env_token())
        finally:
            if self._progress is not None:
                self._progress()

    def _gh_json(self, args: list[str]) -> object:
        out = self._gh(args)
        return json.loads(out) if out.strip() else None

    def get_issue(self, number: int) -> IssueView:
        d = self._gh_json(
            ["issue", "view", str(number), "--repo", self.repo, "--json",
             "number,title,body,labels,comments,updatedAt,author,state,stateReason"]
        )
        return parse_issue(d)  # type: ignore[arg-type]

    def get_pr(self, number: int, *, mergeability: bool = True, checks: bool = True) -> PRView:
        """Fetch one PR. `mergeability=False` DROPS the mergeable/mergeStateStatus
        fields from the query — GitHub computes those lazily and can hang the call
        indefinitely on a just-merged PR (see _GH_TIMEOUT_S). Only the fixer's
        auto-rebase logic consumes them (is_conflicting / needs_rebase); the reviewer
        never does, so the review world fetches without them and cannot hang on that
        computation. Fields left absent parse to '' → is_conflicting/needs_rebase are
        simply False, which is correct for the review path (it doesn't rebase).

        `checks=False` skips the separate `gh pr checks` call the same way — a
        MERGED/CLOSED PR's checks gate nothing (they gated the merge that already
        happened), and the stale `--state all` label list keeps merged PRs in the
        enrichment set for days (list_pr_states). Absent checks parse to () →
        ci_state(()) is NONE, which no consumer reads for a non-open PR."""
        base = ("number,title,body,headRefName,headRefOid,baseRefName,baseRefOid,labels,"
                "comments,reviews,state,closingIssuesReferences,files,isDraft,author")
        fields = base + (",mergeable,mergeStateStatus" if mergeability else "")
        d = self._gh_json(
            ["pr", "view", str(number), "--repo", self.repo, "--json", fields]
        )
        pr = parse_pr(d)  # type: ignore[arg-type]
        if not checks:
            # Never READ ≠ read-as-empty: a PR listed non-OPEN but re-opened
            # between the list and this view would otherwise arrive
            # state=OPEN with falsely empty evidence → Clear →
            # mergeable. No consumer reads checks for the non-open PRs this
            # arm is for, so UNKNOWN costs nothing (#48 review F3).
            return replace(pr, ci=Unavailable("checks not requested"))
        return replace(pr, ci=observe_checks(self.repo, pr, self._gh_json))

    def _checks_for_head(
        self, head_oid: str
    ) -> list[dict[str, object]] | None:
        """Read all check-runs and commit statuses for one immutable SHA."""

        if not head_oid:
            return None
        check_pages = self._gh_json(
            [
                "api",
                "--paginate",
                "--slurp",
                "-H",
                "Accept: application/vnd.github+json",
                f"repos/{self.repo}/commits/{head_oid}/check-runs"
                "?filter=latest&per_page=100",
            ]
        )
        status_pages = self._gh_json(
            [
                "api",
                "--paginate",
                "--slurp",
                f"repos/{self.repo}/commits/{head_oid}/statuses?per_page=100",
            ]
        )
        if not isinstance(check_pages, list) or not isinstance(
            status_pages, list
        ):
            return None
        runs: list[dict] = []
        for page in check_pages:
            if not isinstance(page, dict) or not isinstance(
                page.get("check_runs"), list
            ):
                return None
            if any(not isinstance(item, dict) for item in page["check_runs"]):
                return None
            runs.extend(page["check_runs"])
        statuses: list[dict] = []
        for page in status_pages:
            if not isinstance(page, list) or any(
                not isinstance(item, dict) for item in page
            ):
                return None
            statuses.extend(page)
        latest_runs: dict[tuple[str, str], dict] = {}
        for item in runs:
            app = item.get("app")
            app_identity = (
                str(app.get("id") or app.get("slug") or "")
                if isinstance(app, dict)
                else ""
            )
            key = (app_identity, str(item.get("name") or ""))
            current = latest_runs.get(key)
            score = (
                int(item.get("id") or 0),
                str(item.get("started_at") or item.get("created_at") or ""),
            )
            current_score = (
                int(current.get("id") or 0),
                str(
                    current.get("started_at")
                    or current.get("created_at")
                    or ""
                ),
            ) if current is not None else (-1, "")
            if current is None or score > current_score:
                latest_runs[key] = item
        latest_statuses: dict[str, dict] = {}
        for item in statuses:
            context = str(item.get("context") or "")
            if context not in latest_statuses:
                latest_statuses[context] = item
        return [
            *(
                _check_run_observation(item)
                for item in latest_runs.values()
            ),
            *(
                _commit_status_observation(item)
                for item in latest_statuses.values()
            ),
        ]

    @staticmethod
    def _skip_item(kind: str, number: int, exc: BaseException) -> None:
        import sys
        sys.stderr.write(
            f"skip {kind} #{number} this poll ({type(exc).__name__}: {str(exc)[:120]}) "
            "— re-derived next tick\n"
        )

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
        now = build_time()
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

    def pr_diff(
        self, pr: int, base_oid: str, head_oid: str
    ) -> PullRequestDiff:
        """base...head as GitHub compares it. Empty text is GitHub's answer
        (no change between the commits, or a compare it refused); an outage
        raises `GhUnavailable`. The review role refuses to review an empty diff."""
        if not base_oid or not head_oid:
            raise ValueError("pull-request diff revisions are required")
        text = self._gh(
            [
                "api",
                "-H",
                "Accept: application/vnd.github.v3.diff",
                f"repos/{self.repo}/compare/{base_oid}...{head_oid}",
            ]
        )
        return PullRequestDiff(base_oid, head_oid, text)

    # ── prompt inputs for the two repair legs (review M13 / D6) ──
    # Both of these feed a prompt and nothing else, so both fail SOFT to "": the
    # CI-fix / address-review dispatch must still happen with a weaker prompt (the
    # agent can rediscover the failure itself) rather than not happen at all. A log
    # fetch may never kill a dispatch. The one exception is `MissingListLimit`,
    # which is a bug in this module, not a runtime condition (review C2).
    def workflow_run_attempts(
        self, head: str | tuple[str, ...], run_ids: tuple[int, ...]
    ) -> dict[int, int] | None:
        """Attempt counters for exact workflow runs, after SHA verification."""

        if not head or not run_ids or any(run_id <= 0 for run_id in run_ids):
            return None
        attempts: dict[int, int] = {}
        for run_id in dict.fromkeys(run_ids):
            raw = self._gh_json(
                ["api", f"repos/{self.repo}/actions/runs/{run_id}"]
            )
            if (
                not isinstance(raw, dict)
                or int(raw.get("id") or 0) != run_id
                or str(raw.get("head_sha") or "") not in ((head,) if isinstance(head, str) else head)
            ):
                return None
            attempt = raw.get("run_attempt")
            if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
                return None
            attempts[run_id] = attempt
        return attempts

    def failed_run_log(
        self, head: str, run_ids: tuple[int, ...], *, lines: int = _FAILED_LOG_LINES
    ) -> str:
        """Bounded logs for the observed runs, verified against the observed head.

        A branch's latest failed run may belong to an older revision. Reuse the
        rerun authority's identity verifier and pin the log attempt so a later
        rerun cannot silently change the evidence while it is being fetched.
        """
        if not head or not run_ids or any(type(n) is not int or n <= 0 for n in run_ids):
            return ""
        try:
            attempts = self.workflow_run_attempts(head, run_ids)
            if attempts is None:
                return ""
            logs = []
            for run_id, attempt in attempts.items():
                log = self._gh([
                    "run", "view", str(run_id), "--repo", self.repo,
                    "--attempt", str(attempt), "--log-failed",
                ])
                logs.append(_tail(log, lines, max_bytes=_FAILED_LOG_BYTES))
            return _tail("\n\n".join(logs), lines, max_bytes=_FAILED_LOG_BYTES)
        except MissingListLimit:
            raise
        except Exception:  # noqa: BLE001 — a missing log must not block the fix
            return ""

    def review_comments(self, pr: int, *, limit: int = _INLINE_COMMENT_LIMIT) -> str:
        """The PR's newest inline (file:line) review comments, formatted for the
        prompt. Port of agent/address-review.sh:41; formatting lives in the pure
        `format_review_comments` so it is testable without a real gh.

        `per_page=100` is deliberate: the endpoint's default page is 30 and we take
        the NEWEST `limit` — reading only the first page would hand the fixer the
        OLDEST comments on a busy PR while looking complete (the same silent
        truncation as review C2)."""
        try:
            raw = self._gh_json(
                ["api", f"repos/{self.repo}/pulls/{pr}/comments?per_page=100"]
            )
            return format_review_comments(raw, limit=limit)
        except Exception:  # noqa: BLE001 — a missing comment list must not block the fix
            return ""


    def viewer_login(self) -> str:
        """The login this gateway's credential acts as, or "" when GitHub
        refused to say. GraphQL `viewer`, not REST `/user`: an App installation
        token answers it (as `slug[bot]`) and is refused by `/user`."""
        return self._gh(
            ["api", "graphql", "-f", "query=query{viewer{login}}", "--jq", ".data.viewer.login"]
        ).strip()

    def submit_review(
        self, pr: int, event: str, body: str, head: str
    ) -> bool:
        """POST a review. True iff GitHub created it. False is GitHub's refusal
        (a 4xx: 422 'can't approve your own PR', a 403 from a token without
        pull-request write), which retrying cannot change; an outage raises
        `GhUnavailable` instead. gh's `api ... POST` prints the created-review
        JSON on success (non-empty)."""
        out = self._gh(
            build_submit_review_args(self.repo, pr, event, body, head)
        )
        return bool(out.strip())

    # ── writes ──
    def relabel(self, kind: str, number: int, remove: str | None, add: str | None) -> None:
        commands = build_relabel_args(self.repo, kind, number, remove, add)
        for args in commands or []:
            self._gh(args)

    def has_label(self, kind: str, number: int, label: str) -> bool:
        data = self._gh_json([
            kind, "view", str(number), "--repo", self.repo, "--json", "labels",
        ])
        if not isinstance(data, dict):
            return False
        return any(
            isinstance(item, dict) and item.get("name") == label
            for item in (data.get("labels") or [])
        )

    def has_comment(self, kind: str, number: int, body: str) -> bool:
        """Whether GitHub already contains this exact lifecycle comment.

        Used only after an ambiguous failed write/restart. A timeout can arrive
        after GitHub accepted the POST; probing before replay prevents a second
        clarification comment while still retrying genuinely absent writes.
        """
        data = self._gh_json([
            kind, "view", str(number), "--repo", self.repo, "--json", "comments",
        ])
        if not isinstance(data, dict):
            return False
        return any(
            isinstance(item, dict) and item.get("body") == body
            for item in (data.get("comments") or [])
        )

    def is_issue_closed(self, issue: int) -> bool:
        data = self._gh_json([
            "issue", "view", str(issue), "--repo", self.repo, "--json", "state",
        ])
        return isinstance(data, dict) and str(data.get("state") or "").upper() == "CLOSED"

    def reviewed_on(self, pr: int, head: str, login: str) -> bool:
        """Whether `login` has a standing verdict (APPROVED or CHANGES_REQUESTED)
        on commit `head`, read across every page. A read that did not answer
        completely raises: "absent" is only ever said by a complete read, since
        the review role submits on that answer."""
        data = self._gh_json(["api", f"repos/{self.repo}/pulls/{pr}/reviews?per_page=100", "--paginate", "--slurp"])
        if not isinstance(data, list) or any(not isinstance(page, list) for page in data):
            raise RuntimeError(f"pull request {pr} reviews were not answered completely")
        return any(
            isinstance(item, dict) and isinstance(item.get("user"), dict)
            and login_eq(str(item["user"].get("login") or ""), login)
            and item.get("commit_id") == head
            and str(item.get("state") or "").upper() in ("APPROVED", "CHANGES_REQUESTED")
            for page in data for item in page
        )

    def comment(self, kind: str, number: int, body: str) -> None:
        self._gh(build_comment_args(self.repo, kind, number, body))

    def close_issue(self, number: int, reason: str, comment: str) -> None:
        self._gh(build_close_issue_args(self.repo, number, reason, comment))

    def is_expected_head_merged(
        self, pr: int, expected_head: str
    ) -> bool:
        data = self._gh_json(
            [
                "pr",
                "view",
                str(pr),
                "--repo",
                self.repo,
                "--json",
                "state,headRefOid",
            ]
        )
        return (
            isinstance(data, dict)
            and data.get("headRefOid") == expected_head
            and str(data.get("state") or "").upper() == "MERGED"
        )

    def disable_auto_merge(
        self, pr: int, expected_head: str
    ) -> bool:
        """Revoke any legacy arm before a write that can advance the PR head."""

        data = self._gh_json(
            [
                "pr",
                "view",
                str(pr),
                "--repo",
                self.repo,
                "--json",
                "autoMergeRequest,headRefOid",
            ]
        )
        if (
            not isinstance(data, dict)
            or data.get("headRefOid") != expected_head
        ):
            return False
        if data.get("autoMergeRequest") is None:
            return True
        self._gh(
            [
                "pr",
                "merge",
                str(pr),
                "--repo",
                self.repo,
                "--disable-auto",
            ]
        )
        verified = self._gh_json(
            [
                "pr",
                "view",
                str(pr),
                "--repo",
                self.repo,
                "--json",
                "autoMergeRequest,headRefOid",
            ]
        )
        return (
            isinstance(verified, dict)
            and verified.get("headRefOid") == expected_head
            and verified.get("autoMergeRequest") is None
        )

    def is_branch_updated(self, pr: int, prior_head: str) -> bool:
        data = self._gh_json(
            [
                "pr",
                "view",
                str(pr),
                "--repo",
                self.repo,
                "--json",
                "headRefOid,mergeStateStatus",
            ]
        )
        if not isinstance(data, dict):
            return False
        current_head = str(data.get("headRefOid") or "")
        return (
            bool(current_head)
            and current_head != prior_head
            and str(data.get("mergeStateStatus") or "").upper() != "BEHIND"
            and self.disable_auto_merge(pr, current_head)
        )

    def enable_auto_merge(
        self, pr: int, method: str, expected_head: str
    ) -> bool:
        """Merge only the observed, review-and-CI-qualified head.

        The historical port name remains for command-schema compatibility.
        Persistent GitHub auto-merge is deliberately not armed: its approval
        survives later pushes on repositories without last-push protection.
        """
        if not self.disable_auto_merge(pr, expected_head):
            return False
        self._gh(
            build_enable_auto_merge_args(
                self.repo, pr, method, expected_head
            )
        )
        try:
            return self.is_expected_head_merged(pr, expected_head)
        except Exception:  # noqa: BLE001 — unreadable is not confirmed
            return False

    def update_branch(self, pr: int, expected_head: str) -> bool:
        """Bring a BEHIND PR branch up to date with base (merge base into head), so
        an armed auto-merge can land it. Fail-soft: a transient 'already up to date'
        / merge-conflict race must not abort the poll — it's re-evaluated next tick."""
        try:
            if not self.disable_auto_merge(pr, expected_head):
                return False
            updated = bool(
                self._gh(
                    build_update_branch_args(
                        self.repo, pr, expected_head
                    )
                ).strip()
            )
            return updated and self.is_branch_updated(pr, expected_head)
        except Exception:
            return False


def _subprocess_runner(argv: list[str], token: str | None) -> str:
    """Default runner: invoke gh, optionally with GH_TOKEN set. Returns stdout, and
    "" on non-zero exit — the bash swallowed gh failures with 2>/dev/null so a
    transient error never stranded a label; the domain re-derives next poll.

    BUT a silently-swallowed failure once made a 422 (self-approval) look like the
    reviewer "did nothing": the agent ran, state advanced, no review appeared, no
    message. So on non-zero exit we now also write gh's stderr (and the command) to
    OUR stderr. This stays fail-soft (still returns "" — callers that care, e.g.
    submit_review, check the empty result) while making the failure visible.

    AND (V10) fail-soft is wrong for INFRA failures. A rate limit, a dead network
    or a GitHub 5xx is not an answer about the world; returning "" for those made
    a throttled tick see zero issues and zero PRs — indistinguishable from a calm
    repo, exactly when scale makes throttling likely. Those (and only those,
    matched by `classify_gh_failure`) now raise `GhUnavailable`; every other
    non-zero exit keeps returning "" so "PR not found" / "no such label" / 422
    still degrade the way their callers expect."""
    import os
    import sys

    env = dict(os.environ)
    if token:
        env["GH_TOKEN"] = token
    # Bound EVERY gh call. `gh pr view --json mergeable,mergeStateStatus` triggers a
    # server-side merge-commit computation that GitHub streams lazily; for a
    # just-merged PR whose head ref was deleted it can never settle, so gh blocks on
    # the open socket indefinitely (observed: a single `gh pr view` hung ~14 min and,
    # because the poll loop is synchronous, froze the whole review daemon — no PR
    # opened after it got reviewed). A hard timeout turns an unbounded wedge into a
    # transient error the poll re-derives next tick.
    try:
        res = subprocess.run(
            argv, capture_output=True, text=True, env=env, timeout=_GH_TIMEOUT_S
        )
    except subprocess.TimeoutExpired as exc:
        # A killed call produced NO data — the one thing it must not look like is
        # a successful read of an empty result. `timed_out=True` marks it as the
        # per-CALL infra case (this PR hangs) rather than a systemic one, which is
        # what lets `_safe_get_pr` keep isolating a single wedged `gh pr view`.
        sys.stderr.write(
            f"gh timed out (>{_GH_TIMEOUT_S:.0f}s): {' '.join(argv)}\n"
            "  killed; treating as INFRA failure (poll re-derives next tick)\n"
        )
        raise GhUnavailable(
            "network",
            f"gh timed out after {_GH_TIMEOUT_S:.0f}s: {' '.join(argv[:4])}",
            argv=argv,
            timed_out=True,
        ) from exc
    except OSError as exc:
        if exc.errno not in {errno.EAGAIN, errno.ENOMEM}:
            raise
        printable = _printable_gh_argv(argv)
        detail = str(exc) or "process creation failed"
        logged_detail = _cap_bytes(detail, GhUnavailable._MAX_DETAIL_BYTES)
        sys.stderr.write(
            f"gh failed to start: {printable}\n"
            f"  {logged_detail}\n"
        )
        raise GhUnavailable(
            "local-resource",
            detail,
            argv=argv,
        ) from exc
    if res.returncode != 0:
        # redact a token if one ever appears in argv (it shouldn't — gh reads GH_TOKEN
        # from env, not argv — but be defensive about logging command lines).
        printable = _printable_gh_argv(argv)
        detail = (res.stderr or res.stdout or "").strip()
        capability = "Upgrade to GitHub Pro or make this repository public to enable this feature."
        if (any("/rules/branches/" in arg for arg in argv)
                and detail.removeprefix("gh: ") == f"{capability} (HTTP 403)"):
            try:
                error = json.loads(res.stdout) if res.stdout.strip() else None
                if isinstance(error, list) and len(error) == 1:
                    error = error[0]
                if error is None or (isinstance(error, dict) and error.get("status") == "403"
                        and error.get("message") == capability):
                    return "[[]]"
            except ValueError:
                pass
        logged_detail = _cap_bytes(detail, GhUnavailable._MAX_DETAIL_BYTES)
        truncated = "\n  [gh stderr truncated]" if logged_detail != detail else ""
        sys.stderr.write(f"gh failed (exit {res.returncode}): {printable}\n  {logged_detail}{truncated}\n")
        # Infra? Then this is NOT an answer — raise so the caller can refuse to
        # act on a world it never actually observed. The stderr line above is
        # kept for the raise path too: some callers legitimately swallow
        # GhUnavailable (update_branch, add_blocked_by), and a swallowed infra
        # failure with no trace on stderr is the very bug this section fixes.
        kind = classify_gh_failure(detail)
        if kind is not None:
            raise GhUnavailable(
                kind,
                detail,
                argv=argv,
                retry_after_s=_retry_after(detail),
            )
        return ""
    return res.stdout


def _retry_after(detail: str) -> float | None:
    match = re.search(
        r"retry[- ]after\s*[:=]?\s*(\d+(?:\.\d+)?)",
        detail or "",
        re.IGNORECASE,
    )
    return float(match.group(1)) if match else None
