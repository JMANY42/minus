"""Changing configuration on a running assistant.

Most of `Settings` turns out to be live-changeable, not because anything was
designed for it but because the long-lived objects re-read their values on
every call: `OpenRouterClient.complete` reads `self._settings.chat_model` each
time, `KokoroSpeaker._synthesize` reads `self.voice` per chunk. Where that is
true, changing the attribute is the whole implementation.

Where it is not true the honest answer is "restart", and this module says so
rather than accepting a change that quietly does nothing. There is no
mechanism that guesses: a field is live only if it appears in the table the
composition root builds, because that is the only place that knows where each
value actually lives.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from minus.services.env_file import update_env_file

logger = logging.getLogger(__name__)

# Never leaves the process and never reaches the file from here. The dashboard
# has no reason to read an API key and no business writing one.
SECRETS = frozenset(
    {
        "openrouter_api_key",
        # The Google grant is a credential in three parts, and the refresh
        # token is the half that does not expire. All three are withheld for
        # the same reason the API key is: a dashboard has no use for them.
        "google_client_id",
        "google_client_secret",
        "google_refresh_token",
    }
)

# Not "restart required" -- changing either of these invalidates data that
# already exists, and a restart does not undo that. The vec0 table's dimension
# is fixed when the table is created, and stored vectors are specific to the
# model that produced them, so a silent swap turns similarity search into
# noise rather than breaking loudly.
BLOCKED = {
    "embedding_dim": (
        "The fact store's vector dimension is fixed when the table is created; "
        "changing it would invalidate every embedding in semantic_memory.db."
    ),
    "embedding_model": (
        "Stored embeddings come from this model; changing it without re-embedding "
        "every fact silently corrupts similarity search."
    ),
}

# Read once at startup, so setting it on a live process is a lie. Reported as
# not-applicable rather than as restart-required, because under a service
# there is no console for it to apply to at all.
NOT_APPLICABLE = {
    "console_log_level": "There is no console handler when running as a service.",
}


@dataclass(frozen=True)
class LiveField:
    """A setting that can be changed without a restart, and how."""

    name: str
    apply: Callable[[Any], None]


class ConfigController:
    """Reads and writes configuration on a running assistant."""

    def __init__(
        self,
        settings: Any,
        live: dict[str, LiveField],
        env_path: Path | str | None = None,
    ) -> None:
        self.settings = settings
        self.live = live
        self.env_path = Path(env_path) if env_path is not None else None

    # ---- Reading ----

    def _value(self, name: str) -> Any:
        return getattr(self.settings, name, None)

    def describe(self) -> dict:
        """Every field, its value, and what changing it would take."""
        fields = [name for name in type(self.settings).model_fields if name not in SECRETS]

        return {
            # Every non-secret value in one place, in the order config.py
            # declares them. The four groups below say what may be *done* with
            # a field; a reader that wants to show the whole of the
            # configuration should not have to reassemble it from them -- and
            # blocked and not-applicable fields have no value in those groups
            # at all, only a reason.
            "values": {name: self._value(name) for name in fields},
            "live": {name: self._value(name) for name in fields if name in self.live},
            "restart_required": {
                name: self._value(name)
                for name in fields
                if name not in self.live and name not in BLOCKED and name not in NOT_APPLICABLE
            },
            "blocked": BLOCKED,
            "not_applicable": NOT_APPLICABLE,
            "secrets": sorted(SECRETS),
        }

    # ---- Writing ----

    def _coerce(self, name: str, value: Any) -> Any:
        """Validate against the real model, and take its coerced value.

        Settings has validate_assignment=True, so assigning to a copy both
        rejects nonsense and converts `"0.4"` to a float exactly as loading
        from the environment would -- which means a value set from the
        dashboard and the same value set in .env end up identical.
        """
        candidate = self.settings.model_copy()
        setattr(candidate, name, value)
        return getattr(candidate, name)

    def apply(self, values: dict, *, persist: bool = True) -> dict:
        """Change what can be changed, and report honestly about the rest."""
        result: dict[str, Any] = {
            "applied": [],
            "persisted": [],
            "restart_required": [],
            "rejected": {},
        }
        to_persist: dict[str, str] = {}

        for name, raw in values.items():
            refusal = self._refuse(name)
            if refusal is not None:
                result["rejected"][name] = refusal
                continue

            try:
                value = self._coerce(name, raw)
            except Exception as exc:
                result["rejected"][name] = str(exc).splitlines()[0]
                continue

            field = self.live.get(name)
            if field is not None:
                field.apply(value)
                result["applied"].append(name)
            else:
                result["restart_required"].append(name)

            to_persist[self.env_name(name)] = str(value)

        if persist and to_persist and self.env_path is not None:
            written = update_env_file(self.env_path, to_persist, allowed=frozenset(to_persist))
            result["persisted"] = written

        return result

    def _refuse(self, name: str) -> str | None:
        if name in SECRETS:
            return "Secrets cannot be set over the control socket."
        if name in BLOCKED:
            return BLOCKED[name]
        if name in NOT_APPLICABLE:
            return NOT_APPLICABLE[name]
        if name not in type(self.settings).model_fields:
            return f"Unknown setting {name!r}"
        return None

    @staticmethod
    def env_name(name: str) -> str:
        return f"MINUS_{name.upper()}"
