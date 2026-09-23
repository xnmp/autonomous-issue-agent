"""Export the dashboard's validated API schema for the TypeScript client."""

from __future__ import annotations

import json
from pathlib import Path

from issue_agent.app.dashweb.server import create_app


ROOT = Path(__file__).resolve().parents[1]


def generator_schema(value):
    """Preserve JSON Schema literals for generators that only understand enum."""
    if isinstance(value, list):
        return [generator_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: generator_schema(item) for key, item in value.items()}
    if "const" in result:
        result.setdefault("enum", [result["const"]])
    return result


if __name__ == "__main__":
    target = ROOT / "dashboard" / "openapi.json"
    target.write_text(json.dumps(generator_schema(create_app().openapi()), indent=2, sort_keys=True) + "\n")
