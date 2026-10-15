"""The dashboard's Settings: live knobs read from and written to the real
config files, composed with the real compose-env.sh, picked up by a running
daemon's next tick without a reload (ADR 0007 amendment)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from asgi_client import ASGITestClient as TestClient  # noqa: E402
from issue_agent.adapters.reload import ReloadWatch  # noqa: E402
from issue_agent.adapters.dashweb.ghdata import GhReader  # noqa: E402
from issue_agent.app.config import LIVE_ENV_NAMES, from_env, reload_fingerprint  # noqa: E402
from issue_agent.app.dashweb import server  # noqa: E402
from issue_agent.app.dashweb.service import Backend  # noqa: E402
from issue_agent.app.live_config import LiveConfig  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
HEADERS = {"X-Issue-Agent-Dashboard": "1"}
BASE_URL = "http://127.0.0.1:8321"
SHARED = """# shared defaults
FIX_BOT=fix-bot
REVIEW_BOT='review-bot[bot]'
FIXER_RUNTIME=claude
REVIEWER_RUNTIME=claude
REVIEW_MODEL=opus
export FIX_MODEL_LOW=claude:sonnet:low  # the cheap tier
"""


open_issues: list[dict] = [{"number": 1, "state": "OPEN"}]  # what the fake `gh issue list` answers
gh_error = {"error": ""}


def _gh(_args):
    return ("", gh_error["error"]) if gh_error["error"] else (json.dumps(open_issues), "")


@pytest.fixture
def harness(tmp_path):
    open_issues[:] = [{"number": 1, "state": "OPEN"}]
    gh_error["error"] = ""
    root = tmp_path / "harness"
    (root / "config" / "repos").mkdir(parents=True)
    (root / "bin").mkdir()
    for script in ("compose-env.sh", "point-at.sh"):
        shutil.copy(ROOT / "bin" / script, root / "bin" / script)
    (root / "config" / "shared.env").write_text(SHARED)
    (root / "config" / "shared.env").chmod(0o600)
    (root / "config" / "repos" / "demo.env").write_text(
        f"# the demo overlay\nREPO=o/r\nTARGET_DIR={tmp_path}\nSTATE_DIR={tmp_path / 'state'}\n"
        f"REVIEW_STATE_DIR={tmp_path / 'review-state'}\n# two at once\nFIX_DISPATCH_CONCURRENCY=2\n")
    home = tmp_path / "home"
    environ = {"PATH": os.environ["PATH"], "HOME": str(home)}
    subprocess.run(["bash", str(root / "bin" / "compose-env.sh"), "demo"], env=environ, check=True,
                   capture_output=True)
    snapshot = home / ".config" / "issue-agent" / "demo.env"  # an installed service reads this copy
    snapshot.parent.mkdir(parents=True)
    shutil.copy(root / "config" / ".active.demo.env", snapshot)
    backend = Backend(harness_root=str(root), gh=GhReader(run=_gh),
                      environ=environ)
    app = server.create_app(backend=backend, harness_root=str(root), offload_backend=False)
    return root, snapshot, tmp_path, TestClient(app, headers=HEADERS, base_url=BASE_URL)


def _rows(client) -> dict[str, dict]:
    return {row["key"]: row for row in client.get("/api/repos/demo/settings").json()["settings"]}


def _files(root: Path) -> tuple[str, str, str]:
    config = root / "config"
    return (config / "shared.env").read_text(), (config / "repos" / "demo.env").read_text(), \
        (config / ".active.demo.env").read_text()


def test_settings_show_every_live_knob_with_its_effective_value_and_source_and_nothing_else(harness):
    *_, client = harness
    rows = _rows(client)
    assert set(rows) == LIVE_ENV_NAMES  # no secret, no restart-only knob
    assert (rows["FIX_DISPATCH_CONCURRENCY"]["value"], rows["FIX_DISPATCH_CONCURRENCY"]["source"]) == ("2", "repo")
    assert (rows["FIX_MODEL_LOW"]["value"], rows["FIX_MODEL_LOW"]["source"]) == ("claude:sonnet:low", "shared")
    assert (rows["ISSUE_PICKUP"]["value"], rows["ISSUE_PICKUP"]["source"]) == ("ready", "default")
    assert rows["ISSUE_PICKUP"]["choices"] == ["ready", "open"]
    assert rows["FIX_MODEL_MED"]["effective"] == "claude:(runtime default)"
    assert rows["REVIEW_MODEL"]["runtime"] == "claude"


def test_a_saved_setting_reaches_the_running_daemon_at_its_next_tick_without_a_reload(harness):
    root, snapshot, tmp_path, client = harness
    boot = from_env({"AGENT_ENV": str(snapshot), "PATH": os.environ["PATH"]})
    live = LiveConfig(boot, str(snapshot))
    watch = ReloadWatch(str(tmp_path), files=(str(snapshot),),
                        fingerprint=lambda path: reload_fingerprint(path, {"PATH": os.environ["PATH"]}, "fix"))

    response = client.post("/api/repos/demo/settings", json={"key": "FIX_DISPATCH_CONCURRENCY", "value": "4"})

    assert response.status_code == 200 and response.json()["ok"], response.json()
    _, overlay, active = _files(root)
    assert "# two at once\nFIX_DISPATCH_CONCURRENCY=4\n" in overlay and overlay.startswith("# the demo overlay\n")
    assert "FIX_DISPATCH_CONCURRENCY=4" in active
    assert snapshot.read_text() == active
    assert live.refresh().fix_dispatch_concurrency == 4
    assert watch.changed() is False
    assert _rows(client)["FIX_DISPATCH_CONCURRENCY"]["value"] == "4"
    journal = [json.loads(line) for line in (tmp_path / "state" / "journal.jsonl").read_text().splitlines()]
    assert [(e["event"], e["key"], e["old"], e["new"], e["scope"]) for e in journal] == [
        ("settings_change", "FIX_DISPATCH_CONCURRENCY", "2", "4", "repo")]
    assert journal[0]["operator"]


def test_a_write_without_the_dashboard_header_is_refused(harness):
    root, _, _, client = harness
    before = _files(root)
    bare = TestClient(client._app, base_url=BASE_URL)
    response = bare.post("/api/repos/demo/settings", json={"key": "FIX_DISPATCH_CONCURRENCY", "value": "4"})
    assert response.status_code == 403
    assert _files(root) == before


@pytest.mark.parametrize("key,value", [
    ("FIX_DISPATCH_CONCURRENCY", "0"),
    ("FIX_DISPATCH_CONCURRENCY", "lots"),
    ("FIX_DISPATCH_CONCURRENCY", "999"),
    ("ISSUE_SPEND_CAP_USD", "-5"),
    ("DAILY_SPEND_CAP_USD", "inf"),
    ("FIX_MODEL_LOW", "gemini:pro"),
    ("FIX_MODEL_HIGH", "claude:opus:sideways"),
    ("ISSUE_PICKUP", "everything"),
    ("REPO", "o/evil"),  # not a live knob
    ("FIX_BOT_TOKEN", "ghp_x"),  # never a secret
    ("FIX_MODEL", "a\nREPO=o/evil"),
])
def test_an_invalid_or_foreign_value_is_refused_and_nothing_changes(harness, key, value):
    root, snapshot, _, client = harness
    before, published = _files(root), snapshot.read_text()
    response = client.post("/api/repos/demo/settings", json={"key": key, "value": value})
    assert response.status_code == 400 and response.json()["ok"] is False
    assert _files(root) == before and snapshot.read_text() == published


@pytest.mark.parametrize("body", [
    {"key": "FIX_DISPATCH_CONCURRENCY", "value": "3", "extra": 1},
    {"key": "FIX_MODEL", "value": "x" * 1001},
    {"key": "FIX_MODEL", "value": "x", "scope": "machine"},
    {"value": "3"},
])
def test_a_malformed_body_is_a_422(harness, body):
    root, _, _, client = harness
    before = _files(root)
    assert client.post("/api/repos/demo/settings", json=body).status_code == 422
    assert _files(root) == before


def test_an_unknown_deployment_is_a_404(harness):
    *_, client = harness
    assert client.get("/api/repos/nosuch/settings").status_code == 404
    assert client.post("/api/repos/nosuch/settings", json={"key": "FIX_MODEL", "value": "x"}).status_code == 404


def test_a_shared_write_the_overlay_overrides_warns_and_unsetting_the_overlay_falls_back_to_it(harness):
    root, *_, client = harness
    shared = client.post("/api/repos/demo/settings",
                         json={"key": "FIX_DISPATCH_CONCURRENCY", "value": "3", "scope": "shared"}).json()
    assert shared["ok"] and any("still sets FIX_DISPATCH_CONCURRENCY" in w for w in shared["warnings"])
    assert _rows(client)["FIX_DISPATCH_CONCURRENCY"]["value"] == "2"

    assert client.post("/api/repos/demo/settings", json={"key": "FIX_DISPATCH_CONCURRENCY", "value": None}).json()["ok"]
    row = _rows(client)["FIX_DISPATCH_CONCURRENCY"]
    assert (row["value"], row["source"]) == ("3", "shared")
    assert "FIX_DISPATCH_CONCURRENCY" not in _files(root)[1]
    assert (root / "config" / "shared.env").stat().st_mode & 0o777 == 0o600


def test_a_candidate_that_cannot_compose_is_refused_before_anything_is_written(harness):
    root, snapshot, _, client = harness
    (root / "bin" / "point-at.sh").write_text("# the scrub floor is gone: compose refuses\n")
    before, published = _files(root), snapshot.read_text()
    body = client.post("/api/repos/demo/settings", json={"key": "ISSUE_PICKUP", "value": "open"}).json()
    assert body["ok"] is False and "refused" in body["detail"]
    assert _files(root) == before and snapshot.read_text() == published


def test_a_shared_write_that_fails_for_one_deployment_republishes_the_ones_already_applied(harness, monkeypatch):
    """An installed service reads its snapshot, not `.active`: a rollback that
    only recomposed would leave it running the refused value."""
    root, snapshot, tmp_path, client = harness
    environ = {"PATH": os.environ["PATH"], "HOME": str(tmp_path / "home")}
    later = root / "config" / "repos" / "zz-later.env"
    later.write_text(f"REPO=o/later\nTARGET_DIR={tmp_path}/later\nSTATE_DIR={tmp_path / 'later-state'}\n"
                     f"REVIEW_STATE_DIR={tmp_path / 'later-review'}\n")
    subprocess.run(["bash", str(root / "bin" / "compose-env.sh"), "zz-later"], env=environ, check=True,
                   capture_output=True)
    from issue_agent.adapters import config_files
    compose = config_files.compose

    def flaky(harness_root, name, env):  # the real compose fails after the candidate passed
        if harness_root == str(root) and name == "zz-later":
            return "compose-env.sh failed: disk full"
        return compose(harness_root, name, env)

    monkeypatch.setattr(config_files, "compose", flaky)
    before, published = _files(root), snapshot.read_text()

    body = client.post("/api/repos/demo/settings",
                       json={"key": "DAILY_SPEND_CAP_USD", "value": "7", "scope": "shared"}).json()

    assert body["ok"] is False and "restored" in body["detail"], body
    assert _files(root) == before
    assert snapshot.read_text() == published


def test_a_save_is_refused_while_a_hand_edit_needing_a_restart_is_pending(harness):
    """A non-live edit waits for the operator's own launch: shipping it with a
    cap change would drain and restart every daemon behind 'next tick'."""
    root, snapshot, _, client = harness
    shared = root / "config" / "shared.env"
    shared.write_text(shared.read_text() + "POLL_SECONDS=5\n")
    before, published = _files(root), snapshot.read_text()

    body = client.post("/api/repos/demo/settings", json={"key": "DAILY_SPEND_CAP_USD", "value": "7"}).json()

    assert body["ok"] is False and "POLL_SECONDS" in body["detail"] and "restart" in body["detail"]
    assert _files(root) == before and snapshot.read_text() == published


def test_a_route_onto_a_runtime_the_daemons_did_not_bind_is_refused_as_needing_a_restart(harness):
    root, *_, client = harness
    before = _files(root)
    body = client.post("/api/repos/demo/settings", json={"key": "FIX_MODEL_LOW", "value": "codex:gpt-6-luna:low"}).json()
    assert body["ok"] is False and "runtimes" in body["detail"]
    assert _files(root) == before


def test_a_hand_edit_made_while_saving_is_never_overwritten(harness, monkeypatch):
    root, *_, client = harness
    overlay = root / "config" / "repos" / "demo.env"
    from issue_agent.adapters import config_files
    compose_staged = config_files.compose_staged

    def racing(*args, **kwargs):
        overlay.write_text(overlay.read_text() + "# the operator's edit\n")
        return compose_staged(*args, **kwargs)

    monkeypatch.setattr(config_files, "compose_staged", racing)
    body = client.post("/api/repos/demo/settings", json={"key": "FIX_DISPATCH_CONCURRENCY", "value": "4"}).json()

    assert body["ok"] is False and "changed since it was read" in body["detail"]
    assert overlay.read_text().endswith("FIX_DISPATCH_CONCURRENCY=2\n# the operator's edit\n")


@pytest.mark.parametrize("count,ok", [(999, True), (1000, False)])
def test_open_pickup_is_refused_past_the_fix_observations_item_bound(harness, count, ok):
    root, *_, client = harness
    open_issues[:] = [{"number": n, "state": "OPEN"} for n in range(1, count + 1)]
    body = client.post("/api/repos/demo/settings", json={"key": "ISSUE_PICKUP", "value": "open"}).json()
    assert body["ok"] is ok, body
    assert ("ISSUE_PICKUP" in _files(root)[1]) is ok


def test_open_pickup_is_refused_when_the_open_issues_cannot_be_read(harness):
    root, *_, client = harness
    gh_error["error"] = "HTTP 502"
    body = client.post("/api/repos/demo/settings", json={"key": "ISSUE_PICKUP", "value": "open"}).json()
    assert body["ok"] is False and "could not be counted" in body["detail"]
