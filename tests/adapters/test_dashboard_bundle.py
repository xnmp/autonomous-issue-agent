import hashlib
import json
import os


from issue_agent.adapters.dashweb import bundle


def _dashboard(tmp_path):
    dashboard = tmp_path / "dashboard"
    source = dashboard / "src"
    dist = dashboard / "dist"
    source.mkdir(parents=True)
    dist.mkdir()
    (dashboard / "build-inputs.json").write_text(
        json.dumps({"schema": 1, "files": ["build-inputs.json", "index.html"], "directories": ["src", "public"]})
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
    (tmp_path / "dashboard" / "dist" / bundle.MANIFEST_NAME).write_text(json.dumps(manifest))


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
    assert bundle.source_hashes(tmp_path)["src/main.tsx"] == hashlib.sha256(source.read_bytes()).hexdigest()


def test_atomic_pointer_selects_a_version_and_rejects_escape(tmp_path):
    dashboard = _dashboard(tmp_path)
    legacy_dist = dashboard / "dist"
    version = legacy_dist / "builds" / "v1"
    version.mkdir(parents=True)
    (version / "index.html").write_text("<html>version one</html>")
    (legacy_dist / bundle.POINTER_NAME).write_text(json.dumps({"schema": 1, "build": "builds/v1"}))
    (version / bundle.MANIFEST_NAME).write_text(
        json.dumps({"schema": 1, "inputs": bundle.source_hashes(tmp_path), "outputs": bundle.output_hashes(tmp_path)})
    )

    assert bundle.published_dist(tmp_path) == version.resolve()
    assert bundle.bundle_status(tmp_path).ready

    (legacy_dist / bundle.POINTER_NAME).write_text(json.dumps({"schema": 1, "build": "../../outside"}))
    escaped = bundle.bundle_status(tmp_path)
    assert escaped.state == "stale"
    assert "escapes" in escaped.detail


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
    (dashboard / "dist" / bundle.POINTER_NAME).write_text(json.dumps({"schema": 1, "build": "builds/../loop"}))

    status = bundle.bundle_status(tmp_path)

    assert status.state == "stale"
    assert "pointer is invalid" in status.detail
