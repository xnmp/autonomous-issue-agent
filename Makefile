# Autonomous Issue Agent — operator entry points.
# No slash: GNU make resolves via PATH (NixOS has no /bin/bash).
SHELL := bash
HARNESS_ROOT := $(shell pwd)
TEST_WORKERS ?= 4
TEST_SUITE_JOBS ?= 3
PYTEST_TIMING ?= --durations=10

.PHONY: help test test-suites test-config-generated test-governor test-residuals test-hooks test-compose-env test-deployment-env test-point-at test-domain test-adapters test-app test-parity arch typecheck doctor dashboard-ui dashboard \
	        install point-at run-fix run-review demo-gate \
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
	@echo "  make doctor AGENT_ENV=config/.active.<name>.env   preflight tooling, config, identities, repo access"
	@echo "  make install            install the 'issue-agent' CLI (uv sync + ~/.local/bin shim)"
	@echo "  make point-at REPO=<name>   compose config, preflight, ensure dev branch, and run all 3 daemons"
	@echo "  make run-fix            run the Python fix watcher in the foreground (Ctrl-C to stop)"
	@echo "  make run-review         run the Python review watcher in the foreground"
	@echo "  make install-launchd AGENT_ENV=config/.active.<name>.env   install+load the 2 launchd agents (macOS)"
	@echo "  make install-systemd AGENT_ENV=config/.active.<name>.env   install+enable the 2 systemd user units (Linux)"
	@echo "  make demo-init          create + seed the local demo target repo"
	@echo "  make clean              remove caches and demo scratch"

# test-deployment-env is NOT in this chain: it re-runs the hook and adapter
# suites under a hostile env, substantially increasing wall clock — and `make test` is the
# self-hosted TEST_CMD paid on every gate run. CI runs it as its own step.
test:
	@uv sync --extra dev --extra sdk --extra dash
	@UV_NO_SYNC=1 bash scripts/run_test_suite.sh $(MAKE) --no-print-directory -j$(TEST_SUITE_JOBS) test-suites

# Start the two long tiers together; short contracts fit into their trailing
# capacity instead of delaying the longest scale test by the shell gates.
test-suites: test-adapters test-app test-config-generated test-governor test-residuals test-hooks test-compose-env test-point-at test-domain test-parity arch typecheck
	@echo ""
	@echo "✓ all suites passed"

test-config-generated:
	@echo "== config (generated metadata reference) =="
	@diff -u config/agent.env.example <(uv run --quiet python scripts/generate_config_example.py)
	@diff -u plugin/hooks/checks/_branch_policy.sh <(uv run --quiet python scripts/generate_branch_policy.py)

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

test-compose-env:
	@echo "== compose-env (TARGET_ENV_ pass-through contract) =="
	@bash tests/test_compose_env_passthrough.sh

test-deployment-env:
	@echo "== deployment environment sanitization =="
	@bash tests/test_deployment_env_sanitization.sh

test-point-at:
	@echo "== point-at (gate-probe worktree placement) =="
	@bash tests/test_point_at.sh

test-domain:
	@echo "== domain (pytest) =="
	@uv run --quiet --extra dev pytest tests/domain/ $(PYTEST_TIMING)

# Phase 2 adapters exercise real application wiring and the reused tree-sitter
# bypass core, so declare the SDK and parser dependencies at this tier.
test-adapters:
	@echo "== adapters (pytest: AgentRunner mapping + in-process governor) =="
	@uv run --quiet --extra dev --extra sdk --extra dash pytest tests/adapters/ -n $(TEST_WORKERS) --dist load --maxschedchunk=1 $(PYTEST_TIMING)

# Include dashboard endpoints in the complete gate. Daemon installations still
# keep the web extra optional; only the development environment includes it.
test-app:
	@echo "== app (pytest: orchestration + dashboard + endpoints) =="
	@uv run --quiet --extra dev --extra sdk --extra dash pytest tests/app/ -n $(TEST_WORKERS) --dist load --maxschedchunk=1 $(PYTEST_TIMING)

# Build the dashboard SPA into dashboard/dist (gitignored). Needed before
# `issue-agent dashboard --serve` prefers the interactive UI over the v1 page.
dashboard-ui:
	@PYTHONPATH=src uv run --offline --no-sync python scripts/build_dashboard.py

# Run the operator dashboard with its extras guaranteed present, whatever a
# prior exact sync pruned. This is the supported way to (re)start it.
dashboard:
	@uv run --quiet --extra sdk --extra dash issue-agent dashboard

# Gate transport parity: golden expected verdicts exercise the advisory shell
# and SDK transports through one Python policy source. Mutation cases detect
# policy weakening; explicit capability differences remain independently tested.
# PEP 723 dependencies mirror test-adapters (tree-sitter for classification).
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
	@AGENT_ENV="$${AGENT_ENV:?source bin/compose-env.sh <name> first}" bash bin/doctor.sh

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

# Phase 5 acceptance gate — live end-to-end against a throwaway demo repo.
demo-gate:
	@test -n "$(DEMO_REPO)" || { echo "usage: make demo-gate AGENT_ENV=config/.active.<name>.env DEMO_REPO=owner/repo"; exit 1; }
	@AGENT_ENV="$${AGENT_ENV:?source bin/compose-env.sh <name> first}" DEMO_REPO="$(DEMO_REPO)" bash bin/demo-gate.sh

install-launchd:
	@AGENT_ENV="$${AGENT_ENV:?source bin/compose-env.sh <name> first}" bash bin/install-launchd.sh

uninstall-launchd:
	@bash bin/install-launchd.sh uninstall

install-systemd:
	@AGENT_ENV="$${AGENT_ENV:?source bin/compose-env.sh <name> first}" bash bin/install-systemd.sh

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
