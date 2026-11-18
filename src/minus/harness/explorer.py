"""A small, dockable file explorer for the harness.

Three pieces, deliberately kept out of `app.py`:

  * `ProjectTree` hides the directories a developer never wants to click
    (.git, .venv, caches, the Rust target dir) while still letting the tree
    lazy-load everything else.
  * `ExplorerPane` is the left-docked widget: a diffs button, the tree, and a
    syntax-highlighted preview. Its host lives in `app.py`, which mounts it in
    the left dock container and hides it again.
  * `FilePreview` is a RichLog rather than a TextArea: this view is read-only,
    and Rich's `Syntax` gives line numbers and keyword colouring for the
    languages a developer actually opens without adding an LSP.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from rich.syntax import Syntax
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, DirectoryTree, RichLog

# The side pane may grow until the conversation still has this many columns.
# The user asked for roughly 25-30 characters of reading room in the chat.
CHAT_MIN_COLUMNS = 30

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
    """A file tree with a preview, always docked on the left."""

    def __init__(self, root: Path) -> None:
        super().__init__(id="explorer")
        self.root = Path(root)
        self._has_file = False
        # ("dock", x, y, start_size) for the left edge, or
        # ("split", x, y, start_tree_width) for the tree/preview divider.
        self._drag: tuple | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="explorer-toolbar"):
            yield Button("diffs", id="diffs-btn", classes="dockbtn")
        with Horizontal(id="explorer-main"):
            with Vertical(id="explorer-tree-pane"):
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

    # ---- Mouse resizing ----
    #
    # The left dock's right edge is a two-cell grab zone, and the divider
    # between the tree and the code view is a two-cell grab zone. Textual's
    # mouse capture keeps the move events coming even when the pointer leaves
    # the edge, so the drag stays attached to the pane it started on.

    @property
    def _host(self) -> Widget | None:
        parent = self.parent
        return parent if isinstance(parent, Widget) else None

    def _dock_edge(self, x: float, y: float) -> str:
        if self._host is None:
            return ""
        region = self.region
        if region.y <= y < region.bottom and abs(x - (region.right - 1)) <= 1:
            return "dock"
        return ""

    def _split_edge(self, x: float, y: float) -> str:
        if not self.has_class("-preview"):
            return ""
        pane = self.query_one("#explorer-tree-pane")
        boundary = pane.region.right
        region = self.region
        if region.y <= y < region.bottom and abs(x - boundary) <= 1:
            return "split"
        return ""

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if self._drag is not None:
            return
        x, y = event.screen_x, event.screen_y
        if self._dock_edge(x, y):
            host = self._host
            if host is None:
                return
            self._drag = ("dock", x, y, host.size.width)
            self.capture_mouse()
            event.stop()
            return
        if self._split_edge(x, y):
            pane = self.query_one("#explorer-tree-pane")
            self._drag = ("split", x, y, pane.size.width)
            self.capture_mouse()
            event.stop()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self._drag is None:
            return
        kind, start_x, _start_y, start_size = self._drag
        if kind == "dock":
            new_size = self._clamp_dock(start_size + (event.screen_x - start_x))
            host = self._host
            if host is not None:
                host.styles.width = new_size
        else:
            pane = self.query_one("#explorer-tree-pane")
            # Drag left to widen the code view, right to widen the tree.
            new_width = start_size + (event.screen_x - start_x)
            ceiling = max(16, self.size.width - 20)
            pane.styles.width = max(14, min(ceiling, new_width))
        event.stop()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if self._drag is None:
            return
        self._drag = None
        self.release_mouse()
        event.stop()

    def _clamp_dock(self, value: float) -> int:
        body = self.screen.query_one("#body")
        left = self.screen.query_one("#left").outer_size.width
        right = self.screen.query_one("#right").outer_size.width
        ceiling = max(24, body.size.width - left - right - CHAT_MIN_COLUMNS)
        return int(max(24, min(ceiling, value)))
