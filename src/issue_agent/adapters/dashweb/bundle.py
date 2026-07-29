"""Freshness contract for the generated dashboard SPA.

The Vite build records a SHA-256 for every source/config input. Dashboard
startup recomputes that same map and serves the SPA only when it matches. This
closes the editable-checkout failure mode where a gitignored ``dist`` survives
a pull and silently serves code from an older commit.
"""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import BinaryIO, Literal

MANIFEST_SCHEMA = 1
MANIFEST_NAME = "build-manifest.json"
INPUT_SPEC_NAME = "build-inputs.json"
POINTER_NAME = "current.json"


@dataclass(frozen=True)
class BundleStatus:
    state: Literal["ready", "missing", "stale"]
    detail: str

    @property
    def ready(self) -> bool:
        return self.state == "ready"


@dataclass(frozen=True)
class BundleSnapshot:
    status: BundleStatus
    dist: Path
    lease: BundleLease | None = None


class BundleLease:
    """A process-held shared lock preventing collection of a served version."""

    def __init__(self, handle: BinaryIO):
        self._handle = handle

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.close()

    def __del__(self) -> None:
        self.close()


def _dashboard_root(harness_root: str | os.PathLike[str]) -> Path:
    return Path(harness_root) / "dashboard"


def _legacy_dist(harness_root: str | os.PathLike[str]) -> Path:
    return _dashboard_root(harness_root) / "dist"


def published_dist(harness_root: str | os.PathLike[str]) -> Path:
    """The immutable build selected by the atomic pointer, or legacy ``dist``."""
    root = _legacy_dist(harness_root)
    pointer = root / POINTER_NAME
    if not pointer.is_file():
        return root
    raw = json.loads(pointer.read_text(encoding="utf-8"))
    if (
        not isinstance(raw, dict)
        or raw.get("schema") != MANIFEST_SCHEMA
        or not isinstance(raw.get("build"), str)
        or not raw["build"]
    ):
        raise ValueError(f"dashboard/dist/{POINTER_NAME} has an unsupported shape")
    candidate = (root / raw["build"]).resolve()
    resolved_root = root.resolve()
    if (
        not candidate.is_relative_to(resolved_root)
        or candidate.parent != resolved_root / "builds"
    ):
        raise ValueError(f"dashboard/dist/{POINTER_NAME} escapes dashboard/dist")
    return candidate


def _lease_dist(harness_root: str | os.PathLike[str], dist: Path) -> BundleLease:
    lease_dir = _legacy_dist(harness_root) / ".leases"
    lease_dir.mkdir(parents=True, exist_ok=True)
    handle = (lease_dir / f"{dist.name}.lock").open("a+b")
    fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
    return BundleLease(handle)


def _input_spec(dashboard_root: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    raw = json.loads((dashboard_root / INPUT_SPEC_NAME).read_text(encoding="utf-8"))
    if (
        not isinstance(raw, dict)
        or raw.get("schema") != MANIFEST_SCHEMA
        or not isinstance(raw.get("files"), list)
        or not isinstance(raw.get("directories"), list)
        or not all(isinstance(value, str) for value in raw["files"] + raw["directories"])
    ):
        raise ValueError(f"{INPUT_SPEC_NAME} has an unsupported shape")
    return tuple(raw["files"]), tuple(raw["directories"])


def _input_paths(dashboard_root: Path) -> tuple[Path, ...]:
    files, directories = _input_spec(dashboard_root)
    paths: set[Path] = set()
    for relative in files:
        path = dashboard_root / relative
        if not path.is_file():
            raise FileNotFoundError(f"dashboard build input is missing: {relative}")
        paths.add(path)
    for relative in directories:
        directory = dashboard_root / relative
        if directory.exists() and not directory.is_dir():
            raise ValueError(f"dashboard build input is not a directory: {relative}")
        if directory.is_dir():
            paths.update(
                path
                for path in directory.rglob("*")
                if path.is_file() and not path.is_symlink()
            )
    return tuple(sorted(paths, key=lambda path: path.relative_to(dashboard_root).as_posix()))


def source_hashes(harness_root: str | os.PathLike[str]) -> dict[str, str]:
    dashboard_root = _dashboard_root(harness_root)
    return {
        path.relative_to(dashboard_root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in _input_paths(dashboard_root)
    }


def _output_hashes(dist: Path) -> dict[str, str]:
    symlinks = tuple(path for path in dist.rglob("*") if path.is_symlink())
    if symlinks:
        relative = symlinks[0].relative_to(dist).as_posix()
        raise ValueError(f"dashboard build output is a symlink: {relative}")
    paths = tuple(
        sorted(
            (
                path
                for path in dist.rglob("*")
                if path.is_file()
                and not path.is_symlink()
                and path.name != MANIFEST_NAME
            ),
            key=lambda path: path.relative_to(dist).as_posix(),
        )
    )
    return {
        path.relative_to(dist).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths
    }


def output_hashes(harness_root: str | os.PathLike[str]) -> dict[str, str]:
    return _output_hashes(published_dist(harness_root))


def bundle_snapshot(
    harness_root: str | os.PathLike[str], *, pin: bool = False
) -> BundleSnapshot:
    fallback_dist = _legacy_dist(harness_root)
    try:
        dist = published_dist(harness_root)
        lease = (
            _lease_dist(harness_root, dist)
            if pin and dist.parent == fallback_dist.resolve() / "builds"
            else None
        )
    except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return BundleSnapshot(
            BundleStatus("stale", f"dashboard bundle pointer is invalid ({exc})"),
            fallback_dist,
        )
    if not (dist / "index.html").is_file():
        return BundleSnapshot(
            BundleStatus("missing", f"{dist}/index.html is missing"),
            dist,
            lease,
        )

    manifest_path = dist / MANIFEST_NAME
    if not manifest_path.is_file():
        return BundleSnapshot(
            BundleStatus(
                "stale",
                f"{dist}/{MANIFEST_NAME} is missing (legacy or incomplete build)",
            ),
            dist,
            lease,
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema") != MANIFEST_SCHEMA
            or not isinstance(manifest.get("inputs"), dict)
            or not isinstance(manifest.get("outputs"), dict)
            or not all(
                isinstance(path, str) and isinstance(digest, str)
                for path, digest in manifest["inputs"].items()
            )
            or not all(
                isinstance(path, str) and isinstance(digest, str)
                for path, digest in manifest["outputs"].items()
            )
        ):
            raise ValueError("unsupported shape")
        current_inputs = source_hashes(harness_root)
        current_outputs = _output_hashes(dist)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return BundleSnapshot(
            BundleStatus("stale", f"dashboard bundle metadata is invalid ({exc})"),
            dist,
            lease,
        )

    for label, recorded, current in (
        ("source", manifest["inputs"], current_inputs),
        ("build output", manifest["outputs"], current_outputs),
    ):
        if recorded == current:
            continue
        changed = sorted(
            path
            for path in set(recorded) | set(current)
            if recorded.get(path) != current.get(path)
        )
        sample = ", ".join(changed[:3])
        suffix = "" if len(changed) <= 3 else f", and {len(changed) - 3} more"
        return BundleSnapshot(
            BundleStatus(
                "stale",
                f"dashboard {label} changed after the last build: {sample}{suffix}",
            ),
            dist,
            lease,
        )
    return BundleSnapshot(
        BundleStatus("ready", "dashboard bundle matches its source inputs and outputs"),
        dist,
        lease,
    )


def bundle_status(harness_root: str | os.PathLike[str]) -> BundleStatus:
    return bundle_snapshot(harness_root).status
