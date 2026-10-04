"""Awaited supervised execution under a frozen operator test scope."""

import asyncio
from dataclasses import dataclass
import hashlib
import json
import os
import signal
import subprocess
import time
import tempfile
from decimal import Decimal, InvalidOperation
from collections.abc import Mapping
from types import MappingProxyType

from ..domain.contracts import RunId, TestExecution, TestExecutionStatus, classify_test_exit_code, parse_test_failure_exit_codes
from ..domain.review_test_evidence import (
    MissingOperatorTestCommand, OperatorTestCommand, OperatorTestWaiver, ReviewTestConfiguration,
    ReviewTestInvocation, ReviewTestCompletion, ReviewTestOutcome, ReviewTestTreeProof, digest,
)
from .governor import target_command_inputs
from .child_env import child_process_env
from .process_supervisor import ProcessSupervisor


def workload_digest(environment: Mapping[str, str]) -> str:
    return digest(json.dumps(dict(environment), sort_keys=True, separators=(",", ":")))


def freeze_test_configuration(environment: dict[str, str], config_version: str) -> tuple[ReviewTestConfiguration, Mapping[str, str]]:
    if not isinstance(environment, dict) or any(not isinstance(key, str) or not isinstance(value, str)
                                               or "\0" in key or "\0" in value for key, value in environment.items()):
        raise ValueError("operator environment must contain NUL-free text")
    environment = dict(environment)
    command = environment.get("TEST_CMD", "")
    waiver = environment.get("ALLOW_UNTESTED", "0")
    if waiver not in {"0", "1"}:
        raise ValueError("malformed operator ALLOW_UNTESTED")
    scope = OperatorTestCommand(command) if command.strip() else OperatorTestWaiver() if waiver == "1" else MissingOperatorTestCommand()
    codes = parse_test_failure_exit_codes(environment.get("GATE_TEST_FAILURE_EXIT_CODES", "1"))
    _, workload = target_command_inputs(command, environment, int(environment.get("HARNESS_GATE_DEPTH", "0")))
    workload = child_process_env({key: value for key, value in workload.items() if not key.startswith("ISSUE_AGENT_")},
                                 additional_keys=workload)
    try:
        timeout = Decimal(environment.get("GATE_TEST_TIMEOUT_S", "600")) * 1000
    except InvalidOperation as error:
        raise ValueError("malformed operator timeout") from error
    if not timeout.is_finite() or timeout != timeout.to_integral_value():
        raise ValueError("operator timeout must be a finite exact millisecond duration")
    configuration = ReviewTestConfiguration(scope, tuple(sorted(codes)),
        int(timeout), workload_digest(workload), config_version)
    return configuration, MappingProxyType(workload)


def clean_tree(cwd: str, head: str) -> ReviewTestTreeProof:
    cwd = os.path.abspath(cwd)
    trusted_environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    def git(*args: str, environment=None) -> str:
        result = subprocess.run(["git", "--work-tree=" + cwd, *args], cwd=cwd, env=trusted_environment if environment is None else environment, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise ValueError("pinned checkout/tree evidence unavailable or changed")
        return result.stdout.strip()
    if git("rev-parse", "HEAD") != head:
        raise ValueError("pinned checkout head changed")
    flags = git("ls-files", "-v", "-z")
    if flags and (not flags.endswith("\0") or any(not entry.startswith("H ") or len(entry) <= 2
                                               for entry in flags.split("\0")[:-1])):
        raise ValueError("tracked tree proof requires nonsuppressed valid tracked entries")
    tree, index = git("rev-parse", "HEAD^{tree}"), git("write-tree")
    # A fresh index has no target-controlled stat cache that can hide changed bytes.
    with tempfile.TemporaryDirectory(prefix="ordinary-test-index-") as scratch:
        environment = dict(trusted_environment, GIT_INDEX_FILE=os.path.join(scratch, "index"))
        controls = ("-c", "core.fsmonitor=false", "-c", "core.ignorestat=false",
                    "-c", "core.sparseCheckout=false", "-c", "core.trustctime=true",
                    "-c", "core.checkstat=default", "-c", "core.filemode=true", "-c", "core.symlinks=true")
        git(*controls, "read-tree", "--no-sparse-checkout", "HEAD", environment=environment)
        git(*controls, "diff", "--quiet", "--no-ext-diff", "--ignore-submodules=none", "--",
            environment=environment)
    return ReviewTestTreeProof(head, tree, index, index)


async def _await_teardown(task) -> None:
    # Repeated cancellation must not detach teardown before checkout cleanup.
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()
    if cancelled:
        raise asyncio.CancelledError


@dataclass(frozen=True)
class ReviewTestExecutor:
    supervisor: ProcessSupervisor
    environment: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))

    async def recover(self, invocation: ReviewTestInvocation) -> None:
        self.supervisor.kill_tree(RunId(invocation.invocation_id))

    async def execute(self, invocation: ReviewTestInvocation, cwd: str) -> ReviewTestCompletion:
        config, scope = invocation.configuration, invocation.configuration.scope
        process = None
        readers = []
        content = bytearray()
        hashes = [hashlib.sha256(), hashlib.sha256()]
        before = after = None
        execution = None
        started = time.monotonic()
        async def drain(stream, index):
            while block := await stream.read(8192):
                hashes[index].update(block)
                content.extend(block[:max(0, 2048 - len(content))])
        async def teardown():
            try:
                await self.recover(invocation)
            finally:
                if process is not None and process.returncode is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await asyncio.wait_for(process.wait(), 5)
                if readers:
                    try:
                        await asyncio.wait_for(asyncio.gather(*readers), 5)
                    except asyncio.TimeoutError:
                        for reader in readers:
                            reader.cancel()
                        await asyncio.gather(*readers, return_exceptions=True)
        try:
            if workload_digest(self.environment) != config.workload_digest:
                raise ValueError("executor workload differs from frozen configuration")
            if isinstance(scope, MissingOperatorTestCommand):
                raise ValueError("operator TEST_CMD is not configured")
            before = clean_tree(cwd, invocation.subject.head_sha)
            if isinstance(scope, OperatorTestWaiver):
                after = clean_tree(cwd, invocation.subject.head_sha)
                return ReviewTestCompletion(invocation, ReviewTestOutcome.WAIVED, None, before, after, scope.provenance)
            assert isinstance(scope, OperatorTestCommand)
            argv = ("bash", "-c", scope.command)
            wrapped = self.supervisor.supervised_command(RunId(invocation.invocation_id), argv)
            env = dict(self.environment)
            env.update(self.supervisor.launch_guard_env())
            process = await asyncio.create_subprocess_exec(wrapped[0], "-I", *wrapped[1:], cwd=cwd, env=env,
                start_new_session=True, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            readers = [asyncio.create_task(drain(process.stdout, 0)), asyncio.create_task(drain(process.stderr, 1))]
            try:
                await asyncio.wait_for(process.wait(), config.timeout_seconds)
                code = process.returncode
                status = classify_test_exit_code(code, frozenset(config.failure_codes))
            except asyncio.TimeoutError:
                code, status = None, TestExecutionStatus.TIMED_OUT
            await _await_teardown(asyncio.create_task(teardown()))
            after = clean_tree(cwd, invocation.subject.head_sha)
            execution = TestExecution(status, argv, None if status in {TestExecutionStatus.CANCELLED, TestExecutionStatus.TIMED_OUT} else code,
                int((time.monotonic() - started) * 1000), hashes[0].hexdigest(), hashes[1].hexdigest(),
                config.executor_policy, invocation.subject.head_sha)
            outcome = ReviewTestOutcome.PASSED if status is TestExecutionStatus.PASSED else ReviewTestOutcome.FAILED if status is TestExecutionStatus.TEST_FAILED else ReviewTestOutcome.UNAVAILABLE
            return ReviewTestCompletion(invocation, outcome, execution, before, after,
                (outcome.value + "\n" + content.decode(errors="replace"))[:4096])
        except asyncio.CancelledError:
            await _await_teardown(asyncio.create_task(teardown()))
            raise
        except Exception as exc:
            return ReviewTestCompletion(invocation, ReviewTestOutcome.UNAVAILABLE, execution, before, after,
                f"{type(exc).__name__}: {exc}"[:4096])
        finally:
            await _await_teardown(asyncio.create_task(teardown()))


TEST_INTENT_NAMESPACE = "review-round-test-intent"
TEST_COMPLETION_NAMESPACE = "review-round-test-completion"
TEST_INVOCATION_NAMESPACE = "review-round-test-invocation"
TEST_SCHEDULE_NAMESPACE = "review-round-test-evidence"


def read_test_intent(db, repository: str, subject_id: str) -> ReviewTestInvocation | None:
    from .workflow_codec import decode_workflow
    row = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
        (TEST_INTENT_NAMESPACE, repository, subject_id)).fetchone()
    return decode_workflow(row["payload_json"], ReviewTestInvocation) if row else None


def read_test_completion(db, repository: str, invocation_id: str) -> ReviewTestCompletion | None:
    from .workflow_codec import decode_workflow
    row = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
        (TEST_COMPLETION_NAMESPACE, repository, invocation_id)).fetchone()
    return decode_workflow(row["payload_json"], ReviewTestCompletion) if row else None


def approval_test_error(db, repository, reference, configuration, binding, execution, head, patch):
    if reference is None or configuration is None:
        return "approval lacks current independent operator test evidence"
    completion = read_test_completion(db, repository, reference)
    if completion is None or not completion.approves:
        return "approval lacks a trusted passing test or explicit operator waiver"
    invocation = completion.invocation
    subject = invocation.subject
    if (invocation.configuration != configuration or subject.configuration_digest != configuration.identity
            or subject.rejection != binding or subject.execution != execution
            or subject.head_sha != head or subject.patch_id != patch):
        return "independent test evidence does not cover this current subject/configuration"
    return None
