"""Client generation must retain the server's closed literal unions."""

from copy import deepcopy
from pathlib import Path
import runpy

import pytest

pytest.importorskip("fastapi")


def test_generator_compatibility_preserves_literals_and_does_not_mutate_server_schema():
    export = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts" / "export_dashboard_openapi.py"))
    source = {
        "components": {
            "schemas": {
                "Command": {
                    "properties": {"operation": {"type": "string", "const": "request_rearm"}},
                    "oneOf": [{"const": False}, {"const": 0}, {"const": None}],
                }
            }
        }
    }
    before = deepcopy(source)
    result = export["generator_schema"](source)
    command = result["components"]["schemas"]["Command"]
    assert command["properties"]["operation"] == {"type": "string", "const": "request_rearm", "enum": ["request_rearm"]}
    assert command["oneOf"] == [
        {"const": False, "enum": [False]},
        {"const": 0, "enum": [0]},
        {"const": None, "enum": [None]},
    ]
    assert source == before
