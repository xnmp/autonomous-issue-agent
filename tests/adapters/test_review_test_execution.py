import asyncio
from dataclasses import replace
import os
import subprocess

import pytest

from issue_agent.adapters.process_supervisor import ProcessSupervisor
from issue_agent.adapters.review_test_execution import ReviewTestExecutor, freeze_test_configuration
from issue_agent.domain.review_test_evidence import SUMMARY_LIMIT, ReviewTestOutcome, OperatorTestCommand
from tests.domain.test_review_test_evidence import invocation


def target(tmp_path):
    cwd = tmp_path / "target"
    cwd.mkdir()
    for argv in (["init", "-q"], ["config", "user.name", "tests"], ["config", "user.email", "tests@example.test"]):
        subprocess.run(["git", *argv], cwd=cwd, check=True)
    (cwd / "tracked").write_text("original")
    subprocess.run(["git", "add", "."], cwd=cwd, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=cwd, check=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=cwd, text=True).strip()
    return cwd, head


def run(tmp_path, command, *, timeout="10", setup=lambda cwd: None, environment=None):
    cwd, head = target(tmp_path)
    env = dict(os.environ, TEST_CMD=command, GATE_TEST_TIMEOUT_S=timeout)
    env.update(environment or {})
    config, workload = freeze_test_configuration(env, "v1")
    value = invocation(config)
    value = replace(value, subject=replace(value.subject, head_sha=head))
    setup(cwd)
    executor = ReviewTestExecutor(ProcessSupervisor(str(tmp_path / "supervisor"), grace_seconds=0.05), workload)
    return asyncio.run(executor.execute(value, str(cwd))), cwd, executor, value


@pytest.mark.parametrize("command,outcome", [("true", ReviewTestOutcome.PASSED), ("exit 1", ReviewTestOutcome.FAILED), ("exit 2", ReviewTestOutcome.UNAVAILABLE), ("sleep 10", ReviewTestOutcome.UNAVAILABLE)])
def test_actual_nonpaid_execution_classifies_and_proves_inputs(tmp_path, command, outcome):
    result, _, _, _ = run(tmp_path, command, timeout="0.2" if command.startswith("sleep") else "10")
    assert result.outcome is outcome, repr(result)
    assert result.execution is not None
    assert result.before == result.after


@pytest.mark.parametrize("setup", [lambda cwd: (cwd / "tracked").write_text("dirty"), lambda cwd: subprocess.run(["git", "rm", "--cached", "tracked"], cwd=cwd, check=True)])
def test_dirty_tracked_worktree_or_index_cannot_authorize_pass(tmp_path, setup):
    result, cwd, _, _ = run(tmp_path, "touch executed", setup=setup)
    assert result.outcome is ReviewTestOutcome.UNAVAILABLE
    assert not (cwd / "executed").exists()


def test_test_mutating_tracked_inputs_cannot_authorize_pass(tmp_path):
    result, _, _, _ = run(tmp_path, "echo changed > tracked")
    assert result.outcome is ReviewTestOutcome.UNAVAILABLE


def test_frozen_environment_manifest_and_snapshot_do_not_read_ambient_authority(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_DEPLOY_KNOBS", "WORKLOAD")
    result, _, _, _ = run(tmp_path, 'test "$WORKLOAD" = value && test -z "$PRIVATE_KNOB"',
        environment={"WORKLOAD": "value", "PRIVATE_KNOB": "secret", "HARNESS_DEPLOY_KNOBS": "PRIVATE_KNOB"})
    assert result.outcome is ReviewTestOutcome.PASSED


def test_missing_operator_command_does_not_use_target_true_marker(tmp_path):
    result, cwd, _, _ = run(tmp_path, "", setup=lambda cwd: (cwd / ".harness").mkdir())
    assert result.outcome is ReviewTestOutcome.UNAVAILABLE


def test_large_separate_streams_are_drained_with_bounded_summary(tmp_path):
    result, _, _, _ = run(tmp_path, "head -c 1000000 /dev/zero; head -c 1000000 /dev/zero >&2")
    assert result.outcome is ReviewTestOutcome.PASSED
    assert len(result.summary) <= SUMMARY_LIMIT
    assert result.execution.stdout_digest == result.execution.stderr_digest


def test_registration_hung_wrapper_is_bounded_and_reaped(tmp_path, monkeypatch):
    monkeypatch.setattr(ProcessSupervisor, "supervised_command", lambda self, run_id, argv: (os.sys.executable, "-c", "import time; time.sleep(30)"))
    result, _, _, _ = run(tmp_path, "true", timeout="0.1")
    assert result.outcome is ReviewTestOutcome.UNAVAILABLE


@pytest.mark.parametrize("value", [None, True, [], "bad", "NaN", "Infinity", "0", "-1"])
def test_malformed_operator_timeout_is_rejected(value):
    with pytest.raises(ValueError):
        freeze_test_configuration({"GATE_TEST_TIMEOUT_S": value}, "v1")


def test_cancellation_awaits_supervised_child_teardown(tmp_path):
    cwd, head = target(tmp_path)
    config, workload = freeze_test_configuration(dict(os.environ, TEST_CMD="sleep 30"), "v1")
    value = invocation(config)
    value = replace(value, subject=replace(value.subject, head_sha=head))
    supervisor = ProcessSupervisor(str(tmp_path / "supervisor"), grace_seconds=0.05)
    executor = ReviewTestExecutor(supervisor, workload)
    async def exercise():
        task = asyncio.create_task(executor.execute(value, str(cwd)))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        await executor.recover(value)
    asyncio.run(exercise())
    assert supervisor.recover_orphans() == ()


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_suppressed_tracked_content_cannot_authorize_pass(tmp_path, flag):
    def setup(cwd):
        subprocess.run(["git", "update-index", flag, "tracked"], cwd=cwd, check=True)
        (cwd / "tracked").write_text("dirty")
    result, cwd, _, _ = run(tmp_path, "touch executed; test $(cat tracked) = dirty", setup=setup)
    assert result.outcome is ReviewTestOutcome.UNAVAILABLE and not (cwd / "executed").exists()


def test_test_suppressing_tracked_inputs_invalidates_postproof(tmp_path):
    result, _, _, _ = run(tmp_path, "git update-index --assume-unchanged tracked; echo dirty > tracked")
    assert result.outcome is ReviewTestOutcome.UNAVAILABLE


def test_cancel_during_teardown_is_propagated_after_reap(tmp_path, monkeypatch):
    cwd, head = target(tmp_path)
    config, workload = freeze_test_configuration(dict(os.environ, TEST_CMD="true"), "v1")
    value = invocation(config)
    value = replace(value, subject=replace(value.subject, head_sha=head))
    executor = ReviewTestExecutor(ProcessSupervisor(str(tmp_path / "supervisor"), grace_seconds=0.05), workload)
    async def exercise():
        entered = asyncio.Event()
        original = ReviewTestExecutor.recover
        async def delayed(self, selected):
            entered.set()
            await asyncio.sleep(0.05)
            await original(self, selected)
        monkeypatch.setattr(ReviewTestExecutor, "recover", delayed)
        task = asyncio.create_task(executor.execute(value, str(cwd)))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
    asyncio.run(exercise())
    assert executor.supervisor.recover_orphans() == ()


def test_nonpaid_test_launcher_drops_inherited_accounting_and_publication_authority(tmp_path):
    result, cwd, _, _ = run(tmp_path, "touch executed; test -z \"$ISSUE_AGENT_CONTROL_DB\" && test -z \"$GH_TOKEN\"",
        environment={"ISSUE_AGENT_CONTROL_DB": "/does/not/exist", "ISSUE_AGENT_RUN_ID": "stale-paid-run",
                     "ISSUE_AGENT_OWNER_ID": "stale-owner", "GH_TOKEN": "secret"})
    assert result.outcome is ReviewTestOutcome.PASSED and (cwd / "executed").exists()


def test_supervised_bootstrap_refusal_is_not_a_genuine_test_failure(tmp_path, monkeypatch):
    original = ProcessSupervisor.launch_guard_env
    monkeypatch.setattr(ProcessSupervisor, "launch_guard_env", lambda self: dict(original(self),
        ISSUE_AGENT_SUPERVISOR_PARENT_START_TICKS="0"))
    result, cwd, _, _ = run(tmp_path, "touch executed; exit 1")
    assert result.outcome is ReviewTestOutcome.UNAVAILABLE and result.execution.exit_code == 125
    assert not (cwd / "executed").exists()


def test_same_length_restored_mtime_stat_cache_cannot_hide_dirty_inputs(tmp_path):
    def setup(cwd):
        original = (cwd / "tracked").stat()
        subprocess.run(["git", "config", "core.trustctime", "false"], cwd=cwd, check=True)
        (cwd / "tracked").write_text("modified")
        os.utime(cwd / "tracked", ns=(original.st_atime_ns, original.st_mtime_ns))
        index = cwd / ".git" / "index"
        stat = index.stat()
        os.utime(index, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000_000))
    result, cwd, _, _ = run(tmp_path, "touch executed; test $(cat tracked) = modified", setup=setup)
    assert result.outcome is ReviewTestOutcome.UNAVAILABLE and not (cwd / "executed").exists()


def test_test_restoring_mtime_still_invalidates_post_tree_proof(tmp_path):
    command = "git config core.trustctime false; cp -p tracked stamp; echo -n modified > tracked; touch -r stamp tracked"
    result, _, _, _ = run(tmp_path, command)
    assert result.outcome is ReviewTestOutcome.UNAVAILABLE


def test_target_filemode_config_cannot_hide_pretest_executable_change(tmp_path):
    def setup(cwd):
        subprocess.run(["git", "config", "core.filemode", "false"], cwd=cwd, check=True)
        (cwd / "tracked").chmod(0o755)
    result, cwd, _, _ = run(tmp_path, "touch executed; test -x tracked", setup=setup)
    assert result.outcome is ReviewTestOutcome.UNAVAILABLE and not (cwd / "executed").exists()


def test_target_filemode_config_cannot_hide_posttest_executable_change(tmp_path):
    result, _, _, _ = run(tmp_path, "git config core.filemode false; chmod +x tracked")
    assert result.outcome is ReviewTestOutcome.UNAVAILABLE


def test_ambient_git_worktree_cannot_substitute_pristine_checkout(tmp_path, monkeypatch):
    sibling = tmp_path / "pristine"
    sibling.mkdir()
    (sibling / "tracked").write_text("original")
    def setup(cwd):
        (cwd / "tracked").write_text("modified")
        monkeypatch.setenv("GIT_WORK_TREE", str(sibling))
    result, _, _, _ = run(tmp_path, "true", setup=setup)
    assert result.outcome is ReviewTestOutcome.UNAVAILABLE


def test_ambient_git_index_cannot_replace_actual_dirty_index(tmp_path, monkeypatch):
    from issue_agent.adapters.review_test_execution import clean_tree
    cwd, head = target(tmp_path)
    alternate = tmp_path / "alternate-index"
    alternate.write_bytes((cwd / ".git" / "index").read_bytes())
    subprocess.run(["git", "rm", "--cached", "tracked"], cwd=cwd, check=True)
    monkeypatch.setenv("GIT_INDEX_FILE", str(alternate))
    with pytest.raises(ValueError):
        clean_tree(str(cwd), head)


def test_target_symlink_config_cannot_hide_committed_file_type_change(tmp_path):
    from issue_agent.adapters.review_test_execution import clean_tree
    cwd, _ = target(tmp_path)
    link = cwd / "link"
    link.symlink_to("tracked")
    subprocess.run(["git", "add", "link"], cwd=cwd, check=True)
    subprocess.run(["git", "commit", "-qm", "link"], cwd=cwd, check=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=cwd, text=True).strip()
    subprocess.run(["git", "config", "core.symlinks", "false"], cwd=cwd, check=True)
    link.unlink()
    link.write_text("tracked")
    with pytest.raises(ValueError):
        clean_tree(str(cwd), head)


def test_huge_output_keeps_failure_summary_printed_last(tmp_path):
    result, _, _, _ = run(tmp_path, "head -c 1000000 /dev/zero | tr '\\0' x; echo; echo 'FAILED 3 tests: test_widget'; exit 1")
    assert result.outcome is ReviewTestOutcome.FAILED
    assert result.summary.rstrip().endswith("FAILED 3 tests: test_widget")
    assert "earlier output omitted" in result.summary
    assert len(result.summary) <= SUMMARY_LIMIT


def test_output_shorter_than_bound_is_kept_in_full_without_marker(tmp_path):
    result, _, _, _ = run(tmp_path, "echo first; echo second >&2; exit 1")
    assert "first" in result.summary and "second" in result.summary
    assert "omitted" not in result.summary


def test_empty_output_yields_outcome_only_summary(tmp_path):
    result, _, _, _ = run(tmp_path, "exit 1")
    assert result.summary.strip() == ReviewTestOutcome.FAILED.value


def test_completion_captures_clean_terminal_output(tmp_path):
    result, _, _, _ = run(tmp_path, "printf '\\033[32méchec 日本語\\033[0m\\n'; exit 1")
    assert result.summary == "failed\néchec 日本語\n"
    assert "\x1b" not in result.summary


@pytest.mark.parametrize("escape", [b"\x1b[32m", b"\x1b]title\x07", b"\x1b]title\x1b\\", b"\x1b7"])
@pytest.mark.parametrize("split", [1, 2, 3])
def test_truncated_and_chunked_capture_does_not_expose_escape_fragments(escape, split):
    from issue_agent.adapters.review_test_execution import _OutputTail, EVIDENCE_TAIL_BYTES

    output = _OutputTail()
    output.extend(b"x" * (2 * EVIDENCE_TAIL_BYTES) + escape[:split])
    output.extend(escape[split:] + b"FAIL\n" + b"y" * (EVIDENCE_TAIL_BYTES - 8))
    text = output.text()
    assert "FAIL\n" in text
    assert "32m" not in text and "title" not in text and "\x1b" not in text


@pytest.mark.parametrize("opener,terminator", [(b"\x1b]", b"\x07"), (b"\x1b[", b"m")])
def test_escape_larger_than_capture_bound_does_not_leak_its_continuation(opener, terminator):
    from issue_agent.adapters.review_test_execution import _OutputTail, EVIDENCE_TAIL_BYTES

    output = _OutputTail()
    output.extend(opener + b"3" * (4 * EVIDENCE_TAIL_BYTES))
    output.extend(b"33" + terminator + "résultat".encode())
    assert output.text().endswith("résultat")
    assert "3" not in output.text() and "\x1b" not in output.text()


def test_oversized_osc_with_split_string_terminator_keeps_following_output():
    from issue_agent.adapters.review_test_execution import _OutputTail, EVIDENCE_TAIL_BYTES

    output = _OutputTail()
    output.extend(b"\x1b]" + b"title" * EVIDENCE_TAIL_BYTES + b"\x1b")
    output.extend(b"\\FAIL\n")
    assert output.text().endswith("FAIL\n")
    assert "title" not in output.text() and "\x1b" not in output.text()


def test_interleaved_streams_stay_bounded_and_keep_last_line(tmp_path):
    command = "for i in $(seq 1 20000); do echo out$i; echo err$i >&2; done; echo FINAL-LINE >&2; exit 1"
    result, _, _, _ = run(tmp_path, command)
    assert len(result.summary) <= SUMMARY_LIMIT
    assert "FINAL-LINE" in result.summary
    assert "out1\n" not in result.summary


@pytest.mark.parametrize("prefix", ["", "a"])
def test_multibyte_character_split_at_cut_decodes_cleanly(tmp_path, prefix):
    result, _, _, _ = run(tmp_path, f"printf {prefix!r}; for i in $(seq 20000); do printf é; done; echo END; exit 1")
    assert "�" not in result.summary
    assert result.summary.rstrip().endswith("éEND")
    assert len(result.summary) <= SUMMARY_LIMIT


def test_compiler_error_survives_a_long_echoed_command_line(tmp_path):
    # Live shape (Tauri PR #1075): cargo prints the rustc error, then echoes the
    # failed multi-kilobyte rustc invocation last.
    command = (
        "head -c 200000 /dev/zero | tr '\\0' x; echo; "
        "echo 'error[E0425]: cannot find value `widget` in this scope'; "
        "printf 'error: could not compile; process didn'\\''t exit successfully: `rustc'; "
        "for i in $(seq 120); do printf ' --extern dep%d=/cache/target/debug/deps/libdep%d-0123456789abcdef.rlib' $i $i; done; "
        "echo '` (exit status: 1)'; exit 1"
    )
    result, _, _, _ = run(tmp_path, command)
    assert "error[E0425]: cannot find value `widget`" in result.summary
    assert result.summary.rstrip().endswith("(exit status: 1)")
    assert len(result.summary) <= SUMMARY_LIMIT
