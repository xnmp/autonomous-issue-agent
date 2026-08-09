# Autonomous Issue Agent — operator entry points.
# No slash: GNU make resolves via PATH (NixOS has no /bin/bash).
SHELL := bash
HARNESS_ROOT := $(shell pwd)

.PHONY: help test test-config-generated test-governor test-residuals test-hooks test-deployment-env test-point-at test-domain test-adapters test-app test-parity arch typecheck doctor dashboard-ui \
	        install point-at run-fix run-review run-steward demo-gate \
        install-launchd install-systemd \
        uninstall-launchd uninstall-systemd demo-init lint clean

help:
	@echo "Autonomous Issue Agent — make targets:"
	@echo "  make test               run the full Python, hook, parity, and architecture suites"
	@echo "  make test-domain        run the pure Python domain behavioural suite (Phase 0/1 port)"
	@echo "  make test-adapters      run the Python adapter suite (Phase 2: AgentRunner + governor)"
	@echo "  make test-app           run the app-layer suite (dashboard web collectors + endpoints)"
	@echo "  make dashboard-ui       build the interactive dashboard SPA into dashboard/dist"
	@echo "  make test-parity        run the gate PARITY suite (same case through bash + Python gates)"
	@echo "  make doctor             preflight: tooling, config, identities, repo access"
	@echo "  make install            install the 'issue-agent' CLI (uv sync + ~/.local/bin shim)"
	@echo "  make point-at REPO=<name>   compose config, preflight, ensure dev branch, and run all 3 daemons"
	@echo "  make run-fix            run the Python fix watcher in the foreground (Ctrl-C to stop)"
	@echo "  make run-review         run the Python review watcher in the foreground"
	@echo "  make run-steward        run the Python steward in the foreground"
	@echo "  make install-launchd    install+load the 3 launchd agents (macOS)"
	@echo "  make install-systemd    install+enable the 3 systemd user units (Linux)"
	@echo "  make demo-init          create + seed the local demo target repo"
	@echo "  make clean              remove caches and demo scratch"

# test-deployment-env is NOT in this chain: it re-runs the hook and adapter
# suites under a hostile env, substantially increasing wall clock — and `make test` is the
# self-hosted TEST_CMD paid on every gate run. CI runs it as its own step.
test: test-config-generated test-governor test-residuals test-hooks test-point-at test-domain test-adapters test-app test-parity arch typecheck
	@echo ""
	@echo "✓ all suites passed"

test-config-generated:
	@echo "== config (generated metadata reference) =="
	@diff -u config/agent.env.example <(uv run --quiet python scripts/generate_config_example.py)

test-governor:
	@echo "== governor (pytest) =="
	@uv run --quiet tests/test_governor.py

test-residuals:
	@echo "== accepted residuals (pytest: strict xfail registry) =="
	@uv run --quiet --with pytest --with tree-sitter --with tree-sitter-bash \
		pytest tests/test_residuals.py

test-hooks:
	@echo "== hooks (bash) =="
	@bash tests/test_hooks.sh

test-deployment-env:
	@echo "== deployment environment sanitization =="
	@bash tests/test_deployment_env_sanitization.sh

test-point-at:
	@echo "== point-at (gate-probe worktree placement) =="
	@bash tests/test_point_at.sh

test-domain:
	@echo "== domain (pytest) =="
	@uv run --quiet --with pytest --with tree-sitter --with tree-sitter-bash pytest tests/domain/

# Phase 2 adapters exercise real application wiring and the reused tree-sitter
# bypass core, so declare the SDK and parser dependencies at this tier.
test-adapters:
	@echo "== adapters (pytest: AgentRunner mapping + in-process governor) =="
	@uv run --quiet --extra sdk --with pytest pytest tests/adapters/

# The orchestration/dashboard/CLI app layer. Deliberately NOT installed with
# `--extra dash`:
# the endpoint tests importorskip fastapi, so this target is the standing proof
# that `make test` stays green on a machine that never installed the extra.
test-app:
	@echo "== app (pytest: orchestration + dashboard + endpoints) =="
	@uv run --quiet --extra sdk --with pytest pytest tests/app/

# Build the dashboard SPA into dashboard/dist (gitignored). Needed before
# `issue-agent dashboard --serve` prefers the interactive UI over the v1 page.
dashboard-ui:
	@PYTHONPATH=src uv run --offline --no-sync python scripts/build_dashboard.py

# Run the operator dashboard with its extras guaranteed present, whatever a
# prior exact sync pruned. This is the supported way to (re)start it.
dashboard:
	@uv run --quiet --extra sdk --extra dash issue-agent dashboard

# Gate parity (review M6). The harness has TWO gate implementations — the bash
# checkers (live for codex) and domain/gate.py + adapters/governor.py (live for
# the SDK/Claude path, the reviewer and the steward) — that shared a spec, not
# code, and drifted. This suite runs golden cases through BOTH and fails on any
# disagreement; it is the sync mechanism that replaced the "keep in sync"
# comments. Deps mirror test-adapters (tree-sitter for the classifier); it is a
# PEP 723 script, so `uv run` self-bootstraps pytest.
test-parity:
	@echo "== gate parity (pytest: bash checkers vs domain/gate.py) =="
	@uv run --quiet tests/test_gate_parity.py

# Architectural lint: the hexagonal layering contracts in pyproject.toml
# ([tool.importlinter]). Needs the SDK importable (adapters/app import it), so
# pull in the `dev` extra (which includes import-linter + the SDK).
arch:
	@echo "== arch (import-linter: hexagonal layering) =="
	@uv run --extra dev --with tree-sitter --with tree-sitter-bash lint-imports

typecheck:
	@echo "== typecheck (pyrefly: src + tests) =="
	@uv run pyrefly check
	@echo "== typecheck (pyrefly: required port conformance) =="
	@uv run pyrefly check tests/typecheck/port_conformance.py \
		--error bad-return,bad-argument-type,bad-assignment,missing-attribute \
		--progress-bar no

doctor:
	@bash bin/doctor.sh

# Install the operator CLI: `uv sync` (editable, into the checkout .venv that
# point-at.sh's `uv run` reuses) + a launcher shim on ~/.local/bin. Then
# `issue-agent init` once and `issue-agent start` from inside any target repo.
install:
	@bash bin/install.sh

# One command: compose config/shared.env + config/repos/<REPO>.env, preflight,
# ensure the base branch, prepare local excludes/labels, and run all three daemons in
# the foreground (Ctrl-C stops all). e.g. make point-at REPO=myapp
point-at:
	@test -n "$(REPO)" || { echo "usage: make point-at REPO=<name>  (reads config/repos/<name>.env)"; exit 1; }
	@bash bin/point-at.sh "$(REPO)"

run-fix:
	@uv run --extra sdk --with tree-sitter --with tree-sitter-bash python -m issue_agent.app.main fix

run-review:
	@uv run --extra sdk --with tree-sitter --with tree-sitter-bash python -m issue_agent.app.main review

run-steward:
	@uv run --extra sdk --with tree-sitter --with tree-sitter-bash python -m issue_agent.app.main steward

# Phase 5 acceptance gate — live end-to-end against a throwaway demo repo.
demo-gate:
	@test -n "$(DEMO_REPO)" || { echo "usage: make demo-gate DEMO_REPO=owner/repo"; exit 1; }
	@DEMO_REPO="$(DEMO_REPO)" bash bin/demo-gate.sh

install-launchd:
	@bash bin/install-launchd.sh

uninstall-launchd:
	@bash bin/install-launchd.sh uninstall

install-systemd:
	@bash bin/install-systemd.sh

uninstall-systemd:
	@bash bin/install-systemd.sh uninstall

demo-init:
	@bash examples/demo-target/init-demo.sh

lint:
	@command -v shellcheck >/dev/null 2>&1 && shellcheck -x \
		bin/*.sh plugin/hooks/*.sh plugin/hooks/checks/*.sh \
		|| echo "shellcheck not installed — skipping (brew install shellcheck)"

clean:
	@find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
	@rm -rf examples/demo-target/.git examples/demo-target/.scratch 2>/dev/null || true
	@echo "cleaned"
