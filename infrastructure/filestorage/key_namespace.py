"""Object-key namespaces for remote-sync storage scopes."""

from __future__ import annotations

from urllib.parse import quote

from config.constants.paths import ORGS_DIR_NAME, USERS_DIR_NAME
from config.principal import PrincipalKind, StorageScope
from config.scope_context import current_scope


def _encoded_segment(value: str) -> str:
    """Encode one opaque identity without allowing key-path delimiters."""
    return quote(value, safe="").replace(".", "%2E")


def normalized_key_prefix(prefix: str) -> str:
    """Return an empty prefix or one ending in exactly one slash."""
    stripped = prefix.strip("/")
    return f"{stripped}/" if stripped else ""


def scope_key_prefix(scope: StorageScope | None) -> str:
    """Return the isolated object-key prefix for an organization member."""
    if scope is None or scope.principal.kind is not PrincipalKind.ORG:
        return ""
    organization = _encoded_segment(scope.principal.id)
    member = _encoded_segment(scope.actor.id)
    return f"{ORGS_DIR_NAME}/{organization}/{USERS_DIR_NAME}/{member}/"


def resolved_key_prefix(explicit: str | None) -> str:
    """Resolve a key prefix without allowing an organization scope to flatten."""
    scoped = scope_key_prefix(current_scope())
    if scoped:
        return scoped
    return normalized_key_prefix(explicit or "")


__all__ = [
    "normalized_key_prefix",
    "resolved_key_prefix",
    "scope_key_prefix",
]
