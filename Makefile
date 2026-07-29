# Autonomous Issue Agent — operator entry points.
SHELL := /bin/bash
HARNESS_ROOT := $(shell pwd)

.PHONY: help test test-governor test-hooks test-orchestration test-deployment-env test-point-at test-domain test-adapters test-parity arch doctor \
	        install bootstrap point-at run-fix run-review run-steward run-fix-sh run-review-sh run-steward-sh demo-gate \
        install-launchd install-systemd \
        uninstall-launchd uninstall-systemd demo-init lint clean

help:
	@echo "Autonomous Issue Agent — make targets:"
	@echo "  make test               run the full self-test suite (governor + hooks + orchestration + domain)"
	@echo "  make test-domain        run the pure Python domain behavioural suite (Phase 0/1 port)"
	@echo "  make test-adapters      run the Python adapter suite (Phase 2: AgentRunner + governor)"
	@echo "  make test-parity        run the gate PARITY suite (same case through bash + Python gates)"
	@echo "  make doctor             preflight: tooling, config, identities, repo access"
	@echo "  make install            install the 'issue-agent' CLI (uv sync + ~/.local/bin shim)"
	@echo "  make bootstrap TARGET=/path/to/repo   install the plugin into a target repo"
	@echo "  make point-at REPO=<name>   point harness at config/repos/<name>.env: doctor + dev branch + bootstrap + run all 3 watchers"
	@echo "  make run-fix            run the Python fix watcher in the foreground (Ctrl-C to stop)"
	@echo "  make run-review         run the Python review watcher in the foreground"
	@echo "  make run-steward        run the Python steward in the foreground"
	@echo "  make run-*-sh           run shell compatibility wrappers around Python watchers"
	@echo "  make install-launchd    install+load the 3 launchd agents (macOS)"
	@echo "  make install-systemd    install+enable the 3 systemd user units (Linux)"
	@echo "  make demo-init          create + seed the local demo target repo"
	@echo "  make clean              remove caches and demo scratch"

# test-deployment-env is NOT in this chain: it re-runs the two bash suites
# under a hostile env, doubling their wall clock — and `make test` is the
# self-hosted TEST_CMD paid on every gate run. CI runs it as its own step.
test: test-governor test-hooks test-orchestration test-point-at test-domain test-adapters test-parity arch
	@echo ""
	@echo "✓ all suites passed"

test-governor:
	@echo "== governor (pytest) =="
	@uv run --quiet tests/test_governor.py

test-hooks:
	@echo "== hooks (bash) =="
	@bash tests/test_hooks.sh

test-orchestration:
	@echo "== orchestration (bash, fakes) =="
	@bash tests/test_orchestration.sh

test-deployment-env:
	@echo "== deployment environment sanitization =="
	@bash tests/test_deployment_env_sanitization.sh

test-point-at:
	@echo "== point-at (gate-probe worktree placement) =="
	@bash tests/test_point_at.sh

test-domain:
	@echo "== domain (pytest, pure) =="
	@uv run --quiet --with pytest pytest tests/domain/

# Phase 2 adapters exercise real application wiring and the reused tree-sitter
# bypass core, so declare the SDK and parser dependencies at this tier.
test-adapters:
	@echo "== adapters (pytest: AgentRunner mapping + in-process governor) =="
	@uv run --quiet --extra sdk --with pytest pytest tests/adapters/

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

doctor:
	@bash bin/doctor.sh

# Install the operator CLI: `uv sync` (editable, into the checkout .venv that
# point-at.sh's `uv run` reuses) + a launcher shim on ~/.local/bin. Then
# `issue-agent init` once and `issue-agent start` from inside any target repo.
install:
	@bash bin/install.sh

bootstrap:
	@test -n "$(TARGET)" || { echo "usage: make bootstrap TARGET=/path/to/repo"; exit 1; }
	@bash bin/bootstrap.sh "$(TARGET)"

# One command: compose config/shared.env + config/repos/<REPO>.env, preflight,
# ensure the base branch, bootstrap the target, and run all three watchers in
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

# Legacy shell targets retained as compatibility wrappers. They load the same
# shell-style config files operators used before, then hand orchestration to
# Python instead of running the retired Bash poll loops.
run-fix-sh:
	@bash bin/run-role.sh fix

run-review-sh:
	@bash bin/run-role.sh review

run-steward-sh:
	@bash bin/run-role.sh steward

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
		agent/*.sh review/*.sh steward/*.sh bin/*.sh plugin/hooks/*.sh plugin/hooks/checks/*.sh \
		|| echo "shellcheck not installed — skipping (brew install shellcheck)"

clean:
	@find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
	@rm -rf examples/demo-target/.git examples/demo-target/.scratch 2>/dev/null || true
	@echo "cleaned"
