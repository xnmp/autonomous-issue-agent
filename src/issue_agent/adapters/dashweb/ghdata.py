"""Cached, read-only `gh` list reads the role observation does not cover.

The board itself is derived from `GitHubGateway.build_world`, the observation
the roles read. This module adds only the lists that observation leaves out:
every issue (backlog, titles of done work, the opened/closed trend) and the
merged harness PRs (done work, merges for cost per merged PR). Each read is
cached per TTL however many clients poll, and degrades: a failure returns
`(None, error)` and never raises.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

# Freshness belongs to the observation refresh (service.OBSERVE_TTL_S); this
# only de-duplicates concurrent calls and the two sub-reads of one refresh.
DEFAULT_TTL_S = 2.0
GH_TIMEOUT_S = 30.0

# Body and comments only for open issues (the backlog shows them): with them,
# 1000 issues of history was ~2 MiB and ~28s, so the read hit GH_TIMEOUT_S
# whenever GitHub was slow and the whole view lost its GitHub data (2026-10-13).
ISSUE_FIELDS = "number,title,url,labels,state,createdAt,closedAt"
OPEN_ISSUE_FIELDS = ISSUE_FIELDS + ",body,comments"
# `gh issue list` paginates to this bound; reaching it is reported to callers.
ISSUE_HISTORY_LIMIT = 1000
MERGED_PR_FIELDS = "number,title,url,mergedAt,headRefName"
MERGED_PR_LIMIT = 1000  # 200 truncated tauri-explorer (325 merged); 1000 reads in ~2.4s


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

    def __init__(self, run=None, ttl_s: float = DEFAULT_TTL_S, clock=time.monotonic):
        self._run = run or run_gh
        self._cache = TtlCache(ttl_s=ttl_s, clock=clock)

    def json(self, repo: str, args: list[str]) -> tuple[object | None, str]:
        if not repo:
            return None, "no REPO configured for this deployment"

        def load() -> tuple[object | None, str]:
            out, error = self._run([*args, "-R", repo])
            if error:
                return None, error
            try:
                payload = json.loads(out or "null")
            except ValueError:
                return None, "gh returned unparseable JSON"
            return payload, ""

        return self._cache.get_or_load((repo, tuple(args)), load)

    def issues(self, repo: str, limit: int = ISSUE_HISTORY_LIMIT) -> tuple[object | None, str]:
        """Open and closed issues; open ones also carry body and comments."""
        history, error = self.json(repo, ["issue", "list", "--state", "all", "--limit", str(limit),
                                          "--json", ISSUE_FIELDS])
        if error:
            return None, error
        open_, error = self.json(repo, ["issue", "list", "--state", "open", "--limit", str(limit),
                                        "--json", OPEN_ISSUE_FIELDS])
        if error:
            return None, error
        if not isinstance(history, list) or not isinstance(open_, list):
            return None, "gh returned a non-list issue payload"
        # The open read is authoritative for which issues are open: the two
        # cache entries can be up to a TTL apart, so an issue closed or opened
        # between them takes the open read's word.
        full = index_by_number(open_)
        closed = [i for i in history if isinstance(i, dict) and not is_open(i) and i.get("number") not in full]
        return [*full.values(), *closed], ""

    def merged_prs(self, repo: str, limit: int = MERGED_PR_LIMIT) -> tuple[object | None, str]:
        return self.json(repo, ["pr", "list", "--state", "merged", "--limit", str(limit), "--json", MERGED_PR_FIELDS])


def labels_of(issue: dict) -> list[str]:
    """gh renders labels as objects; a hand-built fixture may use strings."""
    out = []
    for label in issue.get("labels") or []:
        name = label.get("name") if isinstance(label, dict) else label
        if name:
            out.append(str(name))
    return out


def index_by_number(payload) -> dict[int, dict]:
    """gh's JSON array → {number: object}. Anything unexpected is dropped, not
    raised on: a shape change must cost titles, not the whole view."""
    out: dict[int, dict] = {}
    if not isinstance(payload, list):
        return out
    for item in payload:
        if not isinstance(item, dict):
            continue
        number = item.get("number")
        if type(number) is int and number > 0:
            out[number] = item
    return out


def is_open(issue: dict) -> bool:
    return str(issue.get("state") or "").upper() == "OPEN"
