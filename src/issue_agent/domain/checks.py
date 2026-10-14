"""Immutable CI evidence; only complete reads construct Observed."""

from dataclasses import dataclass


@dataclass(frozen=True)
class RequiredCheck:
    context: str
    app_id: int | None = None

    def __post_init__(self) -> None:
        if (not isinstance(self.context, str) or not self.context
                or self.app_id is not None and (type(self.app_id) is not int or self.app_id <= 0)):
            raise ValueError("required check needs an exact context and optional app identity")


@dataclass(frozen=True)
class Check:
    name: str
    state: str = ""
    bucket: str = ""
    # When the report's CI run began: an Actions job's run attempt (so every
    # job of one attempt shares it), another app's check run, a status's post.
    started_at: str = ""
    run_id: int | None = None
    required: bool = False
    app_id: int | None = None
    kind: str = "check"
    # A check run's database id: for a GitHub Actions job, its job id. Each run
    # attempt mints new ones, so it identifies one failure (domain.evidence).
    check_id: int | None = None
    # The Actions run's latest attempt (1 first, +1 per re-run); the
    # observation drops checks an attempt superseded (adapters.github_ci).
    run_attempt: int | None = None

    def __post_init__(self) -> None:
        if (not isinstance(self.name, str) or not self.name
                or any(not isinstance(v, str) for v in (self.state, self.bucket, self.started_at))
                or type(self.required) is not bool or self.kind not in {"check", "status"}
                or self.check_id is not None and self.kind != "check"
                or any(v is not None and (type(v) is not int or v <= 0)
                       for v in (self.app_id, self.run_id, self.check_id, self.run_attempt))):
            raise ValueError("check report is malformed")


@dataclass(frozen=True)
class Unavailable:
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.reason, str) or not self.reason:
            raise ValueError("unavailable CI needs a reason")


@dataclass(frozen=True)
class Observed:
    head: str
    tested_revision: str
    policy: frozenset[RequiredCheck]
    reports: tuple[Check, ...]

    def __post_init__(self) -> None:
        if (any(not isinstance(v, str) or not v for v in (self.head, self.tested_revision))
                or not self.policy and self.head != self.tested_revision
                or not isinstance(self.policy, frozenset)
                or any(not isinstance(r, RequiredCheck) for r in self.policy)
                or not isinstance(self.reports, tuple) or any(not isinstance(c, Check) for c in self.reports)):
            raise ValueError("observed CI requires complete typed revision, policy and reports")


CIObservation = Unavailable | Observed


@dataclass(frozen=True)
class BaseTip:
    """The base branch's tip commit and every check reported on it. Only a
    complete read constructs one; an unread tip is absent, never empty (#313)."""

    ref: str
    oid: str
    reports: tuple[Check, ...]

    def __post_init__(self) -> None:
        if (any(not isinstance(v, str) or not v for v in (self.ref, self.oid))
                or not isinstance(self.reports, tuple) or any(not isinstance(c, Check) for c in self.reports)):
            raise ValueError("base tip requires a ref, a commit and typed reports")


@dataclass(frozen=True)
class BaseRun:
    """A completed push run on the base branch. `completed_at` is the run's
    last update, which a completed run makes when it finishes."""

    workflow: str
    run_id: int
    url: str
    head_sha: str
    conclusion: str  # GitHub's, lowercased
    created_at: str
    completed_at: str

    def __post_init__(self) -> None:
        if type(self.run_id) is not int or self.run_id <= 0 or any(
                not isinstance(v, str) or not v for v in (self.workflow, self.url, self.head_sha, self.conclusion)):
            raise ValueError("base run is malformed")


@dataclass(frozen=True)
class BaseIssue:
    """A base-CI issue the fixer opened, known by its title. `harness_closed`:
    its last close was the fixer's own, not a human's or a merged PR's."""

    number: int
    title: str
    closed_at: str = ""  # "" while open
    harness_closed: bool = False


@dataclass(frozen=True)
class BaseCI:
    """Base runs and the fixer's base-CI issues, open and closed. Only a
    complete read constructs one; an unread base is absent, never empty."""

    runs: tuple[BaseRun, ...]
    issues: tuple[BaseIssue, ...]

    def issue(self, title: str) -> BaseIssue | None:
        """The oldest issue with `title`: a duplicate never changes which one is meant."""
        return next(iter(sorted((i for i in self.issues if i.title == title), key=lambda i: i.number)), None)
