"""Canonical deployment identity shared by launch and operator boundaries."""

import re


def canonical_deployment_name(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9._-]", "-", value.strip().lower()).strip("-")
    return normalized or "target"
