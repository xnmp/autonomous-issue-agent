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
class BaseRun:
    """A push run on the base branch, completed or being re-run. `completed_at`
    is the run's last update, which a completed run makes when it finishes."""

    workflow: str
    run_id: int
    url: str
    head_sha: str
    conclusion: str  # GitHub's, lowercased; "" while the run is being re-run
    created_at: str
    completed_at: str

    def __post_init__(self) -> None:
        if type(self.run_id) is not int or self.run_id <= 0 or not isinstance(self.conclusion, str) or any(
                not isinstance(v, str) or not v for v in (self.workflow, self.url, self.head_sha)):
            raise ValueError("base run is malformed")


@dataclass(frozen=True)
class BaseIssue:
    """A base-CI issue the fixer opened, known by its title: one per workflow
    per red streak (#402)."""

    number: int
    title: str
    closed_at: str = ""  # "" while open


@dataclass(frozen=True)
class BaseHealth:
    """The base branch's health, one observation per tick (#442) in parts
    read apart. `reports`: its tip's checks (#313); None unread, and a red PR
    check is then the PR's. For the fixer (#402): its push `runs`, completed
    or being re-run, and the fixer's base-CI `issues` (the newest 100); None
    unread or not asked for, and nothing is then filed, closed or readied.
    `required`: the workflows with a check the branch requires; `workflows`:
    the repository's active ones (#449). None unread: nothing readied, or
    closed as orphaned."""

    ref: str
    reports: tuple[Check, ...] | None = None
    runs: tuple[BaseRun, ...] | None = None
    issues: tuple[BaseIssue, ...] = ()
    required: frozenset[str] | None = None
    workflows: frozenset[str] | None = None

    def __post_init__(self) -> None:
        if (not isinstance(self.ref, str) or not self.ref
                or self.reports is not None and any(not isinstance(c, Check) for c in self.reports)
                or self.runs is None and (self.issues or self.required is not None or self.workflows is not None)):
            raise ValueError("base health needs a ref, typed reports, and its runs with its issues")
