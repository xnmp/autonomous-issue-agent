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
        out.append(
            Review(
                state=r.get("state", "") or "",
                author=author or "",
                submitted_at=r.get("submittedAt") or r.get("submitted_at") or "",
                body=r.get("body", "") or "",
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
        the poll re-derives it next tick."""
        try:
            return self.get_pr(number, mergeability=mergeability)
        except Exception as exc:  # noqa: BLE001 — deliberately broad: one PR ≠ whole pass
            import sys
            sys.stderr.write(
                f"skip PR #{number} this poll ({type(exc).__name__}: {str(exc)[:120]}) "
                "— re-derived next tick\n"
            )
            return None

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
             "--json", "number", "--limit", "200"]
        ) or []
        return [int(p["number"]) for p in data]

    def build_world(self) -> WorldState:
        """Fetch the slice the daemons need: ai-ready/ai-working/ai-blocked issues
        and ai-pr-open PRs (open + recently merged/closed for cleanup). Composed
        from the typed reads above so it's exercised by the gateway tests."""
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
             "--json", "number", "--limit", "200"]
        ) or []
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
        for i in (self._gh_json(
            ["issue", "list", "--repo", self.repo, "--state", "open",
             "--json", "number", "--limit", "200"]
        ) or []):
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
    submit_review, check the empty result) while making the failure visible."""
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
    except subprocess.TimeoutExpired:
        sys.stderr.write(
            f"gh timed out (>{_GH_TIMEOUT_S:.0f}s): {' '.join(argv)}\n"
            "  killed; treating as empty result (poll re-derives next tick)\n"
        )
        return ""
    if res.returncode != 0:
        # redact a token if one ever appears in argv (it shouldn't — gh reads GH_TOKEN
        # from env, not argv — but be defensive about logging command lines).
        printable = " ".join(argv)
        detail = (res.stderr or res.stdout or "").strip()
        sys.stderr.write(f"gh failed (exit {res.returncode}): {printable}\n  {detail}\n")
        return ""
    return res.stdout
