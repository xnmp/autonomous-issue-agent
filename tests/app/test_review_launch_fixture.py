"""The acceptance provider retains real child identity and bounded cleanup."""

from dataclasses import replace
import os
import subprocess
from types import SimpleNamespace

import pytest

from issue_agent.adapters.control_store import ControlStoreError, SQLiteControlStore
from issue_agent.adapters.process_supervisor import ProcessSupervisor
from tests.adapters.test_control_store import _request, _store
from tests.app.review_acceptance_harness import register_review_launch
from tests.app import review_acceptance_harness


@pytest.mark.parametrize("error", [None, ControlStoreError, KeyboardInterrupt],
                         ids=["accepted", "refused", "interrupted"])
def test_review_launch_binds_a_live_isolated_child_and_reaps_it_even_on_refusal(tmp_path, monkeypatch, error):
    store = _store(tmp_path)
    request = replace(_request(1), owner_id="o/r:review:launch-fixture")
    assert store.reserve(request).admitted
    identities = []
    register = SQLiteControlStore.register_process_lease

    def observed_register(self, run_id, **kwargs):
        identity = ProcessSupervisor.process_identity(kwargs["pid"])
        assert identity is not None and identity.start_ticks == kwargs["process_start_ticks"]
        assert kwargs["pgid"] == os.getpgid(identity.pid) == identity.pid
        assert kwargs["pgid"] != os.getpgrp()
        identities.append(identity)
        if error is not None:
            raise error("launch refused")
        return register(self, run_id, **kwargs)

    monkeypatch.setattr(SQLiteControlStore, "register_process_lease", observed_register)
    task = SimpleNamespace(env={
        "ISSUE_AGENT_CONTROL_DB": store.path,
        "ISSUE_AGENT_RUN_ID": str(request.run_id),
        "ISSUE_AGENT_OWNER_ID": request.owner_id,
    })
    if error is not None:
        with pytest.raises(error, match="launch refused"):
            register_review_launch(task)
        assert store.leases() == ()
    else:
        register_review_launch(task)
        lease, = store.leases()
        assert lease.run_id == request.run_id and lease.pid == identities[0].pid
        assert lease.process_start_ticks == identities[0].start_ticks
    assert len(identities) == 1
    assert not ProcessSupervisor.matches_identity(identities[0].pid, identities[0].start_ticks)
    with pytest.raises(ChildProcessError):
        os.waitpid(identities[0].pid, os.WNOHANG)


def test_review_launch_kills_and_reaps_a_child_that_does_not_leave_its_barrier(tmp_path, monkeypatch):
    store = _store(tmp_path)
    request = replace(_request(1), owner_id="o/r:review:launch-fixture")
    assert store.reserve(request).admitted
    children = []
    spawn = subprocess.Popen

    def delayed_child(args, **kwargs):
        child = spawn([*args[:3], "import time; time.sleep(60)"], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(review_acceptance_harness.subprocess, "Popen", delayed_child)
    task = SimpleNamespace(env={
        "ISSUE_AGENT_CONTROL_DB": store.path,
        "ISSUE_AGENT_RUN_ID": str(request.run_id),
        "ISSUE_AGENT_OWNER_ID": request.owner_id,
    })
    with pytest.raises(subprocess.TimeoutExpired):
        register_review_launch(task)
    child, = children
    assert child.returncode is not None and child.returncode < 0
    with pytest.raises(ChildProcessError):
        os.waitpid(child.pid, os.WNOHANG)
