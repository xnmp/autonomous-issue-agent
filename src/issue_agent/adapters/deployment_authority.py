"""ADR 0008 fencing for deployment-scoped remote effects."""

from __future__ import annotations

import fcntl
import hashlib
import os
import stat
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Protocol

from ..domain.repository import canonical_repository


class OwnerRegistry(Protocol):
    @property
    def path(self) -> str: ...

    def assert_owner_active(self, owner_id: str) -> None: ...


class EffectAuthority(Protocol):
    def effect(self) -> AbstractContextManager[None]: ...


@dataclass(frozen=True)
class UnfencedEffectAuthority:
    """Authority for operator effects that have no deployment retirement."""

    @contextmanager
    def effect(self) -> Iterator[None]:
        yield


def deployment_role(repository: str, owner_id: str) -> str:
    repository = canonical_repository(repository)
    prefix = f"{repository}:"
    if not owner_id.startswith(prefix):
        raise ValueError("deployment owner does not match its repository")
    role = owner_id.removeprefix(prefix).partition(":")[0]
    if not role:
        raise ValueError("deployment owner role is unavailable")
    return role


def _barrier_path(control_path: str, repository: str, role: str) -> str:
    scope = hashlib.sha256(
        f"{canonical_repository(repository)}\0{role}".encode()
    ).hexdigest()[:32]
    return f"{control_path}.remote-effect.{scope}.lock"


@contextmanager
def deployment_effect_barrier(
    control_path: str,
    repository: str,
    role: str,
) -> Iterator[None]:
    """Take exclusive authority to retire one repo/role lineage."""

    with _deployment_effect_lock(
        control_path,
        repository,
        role,
        exclusive=True,
    ):
        yield


@contextmanager
def deployment_effect_authority(
    control_path: str,
    repository: str,
    role: str,
) -> Iterator[None]:
    """Share authority with peer effects while excluding owner retirement."""

    with _deployment_effect_lock(
        control_path,
        repository,
        role,
        exclusive=False,
    ):
        yield


@contextmanager
def _deployment_effect_lock(
    control_path: str,
    repository: str,
    role: str,
    *,
    exclusive: bool,
) -> Iterator[None]:
    if not control_path or not repository or not role:
        raise ValueError("deployment effect authority scope is incomplete")
    path = _barrier_path(control_path, repository, role)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, stat.S_IRUSR | stat.S_IWUSR)
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
        ):
            raise OSError(
                f"deployment effect lock is not a user-owned regular file: "
                f"{path}"
            )
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@dataclass(frozen=True)
class DeploymentAuthority:
    registry: OwnerRegistry
    repository: str
    role: str
    owner_id: str

    def __post_init__(self) -> None:
        repository = canonical_repository(self.repository)
        if not self.role or not self.owner_id:
            raise ValueError("deployment authority identity is incomplete")
        if deployment_role(repository, self.owner_id) != self.role:
            raise ValueError("deployment authority role does not match its owner")
        object.__setattr__(self, "repository", repository)

    @contextmanager
    def effect(self) -> Iterator[None]:
        """Linearize one remote effect with owner retirement."""

        with deployment_effect_authority(
            self.registry.path,
            self.repository,
            self.role,
        ):
            self.registry.assert_owner_active(self.owner_id)
            yield
