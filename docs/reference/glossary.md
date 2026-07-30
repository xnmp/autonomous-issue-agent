# Harness glossary

- **action** — pure domain data describing a requested local or lifecycle
  effect.
- **command** — durable, idempotency-keyed privileged effect request.
- **dispatch** — one decision to pursue an `AgentKind`; it may expand to
  multiple legs.
- **leg** — one independently executed unit with a target, billability, and
  declared budget scopes.
- **run** — one runtime attempt for a leg, identified by `run_id`.
- **session** — provider conversation state that may span/fork across runs.
- **receipt** — durable evidence that an effect or charge reached a terminal
  disposition.
- **world** — one authoritative GitHub observation used by pure decision logic.
- **outcome** — typed interpretation of a completed/cancelled run.
- **projection** — rebuildable read model folded from schema-v2 telemetry.
