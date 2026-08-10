"""Workspace filesystem tools."""

from __future__ import annotations

from minus.errors import WorkspacePathError
from minus.tools.registry import registry
from minus.tools.workspace import display_path, resolve_workspace_path

# Reading a whole file into a voice assistant's context is rarely useful and
# occasionally catastrophic (the models/ directory holds hundreds of MB). Cap
# it and tell the model plainly that it received a prefix.
MAX_FILE_CHARS = 20_000


@registry.tool
def list_workspace_files(path: str = ".") -> dict:
    """List files and folders inside a workspace-relative directory.

    Args:
        path: Workspace-relative directory path to inspect. Defaults to the
            workspace root.
    """
    directory = resolve_workspace_path(path)

    if not directory.exists():
        raise WorkspacePathError(f"Path does not exist: {path}")
    if not directory.is_dir():
        raise WorkspacePathError(f"Path is not a directory: {path}")

    entries = [
        {"name": item.name, "type": "directory" if item.is_dir() else "file"}
        for item in sorted(directory.iterdir(), key=lambda p: p.name.lower())
    ]
    return {"path": display_path(directory), "entries": entries}


@registry.tool
def read_workspace_file(path: str) -> dict:
    """Read a workspace-relative file and return its contents.

    Args:
        path: Workspace-relative file path to read.
    """
    file_path = resolve_workspace_path(path)

    if not file_path.exists():
        raise WorkspacePathError(f"File does not exist: {path}")
    if not file_path.is_file():
        raise WorkspacePathError(f"Path is not a file: {path}")

    try:
        content = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise WorkspacePathError(f"File is not valid UTF-8 text: {path}") from exc

    truncated = len(content) > MAX_FILE_CHARS
    return {
        "path": display_path(file_path),
        "content": content[:MAX_FILE_CHARS],
        "truncated": truncated,
    }
