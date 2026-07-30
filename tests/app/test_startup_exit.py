import pytest

from issue_agent.adapters.control_store import (
    CorruptControlStore,
    IncompatibleSchema,
)
from issue_agent.app import main as main_module
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
    env_file.write_text('REPO="o/r"\n')
    monkeypatch.setenv("AGENT_ENV", str(env_file))
    monkeypatch.setattr(
        main_module, "acquire", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(
        main_module,
        "build_role",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(failure),
    )

    with pytest.raises(SystemExit) as stopped:
        main_module.main(["fix"])
    assert stopped.value.code is expected


def test_reload_candidate_runs_full_config_validation(monkeypatch):
    validated = []

    class Candidate:
        def validate(self):
            validated.append(True)

    monkeypatch.setattr(main_module, "_load_agent_env", lambda env: env)
    monkeypatch.setattr(main_module, "from_env", lambda _env: Candidate())

    main_module._validate_reload_candidate({"REPO": "o/r"})

    assert validated == [True]


def test_second_sigterm_escalates_to_transient_hard_exit():
    exits = []
    handler = main_module._TerminationHandler(exits.append)

    with pytest.raises(SystemExit) as first:
        handler(15, None)
    handler(15, None)

    assert first.value.code is ExitCode.CLEAN
    assert exits == [int(ExitCode.TRANSIENT)]
