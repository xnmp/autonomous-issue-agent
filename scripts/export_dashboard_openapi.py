"""Export the dashboard's validated API schema for the TypeScript client."""

from __future__ import annotations

import json
from pathlib import Path

from issue_agent.app.dashweb.server import create_app


ROOT = Path(__file__).resolve().parents[1]
target = ROOT / "dashboard" / "openapi.json"
target.write_text(json.dumps(create_app().openapi(), indent=2, sort_keys=True) + "\n")
