"""Hot reload — the restart may only ever fire between ticks, and a broken
watch must cost hot reload, never the daemon."""
from __future__ import annotations

import os
import time

import pytest

from issue_agent.adapters.reload import RELOAD_EXIT_CODE, ReloadWatch
from issue_agent.app.execute import Executor
from issue_agent.app.loop import Daemon
from issue_agent.domain.world import WorldState


def _tree(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    f = src / "a.py"
    f.write_text("x = 1\n")
    return src, f


def test_watch_detects_edits_additions_and_deletions(tmp_path):
    src, f = _tree(tmp_path)
    w = ReloadWatch(roots=(str(src),))
    assert w.changed() is False

    os.utime(f, (time.time() + 5, time.time() + 5))       # edit (mtime bump)
    assert w.changed() is True

    w2 = ReloadWatch(roots=(str(src),))
    (src / "b.py").write_text("y = 2\n")                   # addition
    assert w2.changed() is True

    w3 = ReloadWatch(roots=(str(src),))
    f.unlink()                                             # deletion
    assert w3.changed() is True


def test_watch_ignores_unwatched_suffixes_and_survives_missing_roots(tmp_path):
    src, _ = _tree(tmp_path)
    w = ReloadWatch(roots=(str(src),))
    (src / "scratch.log").write_text("noise\n")            # not a watched suffix
    assert w.changed() is False
    # A root that never existed fingerprints as empty — no crash, no restart loop.
    w2 = ReloadWatch(roots=(str(tmp_path / "nope"),))
    assert w2.changed() is False


def test_explicit_files_are_watched(tmp_path):
    src, _ = _tree(tmp_path)
    env = tmp_path / "active.env"
    env.write_text("A=1\n")
    w = ReloadWatch(roots=(str(src),), files=(str(env),))
    os.utime(env, (time.time() + 5, time.time() + 5))
    assert w.changed() is True


class _Gw:
    def relabel(self, *a): ...


def test_daemon_exits_with_reload_code_between_ticks():
    """The change is noticed only AFTER a completed tick (no session can be in
    flight), the tick count still advanced, and the exit carries the reserved
    code point-at's relaunch loop matches on."""
    ticked = []

    d = Daemon(
        name="fix",
        observe=lambda: WorldState(),
        decide=lambda w: ticked.append(1) or [],
        executor=Executor(_Gw()),
        poll_seconds=1,
    )
    logs = []
    d.log = logs.append
    with pytest.raises(SystemExit) as exc:
        d.run_forever(
            sleep=lambda s: None, now=lambda: 0,
            max_ticks=5, reload_check=lambda: len(ticked) >= 2,
        )
    assert exc.value.code == RELOAD_EXIT_CODE
    assert len(ticked) == 2                       # ran ticks, then left between them
    assert any("hot-reload" in m for m in logs)


def test_daemon_without_reload_check_never_exits_early():
    ticked = []
    d = Daemon(
        name="fix", observe=lambda: WorldState(),
        decide=lambda w: ticked.append(1) or [],
        executor=Executor(_Gw()), poll_seconds=1,
    )
    d.run_forever(sleep=lambda s: None, now=lambda: 0, max_ticks=3)
    assert len(ticked) == 3
