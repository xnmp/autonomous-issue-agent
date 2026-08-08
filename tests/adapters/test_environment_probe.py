"""Boot probe I/O edge (2026-08-08, tauri #613/#614)."""

from __future__ import annotations

from issue_agent.adapters import environment as env_adapter
from issue_agent.domain.environment import WEBKITGTK_STACK


def _which(*present: str):
    found = set(present)
    return lambda name: f"/usr/bin/{name}" if name in found else None


def test_probe_reads_the_environment_and_path():
    result = env_adapter.probe_manifest(
        WEBKITGTK_STACK, environ={"DISPLAY": ":0"}, which=_which("WebKitWebDriver")
    )
    assert "X display available (DISPLAY)" in result.text
    assert "WebKitGTK/tauri WebDriver stack present" in result.text
    assert not result.rules_out_live_evidence


def test_probe_on_a_headless_machine_with_a_declared_stack():
    result = env_adapter.probe_manifest(WEBKITGTK_STACK, environ={}, which=_which())
    assert result.rules_out_live_evidence
    assert "CANNOT be produced" in result.text


def test_probe_without_a_declared_stack_attests_facts_only():
    result = env_adapter.probe_manifest(environ={}, which=_which())
    assert result.attested
    assert not result.rules_out_live_evidence
    assert "CANNOT" not in result.text


def test_an_unusable_probe_attests_nothing_at_all():
    # Absent must never read as "all evidence is possible": a probe that cannot
    # run says nothing rather than something false.
    def explode(_name: str) -> str | None:
        raise OSError("PATH unreadable")

    result = env_adapter.probe_manifest(environ={}, which=explode)
    assert not result.attested
    assert result.text == ""


def test_boot_manifest_probes_once_per_process(monkeypatch):
    env_adapter.reset_boot_manifest_cache()
    real_probe = env_adapter.probe_manifest
    calls: list[str] = []

    def counted(declared_stack: str = ""):
        calls.append(declared_stack)
        return real_probe(declared_stack, environ={}, which=_which())

    monkeypatch.setattr(env_adapter, "probe_manifest", counted)
    first = env_adapter.boot_manifest(WEBKITGTK_STACK)
    again = env_adapter.boot_manifest(WEBKITGTK_STACK)

    assert calls == [WEBKITGTK_STACK]
    assert again == first
    env_adapter.reset_boot_manifest_cache()


def test_a_failed_probe_is_retried_rather_than_cached_for_process_life(monkeypatch):
    # A transient failure (unreadable PATH, filesystem blip) must not silence
    # the manifest until the daemon restarts.
    env_adapter.reset_boot_manifest_cache()
    outcomes = iter([
        env_adapter.probe_manifest(environ={}, which=lambda _n: _raise()),
        env_adapter.probe_manifest(WEBKITGTK_STACK, environ={}, which=_which()),
    ])
    calls: list[str] = []

    def flaky(declared_stack: str = ""):
        calls.append(declared_stack)
        return next(outcomes)

    monkeypatch.setattr(env_adapter, "probe_manifest", flaky)
    assert not env_adapter.boot_manifest(WEBKITGTK_STACK).attested
    assert env_adapter.boot_manifest(WEBKITGTK_STACK).attested
    assert len(calls) == 2
    env_adapter.reset_boot_manifest_cache()


def _raise() -> str | None:
    raise OSError("PATH unreadable")
