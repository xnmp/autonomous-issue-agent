"""GitHubGateway — the only module that runs `gh`.

Two halves, the usual pure/impure split:

  * PURE PARSERS (`parse_issue`, `parse_pr`, `parse_world`) — gh `--json` dicts →
    domain value objects (IssueView/PRView/Review/Check/Comment). These are the
    real-vs-fake seam the bash kept getting wrong (the fake `gh` drifting from
    real `gh`), so they are unit-tested against the documented JSON shapes.
  * IMPURE GATEWAY — runs `gh` as a chosen identity (ambient / static PAT /
    GitHub-App installation token) and applies side-effects (relabel, comment,
    close, enable-auto-merge). The identity selection reproduces the bash
    gh_fix / gh_review / gh_steward three-identity model.

Field names match what the bash queries (issue: number,title,body,labels,
comments,updatedAt,author,state; pr: number,title,body,headRefName,headRefOid,
baseRefName,labels,reviews,state,closingIssuesReferences; checks: name,state,
bucket,startedAt,link) so the mapping is mechanical and auditable.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from ..domain.labels import Label
from ..domain.world import (
    Check,
    ClosingRef,
    Comment,
    IssueView,
    PRView,
    Review,
    WorldState,
    extract_declared_paths,
)


# Hard ceiling on any single `gh` subprocess. A `gh pr view` requesting lazily
# computed fields (mergeable / mergeStateStatus) can hang indefinitely on a
# just-merged PR; without a timeout that wedges the synchronous poll loop. 120s is
# far above a healthy call (<2s) yet bounds the pathological hang.
_GH_TIMEOUT_S = 120.0

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
# slice would re-fetch the same stuck issues every poll forever. Matches the
# shape of build_steward_world's closed ai-done slice (also 100). Steady state
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


def _warn_if_list_truncated(query: str, count: int, limit: int = _LIST_LIMIT) -> None:
    """Emit a loud stderr warning when a `gh … list` call returns exactly as many
    items as it asked for. Unlike the fail-soft gh-error path (transient,
    re-derived next tick), a truncated listing recurs every poll and drops the
    same items every time until an operator raises the cap or pagination ships —
    so this is intentionally NOT swallowed. `limit` is a parameter because not
    every world-building list uses `_LIST_LIMIT` (the closed ai-pr-open cleanup
    slice is capped far lower — see `_CLOSED_CLEANUP_LIMIT`)."""
    if count == limit:
        import sys

        sys.stderr.write(
            f"WARNING: {query} returned {count} items (== --limit {limit}) — "
            "results may be TRUNCATED; oldest items silently dropped this poll. "
            "Raise the limit or add pagination.\n"
        )


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

    `kind` is one of "auth" | "rate-limit" | "network" | "server";
    `timed_out` marks the
    one sub-case that is per-CALL rather than systemic (the `_GH_TIMEOUT_S` kill
    of a single hung `gh pr view`), which `_safe_get_pr` still isolates per PR.
    """

    _HEADLINES = {
        "auth": "unavailable (authentication)",
        "rate-limit": "rate-limited",
        "network": "unreachable (network)",
        "server": "server error (5xx)",
    }

    def __init__(
        self,
        kind: str,
        detail: str,
        argv: list[str] | None = None,
        timed_out: bool = False,
        retry_after_s: float | None = None,
    ):
        self.kind = kind
        self.detail = _one_line(detail)
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
# Match the STATUS TOKEN only next to an explicit "HTTP": gh prints "HTTP 503
# (https://api.github.com/…)". A bare "502" is not enough — it could be an issue
# number in "issue #502 not found", and misreading that as an outage would skip
# ticks forever.
_SERVER_RE = re.compile(r"http[/ ]?\s*5\d\d")


def classify_gh_failure(detail: str) -> str | None:
    """Map a failed `gh` invocation's stderr/stdout to an infra kind
    ("rate-limit" | "network" | "server"), or None when the failure is an
    ordinary API answer the caller should keep treating as "" (fail-soft).

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
    return None


def no_checks_answer(argv: list[str], detail: str) -> bool:
    """True iff a failed `gh` call is `pr checks` reporting the HONEST empty
    answer: gh exits 1 with "no checks reported on the '<branch>' branch" when a
    PR simply has no checks — the one non-zero exit that IS an observation, not
    a failure. Everything else non-zero on `pr checks` means the checks were
    never read (#48). Pure so the classification is unit-testable without a
    subprocess, like `classify_gh_failure`."""
    return (
        list(argv[1:3]) == ["pr", "checks"]
        and "no checks reported" in (detail or "").lower()
    )


# ── pure parsers ──────────────────────────────────────────────────────────────
def _labels(raw: list | None) -> frozenset[Label]:
    """gh returns labels as [{"name": "ai-ready"}, …] (issue/pr view) or as bare
    strings (some fakes). Map known names to Label; silently drop unknown ones so
    a stray repo label never crashes the parse."""
    out = set()
    for item in raw or []:
        name = item.get("name") if isinstance(item, dict) else item
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


def _checks(raw: list | None) -> tuple[Check, ...]:
    return tuple(
        Check(
            name=c.get("name", "") or "",
            state=c.get("state", "") or "",
            bucket=c.get("bucket", "") or "",
            # Absent/null on a gh that predates the field, or on a check that has
            # not started — "" means unknown, and the pending deadline treats
            # unknown as "cannot fire" rather than as "infinitely old" (#30).
            started_at=c.get("startedAt") or c.get("started_at") or "",
            advisory=bool(c.get("advisory", False)),
        )
        for c in raw or []
    )


_ACTIONS_RUN_RE = re.compile(r"/actions/runs/(\d+)(?:/|$)")


def _workflow_run_id(check: object) -> int | None:
    if not isinstance(check, dict):
        return None
    match = _ACTIONS_RUN_RE.search(str(check.get("link") or ""))
    return int(match.group(1)) if match else None


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
        # Same normalisation as `issue_state_reason` (GraphQL serves REOPENED).
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
        title=d.get("title", "") or "",
        body=d.get("body", "") or "",
        labels=_labels(d.get("labels")),
        comments=_comments(d.get("comments")),
        reviews=_reviews(d.get("reviews")),
        checks=_checks(d.get("statusCheckRollup") or d.get("checks")),
        state=(d.get("state", "OPEN") or "OPEN").upper(),
        closing_issue=closing,
        changed_files=_changed_files(d.get("files")),
        mergeable=(d.get("mergeable", "") or "").upper(),
        merge_state=(d.get("mergeStateStatus", "") or "").upper(),
        is_draft=bool(d.get("isDraft", False)),
        author=((d.get("author") or {}).get("login", "") or ""),
    )


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


def parse_closing_refs(data: object) -> tuple[ClosingRef, ...]:
    """`gh pr list --json number,author,closingIssuesReferences,baseRefName` →
    one `ClosingRef` per (PR, closed issue) pair (#39).

    Flattened rather than one-per-PR because the guard asks a question about an
    ISSUE ("is anyone already fixing this?"), and a PR may close several. Total
    tolerance for shape: a null author, a missing/odd references list, an
    unparseable number — every one of them yields fewer refs, never an
    exception. This feeds a guard that REFUSES work, so a parse error must
    degrade to "no evidence of a duplicate" (dispatch, as before), never to a
    crashed world build."""
    if not isinstance(data, list):
        return ()
    out: list[ClosingRef] = []
    for p in data:
        if not isinstance(p, dict):
            continue
        try:
            number = int(p["number"])
        except (KeyError, TypeError, ValueError):
            continue
        author = ""
        if isinstance(p.get("author"), dict):
            author = str(p["author"].get("login") or "")
        base = str(p.get("baseRefName") or "")
        refs = p.get("closingIssuesReferences")
        if not isinstance(refs, list):
            continue
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            try:
                issue = int(ref["number"])
            except (KeyError, TypeError, ValueError):
                continue
            out.append(
                ClosingRef(pr=number, issue=issue, author=author, base_ref_name=base)
            )
    return tuple(out)


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


def build_time() -> str:
    """The stamp every world builder puts in `WorldState.now`. The domain may not
    read a clock, so this is the one place a poll's notion of "now" is taken —
    and it is taken once per build, so every time-based gate in a tick judges
    against the same instant."""
    return datetime.now(timezone.utc).isoformat()


def parse_world(issues: list[dict], prs: list[dict], now: str = "") -> WorldState:
    return WorldState(
        issues=tuple(parse_issue(i) for i in issues),
        prs=tuple(parse_pr(p) for p in prs),
        now=now,
    )


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
) -> list[str] | None:
    if not remove and not add:
        return None
    args = [kind, "edit", str(number), "--repo", repo]
    if remove:
        args += ["--remove-label", remove]
    if add:
        args += ["--add-label", add]
    return args


def build_comment_args(repo: str, kind: str, number: int, body: str) -> list[str]:
    return [kind, "comment", str(number), "--repo", repo, "--body", body]


def build_close_issue_args(repo: str, number: int, reason: str, comment: str) -> list[str]:
    args = ["issue", "close", str(number), "--repo", repo, "--reason", reason]
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
        "--auto",
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


def build_blocker_id_args(repo: str, blocker: int) -> list[str]:
    return ["api", f"repos/{repo}/issues/{blocker}", "--jq", ".id"]


def build_add_blocked_by_args(repo: str, issue: int, blocker_id: int) -> list[str]:
    return [
        "api", f"repos/{repo}/issues/{issue}/dependencies/blocked_by",
        "--method", "POST", "-F", f"issue_id={blocker_id}",
    ]


# ── recurrence context ──────────────────────────────────────────────────────
@dataclass(frozen=True)
class MergedRef:
    """One issue the harness has already shipped: the issue, the PR that closed
    it, and when. The unit of `recently_merged_issues` — the "did this come back?"
    reference set (harness #10)."""

    issue: int
    pr: int
    title: str = ""
    merged_at: str = ""


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
        review_pr_label: str = "ai-pr-open",
        review_adopt_unlabeled: bool = False,
    ):
        self.repo = repo
        self.identity = identity or Identity()
        self._run = runner or _subprocess_runner
        # PR label the review daemon selects on (see build_review_world). Default
        # matches the harness pipeline; an external-repo reviewer passes "ai-review".
        self.review_pr_label = review_pr_label
        # When true, build_review_world ALSO fetches open PRs carrying no trigger
        # label, so decide_review can adopt rogue (non-pipeline) PRs into review.
        self.review_adopt_unlabeled = review_adopt_unlabeled

    def _gh(self, args: list[str]) -> str:
        # Guard EVERY call, not just the _gh_json ones: a list subcommand read as
        # raw text (`--jq`) truncates just as silently (review C2).
        _require_list_limit(args)
        return self._run(["gh", *args], self.identity.env_token())

    def _gh_json(self, args: list[str]) -> object:
        out = self._gh(args)
        return json.loads(out) if out.strip() else None

    # ── reads ──
    def list_issue_numbers(
        self, label: str, state: str = "open", limit: int = _LIST_LIMIT
    ) -> list[int]:
        data = self._gh_json(
            ["issue", "list", "--repo", self.repo, "--label", label,
             "--state", state, "--json", "number", "--limit", str(limit)]
        ) or []
        _warn_if_list_truncated(
            f"gh issue list --label {label} --state {state}", len(data), limit
        )
        return [int(i["number"]) for i in data]

    def get_issue(self, number: int) -> IssueView:
        d = self._gh_json(
            ["issue", "view", str(number), "--repo", self.repo, "--json",
             "number,title,body,labels,comments,updatedAt,author,state,stateReason"]
        )
        return parse_issue(d)  # type: ignore[arg-type]

    def change_signature(self) -> str:
        """Cheapest possible "did anything change?" probe: number@updated_at of
        the single most-recently-updated issue-or-PR (the REST issues list
        includes PRs). One REST point — a pool the GraphQL-backed world builds
        never touch. "" on an empty repo or a benignly-failed call; callers must
        read "" as "assume changed" (fail open), never as "no change". Infra
        failures raise GhUnavailable like every other read here."""
        out = self._gh([
            "api",
            f"repos/{self.repo}/issues?state=all&sort=updated"
            "&direction=desc&per_page=1",
            "--jq", r'.[0] | "\(.number)@\(.updated_at)"',
        ])
        return out.strip()

    def merge_gate_protection(self, base_branch: str) -> bool:
        """True iff the base branch's protection makes a merge conditional on
        BOTH ≥1 approving review AND ≥1 required status check — the precondition
        for arming auto-merge at PR creation (FixConfig.arm_on_open). Anything
        less reads False: on a base where GitHub does not hold the gate, early
        arming would merge unreviewed work. Fail-closed on every ambiguity —
        404 (no protection), 403 (token cannot read the admin endpoint), or
        unparseable output all mean "no verified gate"."""
        out = self._gh(["api", f"repos/{self.repo}/branches/{base_branch}/protection"])
        if not out.strip():
            return False
        try:
            d = json.loads(out)
        except ValueError:
            return False
        if not isinstance(d, dict):
            return False
        reviews = (d.get("required_pull_request_reviews") or {}).get(
            "required_approving_review_count", 0
        ) or 0
        rsc = d.get("required_status_checks") or {}
        contexts = rsc.get("contexts") or [
            c.get("context") for c in (rsc.get("checks") or []) if isinstance(c, dict)
        ]
        return int(reviews) >= 1 and any(contexts)

    def open_blockers(self, number: int) -> tuple[int, ...]:
        """Open GitHub-native ``blocked by`` issue numbers for ``number``.

        dependency. Two-step, cheapest-first: the issue object embeds an
        `issue_dependencies_summary` (no extra call) whose `blocked_by` is 0 when
        nothing is declared → return False immediately (the common case). Only if
        it's non-zero do we fetch the blocked_by LIST and check each blocker's
        state — because the summary count may include already-CLOSED blockers, and
        a closed blocker no longer blocks. Fail-soft: any API error → False (never
        strand a dispatchable issue on a transient dependency-read failure)."""
        try:
            summary = self._gh_json(
                ["api", f"repos/{self.repo}/issues/{number}",
                 "--jq", ".issue_dependencies_summary.blocked_by // 0"]
            )
            if not summary or int(summary) == 0:  # type: ignore[arg-type]
                return ()
            blockers = self._gh_json(
                ["api", f"repos/{self.repo}/issues/{number}/dependencies/blocked_by",
                 "--jq", "[.[] | select((.state | ascii_downcase) == \"open\") | .number]"]
            ) or []
            return tuple(
                int(value)
                for value in blockers  # type: ignore[union-attr]
                if isinstance(value, int) and not isinstance(value, bool)
            )
        except GhUnavailable:
            # Fail-soft covers a genuine API ANSWER (endpoint absent, 404, odd
            # shape). An infra failure answered nothing, and "False" here is not
            # neutral — it says "not blocked", which DISPATCHES an issue that may
            # well be blocked. Propagate: skipping the tick is the cheap mistake.
            raise
        except Exception:
            return ()

    def has_open_blocker(self, number: int) -> bool:
        """Compatibility predicate over the structured blocker query."""
        return bool(self.open_blockers(number))

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
        base = ("number,title,body,headRefName,headRefOid,baseRefName,labels,"
                "comments,reviews,state,closingIssuesReferences,files,isDraft,author")
        fields = base + (",mergeable,mergeStateStatus" if mergeability else "")
        d = self._gh_json(
            ["pr", "view", str(number), "--repo", self.repo, "--json", fields]
        )
        pr = parse_pr(d)  # type: ignore[arg-type]
        if not checks:
            # Never READ ≠ read-as-empty: a PR listed non-OPEN but re-opened
            # between the list and this view would otherwise arrive
            # state=OPEN, checks=(), checks_known=True → honest NONE →
            # mergeable. No consumer reads checks for the non-open PRs this
            # arm is for, so UNKNOWN costs nothing (#48 review F3).
            return replace(pr, checks_known=False)
        # checks come from a separate endpoint (bash `pr checks`).
        found = self._gh_json(
            ["pr", "checks", str(number), "--repo", self.repo,
             "--json", "name,state,bucket,startedAt,link"]
        )
        if found is None:
            # gh failed non-infra (infra raised GhUnavailable inside the runner;
            # the honest no-checks exit came back as "[]"). The checks were NOT
            # read this tick — () would parse as honest NONE, which decide_fix
            # treats like OK and could arm auto-merge unverified (#48). Say so:
            # the one failure the runner silences (App-token statusCheckRollup)
            # used to be harmless noise, but UNKNOWN holds the merge gates, and
            # a silent permanent stall is not diagnosable from the logs.
            import sys
            sys.stderr.write(
                f"gh pr checks failed for PR #{number} — checks UNKNOWN this "
                "tick; merge gates hold until a successful read\n"
            )
            return replace(pr, checks_known=False)
        raw_checks = found if isinstance(found, list) else []
        run_conclusions: dict[int, str] = {}
        for check in raw_checks:
            if not isinstance(check, dict) or check.get("bucket") != "fail":
                continue
            run_id = _workflow_run_id(check)
            if run_id is None or run_id in run_conclusions:
                continue
            try:
                conclusion = self._gh(
                    ["api", f"repos/{self.repo}/actions/runs/{run_id}",
                     "--jq", ".conclusion"]
                )
            except Exception:  # optional enrichment: unknown stays a real failure
                conclusion = ""
            run_conclusions[run_id] = conclusion.strip().lower()

        enriched = []
        advisory_names = []
        for check in raw_checks:
            if not isinstance(check, dict):
                continue
            run_id = _workflow_run_id(check)
            advisory = (
                check.get("bucket") == "fail"
                and run_id is not None
                and run_conclusions.get(run_id) == "success"
            )
            enriched.append({**check, "advisory": advisory})
            if advisory:
                advisory_names.append(str(check.get("name") or "unnamed"))
        if advisory_names:
            import sys
            sys.stderr.write(
                f"PR #{number}: advisory continue-on-error checks ignored: "
                f"{', '.join(advisory_names)}\n"
            )
        return replace(pr, checks=_checks(enriched))

    def _safe_get_pr(
        self, number: int, *, mergeability: bool = True, checks: bool = True
    ) -> PRView | None:
        """`get_pr` that isolates a single PR's failure. A hung/empty `gh pr view`
        (→ parse_pr(None) → AttributeError) or any transient read error must NOT
        abort the whole world-build: previously one such PR raised out of the
        generator in build_*_world, killing the entire poll pass so EVERY later PR
        (sorted after it) went unseen — a just-merged PR whose mergeable field hung
        stranded newer PRs from review. Here a failed PR drops to None (logged) and
        the poll re-derives it next tick.

        The ONE thing it must not isolate is a systemic `GhUnavailable`: a rate
        limit or a 5xx fails every subsequent PR fetch identically, so per-item
        degradation would silently rebuild the exact empty world V10 is about.
        Those propagate and the tick is skipped. A `timed_out` GhUnavailable is
        the opposite — it is this ONE PR wedging the socket (the incident above,
        which recurs every poll while the PR exists), so it stays isolated."""
        try:
            return self.get_pr(number, mergeability=mergeability, checks=checks)
        except GhUnavailable as exc:
            if not exc.timed_out:
                raise
            self._skip_item("PR", number, exc)
            return None
        except Exception as exc:  # noqa: BLE001 — deliberately broad: one PR ≠ whole pass
            self._skip_item("PR", number, exc)
            return None

    def _safe_get_issue(self, number: int, *, blocker_gate: bool = False) -> IssueView | None:
        """`get_issue` that isolates a single ISSUE's failure — the exact twin of
        `_safe_get_pr` above, and for the exact same recorded incident on the other
        item type (review M19). `parse_issue({})` raises KeyError and
        `parse_issue(None)` AttributeError, so ONE unreadable issue (transferred to
        another repo, converted to a discussion, locked, or simply a `gh issue view`
        that returns empty) raised straight out of the world-build generator and
        killed the whole fix/steward/triage poll — every tick, permanently, because
        the bad issue is still there next tick. Here it drops to None (logged) and
        every OTHER issue still reaches the domain.

        Same one exception as the PR twin: a non-`timed_out` `GhUnavailable`
        (rate limit, 5xx) fails every subsequent fetch identically, so isolating it
        would silently rebuild the empty world V10 is about — those propagate and
        the tick is skipped.

        `blocker_gate` folds the ai-ready dependency read into the SAME isolation
        boundary: a `gh api …/dependencies/blocked_by` that wedges on one issue is
        that issue's problem, and dropping the issue for a tick is strictly safer
        than either dispatching it ungated or killing the poll."""
        try:
            issue = self.get_issue(number)
            if blocker_gate:
                blockers = self.open_blockers(number)
                issue = replace(
                    issue,
                    blocked_by_open=bool(blockers),
                    open_blocker_numbers=blockers,
                )
            return issue
        except GhUnavailable as exc:
            if not exc.timed_out:
                raise
            self._skip_item("issue", number, exc)
            return None
        except Exception as exc:  # noqa: BLE001 — deliberately broad: one issue ≠ whole pass
            self._skip_item("issue", number, exc)
            return None

    @staticmethod
    def _skip_item(kind: str, number: int, exc: BaseException) -> None:
        import sys
        sys.stderr.write(
            f"skip {kind} #{number} this poll ({type(exc).__name__}: {str(exc)[:120]}) "
            "— re-derived next tick\n"
        )

    def list_pr_numbers(self, label: str, state: str = "open") -> list[int]:
        return [n for n, _ in self.list_pr_states(label, state)]

    def list_pr_states(self, label: str, state: str = "open") -> list[tuple[int, str]]:
        """(number, state) pairs, so world builders can pick per-PR enrichment
        depth BEFORE fetching. The `--state all` label list serves STALE search
        results for closed PRs (merged PRs unlabeled a full day earlier still
        listed, observed 2026-07-27), so the list's membership cannot be
        trusted to mean "needs full enrichment" — but its `state` field is the
        PR object's own, and a MERGED/CLOSED PR needs neither `mergeable`
        (a hang-class lazy computation) nor a `gh pr checks` call. Nine stale
        merged PRs were costing ~18 wasted gh calls per fix tick, growing with
        every merge."""
        data = self._gh_json(
            ["pr", "list", "--repo", self.repo, "--label", label,
             "--state", state, "--json", "number,state", "--limit", str(_LIST_LIMIT)]
        ) or []
        _warn_if_list_truncated(f"gh pr list --label {label} --state {state}", len(data))
        return [(int(p["number"]), str(p.get("state", ""))) for p in data]

    def list_all_open_pr_numbers(self) -> list[int]:
        """Every OPEN PR number, no label filter — feeds rogue-PR adoption."""
        data = self._gh_json(
            ["pr", "list", "--repo", self.repo, "--state", "open",
             "--json", "number", "--limit", str(_LIST_LIMIT)]
        ) or []
        _warn_if_list_truncated("gh pr list --state open", len(data))
        return [int(p["number"]) for p in data]

    def open_closing_refs(self) -> tuple[ClosingRef, ...]:
        """Every OPEN PR's closing-issue references, repo-wide (#39).

        ONE `gh pr list` per full fix world build — no label filter and no
        per-PR fetch, because the guard's whole subject is PRs the harness never
        labelled (an operator's own branch, a contributor's PR). Widening the
        EXISTING per-PR enrichment to all open PRs would have cost two calls per
        PR per poll and, worse, would have changed what `world.prs` means for the
        path-overlap hold, the reaper and auto-merge — a large behavioural blast
        radius for one boolean. This carries only the four fields the guard
        reads.

        Fail-soft to `()`: a guard that refuses work must never be the thing
        that crashes a poll, and an empty result reads as "no duplicate known",
        which is the pre-#39 behaviour. `GhUnavailable` still propagates — it is
        raised inside `_gh_json` and means the whole world is unreadable, which
        is `Daemon.tick`'s call, not this method's."""
        try:
            data = self._gh_json(
                ["pr", "list", "--repo", self.repo, "--state", "open",
                 "--json", "number,author,closingIssuesReferences,baseRefName",
                 "--limit", str(_LIST_LIMIT)]
            ) or []
        except GhUnavailable:
            raise
        except Exception:  # noqa: BLE001 — see the docstring
            return ()
        _warn_if_list_truncated("gh pr list --state open (closing refs)", len(data))  # type: ignore[arg-type]
        return parse_closing_refs(data)

    def build_world(self) -> WorldState:
        """Fetch the slice the daemons need: ai-ready/ai-working/ai-blocked/
        ai-pr-open issues (the last one open AND closed — see below) and
        ai-pr-open PRs (open + recently merged/closed for cleanup). Composed
        from the typed reads above so it's exercised by the gateway tests.

        `GhUnavailable` PROPAGATES out of here (all four world builders): a
        partially-read world is not a world, and the caller (`Daemon.tick`) is
        the only layer that can decide "skip this pass". Note the list calls run
        FIRST, so a throttled poll aborts before any per-item fetch — the empty
        world of V10 is unreachable rather than merely unlikely."""
        issue_numbers: set[int] = set()
        ready_numbers: set[int] = set()
        for lbl in ("ai-ready", "ai-working", "ai-blocked", "ai-pr-open"):
            nums = self.list_issue_numbers(lbl)
            issue_numbers.update(nums)
            if lbl == "ai-ready":
                ready_numbers.update(nums)
        # …and the CLOSED `ai-pr-open` issues (#28). The merge cleanup arm is
        # gated on OBSERVING the issue still carrying ai-pr-open in the same
        # snapshot as its merged PR (`_issue_carries_ai_pr_open`, review P1R-15
        # — it must not stomp a fresh dispatch). But `Fixes #n` is MANDATORY in
        # every harness PR body (check-pr.sh), so GitHub closes the issue at the
        # instant of merge; by the next poll the issue was in neither the open
        # slice above nor any closed slice, the gate read False, and the
        # ai-pr-open→ai-done relabel that would have made it visible again never
        # ran. Self-reinforcing: zero issues ever reached ai-done, the #10
        # recurrence guardrail could never fire, and RemoveWorktree/ClearIssueState
        # never ran (24 stale worktrees / 2.3 GB observed). Fetching the closed
        # half makes the stuck state OBSERVABLE without touching the gate; the
        # REPAIR is `domain.fix.orphaned_cleanup_actions`, which is also what
        # drains this query (an unlabelled merged PR is invisible, so the
        # PR-driven arm alone could never reach a pre-existing backlog).
        # The OPEN half of the same label is in the loop above for the
        # symmetric reason: a CLOSED-unmerged PR leaves its issue open+ai-pr-open,
        # and the requeue-to-ai-ready arm was invisible in exactly the same way.
        issue_numbers.update(
            self.list_issue_numbers("ai-pr-open", "closed", _CLOSED_CLEANUP_LIMIT)
        )
        pr_states: dict[int, str] = {}
        for state in ("open", "all"):
            for n, s in self.list_pr_states("ai-pr-open", state):
                pr_states.setdefault(n, s)
        # Only ai-ready issues are gated on dependencies (pick_ready_issue), so
        # only they pay the blocked-by read — an ai-working/blocked issue never
        # needs it. Cheapest-first inside has_open_blocker keeps it ~1 call each.
        issues = tuple(
            i for n in sorted(issue_numbers)
            if (i := self._safe_get_issue(n, blocker_gate=n in ready_numbers)) is not None
        )
        # Non-OPEN PRs (the stale --state all tail) get the cheap fetch: no
        # mergeable (hang-class) and no checks call — see list_pr_states.
        prs = tuple(
            p for n in sorted(pr_states)
            if (p := self._safe_get_pr(
                n,
                mergeability=pr_states[n] == "OPEN",
                checks=pr_states[n] == "OPEN",
            )) is not None
        )
        # One extra list call, and only on a FULL build (a probe-gated tick never
        # reaches here). Fetched AFTER the per-item enrichment so a duplicate
        # guard can never be the reason a world build fails late: by this point
        # everything else is already in hand.
        return WorldState(
            issues=issues, prs=prs, closing_refs=self.open_closing_refs(),
            now=build_time(),
        )

    def build_review_world(self) -> WorldState:
        """The review daemon's slice: ONLY PRs carrying `self.review_pr_label` (open
        + all, the latter for merged/closed session cleanup). No issues — the reviewer
        never reads them. Used instead of `build_world` so the reviewer can be pointed
        at an EXTERNAL repo (which has none of the harness's issue labels) and trigger
        on a configurable PR tag. `gh pr list --label <missing>` returns [] (exit 0),
        so an absent label degrades to "nothing to review" rather than crashing."""
        pr_states: dict[int, str] = {}
        for state in ("open", "all"):
            for n, s in self.list_pr_states(self.review_pr_label, state):
                pr_states.setdefault(n, s)
        # Rogue-PR adoption: also pull EVERY open PR (no label filter) so a PR that
        # never went through the pipeline (opened directly, no trigger label) is
        # visible to decide_review, which labels+adopts it. Draft/own-bot/opt-out
        # filtering is the domain's job (it has the PRView fields); the adapter just
        # widens the slice. Merged/closed still come only via the label (no need to
        # adopt a closed rogue PR).
        if self.review_adopt_unlabeled:
            for n in self.list_all_open_pr_numbers():
                pr_states.setdefault(n, "OPEN")
        # mergeability=False: the reviewer never reads mergeable/merge_state (only the
        # fixer's rebase logic does), and requesting them can hang gh indefinitely on a
        # just-merged PR — which previously froze the whole review poll. Fetch without
        # them so a comment can always be left. Non-OPEN PRs (the stale --state all
        # tail, see list_pr_states) also skip the checks call — session cleanup
        # reads only number/state/labels.
        prs = tuple(
            p for n in sorted(pr_states)
            if (p := self._safe_get_pr(
                n, mergeability=False, checks=pr_states[n] == "OPEN"
            )) is not None
        )
        return WorldState(issues=(), prs=prs, now=build_time())

    def build_triage_world(self) -> WorldState:
        """All OPEN issues (the steward triages untriaged ones — those carrying no
        lifecycle label — so it must see every open issue, not just labelled ones)
        plus open PR metadata for the steward's ADR-citation reconciler."""
        nums = self._gh_json(
            ["issue", "list", "--repo", self.repo, "--state", "open",
             "--json", "number", "--limit", str(_LIST_LIMIT)]
        ) or []
        _warn_if_list_truncated("gh issue list --state open", len(nums))
        issues = tuple(
            i for n in sorted(int(x["number"]) for x in nums)
            if (i := self._safe_get_issue(n)) is not None
        )
        return WorldState(
            issues=issues,
            prs=self._open_steward_prs(),
            now=build_time(),
        )

    def _open_steward_prs(self) -> tuple[PRView, ...]:
        """Open PR descriptions/files/comments, without mergeability or checks."""

        rows = self._gh_json(
            [
                "pr",
                "list",
                "--repo",
                self.repo,
                "--state",
                "open",
                "--json",
                "number",
                "--limit",
                str(_LIST_LIMIT),
            ]
        ) or []
        _warn_if_list_truncated("gh pr list --state open (steward)", len(rows))
        return tuple(
            pr
            for number in sorted(int(row["number"]) for row in rows)
            if (
                pr := self._safe_get_pr(
                    number,
                    mergeability=False,
                    checks=False,
                )
            )
            is not None
        )

    def build_steward_world(self) -> WorldState:
        """Triage world (all open issues) PLUS recently-CLOSED ai-done issues, so
        the Jira mirror can still post the final Done transition (a merged issue is
        often closed before the steward next polls — build_triage_world's open-only
        slice would miss it). The extra closed-issue query only runs when the
        steward selects this builder (jira on), so a jira-off deployment pays
        nothing."""
        nums: dict[int, None] = {}
        open_issues = self._gh_json(
            ["issue", "list", "--repo", self.repo, "--state", "open",
             "--json", "number", "--limit", str(_LIST_LIMIT)]
        ) or []
        _warn_if_list_truncated("gh issue list --state open", len(open_issues))
        for i in open_issues:
            nums[int(i["number"])] = None
        for i in (self._gh_json(
            ["issue", "list", "--repo", self.repo, "--state", "closed",
             "--label", "ai-done", "--json", "number", "--limit", "100"]
        ) or []):
            nums[int(i["number"])] = None
        issues = tuple(
            i for n in sorted(nums) if (i := self._safe_get_issue(n)) is not None
        )
        return WorldState(
            issues=issues,
            prs=self._open_steward_prs(),
            now=build_time(),
        )

    def pr_for_issue(self, issue: int) -> int | None:
        """Open PR whose head branch matches ^(feat|fix|chore|refactor)/<issue>-.
        Port of agent/lib.sh:pr_for_issue (closing-ref graph is default-branch only)."""
        data = self._gh_json(
            ["pr", "list", "--repo", self.repo, "--state", "open",
             "--json", "number,headRefName", "--limit", str(_LIST_LIMIT)]
        ) or []
        _warn_if_list_truncated("gh pr list --state open (pr_for_issue)", len(data))
        import re

        pat = re.compile(rf"^(feat|fix|chore|refactor)/{issue}-")
        for p in data:
            if pat.match(p.get("headRefName", "")):
                return int(p["number"])
        return None

    def issue_for_pr(self, pr: int) -> int | None:
        p = self.get_pr(pr)
        if p.closing_issue is not None:
            return p.closing_issue
        from ..domain.refs import issue_from_branch

        return issue_from_branch(p.head_ref_name)

    def pr_diff(self, pr: int) -> str:
        return self._gh(["pr", "diff", str(pr), "--repo", self.repo])

    # ── prompt inputs for the two repair legs (review M13 / D6) ──
    # Both of these feed a prompt and nothing else, so both fail SOFT to "": the
    # CI-fix / address-review dispatch must still happen with a weaker prompt (the
    # agent can rediscover the failure itself) rather than not happen at all. A log
    # fetch may never kill a dispatch. The one exception is `MissingListLimit`,
    # which is a bug in this module, not a runtime condition (review C2).
    def failed_run_id(self, pr: int, *, branch: str = "") -> int | None:
        """databaseId of the newest FAILED Actions run on this PR's head branch,
        or None (green branch; runs exist but none failed; no Actions run at all
        — a red PR whose only red mark is an external status context).

        Extracted from `failed_run_log` so the CI-fix prompt and the free re-run
        (#38) can never disagree about WHICH run failed. Keyed on the REAL head
        branch for the reason the bash comment gives: `gh run list` has no PR
        filter, and deriving the branch from the issue number is what the bash
        got wrong first. Raises nothing but `MissingListLimit` — see the section
        comment above."""
        head = branch or self.get_pr(pr, mergeability=False).head_ref_name
        if not head:
            return None
        runs = self._gh_json(
            ["run", "list", "--repo", self.repo, "--branch", head,
             "--status", "failure", "--json", "databaseId", "--limit", "1"]
        ) or []
        run_id = runs[0].get("databaseId") if runs else None  # type: ignore[union-attr]
        return int(run_id) if run_id else None

    def rerun_failed_checks(self, pr: int, *, branch: str = "") -> int | None:
        """`gh run rerun <id> --failed` on this PR's newest failed run (#38).
        Returns the run id re-run, or None if there was nothing to re-run or the
        call failed.

        Fails SOFT by contract, and the caller (the `RerunFailedChecks` arm of
        the Executor) marks the head's allowance as spent BEFORE getting here —
        so "no Actions run found" (external status contexts, a status with no
        run behind it) and "gh refused" both degrade to the paid FIX_CI leg on
        the next poll, which is exactly the pre-#38 behaviour. The failure a
        soft return must NOT become is a retry loop, and it cannot: the marker
        is already written."""
        try:
            run_id = self.failed_run_id(pr, branch=branch)
            if run_id is None:
                return None
            self._gh(["run", "rerun", str(run_id), "--repo", self.repo, "--failed"])
            return run_id
        except MissingListLimit:
            raise
        except Exception:  # noqa: BLE001 — see the docstring
            return None

    def failed_run_log(
        self, pr: int, *, branch: str = "", lines: int = _FAILED_LOG_LINES
    ) -> str:
        """Tail of the newest FAILED Actions run on this PR's head branch.

        Port of agent/fix-ci.sh:39-42 (`gh run list --branch … --status failure`
        → `gh run view <id> --log-failed | tail -150`). Keyed on the REAL head
        branch, as the bash comment insists: `gh run list` has no PR filter, and
        deriving the branch from the issue number is what the bash got wrong
        first. Pass `branch` when the caller already holds the PRView, to save the
        `gh pr view` round-trip."""
        try:
            run_id = self.failed_run_id(pr, branch=branch)
            if run_id is None:
                return ""  # green branch, or runs exist but none failed
            log = self._gh(
                ["run", "view", str(run_id), "--repo", self.repo, "--log-failed"]
            )
            return _tail(log, lines, max_bytes=_FAILED_LOG_BYTES)
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

    # ── recurrence context (harness #10) ──
    # Both of these exist to record that a shipped fix came back. Both are pure
    # CONTEXT — advisory label/comment/journal, never a reopen or a dispatch — so
    # both fail SOFT: a recurrence check that raises would take the steward's
    # triage leg (or its whole poll) down with it, trading working triage for a
    # metric. `MissingListLimit` still propagates; that is a bug here, not a
    # runtime condition (review C2).
    def recently_merged_issues(self, author: str, limit: int = 30) -> tuple[MergedRef, ...]:
        """The issues `author`'s recently-MERGED PRs closed, newest PR first.

        One entry per referenced issue (a PR closing two issues yields two), and
        the issue's OWN number/title are used — the PR title describes the change,
        the issue title describes the report, and a recurrence classifier is
        matching a new report against old REPORTS.

        `limit` is the PR page size and is deliberately small (K≈30 — the recent
        past is what can plausibly recur), so `_warn_if_list_truncated` is NOT
        called: truncation here is the intent, not a silent loss.
        """
        if not author.strip():
            return ()
        try:
            data = self._gh_json(
                ["pr", "list", "--repo", self.repo, "--state", "merged",
                 "--author", author, "--json",
                 "number,title,mergedAt,closingIssuesReferences",
                 "--limit", str(limit)]
            ) or []
            out: list[MergedRef] = []
            for p in data:  # type: ignore[union-attr]
                pr = int(p.get("number") or 0)
                merged_at = str(p.get("mergedAt") or "")
                for ref in (p.get("closingIssuesReferences") or []):
                    number = ref.get("number")
                    if number:
                        out.append(MergedRef(
                            issue=int(number), pr=pr,
                            title=str(ref.get("title") or ""), merged_at=merged_at,
                        ))
            return tuple(out)
        except MissingListLimit:
            raise
        except Exception:  # noqa: BLE001 — see the section comment
            return ()

    def issue_state_reason(self, number: int) -> str:
        """An issue's `stateReason`, lower-cased ("reopened" | "completed" |
        "not_planned" | ""). Lower-cased because GitHub serves the GraphQL enum
        (REOPENED), and `parse_issue` already normalises `state` the same way —
        callers should compare against one casing, not two.

        This is the DETERMINISTIC half of the recurrence signal. It is needed at
        all because the harness merges to a non-default base branch, so GitHub
        never auto-closes the referenced issue and the harness closes it itself:
        "ai-done and open again" is therefore ambiguous (it can simply mean the
        close has not happened yet), and only `reopened` says a human deliberately
        put it back. "" on any failure — an unreadable state reason means "no
        evidence of a recurrence", which is the silent, correct default.
        """
        try:
            d = self._gh_json(
                ["issue", "view", str(number), "--repo", self.repo,
                 "--json", "stateReason"]
            )
            if not isinstance(d, dict):
                return ""
            return str(d.get("stateReason") or "").lower()
        except Exception:  # noqa: BLE001 — see the section comment
            return ""

    def submit_review(
        self, pr: int, event: str, body: str, head: str
    ) -> bool:
        """POST a review. Returns True iff gh succeeded. A failed submit (e.g. 422
        'can't approve your own PR' when the identity collides with the author, or a
        403 from a misconfigured App) returns False so the caller does NOT record a
        phantom 'reviewed' state — the daemon retries the same PR next poll. The
        underlying gh failure is surfaced on stderr by `_subprocess_runner`. gh's
        `api ... POST` prints the created-review JSON on success (non-empty)."""
        out = self._gh(
            build_submit_review_args(self.repo, pr, event, body, head)
        )
        return bool(out.strip())

    # ── writes ──
    def relabel(self, kind: str, number: int, remove: str | None, add: str | None) -> None:
        args = build_relabel_args(self.repo, kind, number, remove, add)
        if args is not None:
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

    def is_issue_closed(self, number: int) -> bool:
        data = self._gh_json([
            "issue", "view", str(number), "--repo", self.repo, "--json", "state",
        ])
        return isinstance(data, dict) and str(data.get("state") or "").upper() == "CLOSED"

    def has_review(self, pr: int, event: str, body: str, head: str) -> bool:
        state = {
            "APPROVE": "APPROVED",
            "REQUEST_CHANGES": "CHANGES_REQUESTED",
            "COMMENT": "COMMENTED",
        }.get(event.upper(), event.upper())
        data = self._gh_json(
            [
                "api",
                f"repos/{self.repo}/pulls/{pr}/reviews",
                "--paginate",
                "--slurp",
            ]
        )
        if not isinstance(data, list):
            return False
        reviews = [
            item
            for page in data
            for item in (page if isinstance(page, list) else [page])
        ]
        return any(
            isinstance(item, dict)
            and str(item.get("state") or "").upper() == state
            and item.get("body") == body
            and item.get("commit_id") == head
            for item in reviews
        )

    def is_auto_merge_enabled_or_merged(
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
                "autoMergeRequest,state,headRefOid",
            ]
        )
        return (
            isinstance(data, dict)
            and data.get("headRefOid") == expected_head
            and (
                data.get("autoMergeRequest") is not None
                or str(data.get("state") or "").upper() == "MERGED"
            )
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
        return (
            bool(data.get("headRefOid"))
            and data.get("headRefOid") != prior_head
            and str(data.get("mergeStateStatus") or "").upper() != "BEHIND"
        )

    def max_run_attempt(self, head: str) -> int | None:
        data = self._gh_json(
            [
                "api",
                f"repos/{self.repo}/actions/runs?head_sha={head}&per_page=100",
            ]
        )
        if not isinstance(data, dict) or not isinstance(
            data.get("workflow_runs"), list
        ):
            return None
        attempts = [
            int(item.get("run_attempt") or 0)
            for item in data["workflow_runs"]
            if isinstance(item, dict)
        ]
        return max(attempts, default=0)

    def has_blocked_by(self, issue: int, blocker: int) -> bool:
        data = self._gh_json(
            [
                "api",
                f"repos/{self.repo}/issues/{issue}/dependencies/blocked_by",
                "--jq",
                "[.[].number]",
            ]
        )
        return isinstance(data, list) and blocker in data

    def comment(self, kind: str, number: int, body: str) -> None:
        self._gh(build_comment_args(self.repo, kind, number, body))

    def close_issue(self, number: int, reason: str, comment: str) -> None:
        self._gh(build_close_issue_args(self.repo, number, reason, comment))

    def enable_auto_merge(
        self, pr: int, method: str, expected_head: str
    ) -> bool:
        """`gh pr merge --auto`. Returns True iff GitHub actually armed
        auto-merge — or merged the PR outright (gh merges immediately when the
        PR is already CLEAN). Success is VERIFIED with a follow-up read, not
        inferred from stdout: this command prints nothing useful on either
        outcome, and on a base branch with no required-check protection the
        enablePullRequestAutoMerge mutation fails (GraphQL: "Branch does not
        have required protected branch rules") while an already-green PR merges
        fine. Observed 2026-07-26: the arm failed on a just-pushed PR (checks
        NONE), the marker was written anyway, and #537 sat APPROVED+green+CLEAN
        but unmergeable-by-harness forever — armed-in-state, never
        armed-on-GitHub. The caller must key the marker on this return."""
        self._gh(
            build_enable_auto_merge_args(
                self.repo, pr, method, expected_head
            )
        )
        try:
            return self.is_auto_merge_enabled_or_merged(pr, expected_head)
        except Exception:  # noqa: BLE001 — unreadable is not confirmed
            return False

    def update_branch(self, pr: int, expected_head: str) -> bool:
        """Bring a BEHIND PR branch up to date with base (merge base into head), so
        an armed auto-merge can land it. Fail-soft: a transient 'already up to date'
        / merge-conflict race must not abort the poll — it's re-evaluated next tick."""
        try:
            return bool(
                self._gh(
                    build_update_branch_args(
                        self.repo, pr, expected_head
                    )
                ).strip()
            )
        except Exception:
            return False

    def add_blocked_by(self, issue: int, blocker: int) -> None:
        """Create a native 'issue is blocked_by blocker' dependency. The POST wants
        the blocker's NUMERIC id (not its issue number), so resolve that first.
        Fail-soft: a missing blocker, an already-existing link (422), or any API
        error is logged and swallowed — a dependency-wiring hiccup must never abort
        the steward poll (the link is re-attempted next triage if still needed)."""
        try:
            blocker_id = self._gh_json(
                build_blocker_id_args(self.repo, blocker)
            )
            if not blocker_id:
                return
            self._gh(build_add_blocked_by_args(self.repo, issue, int(blocker_id)))  # type: ignore[arg-type]
        except Exception:
            return


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
    if res.returncode != 0:
        # redact a token if one ever appears in argv (it shouldn't — gh reads GH_TOKEN
        # from env, not argv — but be defensive about logging command lines).
        printable = " ".join(argv)
        detail = (res.stderr or res.stdout or "").strip()
        # A KNOWN-benign failure — `gh pr checks` reading statusCheckRollup with an
        # App token that lacks the checks read scope ("Resource not accessible by
        # integration"). The reviewer does not gate on checks, so this is pure noise:
        # one line per PR per poll, flooding stdout. Gate it behind ISSUE_AGENT_DEBUG.
        # Every OTHER gh failure stays loud — the stderr write exists precisely so a
        # real failure (e.g. a 422 self-approval that made the reviewer look like it
        # did nothing) is visible rather than silently swallowed.
        benign_checks_rollup = (
            "pr checks" in printable
            and "statusCheckRollup" in detail
            and "Resource not accessible by integration" in detail
        )
        debug_on = os.environ.get("ISSUE_AGENT_DEBUG", "0") not in ("", "0", "false", "False")
        if not benign_checks_rollup or debug_on:
            sys.stderr.write(f"gh failed (exit {res.returncode}): {printable}\n  {detail}\n")
        # Infra? Then this is NOT an answer — raise so the caller can refuse to
        # act on a world it never actually observed. The stderr line above is
        # kept for the raise path too: some callers legitimately swallow
        # GhUnavailable (update_branch, add_blocked_by), and a swallowed infra
        # failure with no trace on stderr is the very bug this section fixes.
        # A no-checks PR is the one non-zero `pr checks` exit that IS an answer:
        # return the empty JSON list gh would have printed, so get_pr can tell
        # honest NONE (mergeable) from a failed read (UNKNOWN — never mergeable,
        # #48). Any other swallowed failure stays "" → checks_known=False.
        # MUST run before the infra classification: gh's message embeds the
        # BRANCH NAME ("no checks reported on the 'fix/12-request-timeout'
        # branch"), and a branch containing "timeout"/"connection reset"/… would
        # otherwise read as a network outage — GhUnavailable → tick skipped,
        # every tick, for as long as that PR exists.
        if no_checks_answer(argv, detail):
            return "[]"
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
