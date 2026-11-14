"""A small, dockable file explorer for the harness.

Three pieces, deliberately kept out of `app.py`:

  * `ProjectTree` hides the directories a developer never wants to click
    (.git, .venv, caches, the Rust target dir) while still letting the tree
    lazy-load everything else.
  * `ExplorerPane` is the dockable widget: four placement buttons, the tree,
    and a syntax-highlighted preview. Its host lives in `app.py`, which moves
    the pane between four dock containers.
  * `FilePreview` is a RichLog rather than a TextArea: this view is read-only,
    and Rich's `Syntax` gives line numbers and keyword colouring for the
    languages a developer actually opens without adding an LSP.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from rich.syntax import Syntax
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DirectoryTree, RichLog

# Directories that are either huge, machine-specific, or both. The explorer is
# for reading the checkout, not for browsing a virtualenv or a build cache.
HIDDEN_DIRS = {
    ".git",
    ".venv",
    ".venv.old",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    "target",
    "logs",
    "memory",
    "quarantine",
    "library",
}

MAX_PREVIEW_BYTES = 160 * 1024
MAX_PREVIEW_LINES = 5000
MAX_DIFF_BYTES = 256 * 1024


class ProjectTree(DirectoryTree):
    """The workspace, minus the trees nobody reads."""

    def filter_paths(self, paths):
        return [
            path for path in paths if path.name not in HIDDEN_DIRS and not path.name.startswith(".")
        ]


class FilePreview(RichLog):
    """Read-only, syntax-highlighted file content."""

    def show(self, path: Path) -> None:
        self.clear()
        try:
            size = path.stat().st_size
            with path.open("rb") as handle:
                raw = handle.read(MAX_PREVIEW_BYTES)
        except OSError as exc:
            self.write(f"cannot read {path}: {exc}")
            return
        if b"\x00" in raw:
            self.write(f"{path.name} · binary file · {size:,} bytes")
            return
        try:
            code = raw.decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover - decode with errors cannot fail
            self.write(f"{path.name} · undecodable text")
            return
        lines = code.splitlines()[:MAX_PREVIEW_LINES]
        truncated = size > MAX_PREVIEW_BYTES or len(code.splitlines()) > MAX_PREVIEW_LINES
        if truncated:
            lines.append("… (truncated for preview)")
        code = "\n".join(lines)
        try:
            lexer = Syntax.guess_lexer(str(path), code)
        except Exception:
            lexer = "text"
        self.write(
            Syntax(
                code,
                lexer,
                line_numbers=True,
                word_wrap=False,
                theme="monokai",
            )
        )
        if truncated:
            self.write(
                f"preview capped at {MAX_PREVIEW_BYTES // 1024} KiB / {MAX_PREVIEW_LINES} lines"
            )

    def show_diff(self, label: str, diff: str) -> None:
        """Render a unified diff with the diff lexer (green/red hunks)."""
        self.clear()
        self.write(label)
        if not diff.strip():
            self.write("no changes")
            return
        if len(diff) > MAX_DIFF_BYTES:
            diff = diff[:MAX_DIFF_BYTES] + "\n… diff truncated …"
        self.write(
            Syntax(
                diff,
                "diff",
                line_numbers=False,
                word_wrap=False,
                theme="monokai",
            )
        )


class ExplorerPane(Vertical):
    """A file tree with a preview, movable between four docks."""

    def __init__(self, root: Path) -> None:
        super().__init__(id="explorer")
        self.root = Path(root)
        self._has_file = False

    def compose(self) -> ComposeResult:
        with Horizontal(id="explorer-toolbar"):
            yield Button("left", id="dock-left-btn", classes="dockbtn")
            yield Button("right", id="dock-right-btn", classes="dockbtn")
            yield Button("top", id="dock-top-btn", classes="dockbtn")
            yield Button("bottom", id="dock-bottom-btn", classes="dockbtn")
            yield Button("nvim", id="nvim-btn", classes="dockbtn nvimbtn")
            yield Button("diffs", id="diffs-btn", classes="dockbtn")
        yield ProjectTree(self.root, id="file-tree")
        yield FilePreview(id="file-preview", wrap=False, markup=False)

    def on_mount(self) -> None:
        # The tree is the primary control; give it room until a file is picked.
        # Moving the pane between docks remounts it, so an existing preview is
        # preserved rather than reset to the tree-only layout.
        if not self._has_file:
            self.query_one("#file-preview", FilePreview).display = False

    def show_file(self, path: Path) -> None:
        self._has_file = True
        self.add_class("-preview")
        preview = self.query_one("#file-preview", FilePreview)
        preview.display = True
        preview.show(Path(path))
        preview.scroll_home(animate=False)

    def show_diff(self, path: Path | None = None) -> str:
        """Show the working-tree diff, or the selected file's diff.

        `git diff HEAD` is the useful view while developing: it includes both
        staged and unstaged changes. A workspace that is not a git checkout
        says so rather than showing an empty pane.
        """
        command = ["git", "-C", str(self.root), "diff", "--no-color", "HEAD"]
        label = "DIFF · workspace"
        if path is not None:
            command += ["--", str(path)]
            try:
                label = f"DIFF · {Path(path).relative_to(self.root)}"
            except ValueError:
                label = f"DIFF · {path}"
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=15, check=False
            )
        except (OSError, subprocess.SubprocessError) as exc:
            diff = f"cannot read git diff: {exc}"
        else:
            diff = completed.stdout if completed.returncode == 0 else completed.stderr
        self._has_file = True
        self.add_class("-preview")
        preview = self.query_one("#file-preview", FilePreview)
        preview.display = True
        preview.show_diff(label, diff)
        preview.scroll_home(animate=False)
        return diff

    def toggle_preview(self) -> None:
        """Collapse or restore the preview without losing the tree."""
        preview = self.query_one("#file-preview", FilePreview)
        preview.display = not preview.display
        self.set_class(preview.display, "-preview")
