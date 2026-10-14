"""Every structured-output contract is a well-formed object schema whose
required keys it declares: a required-but-undeclared key makes a Claude leg
emit a value nothing reads, and Codex's strict rewrite drops it silently."""

import pytest

from issue_agent.app.planners.schemas import FIX_SCHEMA, REVIEW_SCHEMA
from issue_agent.domain.triage import steward_schema, triage_schema

_TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}

SCHEMAS = {
    "fix": FIX_SCHEMA,
    "review": REVIEW_SCHEMA,
    "triage": triage_schema(),
    "steward": steward_schema((2, 3), True, (2,)),
    "steward-own-criteria": steward_schema((), False, ()),
}


def _well_formed(schema: dict, where: str) -> None:
    kind = schema.get("type")
    kinds = kind if isinstance(kind, list) else [kind]
    assert kinds and all(k in _TYPES for k in kinds), f"{where}: bad type {kind!r}"
    if "object" in kinds:
        props = schema.get("properties")
        required = schema.get("required", [])
        assert isinstance(props, dict) and props, f"{where}: an object schema declares its properties"
        assert isinstance(required, list) and len(required) == len(set(required)), f"{where}: required is a set"
        undeclared = [key for key in required if key not in props]
        assert not undeclared, f"{where}: required but undeclared {undeclared}"
        for key, sub in props.items():
            _well_formed(sub, f"{where}.{key}")
    if "array" in kinds:
        _well_formed(schema["items"], f"{where}[]")
    if "enum" in schema:
        assert isinstance(schema["enum"], list) and schema["enum"], f"{where}: an enum lists its values"


@pytest.mark.parametrize("name", sorted(SCHEMAS))
def test_every_contract_is_well_formed_and_declares_what_it_requires(name):
    _well_formed(SCHEMAS[name], name)


def test_the_fix_contract_requires_only_what_the_harness_reads():
    assert FIX_SCHEMA["required"] == ["status", "summary"]


def test_the_check_rejects_a_required_key_the_schema_never_declares():
    broken = {"type": "object", "properties": {"status": {"type": "string"}}, "required": ["status", "ticket_key"]}
    with pytest.raises(AssertionError, match="ticket_key"):
        _well_formed(broken, "broken")
