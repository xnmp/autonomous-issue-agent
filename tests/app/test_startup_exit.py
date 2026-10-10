import signal
import subprocess
import sys

import pytest

from issue_agent.adapters.control_store import (
    CorruptControlStore,
    IncompatibleSchema,
    RetiredOwnerError,
)
from issue_agent.app import main as main_module
from issue_agent.domain.recovery import RecoveryTrigger
from issue_agent.domain.supervision import ExitCode


@pytest.mark.parametrize(
    ("failure", "expected"),
    (
        (ValueError("bad config"), ExitCode.INVALID_CONFIG),
        (IncompatibleSchema("future"), ExitCode.INCOMPATIBLE_SCHEMA),
        (CorruptControlStore("bad db"), ExitCode.STORE_CORRUPTION),
    ),
)
def test_startup_failures_map_to_permanent_exit_classes(
    tmp_path, monkeypatch, failure, expected
):
    env_file = tmp_path / "agent.env"
    env_file.write_text('REPO="o/r"\nFIX_BOT="fixer"\n')
    monkeypatch.setenv("AGENT_ENV", str(env_file))
    monkeypatch.setattr(
        main_module, "acquire", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(
        main_module,
        "build_role",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(failure),
    )
    monkeypatch.setattr(
        main_module, "_recover_pre_adapter_processes", lambda *_args: ()
    )

    with pytest.raises(SystemExit) as stopped:
        main_module.main(["fix"])
    assert stopped.value.code is expected


def test_daemon_drains_on_sigterm_but_restores_the_callers_handler_on_exit(
    tmp_path, monkeypatch
):
    env_file = tmp_path / "agent.env"
    env_file.write_text('REPO="o/r"\nFIX_BOT="fixer"\n')
    monkeypatch.setenv("AGENT_ENV", str(env_file))
    monkeypatch.setattr(main_module, "acquire", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        main_module, "_recover_pre_adapter_processes", lambda *_args: ()
    )
    callers_handler = signal.getsignal(signal.SIGTERM)

    def build_role(*_args, **_kwargs):
        # The running daemon converts the first SIGTERM into an orderly drain.
        with pytest.raises(SystemExit) as drained:
            signal.raise_signal(signal.SIGTERM)
        assert drained.value.code == ExitCode.CLEAN
        raise ValueError("stop after observing the drain")

    monkeypatch.setattr(main_module, "build_role", build_role)

    with pytest.raises(SystemExit) as stopped:
        main_module.main(["fix"])

    assert stopped.value.code is ExitCode.INVALID_CONFIG
    assert signal.getsignal(signal.SIGTERM) is callers_handler


def test_daemon_process_keeps_the_drain_handler_through_interpreter_teardown():
    # A second SIGTERM while executor threads are joined must still map to the
    # stable TRANSIENT hard exit, so the process entry never restores SIG_DFL.
    probe = (
        "import signal, sys\n"
        "import issue_agent.app.main as m\n"
        "m._serve = lambda role: None\n"
        "sys.argv = ['main', 'fix']\n"
        "m._process_main()\n"
        "assert isinstance(signal.getsignal(signal.SIGTERM), m._TerminationHandler)\n"
    )

    completed = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=60
    )

    assert completed.returncode == 0, completed.stderr


def test_reload_candidate_runs_full_config_validation(monkeypatch):
    validated = []

    class Candidate:
        def validate(self):
            validated.append(True)

    monkeypatch.setattr(main_module, "_load_agent_env", lambda env: env)
    monkeypatch.setattr(main_module, "from_env", lambda _env: Candidate())

    main_module._validate_reload_candidate({"REPO": "o/r"})

    assert validated == [True]


def test_retired_deployment_owner_exits_cleanly_without_service_restart(
    tmp_path,
    monkeypatch,
):
    env_file = tmp_path / "agent.env"
    env_file.write_text('REPO="o/r"\nFIX_BOT="fixer"\n')
    monkeypatch.setenv("AGENT_ENV", str(env_file))
    monkeypatch.setattr(
        main_module, "acquire", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(
        main_module,
        "build_role",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RetiredOwnerError("old owner")
        ),
    )
    monkeypatch.setattr(
        main_module, "_recover_pre_adapter_processes", lambda *_args: ()
    )

    with pytest.raises(SystemExit) as stopped:
        main_module.main(["fix"])
    assert stopped.value.code is ExitCode.CLEAN


def test_second_sigterm_escalates_to_transient_hard_exit():
    exits = []
    handler = main_module._TerminationHandler(exits.append)

    with pytest.raises(SystemExit) as first:
        handler(15, None)
    handler(15, None)

    assert first.value.code is ExitCode.CLEAN
    assert exits == [int(ExitCode.TRANSIENT)]


def test_boot_recovery_is_explicitly_sequenced_before_readiness(
    tmp_path, monkeypatch
):
    events = []
    env_file = tmp_path / "agent.env"
    env_file.write_text('REPO="o/r"\n')
    monkeypatch.setenv("AGENT_ENV", str(env_file))
    monkeypatch.setenv("HOT_RELOAD", "0")
    monkeypatch.setenv("ISSUE_AGENT_MAX_TICKS", "1")
    monkeypatch.setattr(
        main_module, "acquire", lambda *_args, **_kwargs: object()
    )

    class Config:
        state_dir = str(tmp_path / "fix")
        review_state_dir = str(tmp_path / "review")
        steward_state_dir = str(tmp_path / "steward")

        def validate(self):
            events.append("validate")

    class Daemon:
        requires_hard_exit = False

        def reconcile(self, trigger):
            events.append(("reconcile", trigger))

        def run_forever(self, **_kwargs):
            events.append("run")

    class Notifier:
        def __init__(self, _env):
            pass

        def progress(self, _status):
            pass

        def ready(self, _status):
            events.append("ready")

        def stopping(self, _status):
            events.append("stopping")

    monkeypatch.setattr(main_module, "from_env", lambda _env: Config())
    monkeypatch.setattr(
        main_module,
        "_recover_pre_adapter_processes",
        lambda trigger, *_args: events.append(("preflight", trigger)) or (),
    )
    monkeypatch.setattr(
        main_module,
        "build_role",
        lambda *_args: events.append("build") or Daemon(),
    )
    monkeypatch.setattr(
        main_module,
        "migrate_legacy_spend",
        lambda *_args: events.append("migrate") or 0,
    )
    monkeypatch.setattr(main_module, "SystemdNotifier", Notifier)

    main_module.main(["fix"])

    assert events == [
        "validate",
        ("preflight", RecoveryTrigger.BOOT),
        "build",
        "migrate",
        ("reconcile", RecoveryTrigger.BOOT),
        "ready",
        "run",
        "stopping",
    ]


def test_missing_recovery_trigger_withholds_readiness(tmp_path, monkeypatch):
    env_file = tmp_path / "agent.env"
    env_file.write_text('REPO="o/r"\n')
    monkeypatch.setenv("AGENT_ENV", str(env_file))
    monkeypatch.setattr(
        main_module, "acquire", lambda *_args, **_kwargs: object()
    )

    class Config:
        state_dir = str(tmp_path / "fix")
        review_state_dir = str(tmp_path / "review")
        steward_state_dir = str(tmp_path / "steward")

        def validate(self):
            return None

    class Daemon:
        reconcile = None

    notifications = []

    class Notifier:
        def __init__(self, _env):
            pass

        def progress(self, _status):
            pass

        def ready(self, status):
            notifications.append(status)

    monkeypatch.setattr(main_module, "from_env", lambda _env: Config())
    monkeypatch.setattr(
        main_module, "_recover_pre_adapter_processes", lambda *_args: ()
    )
    monkeypatch.setattr(main_module, "build_role", lambda *_args: Daemon())
    monkeypatch.setattr(
        main_module, "migrate_legacy_spend", lambda *_args: 0
    )
    monkeypatch.setattr(main_module, "SystemdNotifier", Notifier)

    with pytest.raises(SystemExit) as stopped:
        main_module.main(["fix"])

    assert stopped.value.code is ExitCode.INVALID_CONFIG
    assert notifications == []


@pytest.mark.parametrize("failure_seam", ("max_ticks", "reload_watch"))
def test_systemd_ready_is_withheld_until_synchronous_startup_succeeds(
    tmp_path,
    monkeypatch,
    failure_seam,
):
    env_file = tmp_path / "agent.env"
    env_file.write_text('REPO="o/r"\n')
    monkeypatch.setenv("AGENT_ENV", str(env_file))
    monkeypatch.setenv("HOT_RELOAD", "1")
    monkeypatch.setattr(
        main_module, "acquire", lambda *_args, **_kwargs: object()
    )

    class Daemon:
        requires_hard_exit = False

        def reconcile(self, _trigger):
            return None

        def run_forever(self, **_kwargs):
            raise AssertionError("startup failure entered the daemon loop")

    class Config:
        repo = "o/r"
        repo_input = "o/r"
        target_dir = ""
        repointed_from_deployments = ()
        state_dir = str(tmp_path / "fix")
        review_state_dir = str(tmp_path / "review")
        steward_state_dir = str(tmp_path / "steward")

        def validate(self):
            return None

    notifications = []

    class Notifier:
        def __init__(self, _env):
            pass

        def ready(self, status):
            notifications.append(status)

    monkeypatch.setattr(main_module, "build_role", lambda *_args: Daemon())
    monkeypatch.setattr(main_module, "from_env", lambda _env: Config())
    monkeypatch.setattr(
        main_module, "migrate_legacy_spend", lambda *_args: 0
    )
    monkeypatch.setattr(main_module, "SystemdNotifier", Notifier)
    if failure_seam == "max_ticks":
        monkeypatch.setenv("ISSUE_AGENT_MAX_TICKS", "not-an-integer")
        expected = SystemExit
    else:
        monkeypatch.delenv("ISSUE_AGENT_MAX_TICKS", raising=False)

        class BrokenReloadWatch:
            def __init__(self, *_args, **_kwargs):
                raise RuntimeError("reload watch initialization failed")

        monkeypatch.setattr(
            "issue_agent.adapters.reload.ReloadWatch",
            BrokenReloadWatch,
        )
        expected = RuntimeError

    with pytest.raises(expected):
        main_module.main(["fix"])

    assert notifications == []
