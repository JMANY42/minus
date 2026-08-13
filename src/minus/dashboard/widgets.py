"""The panels down the right-hand side, and the two panes on the left."""

from __future__ import annotations

import time
from typing import Any, ClassVar

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.widget import Widget
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

    def scroll_current(self, direction: str) -> None:
        """Scroll the view that is showing, without it having focus.

        The views' own scroll bindings only fire while one of them holds the
        keys, so with focus nowhere the arrows did nothing at all. Calling the
        same scroll methods those bindings call keeps a bare arrow moving the
        pane by the same amount either way.
        """
        view = self.query_one(f"#view-{self.current}")
        getattr(view, f"scroll_{direction}")(animate=False)

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

    Two halves. `summary()` is the handful of lines the panel shows while it is
    one of six sharing the column, and every subclass fills it in. The expanded
    half is a widget tree of its own: `compose_expanded()` builds it, and CSS
    lays it out only while the panel has the column. That is where a panel with
    something to *do* puts the thing that does it -- the memory panel's fact
    list is the first. A panel with nothing interactive yet inherits the
    default, a static list of the options it will one day offer.

    The expanded subtree is composed up front and hidden rather than mounted on
    demand: mounting is asynchronous, so focusing what was just mounted becomes
    a two-step dance, while a class change applies the stylesheet immediately
    and `expand()` can hand over focus in the same breath.

    `can_focus` because a Vertical is not focusable by default, which left the
    whole right-hand column unreachable by tab and an expanded panel with no
    way to scroll what did not fit. Textual propagates it to the subclasses.
    """

    title = "panel"
    hotkey = "?"

    # True for a panel whose expanded view wants the whole frame: the summary
    # stops being laid out while it is open. One CSS rule rather than a branch
    # anywhere, and the summary is back the moment the panel collapses.
    takeover = False

    def __init__(self, panel_id: str) -> None:
        classes = "panel -takeover" if self.takeover else "panel"
        super().__init__(id=panel_id, classes=classes)
        self.data: Any = None

    def compose(self) -> ComposeResult:
        yield Static(id="summary")
        with Vertical(id="expanded"):
            yield from self.compose_expanded()

    def compose_expanded(self) -> ComposeResult:
        """What the panel shows once it has the column."""
        yield Static(id="options")

    def interactive(self) -> Widget | None:
        """The widget inside the expanded view that keys should go to.

        None for a panel that is only something to read, which is why
        `expand()` falls back to focusing the panel itself.
        """
        return None

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
        self.redraw_expanded()

    def redraw_expanded(self) -> None:
        """Fill the expanded half. Overridden by panels that build their own."""
        entries = self.options()
        body = "\n".join(f"  {entry}" for entry in entries) if entries else "  (nothing here yet)"
        self.query_one("#options", Static).update(f"\n  more options\n  ------------\n{body}")

    def expand(self) -> None:
        """Take the column, and hand focus to whatever is interactive inside.

        Focusing the inner widget rather than the panel is what makes its keys
        work without tabbing to it, and -- because Textual only routes keys to
        what has focus -- what stops its arrows reaching the viewer.
        """
        self.add_class("-expanded")
        target = self.interactive()
        (target if target is not None else self).focus()

    def collapse(self) -> None:
        """Give the column back, and take focus out of what is being hidden.

        The focus move is not optional. `Widget.focusable` looks at visibility
        and not at `display`, so focus can sit on a widget CSS has stopped
        laying out at all -- where it goes on swallowing every key aimed at
        something else.
        """
        held = self.holds_focus()
        self.remove_class("-expanded", "-hidden")
        if held:
            self.focus()

    def holds_focus(self) -> bool:
        """True if focus is on something inside this panel rather than on it."""
        focused = self.app.focused
        return focused is not None and self in focused.ancestors

    def update(self, data: Any) -> None:
        self.data = data
        self.redraw()


class FactList(Widget, can_focus=True):
    """Every fact MINUS remembers, with a cursor and a set of marks.

    The `minus memory` curses tool (scripts/memory_tui.py) brought inside the
    dashboard, keys and all: arrow to a fact, space to mark it, `d` to forget
    the marked ones. Kept deliberately parallel to it rather than shared with
    it -- that one holds `Fact` records straight out of sqlite, this one holds
    whatever came back over the socket, and one function bending to both would
    be worse than two that each say what they mean.

    Not Textual's SelectionList, which draws its checkboxes out of the block
    glyphs the console font this is built for does not carry -- the same reason
    ascii_bar exists.

    Draws its own window instead of living in a scroller. It knows where the
    cursor is, so it can keep it on screen without one, and owning up and down
    outright is what stops them reaching whatever is behind it.
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("up,k", "move(-1)", "up", show=False),
        Binding("down,j", "move(1)", "down", show=False),
        Binding("space", "mark", "mark", show=False),
        Binding("d", "delete", "forget", show=False),
        # No `a`/`n` for mark-all and clear, which the curses tool does have: a
        # focused widget's bindings beat the app's, so binding `a` here would
        # quietly stop it opening the agents panel.
    ]

    HINT = "  ↑↓ move · space mark · d forget"

    class Delete(Message):
        """The user has confirmed that these facts should be forgotten."""

        def __init__(self, ids: list[str]) -> None:
            self.ids = ids
            super().__init__()

    def __init__(self) -> None:
        super().__init__(id="fact-list")
        self.facts: list[dict] = []
        self.cursor = 0
        self.marked: set[str] = set()
        # Armed by the first `d`, spent by the second. Deleting is a hard
        # delete with no history behind it, so it takes saying twice.
        self.pending = False

    def set_facts(self, facts: list[dict]) -> None:
        """Take a fresh list without losing the reader's place in it.

        Marks are kept by id and dropped when their fact is gone, so a refresh
        arriving while the panel is open -- a rollover repopulates the panels
        on its own -- cannot leave a mark pointing at something deleted.
        """
        self.facts = list(facts)
        ids = {fact.get("id") for fact in self.facts}
        self.marked &= ids
        self.cursor = max(0, min(self.cursor, len(self.facts) - 1))
        self.refresh()

    @property
    def selected(self) -> list[str]:
        """What `d` would forget: everything marked, or else what is under the cursor."""
        if self.marked:
            return [fact["id"] for fact in self.facts if fact["id"] in self.marked]
        if not self.facts:
            return []
        return [self.facts[self.cursor]["id"]]

    def render(self) -> Text:
        # A window of rows that follows the cursor, then the footer on the last
        # line -- the arrangement the curses tool uses, minus its header, which
        # the panel's border title already provides.
        height = max(1, self.size.height - 1)
        top = max(0, self.cursor - height + 1)
        shown = range(top, min(top + height, len(self.facts)))

        lines = [self.row(index) for index in shown]
        if not lines:
            lines.append(self.one_line(Text("  no facts", style="bright_black")))
        lines.extend([Text()] * (height - len(lines)))
        lines.append(self.one_line(Text(self.footer(), style="bright_black")))
        return Text("\n").join(lines)

    def one_line(self, line: Text, pad: bool = False) -> Text:
        """Crop a row to the panel rather than let it fold into two.

        A folded row pushes everything under it down and the footer off the
        bottom, because the window is counted in facts and not in lines.
        `pad` fills the row out to the full width, so the cursor reads as a bar
        across the panel rather than a highlight that stops after the text.
        """
        line.truncate(max(self.size.width, 0), overflow="ellipsis", pad=pad)
        return line

    def row(self, index: int) -> Text:
        fact = self.facts[index]
        checkbox = "[x]" if fact["id"] in self.marked else "[ ]"
        inactive = "" if fact.get("active", True) else " (inactive)"
        line = f" {checkbox} {fact['attribute']} = {fact['value']}{inactive}"
        return self.one_line(Text(line, style=self.row_style(index)), pad=True)

    def row_style(self, index: int) -> str:
        """The curses tool's colours, said in rich rather than in colour pairs."""
        marked = self.facts[index]["id"] in self.marked
        if index == self.cursor:
            return "red on yellow" if marked else "black on yellow"
        return "red" if marked else ""

    def footer(self) -> str:
        if self.pending:
            return f"  forget {len(self.selected)} fact(s)? press d again"
        return self.HINT

    def action_move(self, step: int) -> None:
        self.cursor = max(0, min(self.cursor + step, len(self.facts) - 1))
        self.pending = False
        self.refresh()

    def action_mark(self) -> None:
        if not self.facts:
            return
        fact_id = self.facts[self.cursor]["id"]
        self.marked ^= {fact_id}
        self.pending = False
        self.refresh()

    def action_delete(self) -> None:
        ids = self.selected
        if not ids:
            return
        if not self.pending:
            self.pending = True
            self.refresh()
            return
        self.pending = False
        self.marked.clear()
        self.refresh()
        self.post_message(self.Delete(ids))

    def on_blur(self) -> None:
        # Leaving the list disarms it: a `d` typed a minute later, somewhere
        # else, must not be the second half of this one.
        self.pending = False
        self.refresh()


class MemoryPanel(Panel):
    title = "memory"
    hotkey = "m"

    def __init__(self) -> None:
        super().__init__("panel-memory")

    def compose_expanded(self) -> ComposeResult:
        yield FactList()

    def interactive(self) -> Widget | None:
        return self.query_one(FactList)

    def redraw_expanded(self) -> None:
        facts = (self.data or {}).get("facts") or []
        self.query_one(FactList).set_facts(facts)

    def summary(self) -> str:
        if not self.data:
            return "  no data"
        facts = self.data.get("facts") or []
        return "\n".join(
            [
                f"  {len(facts)} facts | 0 procedures | 0 experiences ( not implemented yet )",
                f"  {self.data.get('conversations', 0)} conversations",
                f"  {self.data.get('deep_notes', 0)} deep notes",
            ]
        )


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
