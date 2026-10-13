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
    started_at: str = ""
    run_id: int | None = None
    required: bool = False
    app_id: int | None = None
    kind: str = "check"

    def __post_init__(self) -> None:
        if (not isinstance(self.name, str) or not self.name
                or any(not isinstance(v, str) for v in (self.state, self.bucket, self.started_at))
                or type(self.required) is not bool or self.kind not in {"check", "status"}
                or any(v is not None and (type(v) is not int or v <= 0) for v in (self.app_id, self.run_id))):
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
