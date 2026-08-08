"""Environment capability observations → the harness-attested manifest text.

Pure: the adapter reads the process environment and PATH and hands the readings
here; nothing in this module touches os, PATH, the clock, or any I/O.

Provenance (ADR 0012): the observations come from the harness's own boot probe,
in the daemon process. No PR, issue, comment, or agent output can reach them,
which is why prompts state the manifest OUTSIDE every untrusted fence.

The manifest names ONLY what was probed and, by default, draws no conclusion
from it. The probe knows one GUI stack (WebKitGTK/tauri); a repo driving
headless Chromium through Playwright would be perfectly able to produce e2e
evidence with every binary below absent, and a harness-attested
"evidence CANNOT be produced" there would lower the evidence bar globally on a
falsehood. The categorical conclusion is therefore emitted only when the
deployment DECLARES a stack these probes actually cover (`EVIDENCE_STACK`).
"""

from __future__ import annotations

from dataclasses import dataclass

# Env vars that indicate an attached graphical session. WAYLAND_DISPLAY is
# checked alongside DISPLAY because a Wayland session need not export DISPLAY
# unless Xwayland is running.
DISPLAY_VARS: tuple[str, ...] = ("DISPLAY", "WAYLAND_DISPLAY")

# The WebKitGTK/tauri driver binaries. WebKitWebDriver is what the
# `webkit2gtk-driver` package installs; `tauri-driver` is the tauri-side shim
# over it. Xvfb is tracked separately — it substitutes for a display, it is not
# a driver.
DRIVER_BINARIES: tuple[str, ...] = ("WebKitWebDriver", "tauri-driver")
XVFB_BINARY = "Xvfb"

PROBED_BINARIES: tuple[str, ...] = (*DRIVER_BINARIES, XVFB_BINARY)

# The one stack these probes cover. An operator declaring it is asserting "this
# deployment's GUI evidence goes through WebKitGTK/tauri", which is what makes
# the probe's silence conclusive rather than merely uninformed.
WEBKITGTK_STACK = "webkitgtk"
EVIDENCE_STACKS: tuple[str, ...] = ("", WEBKITGTK_STACK)


@dataclass(frozen=True)
class EnvironmentObservation:
    """What the boot probe saw. Observations only — no judgement."""

    display_vars: tuple[str, ...] = ()
    xvfb: bool = False
    drivers: tuple[str, ...] = ()

    @property
    def webkitgtk_capture_possible(self) -> bool:
        # A WebKitGTK/tauri capture needs somewhere to draw (a real display or
        # Xvfb) AND a driver to run the app. Either half missing makes THAT
        # stack's live-app/e2e/screenshot evidence unproducible. Says nothing
        # about any other stack.
        return bool(self.display_vars or self.xvfb) and bool(self.drivers)


@dataclass(frozen=True)
class EnvironmentManifest:
    """The attested text plus the one judgement prompt builders branch on.

    `rules_out_live_evidence` is False whenever the harness has NOT been told
    which stack this repo uses — the probe's silence is then ignorance, not
    evidence, and no prompt may instruct anyone to skip e2e work over it."""

    text: str = ""
    rules_out_live_evidence: bool = False

    @property
    def attested(self) -> bool:
        return bool(self.text)


def observe(
    environ: dict[str, str] | None,
    present: dict[str, bool] | None,
) -> EnvironmentObservation:
    """Pure: raw readings in, settled observation out.

    `present` maps binary name → found on PATH. A display variable set to the
    empty string is NOT a display (that is how a headless service unit blanks
    it rather than unsetting it)."""
    env = environ or {}
    found = present or {}
    return EnvironmentObservation(
        display_vars=tuple(
            name for name in DISPLAY_VARS if (env.get(name) or "").strip()
        ),
        xvfb=bool(found.get(XVFB_BINARY)),
        drivers=tuple(name for name in DRIVER_BINARIES if found.get(name)),
    )


def _facts(observation: EnvironmentObservation) -> str:
    display = (
        f"X display available ({', '.join(observation.display_vars)})"
        if observation.display_vars
        else "no X display"
    )
    xvfb = f"{XVFB_BINARY} present" if observation.xvfb else f"no {XVFB_BINARY}"
    drivers = (
        "WebKitGTK/tauri WebDriver stack present "
        f"({', '.join(observation.drivers)})"
        if observation.drivers
        else (
            "no WebKitGTK/tauri WebDriver stack "
            f"({'/'.join(DRIVER_BINARIES)})"
        )
    )
    return (
        "environment capabilities (harness-attested): "
        f"{display}, {xvfb}, {drivers}."
    )


def manifest(
    observation: EnvironmentObservation, declared_stack: str = ""
) -> EnvironmentManifest:
    """Render the probe's readings, with a conclusion only where one is earned.

    Undeclared stack → facts, plus an explicit statement that they settle
    nothing about other toolchains. Attesting facts is always safe; attesting a
    conclusion about a stack the harness was never told about is not."""
    facts = _facts(observation)
    if declared_stack != WEBKITGTK_STACK:
        return EnvironmentManifest(
            text=(
                f"{facts} These are observations about this machine, not a "
                "verdict on what evidence is possible: this deployment has not "
                "declared which GUI/e2e toolchain the repo uses "
                "(EVIDENCE_STACK is unset), and other stacks — headless "
                "Chromium, Playwright, a project-supplied harness — may work "
                "with none of the above present."
            ),
            rules_out_live_evidence=False,
        )
    if observation.webkitgtk_capture_possible:
        return EnvironmentManifest(
            text=(
                f"{facts} This deployment declares EVIDENCE_STACK="
                f"{WEBKITGTK_STACK}, which these probes cover: live-app/e2e/"
                "screenshot evidence CAN be produced in this environment."
            ),
            rules_out_live_evidence=False,
        )
    return EnvironmentManifest(
        text=(
            f"{facts} This deployment declares EVIDENCE_STACK="
            f"{WEBKITGTK_STACK}, which these probes cover: live-app/e2e/"
            "screenshot evidence CANNOT be produced in this environment; "
            "judge on unit/contract tiers."
        ),
        rules_out_live_evidence=True,
    )
