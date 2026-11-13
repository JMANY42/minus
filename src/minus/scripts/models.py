"""Choosing which model runs which tier, from the command line.

`Settings` already names the three models and `assembly.build_config_controller`
already marks all three as live, so this is not new state: it is the missing way
to change them. Two routes, in order:

  * through the control socket, so a running assistant applies the change to the
    live objects, validates it with pydantic and rewrites `.env` preserving
    comments -- exactly what the dashboard's settings panel does;
  * straight into `.env` when nothing is listening, so the next start uses it and
    the command is still useful on a stopped assistant.

A name is validated by assigning it to a copy of `Settings`, which is the same
check a live write gets. Every tier model is a plain string, so the only
rejection a string field can produce is a type error -- which is why an empty
name is refused here rather than handed on.
"""

from __future__ import annotations

import logging

import httpx

from minus.config import Settings
from minus.control.client import ControlClient, NotRunning
from minus.paths import control_socket, project_root
from minus.services.env_file import update_env_file

logger = logging.getLogger(__name__)

# Setting name -> the tier it chooses. The order is the order people say them.
TIERS: dict[str, str] = {
    "chat_model": "conversational",
    "deep_model": "deep",
    "fact_extraction_model": "extraction",
}

# The name each tier's setting is known by on the command line.
FLAGS = {
    "conversational": "chat_model",
    "deep": "deep_model",
    "extraction": "fact_extraction_model",
}


def configured(settings: Settings) -> dict[str, str]:
    """The three models currently configured, keyed by tier label."""
    return {TIERS[name]: getattr(settings, name) for name in TIERS}


def resolve(
    settings: Settings,
    *,
    conversational: str | None = None,
    deep: str | None = None,
    extraction: str | None = None,
) -> dict[str, str]:
    """Validate the requested models and return `{setting_name: model}`.

    Raises ValueError with the pydantic message when a value is refused, so the
    command reports why rather than writing something the next boot rejects.
    """
    requested = {
        "chat_model": conversational,
        "deep_model": deep,
        "fact_extraction_model": extraction,
    }
    changes: dict[str, str] = {}
    for name, value in requested.items():
        if value is None:
            continue
        if not value.strip():
            raise ValueError(f"{TIERS[name]}: a model name is required")

        candidate = settings.model_copy()
        try:
            setattr(candidate, name, value.strip())
        except Exception as exc:  # pydantic ValidationError, and only that
            raise ValueError(f"{TIERS[name]}: {str(exc).splitlines()[0]}") from exc
        changes[name] = getattr(candidate, name)

    if not changes:
        raise ValueError("Nothing to set: name a model, or use --deep/--extraction")
    return changes


def env_updates(changes: dict[str, str]) -> dict[str, str]:
    """`{"chat_model": "x"}` as the `MINUS_` keys `.env` holds."""
    return {f"MINUS_{name.upper()}": value for name, value in changes.items()}


def apply(
    changes: dict[str, str],
    settings: Settings,
    *,
    env_path=None,
    connect=ControlClient,
    socket_path=None,
) -> dict:
    """Apply `changes` live if possible, and persist them either way.

    Returns a report rather than raising for the ordinary case of a stopped
    assistant: "written to .env, takes effect on next start" is a success, and
    the caller prints it as one.
    """
    socket_path = control_socket() if socket_path is None else socket_path
    env_path = (project_root() / ".env") if env_path is None else env_path

    try:
        with connect(socket_path) as client:
            result = client.request("set_config", values=changes)
        result.setdefault("persisted", [])
        return {"live": True, **result}
    except NotRunning:
        updates = env_updates(changes)
        written = update_env_file(env_path, updates, allowed=frozenset(updates))
        return {
            "live": False,
            "applied": [],
            "persisted": written,
            "restart_required": sorted(changes),
            "rejected": {},
        }


def list_candidates(
    settings: Settings,
    needle: str = "",
    *,
    limit: int = 40,
    fetch=None,
) -> list[dict]:
    """Models OpenRouter offers, filtered by a case-insensitive substring.

    `fetch` is injectable so the filtering is testable without a network call;
    the default reads the public catalogue, which needs no API key.
    """
    fetch = fetch or _fetch_catalogue
    models = fetch(settings)
    matched = [
        model
        for model in models
        if needle.lower() in str(model.get("id", "")).lower()
        or needle.lower() in str(model.get("name", "")).lower()
    ]
    return matched[:limit]


def _fetch_catalogue(settings: Settings) -> list[dict]:
    base_url = settings.llm_base_url or settings.openrouter_base_url
    response = httpx.get(f"{base_url.rstrip('/')}/models", timeout=10.0)
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    return [model for model in (data or []) if isinstance(model, dict)]
