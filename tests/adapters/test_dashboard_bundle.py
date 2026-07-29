import fcntl
import hashlib
import json
import os
import signal
import subprocess

import pytest

from issue_agent.adapters.dashweb import build as dashboard_build
from issue_agent.adapters.dashweb import bundle


def _dashboard(tmp_path):
    dashboard = tmp_path / "dashboard"
    source = dashboard / "src"
    dist = dashboard / "dist"
    source.mkdir(parents=True)
    dist.mkdir()
    (dashboard / "build-inputs.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "files": ["build-inputs.json", "index.html"],
                "directories": ["src", "public"],
            }
        )
    )
    (dashboard / "index.html").write_text('<div id="root"></div>')
    (source / "main.tsx").write_text("export const version = 1;")
    (dist / "index.html").write_text("<html>built app</html>")
    return dashboard


def _record_current_inputs(tmp_path):
    manifest = {
        "schema": bundle.MANIFEST_SCHEMA,
        "inputs": bundle.source_hashes(tmp_path),
        "outputs": bundle.output_hashes(tmp_path),
    }
    (tmp_path / "dashboard" / "dist" / bundle.MANIFEST_NAME).write_text(
        json.dumps(manifest)
    )


def test_matching_build_manifest_is_ready(tmp_path):
    _dashboard(tmp_path)
    _record_current_inputs(tmp_path)

    status = bundle.bundle_status(tmp_path)

    assert status.ready
    assert status.state == "ready"


def test_changed_added_and_removed_inputs_each_make_the_bundle_stale(tmp_path):
    dashboard = _dashboard(tmp_path)
    _record_current_inputs(tmp_path)

    (dashboard / "src" / "main.tsx").write_text("export const version = 2;")
    changed = bundle.bundle_status(tmp_path)
    assert changed.state == "stale"
    assert "src/main.tsx" in changed.detail

    (dashboard / "src" / "main.tsx").write_text("export const version = 1;")
    (dashboard / "src" / "new.ts").write_text("export {};")
    added = bundle.bundle_status(tmp_path)
    assert added.state == "stale"
    assert "src/new.ts" in added.detail

    (dashboard / "src" / "new.ts").unlink()
    (dashboard / "src" / "main.tsx").unlink()
    removed = bundle.bundle_status(tmp_path)
    assert removed.state == "stale"
    assert "src/main.tsx" in removed.detail


def test_missing_or_malformed_manifest_never_looks_ready(tmp_path):
    dashboard = _dashboard(tmp_path)

    missing = bundle.bundle_status(tmp_path)
    assert missing.state == "stale"
    assert "manifest" in missing.detail

    manifest = dashboard / "dist" / bundle.MANIFEST_NAME
    manifest.write_text("{")
    malformed = bundle.bundle_status(tmp_path)
    assert malformed.state == "stale"
    assert "invalid" in malformed.detail

    manifest.write_text(json.dumps({"schema": 999, "inputs": {}, "outputs": {}}))
    unsupported = bundle.bundle_status(tmp_path)
    assert unsupported.state == "stale"
    assert "invalid" in unsupported.detail


def test_manifest_hashes_file_bytes_not_timestamps(tmp_path):
    dashboard = _dashboard(tmp_path)
    _record_current_inputs(tmp_path)
    source = dashboard / "src" / "main.tsx"
    before = source.stat().st_mtime_ns
    source.write_text("export const version = 2;")
    os.utime(source, ns=(before, before))

    assert bundle.bundle_status(tmp_path).state == "stale"
    assert bundle.source_hashes(tmp_path)["src/main.tsx"] == hashlib.sha256(
        source.read_bytes()
    ).hexdigest()


def test_deleted_tampered_and_unexpected_outputs_each_make_the_bundle_stale(tmp_path):
    dashboard = _dashboard(tmp_path)
    asset = dashboard / "dist" / "assets" / "app.js"
    asset.parent.mkdir()
    asset.write_text("console.log('built');")
    _record_current_inputs(tmp_path)

    asset.write_text("console.log('tampered');")
    tampered = bundle.bundle_status(tmp_path)
    assert tampered.state == "stale"
    assert "build output" in tampered.detail
    assert "assets/app.js" in tampered.detail

    asset.unlink()
    deleted = bundle.bundle_status(tmp_path)
    assert deleted.state == "stale"
    assert "assets/app.js" in deleted.detail

    asset.write_text("console.log('built');")
    (dashboard / "dist" / "unexpected.js").write_text("unexpected")
    unexpected = bundle.bundle_status(tmp_path)
    assert unexpected.state == "stale"
    assert "unexpected.js" in unexpected.detail


def test_atomic_pointer_selects_a_version_and_rejects_escape(tmp_path):
    dashboard = _dashboard(tmp_path)
    legacy_dist = dashboard / "dist"
    version = legacy_dist / "builds" / "v1"
    version.mkdir(parents=True)
    (version / "index.html").write_text("<html>version one</html>")
    (legacy_dist / bundle.POINTER_NAME).write_text(
        json.dumps({"schema": 1, "build": "builds/v1"})
    )
    (version / bundle.MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema": 1,
                "inputs": bundle.source_hashes(tmp_path),
                "outputs": bundle.output_hashes(tmp_path),
            }
        )
    )

    assert bundle.published_dist(tmp_path) == version.resolve()
    assert bundle.bundle_status(tmp_path).ready

    (legacy_dist / bundle.POINTER_NAME).write_text(
        json.dumps({"schema": 1, "build": "../../outside"})
    )
    escaped = bundle.bundle_status(tmp_path)
    assert escaped.state == "stale"
    assert "escapes" in escaped.detail


def test_bundle_snapshot_resolves_the_published_version_only_once(tmp_path, monkeypatch):
    dashboard = _dashboard(tmp_path)
    version = dashboard / "dist" / "builds" / "v1"
    version.mkdir(parents=True)
    index = version / "index.html"
    index.write_text("<html>version one</html>")
    (version / bundle.MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema": 1,
                "inputs": bundle.source_hashes(tmp_path),
                "outputs": {
                    "index.html": hashlib.sha256(index.read_bytes()).hexdigest()
                },
            }
        )
    )
    calls = 0

    def resolve_once(_root):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise AssertionError("published version was resolved more than once")
        return version

    monkeypatch.setattr(bundle, "published_dist", resolve_once)

    snapshot = bundle.bundle_snapshot(tmp_path)

    assert snapshot.status.ready
    assert snapshot.dist == version
    assert calls == 1


def test_bundle_selection_holds_the_build_lock_until_its_lease_is_acquired(
    tmp_path, monkeypatch
):
    dashboard = _dashboard(tmp_path)
    version = dashboard / "dist" / "builds" / "v1"
    version.mkdir(parents=True)
    index = version / "index.html"
    index.write_text("<html>version one</html>")
    (dashboard / "dist" / bundle.POINTER_NAME).write_text(
        json.dumps({"schema": 1, "build": "builds/v1"})
    )
    (version / bundle.MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema": 1,
                "inputs": bundle.source_hashes(tmp_path),
                "outputs": {
                    "index.html": hashlib.sha256(index.read_bytes()).hexdigest()
                },
            }
        )
    )
    original_lease = bundle._lease_dist
    writer_was_excluded = False

    def lease_while_probing_writer(root, dist):
        nonlocal writer_was_excluded
        with (dashboard / ".build.lock").open("a+b") as writer:
            try:
                fcntl.flock(writer.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                writer_was_excluded = True
        return original_lease(root, dist)

    monkeypatch.setattr(bundle, "_lease_dist", lease_while_probing_writer)

    snapshot = bundle.bundle_snapshot(tmp_path, pin=True)

    assert snapshot.status.ready
    assert snapshot.lease is not None
    assert writer_was_excluded
    snapshot.lease.close()


def test_symlinked_output_is_stale(tmp_path):
    dashboard = _dashboard(tmp_path)
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    _record_current_inputs(tmp_path)
    (dashboard / "dist" / "leak.txt").symlink_to(outside)

    status = bundle.bundle_status(tmp_path)

    assert status.state == "stale"
    assert "symlink" in status.detail


def test_symlink_loop_in_pointer_degrades_to_stale(tmp_path):
    dashboard = _dashboard(tmp_path)
    loop = dashboard / "dist" / "loop"
    loop.symlink_to(loop)
    (dashboard / "dist" / bundle.POINTER_NAME).write_text(
        json.dumps({"schema": 1, "build": "builds/../loop"})
    )

    status = bundle.bundle_status(tmp_path)

    assert status.state == "stale"
    assert "pointer is invalid" in status.detail


def test_collection_preserves_current_and_leased_builds(tmp_path):
    dashboard = tmp_path / "dashboard"
    builds = dashboard / "dist" / "builds"
    for name in ("current", "served", "abandoned"):
        (builds / name).mkdir(parents=True)
        (builds / name / "index.html").write_text(name)
    (dashboard / "dist" / bundle.POINTER_NAME).write_text(
        json.dumps({"schema": 1, "build": "builds/current"})
    )
    leases = dashboard / "dist" / ".leases"
    leases.mkdir()
    served_lease = (leases / "served.lock").open("a+b")
    fcntl.flock(served_lease.fileno(), fcntl.LOCK_SH)
    try:
        dashboard_build.garbage_collect_builds(dashboard)
        assert (builds / "current").is_dir()
        assert (builds / "served").is_dir()
        assert not (builds / "abandoned").exists()
    finally:
        served_lease.close()

    dashboard_build.garbage_collect_builds(dashboard)
    assert not (builds / "served").exists()


def test_build_timeout_terminates_the_process_group(tmp_path, monkeypatch):
    class HungProcess:
        pid = 9876

        def __init__(self):
            self.waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired("npm", timeout)
            return -15

    process = HungProcess()
    signals = []
    monkeypatch.setattr(
        dashboard_build.subprocess,
        "Popen",
        lambda argv, cwd, start_new_session: process,
    )
    monkeypatch.setattr(
        dashboard_build.os,
        "killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )

    result = dashboard_build.run_process(["npm", "ci"], tmp_path, 0.01)

    assert result == dashboard_build.TIMEOUT_EXIT
    assert signals == [(process.pid, signal.SIGTERM)]


def test_parent_termination_is_forwarded_to_the_nested_process_group(
    tmp_path, monkeypatch
):
    handlers = {}

    class RunningProcess:
        pid = 9876

        def __init__(self):
            self.waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                handlers[signal.SIGTERM](signal.SIGTERM, None)
            return -15

    process = RunningProcess()
    signals = []
    monkeypatch.setattr(
        dashboard_build.subprocess,
        "Popen",
        lambda argv, cwd, start_new_session: process,
    )
    monkeypatch.setattr(
        dashboard_build.signal,
        "getsignal",
        lambda _signal: signal.SIG_DFL,
    )
    monkeypatch.setattr(
        dashboard_build.signal,
        "signal",
        lambda signum, handler: handlers.__setitem__(signum, handler),
    )
    monkeypatch.setattr(
        dashboard_build.os,
        "killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )

    with pytest.raises(SystemExit, match="143"):
        dashboard_build.run_process(["npm", "ci"], tmp_path, 30)

    assert signals == [(process.pid, signal.SIGTERM)]


def test_build_lock_wait_is_included_in_the_timeout(tmp_path, monkeypatch):
    dashboard = tmp_path / "dashboard"
    dashboard.mkdir()
    times = iter((0.0, 1.0))
    monkeypatch.setattr(dashboard_build, "BUILD_TIMEOUT_S", 0.5)
    monkeypatch.setattr(dashboard_build.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(
        dashboard_build.fcntl,
        "flock",
        lambda _fd, _flags: (_ for _ in ()).throw(BlockingIOError()),
    )
    monkeypatch.setattr(
        dashboard_build,
        "run_process",
        lambda *_args: pytest.fail("build started without acquiring the lock"),
    )

    assert dashboard_build.build_dashboard(tmp_path) == dashboard_build.TIMEOUT_EXIT
