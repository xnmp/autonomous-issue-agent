"""Infrastructure adapter for cached, read-only ``gh`` queries.

The v1 dashboard is journal-only on purpose (a diagnostic that fails when
GitHub does is worse than one that is honest about its scope). This module
keeps that posture while adding what the journal structurally cannot know —
titles, human-applied labels, whether a PR actually merged — under two rules:

  * **cached** — one `gh` invocation per (repo, query) per TTL, however many
    clients poll;
  * **degrading** — a failure returns `(None, error)`, never raises, and the
    payload it feeds carries `gh_ok: false` plus a warning.

`gh` shells out rather than talking to the API directly so it inherits the
operator's auth, host config and proxy exactly as every other harness call does.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

DEFAULT_TTL_S = 45.0
PR_DETAIL_TTL_S = 180.0
GH_TIMEOUT_S = 30.0

ISSUE_FIELDS = (
    "number,title,url,body,labels,state,createdAt,closedAt,updatedAt,comments"
)
# `gh issue list` paginates to this bound. Reaching it is surfaced to callers:
# a long-run trend that quietly omitted history would be worse than no trend.
ISSUE_HISTORY_LIMIT = 1000
PR_FIELDS = (
    "number,title,url,state,mergedAt,updatedAt,reviewDecision,"
    "statusCheckRollup,autoMergeRequest,latestReviews,labels,comments,"
    "isDraft,mergeable,mergeStateStatus,headRefName,closingIssuesReferences"
)
PR_DETAIL_FIELDS = (
    "number,url,state,headRefName,headRefOid,closingIssuesReferences,"
    "commits,files"
)


@dataclass
class TtlCache:
    """Tiny monotonic-clock TTL store. Errors are cached alongside successes:
    the failure mode worth guarding is `gh` being down while three views poll
    every five seconds."""

    ttl_s: float = DEFAULT_TTL_S
    clock: Callable[[], float] = time.monotonic
    _entries: dict = field(default_factory=dict)
    _inflight: dict = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _sweep_expired(self, now: float) -> None:
        self._entries = {
            key: entry
            for key, entry in self._entries.items()
            if now < entry[0]
        }

    def get(self, key):
        # Locked, and `pop` rather than `del`: routes are sync `def`, so they
        # run concurrently in the threadpool and two of them can expire the
        # same key at once.
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires, value = entry
            if self.clock() >= expires:
                self._entries.pop(key, None)
                return None
            return value

    def put(self, key, value):
        with self._lock:
            now = self.clock()
            self._sweep_expired(now)
            self._entries[key] = (now + self.ttl_s, value)
        return value

    def get_or_load(self, key, load: Callable[[], object]):
        """Return one cached/load result while concurrent misses wait for it."""
        while True:
            with self._lock:
                entry = self._entries.get(key)
                if entry is not None and self.clock() < entry[0]:
                    return entry[1]
                self._entries.pop(key, None)
                pending = self._inflight.get(key)
                if pending is None:
                    pending = threading.Event()
                    self._inflight[key] = pending
                    break
            pending.wait()

        try:
            value = load()
        except BaseException:
            with self._lock:
                completed = self._inflight.pop(key, None)
                if completed is not None:
                    completed.set()
            raise

        with self._lock:
            now = self.clock()
            self._sweep_expired(now)
            self._entries[key] = (now + self.ttl_s, value)
            completed = self._inflight.pop(key, None)
            if completed is not None:
                completed.set()
        return value


def run_gh(args: list[str]) -> tuple[str, str]:
    """(stdout, error). Never raises — a missing `gh` is an error string."""
    try:
        proc = subprocess.run(
            ["gh", *args], capture_output=True, text=True, timeout=GH_TIMEOUT_S
        )
    except FileNotFoundError:
        return "", "gh is not installed"
    except subprocess.TimeoutExpired:
        return "", f"gh timed out after {GH_TIMEOUT_S:.0f}s"
    except OSError as exc:
        return "", f"gh could not run: {exc}"
    if proc.returncode != 0:
        return "", (proc.stderr or proc.stdout or "gh failed").strip()[:400]
    return proc.stdout, ""


class GhReader:
    """Read-only `gh` façade. `run` is injectable so tests never shell out."""

    def __init__(
        self,
        run=None,
        ttl_s: float = DEFAULT_TTL_S,
        clock=time.monotonic,
        pr_ttl_s: float = PR_DETAIL_TTL_S,
    ):
        self._run = run or run_gh
        self._cache = TtlCache(ttl_s=ttl_s, clock=clock)
        self._pr_cache = TtlCache(ttl_s=pr_ttl_s, clock=clock)

    def json(self, repo: str, args: list[str]) -> tuple[object | None, str]:
        return self._cached_json(self._cache, repo, args)

    def _cached_json(
        self, cache: TtlCache, repo: str, args: list[str]
    ) -> tuple[object | None, str]:
        if not repo:
            return None, "no REPO configured for this deployment"
        key = (repo, tuple(args))

        def load() -> tuple[object | None, str]:
            out, error = self._run([*args, "-R", repo])
            if error:
                return None, error
            try:
                payload = json.loads(out or "null")
            except ValueError:
                return None, "gh returned unparseable JSON"
            return payload, ""

        return cache.get_or_load(key, load)

    def issues(self, repo: str, limit: int = ISSUE_HISTORY_LIMIT) -> tuple[object | None, str]:
        """Open AND closed issues in one call: the done cards need titles for
        issues that have since been closed, and a second query would double the
        rate-limit cost to answer the same question."""
        return self.json(
            repo,
            ["issue", "list", "--state", "all", "--limit", str(limit),
             "--json", ISSUE_FIELDS],
        )

    def prs(self, repo: str, limit: int = 200) -> tuple[object | None, str]:
        return self.json(
            repo,
            ["pr", "list", "--state", "all", "--limit", str(limit),
             "--json", PR_FIELDS],
        )

    def pr(self, repo: str, number: int) -> tuple[object | None, str]:
        """One PR's change history, fetched only for the issue detail view."""
        return self._cached_json(
            self._pr_cache,
            repo,
            ["pr", "view", str(number), "--json", PR_DETAIL_FIELDS],
        )


def index_by_number(payload) -> dict[int, dict]:
    """gh's JSON array → {number: object}. Anything unexpected is dropped, not
    raised on: a shape change must cost titles, not the whole view."""
    out: dict[int, dict] = {}
    if not isinstance(payload, list):
        return out
    for item in payload:
        if not isinstance(item, dict):
            continue
        raw_number = item.get("number")
        if raw_number is None:
            continue
        try:
            number = int(raw_number)
        except (TypeError, ValueError):
            continue
        out[number] = item
    return out


def is_open(issue: dict) -> bool:
    return str(issue.get("state") or "").upper() == "OPEN"
