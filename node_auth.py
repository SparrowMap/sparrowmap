"""Node/device authentication primitives.

This module preserves current node submission authentication behavior without
depending on HTTP handlers or vehicle-sighting modules.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import hmac

import db
import nodes as node_mod


@dataclass
class NodeAuthenticationResult:
    """The resolved node and result of authenticating one submitted event."""

    node_id: Any
    node: dict[str, Any] | None
    signature_verified: bool
    status_code: int | None = None
    error: str | None = None

    @property
    def allowed(self) -> bool:
        return self.error is None


def extract_node_id(event: dict[str, Any]) -> Any:
    """Return the submitted node ID with current event semantics."""
    return event.get("node_id")


def verify_node_bearer(authorization: str | None, node: dict[str, Any]) -> bool:
    """Preserve `_token_ok`'s case-sensitive Bearer parsing and open-node rule."""
    if not node.get("token"):
        return True
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    return hmac.compare_digest(supplied, node["token"])


def authenticate_node_bearer(
    node_id: str, authorization: str | None,
) -> NodeAuthenticationResult:
    """Resolve a node and apply the tokenless-compatible header bearer policy."""
    node = db.node(node_id)
    if not node:
        return NodeAuthenticationResult(node_id, None, False, 404, "unknown node")
    if not verify_node_bearer(authorization, node):
        return NodeAuthenticationResult(
            node_id, node, False, 401, "bad node token"
        )
    return NodeAuthenticationResult(node_id, node, False)


def authenticate_required_node_bearer(
    node_id: str, authorization: str | None,
) -> NodeAuthenticationResult:
    """Resolve a node and require its configured header bearer token."""
    node = db.node(node_id)
    if not node:
        return NodeAuthenticationResult(node_id, None, False, 404, "unknown node")
    if not node.get("token"):
        return NodeAuthenticationResult(
            node_id, node, False, 401, "this node has no token; re-enroll it"
        )
    if not verify_node_bearer(authorization, node):
        return NodeAuthenticationResult(
            node_id, node, False, 401, "bad node token"
        )
    return NodeAuthenticationResult(node_id, node, False)


def authenticate_active_node_bearer(
    node_id: str, authorization: str | None,
) -> NodeAuthenticationResult:
    """Resolve a node, require active status, then apply the tokenless-
    compatible header bearer policy. No Ed25519 signature is required or
    checked; this is distinct from `authenticate_node_submission`, which
    always verifies a signature for keyed nodes.
    """
    node = db.node(node_id)
    if not node:
        return NodeAuthenticationResult(node_id, None, False, 404, "unknown node")
    if node["status"] != "active":
        return NodeAuthenticationResult(
            node_id, node, False, 403, f"node is {node['status']}"
        )
    if not verify_node_bearer(authorization, node):
        return NodeAuthenticationResult(
            node_id, node, False, 401, "bad node token"
        )
    return NodeAuthenticationResult(node_id, node, False)


def authenticate_node_submission(
    event: dict[str, Any], authorization: str | None,
) -> NodeAuthenticationResult:
    """Resolve and authenticate a node submission in its existing denial order."""
    node_id = extract_node_id(event)
    node = db.node(node_id) if node_id else None
    if not node:
        return NodeAuthenticationResult(node_id, None, False, 404, "unknown node")
    if node["status"] != "active":
        return NodeAuthenticationResult(
            node_id, node, False, 403, f"node is {node['status']}"
        )

    signature_verified = node_mod.verify_event(
        event, event.get("sig", ""), node.get("pubkey") or ""
    )
    if node.get("pubkey") and not signature_verified:
        return NodeAuthenticationResult(
            node_id, node, signature_verified, 401, "signature did not verify"
        )
    if not verify_node_bearer(authorization, node):
        return NodeAuthenticationResult(
            node_id, node, signature_verified, 401, "bad node token"
        )
    return NodeAuthenticationResult(node_id, node, signature_verified)
