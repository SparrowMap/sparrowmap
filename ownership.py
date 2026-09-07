"""Sighting ownership predicate.

Answers exactly one question: does this sighting belong to this node? No
Handler dependency, no DB lookup, no HTTP response generation, no
authentication, no logging. Callers remain responsible for what happens when
the answer is False - that consequence differs by route and is intentionally
not standardized here.
"""
from __future__ import annotations

from typing import Any


def sighting_belongs_to_node(sighting: dict[str, Any] | None, node: dict[str, Any]) -> bool:
    """Preserve the existing `(sighting.get("node_id") or "") == node["id"]` check."""
    if not sighting:
        return False
    return (sighting.get("node_id") or "") == node["id"]
