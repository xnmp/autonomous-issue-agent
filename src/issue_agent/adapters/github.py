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
bucket) so the mapping is mechanical and auditable.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, replace

from ..domain.labels import Label
from ..domain.world import (
    Check,
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


def _warn_if_list_truncated(query: str, count: int) -> None:
    """Emit a loud stderr warning when a `gh … list` call returns exactly
    `_LIST_LIMIT` items. Unlike the fail-soft gh-error path (transient, re-derived
    next tick), a truncated listing recurs every poll and drops the same items
    every time until an operator raises `_LIST_LIMIT` or pagination ships — so
    this is intentionally NOT swallowed."""
    if count == _LIST_LIMIT:
        import sys

        sys.stderr.write(
            f"WARNING: {query} returned {count} items (== --limit {_LIST_LIMIT}) — "
            "results may be TRUNCATED; oldest items silently dropped this poll. "
            "Raise _LIST_LIMIT or add pagination.\n"
        )


# ── infra-failure detection (V10) ─────────────────────────────────────────────
class GhUnavailable(RuntimeError):
    """GitHub could not ANSWER — the `gh` call failed for infrastructure reasons
    (rate limit, network, 5xx), not because the world is empty.

    The lesson: `_subprocess_runner` used to map every failure to `""`, so a
    rate-limited poll observed an EMPTY world — zero PRs, zero issues — and the
    domain, being honest about what it saw, acted on it (nothing to review,
    nothing in flight, cleanup fair game). "Nothing to do" and "I could not
    look" must never be the same value. Raising is the entire fix: a world built
    on a failed read is a lie, so the daemon skips the tick and the poll loop —
    which IS the retry — looks again next interval. No backoff lives here.

    `kind` is one of "rate-limit" | "network" | "server"; `timed_out` marks the
    one sub-case that is per-CALL rather than systemic (the `_GH_TIMEOUT_S` kill
    of a single hung `gh pr view`), which `_safe_get_pr` still isolates per PR.
    """

    _HEADLINES = {
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
    ):
        self.kind = kind
        self.detail = _one_line(detail)
        self.argv = tuple(argv or ())
        self.timed_out = timed_out
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

    Order matters: rate-limit → server → network. "504 Gateway Timeout" contains
    "timeout", so the 5xx test must run before the network test or a GitHub
    outage would be misfiled as a local network fault (same tick-skip either way,
    but the operator reads the kind to decide whether to fix their box)."""
    text = (detail or "").lower()
    if any(p in text for p in _RATE_LIMIT_PATTERNS):
        return "rate-limit"
    if _SERVER_RE.search(text) or any(p in text for p in _SERVER_PATTERNS):
        return "server"
    if any(p in text for p in _NETWORK_PATTERNS):
        return "network"
    return None


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
        Check(name=c.get("name", "") or "", state=c.get("state", "") or "", bucket=c.get("bucket", "") or "")
        for c in raw or []
    )


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


def parse_world(issues: list[dict], prs: list[dict]) -> WorldState:
    return WorldState(
        issues=tuple(parse_issue(i) for i in issues),
        prs=tuple(parse_pr(p) for p in prs),
    )


# ── write command builders ──────────────────────────────────────────────────
def build_submit_review_args(repo: str, pr: int, event: str, body: str) -> list[str]:
    return [
        "api", f"repos/{repo}/pulls/{pr}/reviews", "--method", "POST",
        "-f", f"event={event}", "-f", f"body={body}",
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


def build_enable_auto_merge_args(repo: str, pr: int, method: str) -> list[str]:
    return ["pr", "merge", str(pr), "--repo", repo, "--auto", f"--{method}", "--delete-branch"]


def build_update_branch_args(repo: str, pr: int) -> list[str]:
    return ["pr", "update-branch", str(pr), "--repo", repo]


def build_blocker_id_args(repo: str, blocker: int) -> list[str]:
    return ["api", f"repos/{repo}/issues/{blocker}", "--jq", ".id"]


def build_add_blocked_by_args(repo: str, issue: int, blocker_id: int) -> list[str]:
    return [
        "api", f"repos/{repo}/issues/{issue}/dependencies/blocked_by",
        "--method", "POST", "-F", f"issue_id={blocker_id}",
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
        return self._run(["gh", *args], self.identity.env_token())

    def _gh_json(self, args: list[str]) -> object:
        out = self._gh(args)
        return json.loads(out) if out.strip() else None

    # ── reads ──
    def list_issue_numbers(self, label: str, state: str = "open") -> list[int]:
        data = self._gh_json(
            ["issue", "list", "--repo", self.repo, "--label", label,
             "--state", state, "--json", "number"]
        ) or []
        return [int(i["number"]) for i in data]

    def get_issue(self, number: int) -> IssueView:
        d = self._gh_json(
            ["issue", "view", str(number), "--repo", self.repo, "--json",
             "number,title,body,labels,comments,updatedAt,author,state"]
        )
        return parse_issue(d)  # type: ignore[arg-type]

    def has_open_blocker(self, number: int) -> bool:
        """True iff issue `number` has ≥1 OPEN GitHub-native 'blocked by'
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
                return False
            blockers = self._gh_json(
                ["api", f"repos/{self.repo}/issues/{number}/dependencies/blocked_by",
                 "--jq", "[.[] | .state]"]
            ) or []
            return any(str(s).lower() == "open" for s in blockers)  # type: ignore[union-attr]
        except GhUnavailable:
            # Fail-soft covers a genuine API ANSWER (endpoint absent, 404, odd
            # shape). An infra failure answered nothing, and "False" here is not
            # neutral — it says "not blocked", which DISPATCHES an issue that may
            # well be blocked. Propagate: skipping the tick is the cheap mistake.
            raise
        except Exception:
            return False

    def get_pr(self, number: int, *, mergeability: bool = True) -> PRView:
        """Fetch one PR. `mergeability=False` DROPS the mergeable/mergeStateStatus
        fields from the query — GitHub computes those lazily and can hang the call
        indefinitely on a just-merged PR (see _GH_TIMEOUT_S). Only the fixer's
        auto-rebase logic consumes them (is_conflicting / needs_rebase); the reviewer
        never does, so the review world fetches without them and cannot hang on that
        computation. Fields left absent parse to '' → is_conflicting/needs_rebase are
        simply False, which is correct for the review path (it doesn't rebase)."""
        base = ("number,title,body,headRefName,headRefOid,baseRefName,labels,"
                "reviews,state,closingIssuesReferences,files,isDraft,author")
        fields = base + (",mergeable,mergeStateStatus" if mergeability else "")
        d = self._gh_json(
            ["pr", "view", str(number), "--repo", self.repo, "--json", fields]
        )
        pr = parse_pr(d)  # type: ignore[arg-type]
        # checks come from a separate endpoint (bash `pr checks`).
        checks = self._gh_json(
            ["pr", "checks", str(number), "--repo", self.repo, "--json", "name,state,bucket"]
        ) or []
        return replace(pr, checks=_checks(checks))  # type: ignore[arg-type]

    def _safe_get_pr(self, number: int, *, mergeability: bool = True) -> PRView | None:
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
            return self.get_pr(number, mergeability=mergeability)
        except GhUnavailable as exc:
            if not exc.timed_out:
                raise
            self._skip_pr(number, exc)
            return None
        except Exception as exc:  # noqa: BLE001 — deliberately broad: one PR ≠ whole pass
            self._skip_pr(number, exc)
            return None

    @staticmethod
    def _skip_pr(number: int, exc: BaseException) -> None:
        import sys
        sys.stderr.write(
            f"skip PR #{number} this poll ({type(exc).__name__}: {str(exc)[:120]}) "
            "— re-derived next tick\n"
        )

    def list_pr_numbers(self, label: str, state: str = "open") -> list[int]:
        data = self._gh_json(
            ["pr", "list", "--repo", self.repo, "--label", label,
             "--state", state, "--json", "number"]
        ) or []
        return [int(p["number"]) for p in data]

    def list_all_open_pr_numbers(self) -> list[int]:
        """Every OPEN PR number, no label filter — feeds rogue-PR adoption."""
        data = self._gh_json(
            ["pr", "list", "--repo", self.repo, "--state", "open",
             "--json", "number", "--limit", str(_LIST_LIMIT)]
        ) or []
        _warn_if_list_truncated("gh pr list --state open", len(data))
        return [int(p["number"]) for p in data]

    def build_world(self) -> WorldState:
        """Fetch the slice the daemons need: ai-ready/ai-working/ai-blocked issues
        and ai-pr-open PRs (open + recently merged/closed for cleanup). Composed
        from the typed reads above so it's exercised by the gateway tests.

        `GhUnavailable` PROPAGATES out of here (all four world builders): a
        partially-read world is not a world, and the caller (`Daemon.tick`) is
        the only layer that can decide "skip this pass". Note the list calls run
        FIRST, so a throttled poll aborts before any per-item fetch — the empty
        world of V10 is unreachable rather than merely unlikely."""
        issue_numbers: set[int] = set()
        ready_numbers: set[int] = set()
        for lbl in ("ai-ready", "ai-working", "ai-blocked"):
            nums = self.list_issue_numbers(lbl)
            issue_numbers.update(nums)
            if lbl == "ai-ready":
                ready_numbers.update(nums)
        prs_numbers: set[int] = set()
        for state in ("open", "all"):
            prs_numbers.update(self.list_pr_numbers("ai-pr-open", state))
        # Only ai-ready issues are gated on dependencies (pick_ready_issue), so
        # only they pay the blocked-by read — an ai-working/blocked issue never
        # needs it. Cheapest-first inside has_open_blocker keeps it ~1 call each.
        issues = tuple(
            replace(self.get_issue(n), blocked_by_open=self.has_open_blocker(n))
            if n in ready_numbers else self.get_issue(n)
            for n in sorted(issue_numbers)
        )
        prs = tuple(
            p for n in sorted(prs_numbers) if (p := self._safe_get_pr(n)) is not None
        )
        return WorldState(issues=issues, prs=prs)

    def build_review_world(self) -> WorldState:
        """The review daemon's slice: ONLY PRs carrying `self.review_pr_label` (open
        + all, the latter for merged/closed session cleanup). No issues — the reviewer
        never reads them. Used instead of `build_world` so the reviewer can be pointed
        at an EXTERNAL repo (which has none of the harness's issue labels) and trigger
        on a configurable PR tag. `gh pr list --label <missing>` returns [] (exit 0),
        so an absent label degrades to "nothing to review" rather than crashing."""
        prs_numbers: set[int] = set()
        for state in ("open", "all"):
            prs_numbers.update(self.list_pr_numbers(self.review_pr_label, state))
        # Rogue-PR adoption: also pull EVERY open PR (no label filter) so a PR that
        # never went through the pipeline (opened directly, no trigger label) is
        # visible to decide_review, which labels+adopts it. Draft/own-bot/opt-out
        # filtering is the domain's job (it has the PRView fields); the adapter just
        # widens the slice. Merged/closed still come only via the label (no need to
        # adopt a closed rogue PR).
        if self.review_adopt_unlabeled:
            prs_numbers.update(self.list_all_open_pr_numbers())
        # mergeability=False: the reviewer never reads mergeable/merge_state (only the
        # fixer's rebase logic does), and requesting them can hang gh indefinitely on a
        # just-merged PR — which previously froze the whole review poll. Fetch without
        # them so a comment can always be left.
        prs = tuple(
            p for n in sorted(prs_numbers)
            if (p := self._safe_get_pr(n, mergeability=False)) is not None
        )
        return WorldState(issues=(), prs=prs)

    def build_triage_world(self) -> WorldState:
        """All OPEN issues (the steward triages untriaged ones — those carrying no
        lifecycle label — so it must see every open issue, not just labelled ones)."""
        nums = self._gh_json(
            ["issue", "list", "--repo", self.repo, "--state", "open",
             "--json", "number", "--limit", str(_LIST_LIMIT)]
        ) or []
        _warn_if_list_truncated("gh issue list --state open", len(nums))
        issues = tuple(self.get_issue(int(i["number"])) for i in nums)
        return WorldState(issues=issues)

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
        issues = tuple(self.get_issue(n) for n in sorted(nums))
        return WorldState(issues=issues)

    def pr_for_issue(self, issue: int) -> int | None:
        """Open PR whose head branch matches ^(feat|fix|chore|refactor)/<issue>-.
        Port of agent/lib.sh:pr_for_issue (closing-ref graph is default-branch only)."""
        data = self._gh_json(
            ["pr", "list", "--repo", self.repo, "--state", "open",
             "--json", "number,headRefName"]
        ) or []
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

    def submit_review(self, pr: int, event: str, body: str) -> bool:
        """POST a review. Returns True iff gh succeeded. A failed submit (e.g. 422
        'can't approve your own PR' when the identity collides with the author, or a
        403 from a misconfigured App) returns False so the caller does NOT record a
        phantom 'reviewed' state — the daemon retries the same PR next poll. The
        underlying gh failure is surfaced on stderr by `_subprocess_runner`. gh's
        `api ... POST` prints the created-review JSON on success (non-empty)."""
        out = self._gh(build_submit_review_args(self.repo, pr, event, body))
        return bool(out.strip())

    # ── writes ──
    def relabel(self, kind: str, number: int, remove: str | None, add: str | None) -> None:
        args = build_relabel_args(self.repo, kind, number, remove, add)
        if args is not None:
            self._gh(args)

    def comment(self, kind: str, number: int, body: str) -> None:
        self._gh(build_comment_args(self.repo, kind, number, body))

    def close_issue(self, number: int, reason: str, comment: str) -> None:
        self._gh(build_close_issue_args(self.repo, number, reason, comment))

    def enable_auto_merge(self, pr: int, method: str) -> None:
        self._gh(build_enable_auto_merge_args(self.repo, pr, method))

    def update_branch(self, pr: int) -> None:
        """Bring a BEHIND PR branch up to date with base (merge base into head), so
        an armed auto-merge can land it. Fail-soft: a transient 'already up to date'
        / merge-conflict race must not abort the poll — it's re-evaluated next tick."""
        try:
            self._gh(build_update_branch_args(self.repo, pr))
        except Exception:
            return

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
        kind = classify_gh_failure(detail)
        if kind is not None:
            raise GhUnavailable(kind, detail, argv=argv)
        return ""
    return res.stdout
