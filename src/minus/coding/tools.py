"""Workspace tools for `minus code`, gated by an approver.

The gate is the point. A model with `run_shell` and `write_file` is a model with
the user's whole account, so every mutating action is first rendered as an
`ApprovalRequest` and only runs if the approver says yes. Declining returns a
normal tool result telling the model not to retry, rather than raising: a
refusal is information, not a crash.

Every path goes through `tools/workspace.py::resolve_workspace_path` with this
session's root, so escapes (absolute paths, `..`, symlinks out) are refused
before anything is written or read.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from minus.errors import ToolExecutionError
from minus.tools.workspace import resolve_workspace_path

MAX_READ_BYTES = 262144
MAX_STREAM_CHARS = 16384
DEFAULT_SHELL_TIMEOUT = 120


@dataclass(frozen=True)
class ApprovalRequest:
    """One thing the model wants to do to the user's machine."""

    action: str
    detail: str


class Approver(Protocol):
    def approve(self, request: ApprovalRequest) -> bool: ...


class AlwaysApprover:
    """Approve everything. Only reachable through `--yes`."""

    def approve(self, request: ApprovalRequest) -> bool:
        return True


class ConsoleApprover:
    """Ask on the terminal, with a session-wide "always" escape."""

    def __init__(
        self,
        read: Callable[[str], str] = input,
        echo: Callable[[str], None] = print,
    ) -> None:
        self._read = read
        self._echo = echo
        self.always = False

    def approve(self, request: ApprovalRequest) -> bool:
        if self.always:
            return True
        answer = self._read(
            f"  allow {request.action}: {request.detail} [y]es/[n]o/[a]lways "
        ).strip().lower()
        if answer in ("a", "always"):
            self.always = True
            return True
        return answer in ("y", "yes")


def _clip(text: str | None) -> str:
    if not text:
        return ""
    if len(text) <= MAX_STREAM_CHARS:
        return text
    return text[:MAX_STREAM_CHARS] + "...[truncated]"


class CodingTools:
    """The five tools a coding session may call."""

    def __init__(self, registry: Any, root: Path, approver: Approver) -> None:
        self.registry = registry
        self.root = Path(root).resolve()
        self.approver = approver

    def register(self) -> None:
        for tool in (
            self.list_files,
            self.read_file,
            self.write_file,
            self.apply_patch,
            self.run_shell,
        ):
            self.registry.tool(tool, category="coding")

    # ---- Helpers ----

    def _relative(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.root)) or "."
        except ValueError:  # pragma: no cover - resolve_workspace_path prevents it
            return str(path)

    @staticmethod
    def _declined(action: str) -> dict:
        return {
            "status": "declined",
            "note": f"The user declined this {action}. Do not retry it; ask what to change.",
        }

    # ---- Tools ----

    def list_files(self, path: str = ".") -> dict:
        """List a directory inside the workspace.

        Args:
            path: Workspace-relative directory, default the root.
        """
        target = resolve_workspace_path(path, root=self.root)
        if not target.is_dir():
            raise ToolExecutionError(f"{path!r} is not a directory")

        entries = []
        for entry in sorted(target.iterdir(), key=lambda item: (item.is_file(), item.name.lower())):
            is_file = entry.is_file()
            entries.append(
                {
                    "name": entry.name,
                    "type": "file" if is_file else "dir",
                    "size": entry.stat().st_size if is_file else 0,
                }
            )
        return {"path": self._relative(target), "entries": entries}

    def read_file(self, path: str) -> dict:
        """Read a workspace file as text.

        Args:
            path: Workspace-relative file path.
        """
        target = resolve_workspace_path(path, root=self.root)
        if not target.is_file():
            raise ToolExecutionError(f"{path!r} is not a file")

        size = target.stat().st_size
        with target.open("r", encoding="utf-8", errors="replace") as handle:
            content = handle.read(MAX_READ_BYTES)
        return {
            "path": self._relative(target),
            "content": content,
            "truncated": size > MAX_READ_BYTES,
        }

    def write_file(self, path: str, content: str) -> dict:
        """Write a text file, creating parent directories, after approval.

        Args:
            path: Workspace-relative file path.
            content: The complete new contents.
        """
        target = resolve_workspace_path(path, root=self.root)
        relative = self._relative(target)
        if not self.approver.approve(ApprovalRequest("write", relative)):
            return self._declined("write")

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"path": relative, "written": len(content)}

    def apply_patch(self, path: str, old: str, new: str) -> dict:
        """Replace one exact occurrence of `old` with `new` in a file.

        Args:
            path: Workspace-relative file path.
            old: The exact text to replace; must appear exactly once.
            new: The replacement text.
        """
        target = resolve_workspace_path(path, root=self.root)
        if not target.is_file():
            raise ToolExecutionError(f"{path!r} is not a file")

        original = target.read_text(encoding="utf-8")
        count = original.count(old)
        if count == 0:
            raise ToolExecutionError(f"{old!r} does not appear in {path!r}")
        if count > 1:
            raise ToolExecutionError(
                f"{old!r} appears {count} times in {path!r}; include more context"
            )

        relative = self._relative(target)
        if not self.approver.approve(ApprovalRequest("patch", relative)):
            return self._declined("patch")

        target.write_text(original.replace(old, new, 1), encoding="utf-8")
        return {"path": relative, "replaced": 1}

    def run_shell(self, command: str, timeout_seconds: int = DEFAULT_SHELL_TIMEOUT) -> dict:
        """Run a shell command in the workspace root, after approval.

        Args:
            command: Shell command, run with `sh -c` inside the workspace.
            timeout_seconds: Kill the whole process group after this long.
        """
        if not self.approver.approve(ApprovalRequest("shell", command)):
            return self._declined("command")

        try:
            process = subprocess.Popen(
                command,
                shell=True,
                cwd=self.root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            return {"exit_code": 127, "timed_out": False, "stdout": "", "stderr": str(exc)}

        try:
            stdout, stderr = process.communicate(timeout=max(1, int(timeout_seconds)))
        except subprocess.TimeoutExpired:
            # `communicate` kills the direct child; the process group is what
            # gets the grandchildren too.
            with contextlib.suppress(OSError, ProcessLookupError):
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            stdout, stderr = process.communicate()
            return {
                "exit_code": None,
                "timed_out": True,
                "stdout": _clip(stdout),
                "stderr": _clip(stderr),
            }

        return {
            "exit_code": process.returncode,
            "timed_out": False,
            "stdout": _clip(stdout),
            "stderr": _clip(stderr),
        }
