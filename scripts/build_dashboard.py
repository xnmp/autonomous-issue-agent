"""CLI wrapper for the locked dashboard frontend builder."""

from pathlib import Path
from issue_agent.adapters.dashweb.build import build_dashboard


if __name__ == "__main__":
    raise SystemExit(build_dashboard(Path(__file__).resolve().parent.parent))
