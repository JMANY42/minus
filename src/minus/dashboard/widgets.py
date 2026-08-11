"""The panels down the right-hand side, and the two panes on the left."""

from __future__ import annotations

from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import ContentSwitcher, Input, RichLog, Static


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
        self.index = index % len(self.VIEWS)
        self.query_one(ContentSwitcher).current = f"view-{self.current}"
        self.refresh_tabs()

    def cycle(self, step: int) -> None:
        self.show(self.index + step)

    def refresh_tabs(self) -> None:
        labels = []
        for index, view in enumerate(self.VIEWS):
            title = self.TITLES[view]
            labels.append(f" {title.upper()} " if index == self.index else f" {title} ")
        self.query_one("#viewer-tabs", Static).update("←" + "│".join(labels) + "→")


class PromptPane(Vertical):
    """Type here; it reaches MINUS as though it had been spoken."""

    def __init__(self) -> None:
        super().__init__(id="prompt")

    def compose(self) -> ComposeResult:
        yield RichLog(id="prompt-echo", markup=False, wrap=True, max_lines=200)
        yield Input(placeholder="say something…", id="prompt-input")

    def on_mount(self) -> None:
        self.border_title = "input"

    def echo(self, line: str) -> None:
        self.query_one("#prompt-echo", RichLog).write(line)


class Panel(Vertical):
    """One management panel, collapsed to a summary until it is expanded.

    Subclasses fill in `summary()`. `options()` is where the expanded contents
    will go and returns nothing today -- deliberately, so that adding them
    later is a matter of returning a list rather than restructuring anything.
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
        self.border_title = f"{self.title}  [{self.hotkey}]"
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
        if deep.get("in_flight"):
            elapsed = deep.get("elapsed_seconds") or 0
            question = (deep.get("question") or "")[:30]
            return f"  deep tier: thinking {elapsed:.0f}s\n  {question}"
        return "  deep tier: idle\n  no other agents"
