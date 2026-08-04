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
