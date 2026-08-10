"""Single source of truth for every filesystem location MINUS uses.

Before this module, the project root was recomputed with `Path(__file__)`
arithmetic in four separate places (the tool handler, the logging setup, the
TTS model loader and the memory manager). Each used a different number of
`.parents[...]` hops, so moving any file one directory deeper silently
repointed that module's data directory somewhere else.

Everything now derives from `project_root()`, which can be overridden with
`MINUS_PROJECT_ROOT` for tests or for running from an installed (non-editable)
wheel where walking up from `__file__` would land in site-packages.
"""

from __future__ import annotations

import os
from pathlib import Path

# src/minus/paths.py -> src/minus -> src -> <repo root>
_ROOT_FROM_SOURCE = Path(__file__).resolve().parents[2]

_ENV_PROJECT_ROOT = "MINUS_PROJECT_ROOT"
_ENV_CONTROL_SOCKET = "MINUS_CONTROL_SOCKET"


def project_root() -> Path:
    """The repo root: the directory holding models/, memory/ and logs/."""
    override = os.getenv(_ENV_PROJECT_ROOT)
    if override:
        return Path(override).expanduser().resolve()
    return _ROOT_FROM_SOURCE


def models_dir() -> Path:
    return project_root() / "models"


def logs_dir() -> Path:
    return project_root() / "logs"


def memory_dir() -> Path:
    return project_root() / "memory"


def conversations_dir() -> Path:
    return memory_dir() / "conversations"


def condensed_conversations_dir() -> Path:
    return memory_dir() / "condensed_conversations"


def semantic_memory_db() -> Path:
    return memory_dir() / "semantic_memory.db"


def control_socket() -> Path:
    """Where the running assistant listens for the dashboard.

    Deliberately not under `project_root()`. A socket is an artefact of *this
    boot on this machine*, not of the project: a stale one left in a synced or
    backed-up directory would be a lie about a process that no longer exists.
    `$XDG_RUNTIME_DIR` is exactly the directory for this -- it is per-user,
    0700, on tmpfs, and cleaned when the last session ends. The /tmp fallback
    is for a bare tty login without logind, where it is unset.

    Kept short on purpose: an AF_UNIX path is capped at 108 bytes, and the
    error when it overflows is silent truncation rather than a clear failure.
    """
    override = os.getenv(_ENV_CONTROL_SOCKET)
    if override:
        return Path(override).expanduser()

    runtime = os.getenv("XDG_RUNTIME_DIR")
    base = Path(runtime) / "minus" if runtime else Path(f"/tmp/minus-{os.getuid()}")
    return base / "control.sock"


def deep_notes_dir() -> Path:
    """Where the deep tier's full written answers land.

    The spoken summary goes to the speaker; this is the other half, kept as
    text so a dashboard can render it without going through TTS.
    """
    return memory_dir() / "deep_notes"
