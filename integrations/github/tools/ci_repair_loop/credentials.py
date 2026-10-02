"""Resolve configured credentials afresh in the background worker."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from config.constants import GH_TOKEN_ENV, GITHUB_MCP_AUTH_TOKEN_ENV, GITHUB_TOKEN_ENV
from config.llm_credentials import resolve_env_credential
from integrations.catalog import resolve_effective_integrations
from integrations.github.helpers import github_creds


def configured_token(explicit: str | None = None, *, connection_id: str | None = None) -> str:
    """Prefer injected credentials, then the effective integration and env fallback."""
    token = effective_github_token(explicit, connection_id=connection_id)
    if token:
        return token
    if connection_id:
        raise ValueError("The selected GitHub connection is unavailable; repair stopped.")
    raise ValueError("Configure GitHub with `opensre integrations setup github` before scheduling.")


def effective_github_token(explicit: str | None = None, *, connection_id: str | None = None) -> str:
    """Resolve a GitHub token from any configured source; ``""`` when absent.

    An explicit connection is resolved exactly and never falls back to another
    stored connection or environment credential.
    """
    if explicit:
        return explicit
    token = stored_github_token(connection_id)
    if token:
        return token
    if connection_id:
        return ""
    for name in (GITHUB_MCP_AUTH_TOKEN_ENV, GITHUB_TOKEN_ENV, GH_TOKEN_ENV):
        token = resolve_env_credential(name)
        if token:
            return token
    return ""


def _selected_connection_config(github: Mapping[str, object], connection_id: str) -> dict[str, Any]:
    """Return one exact available connection config, or an empty mapping."""
    instances = github.get("instances")
    if isinstance(instances, list):
        matches = [
            instance
            for instance in instances
            if isinstance(instance, dict)
            and str(instance.get("connection_id") or instance.get("integration_id") or "")
            == connection_id
        ]
        if len(matches) != 1 or matches[0].get("available") is False:
            return {}
        config = matches[0].get("config")
        return dict(config) if isinstance(config, dict) else {}

    config = github.get("config")
    if not isinstance(config, dict):
        return {}
    configured_id = str(config.get("connection_id") or config.get("integration_id") or "")
    return dict(config) if configured_id == connection_id else {}


def stored_github_token(connection_id: str | None = None) -> str:
    """Token of the effective GitHub integration; its entry wraps the classified config."""
    github = resolve_effective_integrations().get("github", {})
    if connection_id:
        config = _selected_connection_config(github, connection_id)
    else:
        candidate = github.get("config")
        config = candidate if isinstance(candidate, dict) else {}
    if not config:
        return ""
    creds = github_creds(config)
    return str(creds.get("github_token") or "")


def account_id(user: Mapping[str, object]) -> int:
    """Require the stable GitHub account ID before authorizing a durable run."""
    value = user.get("id")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("GitHub did not return a valid account identity.")
    return value
