"""The panels down the right-hand side, and the two panes on the left."""

from __future__ import annotations

import time
from typing import Any, ClassVar

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import ContentSwitcher, Input, RichLog, Static

# The label each role speaks under, and the style that label is drawn in. Rich
# style names rather than the ansi_* ones theme.tcss is restricted to: this is
# a renderable, not a stylesheet, and rich resolves these against the
# terminal's own sixteen just as faithfully.
SPEAKERS: dict[str, tuple[str, str]] = {
    "user": ("you", "bold cyan"),
    "assistant": ("minus", "bold green"),
    "tool": ("·", "bright_black"),
}

# Wide enough for the longest label, which is "minus".
SPEAKER_WIDTH = 5


def render_turn(role: str, text: str) -> Table:
    """One line of transcript: who spoke, then what they said.

    A two-column grid rather than a prefixed string. It buys two things at
    once: the label can carry its own style while the speech stays plain, and
    rich wraps the speech inside its own column, so a long turn hangs under
    itself instead of returning to the left wall. Doing that by hand with
    textwrap would have to be redone on every resize.
    """
    label, label_style = SPEAKERS.get(role, (role, ""))
    grid = Table.grid(padding=(0, 1))
    grid.add_column(width=SPEAKER_WIDTH, justify="right", no_wrap=True, style=label_style)
    grid.add_column(ratio=1, overflow="fold")
    grid.add_row(label, Text(text, style="bright_black" if role == "tool" else ""))
    return grid


def deep_elapsed(deep: dict | None) -> float | None:
    """How long the deep tier has been thinking, counted here rather than there.

    The snapshot it comes from is pushed on state edges only, so the
    `elapsed_seconds` in it is a reading taken when the tier started and never
    moves afterwards -- which is why the timer used to stop a second or two in.
    Counting from the wall-clock `started_at` instead means every redraw
    produces a fresh number without asking the assistant for one.

    Falls back to the frozen reading for an assistant too old to send a start
    time, so a mismatched pair shows a stale timer rather than none at all.
    """
    if not deep or not deep.get("in_flight"):
        return None
    started_at = deep.get("started_at")
    if started_at is None:
        return deep.get("elapsed_seconds")
    # Clamped: a clock stepped backwards between the two readings would
    # otherwise count down through zero.
    return max(0.0, time.time() - started_at)


def ascii_bar(percent: float | None, width: int = 12) -> str:
    """A meter drawn in characters the console font certainly has.

    Textual's ProgressBar and Sparkline both render block-drawing glyphs that
    the VT font does not carry, so they come out as blanks on the machine this
    is for. Hashes and dashes always work.
    """
    if percent is None:
        return f"[{'?' * width}]   --"
    filled = max(0, min(width, round(width * percent / 100.0)))
    return f"[{'#' * filled}{'-' * (width - filled)}] {percent:4.0f}%"


def human_bytes(count: float) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if count < 1024:
            return f"{count:.0f}{unit}"
        count /= 1024
    return f"{count:.0f}P"


class ViewerPane(Vertical):
    """The conversation, the log, or a deep-think write-up."""

    VIEWS: ClassVar[tuple[str, ...]] = ("conversation", "log", "deep")
    TITLES: ClassVar[dict[str, str]] = {
        "conversation": "conversation",
        "log": "log",
        "deep": "deep think",
    }

    def __init__(self) -> None:
        super().__init__(id="viewer")
        self.index = 0

    def compose(self) -> ComposeResult:
        yield Static(id="viewer-tabs")
        with ContentSwitcher(initial="view-conversation", id="viewer-body"):
            # markup=False throughout: log lines and transcripts are full of
            # square brackets from pretty_json, which Textual would otherwise
            # try to parse as markup and raise on.
            yield RichLog(id="view-conversation", markup=False, wrap=True, max_lines=2000)
            yield RichLog(id="view-log", markup=False, wrap=False, max_lines=2000)
            yield VerticalScroll(Static(id="deep-body"), id="view-deep")

    def on_mount(self) -> None:
        self.border_title = "viewer"
        self.refresh_tabs()

    @property
    def current(self) -> str:
        return self.VIEWS[self.index]

    def show(self, index: int) -> None:
        was_viewing = self.viewing()
        self.index = index % len(self.VIEWS)
        self.query_one(ContentSwitcher).current = f"view-{self.current}"
        self.refresh_tabs()
        # Focus follows the switch, or it is left on a view that is no longer
        # displayed -- at which point the scroll keys move something invisible.
        if was_viewing:
            self.focus_current()

    def viewing(self) -> bool:
        """True if focus is currently inside one of the views."""
        focused = self.app.focused
        return focused is not None and focused.id in {f"view-{view}" for view in self.VIEWS}

    def focus_current(self) -> None:
        """Focus the view that is showing.

        Not `self.focus()`: ViewerPane is a Vertical, and Vertical.can_focus
        is False, so focusing it silently does nothing at all -- which is why
        escape looked dead from the input box while in fact firing correctly.
        The RichLog inside is the focusable thing, and focusing it is also
        what makes the scroll keys move what you are actually looking at.

        `focusable` covers visibility too, so this can never land on one of
        the ContentSwitcher's hidden children.
        """
        view = self.query_one(f"#view-{self.current}")
        if view.focusable:
            view.focus()
        else:
            # Nothing to focus, but the input must still be released or the
            # panel hotkeys keep being typed into it.
            self.app.set_focus(None)

    def cycle(self, step: int) -> None:
        self.show(self.index + step)

    def refresh_tabs(self) -> None:
        labels = []
        for index, view in enumerate(self.VIEWS):
            title = self.TITLES[view]
            labels.append(f" {title.upper()} " if index == self.index else f" {title} ")
        self.query_one("#viewer-tabs", Static).update("←" + "│".join(labels) + "→")


class PromptPane(Vertical):
    """Type here; it reaches MINUS as though it had been spoken.

    Just the one line. What you type used to be echoed into a log above it,
    which said the same thing twice: the transcript above already shows the
    turn as soon as MINUS records it.
    """

    def __init__(self) -> None:
        super().__init__(id="prompt")

    def compose(self) -> ComposeResult:
        yield Input(placeholder="say something…", id="prompt-input")

    def on_mount(self) -> None:
        self.border_title = "input"


class ConsolePane(Vertical):
    """Everything the assistant writes to stdout and stderr.

    Follows a file `minus serve` redirects its own descriptors into, which is
    the same bargain the viewer strikes: reads come off the disk, so the
    console still has yesterday's crash in it with nothing running.
    """

    def __init__(self) -> None:
        super().__init__(id="console")

    def compose(self) -> ComposeResult:
        # wrap=False to match the log view: console output is mostly
        # tracebacks and progress bars, and folding those makes them harder
        # to read rather than easier.
        yield RichLog(id="console-body", markup=False, wrap=False, max_lines=2000)
        yield Static(id="console-live")

    def on_mount(self) -> None:
        self.border_title = r"console  \[c]"

    def write(self, line: str, style: str = "") -> None:
        self.query_one("#console-body", RichLog).write(Text(line, style=style))

    def show_live(self, text: str) -> None:
        """Draw the line a writer is still overwriting, on its own row.

        Kept out of the RichLog above deliberately. That widget has no way to
        replace a line it has already rendered -- `lines` holds rendered
        Strips, and rewriting one means repairing virtual_size, an internal
        cache and the deferred-render queue that is live during backfill. A
        row of its own costs one line and no private API, and it puts the
        spinner where the eye already looks for a status line.
        """
        self.query_one("#console-live", Static).update(text)


class Panel(Vertical, can_focus=True):
    """One management panel, collapsed to a summary until it is expanded.

    Subclasses fill in `summary()`. `options()` is where the expanded contents
    will go and returns nothing today -- deliberately, so that adding them
    later is a matter of returning a list rather than restructuring anything.

    `can_focus` because a Vertical is not focusable by default, which left the
    whole right-hand column unreachable by tab and an expanded panel with no
    way to scroll what did not fit. Textual propagates it to the subclasses.
    """

    title = "panel"
    hotkey = "?"

    def __init__(self, panel_id: str) -> None:
        super().__init__(id=panel_id, classes="panel")
        self.data: Any = None

    def compose(self) -> ComposeResult:
        yield Static(id="summary")
        yield Static(id="options")

    def on_mount(self) -> None:
        # Escaped, or Textual reads the brackets as content markup and every
        # panel silently loses the one thing that says which key opens it.
        self.border_title = rf"{self.title}  \[{self.hotkey}]"
        self.redraw()

    def summary(self) -> str:
        return "…"

    def options(self) -> list[str]:
        return []

    def redraw(self) -> None:
        self.query_one("#summary", Static).update(self.summary())
        entries = self.options()
        body = "\n".join(f"  {entry}" for entry in entries) if entries else "  (nothing here yet)"
        self.query_one("#options", Static).update(f"\n  more options\n  ------------\n{body}")

    def update(self, data: Any) -> None:
        self.data = data
        self.redraw()


class MemoryPanel(Panel):
    title = "memory"
    hotkey = "m"

    def __init__(self) -> None:
        super().__init__("panel-memory")

    def summary(self) -> str:
        if not self.data:
            return "  no data"
        facts = self.data.get("facts") or []
        lines = [f"  {len(facts)} facts"]
        for fact in facts[:4]:
            lines.append(f"  {fact['attribute']} = {fact['value']}"[:40])
        lines.append(f"  {self.data.get('conversations', 0)} conversations")
        lines.append(f"  {self.data.get('deep_notes', 0)} deep notes")
        return "\n".join(lines)


class HardwarePanel(Panel):
    title = "hardware"
    hotkey = "h"

    def __init__(self) -> None:
        super().__init__("panel-hardware")

    def summary(self) -> str:
        if not self.data:
            return "  no data"

        lines = [f"  cpu  {ascii_bar(self.data.get('cpu_percent'))}"]

        memory = self.data.get("memory") or {}
        if memory:
            lines.append(
                f"  mem  {ascii_bar(memory.get('percent'))}"
                f"  {human_bytes(memory.get('used', 0))}/{human_bytes(memory.get('total', 0))}"
            )

        load = self.data.get("load")
        if load:
            lines.append(f"  load {load[0]:.2f} {load[1]:.2f} {load[2]:.2f}")

        temperature = self.data.get("temperature")
        if temperature is not None:
            lines.append(f"  temp {temperature:.0f}C")

        for gpu in self.data.get("gpus") or []:
            lines.append(f"  gpu  {ascii_bar(gpu['utilization'])}  {gpu['temperature']:.0f}C")
            lines.append(f"       {gpu['memory_used']:.0f}/{gpu['memory_total']:.0f}M")

        return "\n".join(lines)


class ToolsPanel(Panel):
    title = "tools"
    hotkey = "t"

    def __init__(self) -> None:
        super().__init__("panel-tools")

    def summary(self) -> str:
        tools = self.data or []
        if not tools:
            return "  no data"
        lines = [f"  {len(tools)} tools"]
        lines.extend(f"  {tool['name']}" for tool in tools)
        return "\n".join(lines)


class ProgramsPanel(Panel):
    title = "external programs"
    hotkey = "p"

    def __init__(self) -> None:
        super().__init__("panel-programs")

    def summary(self) -> str:
        # Honest rather than a plausible-looking empty table: there is no
        # external-program abstraction in MINUS at all yet.
        return "  (none)\n  not implemented"


class AgentsPanel(Panel):
    title = "agents"
    hotkey = "a"

    def __init__(self) -> None:
        super().__init__("panel-agents")

    def summary(self) -> str:
        deep = (self.data or {}).get("deep") or {}
        elapsed = deep_elapsed(deep)
        if elapsed is not None:
            question = (deep.get("question") or "")[:30]
            return f"  deep tier: thinking {elapsed:.0f}s\n  {question}"
        return "  deep tier: idle\n  no other agents"


class ManagementPanel(Panel):
    """Where changing MINUS from the dashboard will live.

    Empty on purpose. The socket already carries `get_config`/`set_config`
    (assembly.build_control_handlers), so this is the place those grow into
    rather than a new one to invent later.
    """

    title = "management"
    hotkey = "g"

    def __init__(self) -> None:
        super().__init__("panel-management")

    def summary(self) -> str:
        return "  (nothing here yet)"
