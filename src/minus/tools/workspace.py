"""Confine tool filesystem access to the workspace.

The containment check used to compare resolved paths as strings:

    workspace_prefix = str(WORKSPACE_ROOT.resolve()) + os.sep
    if resolved != root and not str(resolved).startswith(workspace_prefix):

That is correct but brittle -- it depends on separator handling and on both
sides being resolved the same way. `Path.is_relative_to` expresses the same
intent directly and is not fooled by casing or trailing-separator differences.

Resolution happens before the check, so symlinks that point outside the
workspace are rejected rather than followed.
"""

from __future__ import annotations

from pathlib import Path

from minus.errors import WorkspacePathError
from minus.paths import project_root


def resolve_workspace_path(relative_path: str = ".", root: Path | None = None) -> Path:
    """Resolve a workspace-relative path, refusing anything that escapes it."""
    workspace = (root or project_root()).resolve()
    candidate = Path(relative_path)

    if candidate.is_absolute():
        raise WorkspacePathError(
            f"Tool paths must be workspace-relative, got absolute path: {relative_path}"
        )

    resolved = (workspace / candidate).resolve()

    if not resolved.is_relative_to(workspace):
        raise WorkspacePathError(f"Tool path escapes the workspace root: {relative_path}")

    return resolved


def display_path(path: Path, root: Path | None = None) -> str:
    """Render an absolute path back as the workspace-relative form the model sent."""
    workspace = (root or project_root()).resolve()
    relative = path.relative_to(workspace)
    return str(relative) if str(relative) != "." else "."
