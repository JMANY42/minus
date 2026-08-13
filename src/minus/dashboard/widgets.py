"""The panels down the right-hand side, and the two panes on the left."""

from __future__ import annotations

import textwrap
import time
from dataclasses import dataclass
from typing import Any, ClassVar

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.widget import Widget
from textual.widgets import ContentSwitcher, Input, RichLog, Static

from minus.dashboard.choices import OTHER, choices_for

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


def wrap_indented(text: str, width: int, indent: str = "  ") -> str:
    """`text` as lines that all start at `indent` and none of which exceed `width`.

    By hand rather than left to the widget: a panel's summary is a string
    handed to a Static, and the Static wraps it back to the left wall, so the
    second line of a two-line question would start under the border instead of
    under the first. The cost is that it has to be redone whenever the panel
    changes width, which is what the caller's `on_resize` is for.
    """
    if width <= len(indent) + 1:
        # No room to wrap into. Better one over-long line the Static will fold
        # than a column of single characters.
        return indent + text
    return "\n".join(textwrap.wrap(text, width, initial_indent=indent, subsequent_indent=indent))


def crop(line: Text, width: int, pad: bool = False) -> Text:
    """Crop a row to the panel rather than let it fold into two.

    Shared by the two lists that draw their own window. A folded row pushes
    everything under it down and the footer off the bottom, because the window
    is counted in rows and not in lines. `pad` fills the row out to the full
    width, so the cursor reads as a bar across the panel rather than a
    highlight that stops after the text.
    """
    line.truncate(max(width, 0), overflow="ellipsis", pad=pad)
    return line


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
            lines.append(crop(Text("  no facts", style="bright_black"), self.size.width))
        lines.extend([Text()] * (height - len(lines)))
        lines.append(crop(Text(self.footer(), style="bright_black"), self.size.width))
        return Text("\n").join(lines)

    def row(self, index: int) -> Text:
        fact = self.facts[index]
        checkbox = "[x]" if fact["id"] in self.marked else "[ ]"
        inactive = "" if fact.get("active", True) else " (inactive)"
        line = f" {checkbox} {fact['attribute']} = {fact['value']}{inactive}"
        return crop(Text(line, style=self.row_style(index)), self.size.width, pad=True)

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
            # The whole question, wrapped: it used to be cut at 30 characters,
            # which for most questions meant the panel showed the beginning of
            # one and left the reader to guess the rest.
            question = deep.get("question") or ""
            lines = [f"  deep tier: thinking {elapsed:.0f}s"]
            if question:
                lines.append(wrap_indented(question, self.content_size.width))
            return "\n".join(lines)
        return "  deep tier: idle\n  no other agents"

    def on_resize(self) -> None:
        # The question is wrapped against the panel's own width, so a panel
        # that just changed width is holding a summary wrapped for the old one.
        self.redraw()


@dataclass(frozen=True)
class Heading:
    """A section divider in the settings list. Nothing can be done to one."""

    text: str


@dataclass(frozen=True)
class Setting:
    """One field of config.py: its value, and what changing it would do."""

    name: str
    value: str
    note: str
    editable: bool


# What the three groups are called on screen, and what each row under them
# does when it is changed. Said in the panel's own words rather than repeated
# from config_control: the reasons that module carries are for the fields it
# refuses, and those it sends along with the refusal.
APPLIES_NOW = "applies immediately"
NEEDS_RESTART = "saved to .env; applies on the next restart"
NEVER_SHOWN = "never sent over the control socket"


def show_value(value: Any) -> str:
    """A setting's value as one line. Every field in config.py is a scalar."""
    return "" if value is None else str(value)


def build_setting_rows(described: dict | None) -> list[Heading | Setting]:
    """Lay `get_config` out as a list, in the order config.py declares it.

    Three sections, and the third is the point of showing it at all: a panel
    that listed only what it could change would leave the reader wondering
    where the rest of the file went, and guessing that the missing ones were
    the dangerous ones. They are here, with their values and with the reason
    they are not editable.
    """
    if not described:
        return []

    values = described.get("values") or {}
    live = described.get("live") or {}
    restart = described.get("restart_required") or {}
    # Two different refusals -- one would corrupt data, the other applies to
    # something that is not there -- but from a reader's side they are one
    # thing: a row with a reason on it instead of a way in.
    refused = {**(described.get("blocked") or {}), **(described.get("not_applicable") or {})}
    secrets = described.get("secrets") or []

    def value_of(name: str) -> str:
        # `values` carries every non-secret field; the per-group dicts are the
        # fallback for an assistant too old to send it.
        return show_value(values.get(name, live.get(name, restart.get(name))))

    rows: list[Heading | Setting] = []

    if live:
        rows.append(Heading("live"))
        rows += [Setting(name, value_of(name), APPLIES_NOW, True) for name in live]
    if restart:
        rows.append(Heading("restart required"))
        rows += [Setting(name, value_of(name), NEEDS_RESTART, True) for name in restart]

    read_only = [Setting(name, value_of(name), reason, False) for name, reason in refused.items()]
    read_only += [Setting(name, "********", NEVER_SHOWN, False) for name in secrets]
    if read_only:
        rows.append(Heading("cannot be changed here"))
        rows += read_only

    return rows


def describe_change(name: str, result: dict) -> tuple[str, str]:
    """What to tell the user about one `set_config`, and how loudly.

    Reported from what the assistant says it did rather than from what was
    asked of it. The three answers are genuinely different -- a value can be
    refused, or saved but not applied, or applied -- and a panel that said
    "done" to all three would be lying twice.
    """
    rejected = (result.get("rejected") or {}).get(name)
    if rejected:
        return f"{name} refused: {rejected}", "error"
    if name in (result.get("restart_required") or []):
        # Pointing at R rather than leaving "a restart" as the reader's
        # problem: the key that does it is on this screen.
        return f"{name} saved; press R to restart and apply it", "warning"
    saved = " and saved to .env" if result.get("persisted") else ""
    return f"{name} applied{saved}", "information"


class SettingList(Widget, can_focus=True):
    """Every setting in config.py, with the changeable ones changeable.

    Built the way FactList is -- its own window, its own cursor, its own
    footer -- and for the same reasons: it knows where the cursor is, so it
    keeps it on screen without living in a scroller, and owning the arrows
    outright is what stops them reaching the viewer behind it.

    Section headings are rows like any other, so the window arithmetic stays
    one list, and the cursor steps over them: there is nothing to do to a
    heading. The bottom three lines are the panel's answer to a column too
    narrow for a long value -- whatever the cursor is on is written out there
    in full, wrapped, with what changing it would do.
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("up,k", "move(-1)", "up", show=False),
        Binding("down,j", "move(1)", "down", show=False),
        Binding("enter", "edit", "edit", show=False),
    ]

    HINT = "  ↑↓ move · enter edit · esc back"
    EDIT_HINT = "  enter apply · esc cancel"
    # Two lines for the detail and one for the hint. Fixed, so that the number
    # of rows the window holds does not change as the cursor moves.
    FOOTER_LINES = 3
    # Long enough for the longest field name in config.py, on a column wide
    # enough to give it that much and still have room for a value.
    NAME_WIDTH = 26

    class Edit(Message):
        """The user wants to change this setting."""

        def __init__(self, name: str, value: str) -> None:
            self.name = name
            self.value = value
            super().__init__()

    class Refused(Message):
        """The user tried to change something that cannot be changed."""

        def __init__(self, name: str, reason: str) -> None:
            self.name = name
            self.reason = reason
            super().__init__()

    def __init__(self) -> None:
        super().__init__(id="setting-list")
        self.rows: list[Heading | Setting] = []
        self.cursor = 0
        self.top = 0
        # The name being edited, or None. Held here rather than in the panel
        # because it is what the hint line at the bottom has to know.
        self.editing: str | None = None

    # ---- Contents ----

    def set_config(self, described: dict | None) -> None:
        """Take a fresh `get_config` without losing the reader's place in it.

        By name, not by index: the panel is repopulated after every change, and
        a list that renumbered itself under the cursor would move the cursor to
        a different setting each time one was applied.
        """
        held = self.current()
        self.rows = build_setting_rows(described)
        self.cursor = self.index_of(held.name) if held is not None else self.first()
        self.refresh()

    def index_of(self, name: str) -> int:
        for index, row in enumerate(self.rows):
            if isinstance(row, Setting) and row.name == name:
                return index
        return self.first()

    def first(self) -> int:
        for index, row in enumerate(self.rows):
            if isinstance(row, Setting):
                return index
        return 0

    def current(self) -> Setting | None:
        if 0 <= self.cursor < len(self.rows):
            row = self.rows[self.cursor]
            if isinstance(row, Setting):
                return row
        return None

    # ---- Keys ----

    def action_move(self, step: int) -> None:
        index = self.cursor + step
        while 0 <= index < len(self.rows) and isinstance(self.rows[index], Heading):
            index += step
        if 0 <= index < len(self.rows):
            self.cursor = index
        self.refresh()

    def action_edit(self) -> None:
        row = self.current()
        if row is None:
            return
        if not row.editable:
            self.post_message(self.Refused(row.name, row.note))
            return
        self.editing = row.name
        self.refresh()
        self.post_message(self.Edit(row.name, row.value))

    def done_editing(self) -> None:
        self.editing = None
        self.refresh()

    # ---- Drawing ----

    def render(self) -> Text:
        height = max(1, self.size.height - self.FOOTER_LINES)
        # The window is nudged rather than recomputed, so that moving the
        # cursor one row does not jump the whole list: it scrolls only when the
        # cursor has actually left the rows on screen.
        self.top = max(0, min(self.top, len(self.rows) - height, self.cursor))
        if self.cursor >= self.top + height:
            self.top = self.cursor - height + 1

        shown = range(self.top, min(self.top + height, len(self.rows)))
        lines = [self.line(index) for index in shown]
        if not lines:
            lines.append(crop(Text("  no settings", style="bright_black"), self.size.width))
        lines.extend([Text()] * (height - len(lines)))

        lines.extend(self.detail())
        lines.append(crop(Text(self.hint(), style="bright_black"), self.size.width))
        return Text("\n").join(lines)

    def line(self, index: int) -> Text:
        row = self.rows[index]
        if isinstance(row, Heading):
            return crop(Text(f"  -- {row.text} --", style="bold"), self.size.width)

        width = min(self.NAME_WIDTH, max(8, self.size.width // 2))
        name = row.name if len(row.name) <= width else f"{row.name[: width - 1]}…"
        line = Text(f"  {name:<{width}} {row.value}", style=self.row_style(index, row))
        return crop(line, self.size.width, pad=True)

    def row_style(self, index: int, row: Setting) -> str:
        if index == self.cursor:
            return "black on yellow"
        return "" if row.editable else "bright_black"

    def detail(self) -> list[Text]:
        """The row under the cursor, written out in full over two lines.

        Which is what makes a truncated row recoverable: the column is half a
        terminal wide and a model name is most of that on its own.
        """
        row = self.current()
        text = f"{row.name} = {row.value}   ({row.note})" if row is not None else ""
        wrapped = textwrap.wrap(text, max(self.size.width - 2, 1))[:2]
        wrapped += [""] * (2 - len(wrapped))
        return [crop(Text(f" {line}", style="bright_black"), self.size.width) for line in wrapped]

    def hint(self) -> str:
        return self.EDIT_HINT if self.editing is not None else self.HINT


class ChoiceList(Widget, can_focus=True):
    """The menu of what one setting can be set to.

    Textual has a Select, and it is the wrong one here for the reason
    ascii_bar exists: it draws its marker as ▼ and its frame out of the
    half-block set, and the console font this dashboard is built for carries
    neither. This is the same shape drawn the same way the other two lists are
    -- its own window, its own cursor, its own footer -- so it costs no glyph
    the terminal does not have.

    It opens on the value that is already set rather than at the top, which is
    what makes it a menu of where you could go rather than a list you have to
    find yourself in first.
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("up,k", "move(-1)", "up", show=False),
        Binding("down,j", "move(1)", "down", show=False),
        Binding("enter", "choose", "choose", show=False),
    ]

    HINT = "  ↑↓ move · enter choose · esc cancel"
    # The heading at the top and the hint at the bottom.
    FOOTER_LINES = 2

    class Chosen(Message):
        """The user has picked one of the options."""

        def __init__(self, value: str) -> None:
            self.value = value
            super().__init__()

    def __init__(self) -> None:
        super().__init__(id="setting-choices")
        # Not `self.name`: Widget already has one, and shadowing it breaks the
        # DOM's own bookkeeping.
        self.setting = ""
        self.current = ""
        self.options: list[str] = []
        self.cursor = 0
        self.top = 0

    def open(self, setting: str, current: str, options: tuple[str, ...]) -> None:
        self.setting = setting
        self.current = current
        # Whatever is set is always in the menu, even when it is not one of
        # ours -- .env may hold any of OpenRouter's four hundred models, and a
        # menu that could not show you where you already are would be
        # misreporting the assistant rather than offering to change it.
        self.options = list(options)
        if current and current not in self.options:
            self.options.insert(0, current)
        self.options.append(OTHER)

        self.cursor = self.options.index(current) if current in self.options else 0
        self.top = 0
        self.refresh()

    # ---- Keys ----

    def action_move(self, step: int) -> None:
        self.cursor = max(0, min(self.cursor + step, len(self.options) - 1))
        self.refresh()

    def action_choose(self) -> None:
        if self.options:
            self.post_message(self.Chosen(self.options[self.cursor]))

    # ---- Drawing ----

    def render(self) -> Text:
        height = max(1, self.size.height - self.FOOTER_LINES)
        self.top = max(0, min(self.top, len(self.options) - height, self.cursor))
        if self.cursor >= self.top + height:
            self.top = self.cursor - height + 1

        shown = range(self.top, min(self.top + height, len(self.options)))
        heading = f"  -- {self.setting} --" if self.setting else "  --"
        lines = [crop(Text(heading, style="bold"), self.size.width)]
        lines += [self.line(index) for index in shown]
        # One row of the widget is the heading, so the padding counts against
        # the options rather than against everything drawn so far.
        lines.extend([Text()] * (height - len(shown)))
        lines.append(crop(Text(self.HINT, style="bright_black"), self.size.width))
        return Text("\n").join(lines)

    def line(self, index: int) -> Text:
        option = self.options[index]
        # A star on what is set, so the menu says where you are as well as
        # where you could go. The cursor is the bar; the two are different
        # questions and used to be answered by the same mark.
        mark = "*" if option == self.current else " "
        style = "black on yellow" if index == self.cursor else ""
        if option == OTHER and index != self.cursor:
            style = "bright_black"
        return crop(Text(f"  {mark} {option}", style=style), self.size.width, pad=True)


class ManagementPanel(Panel):
    """Changing MINUS from the dashboard.

    The socket has carried `get_config`/`set_config` since the control server
    was written (assembly.build_control_handlers); this is the half that was
    missing. Every field of config.py is listed, the ones that can be changed
    are changed here, and the ones that cannot say why rather than being left
    out and wondered about.

    Takeover, unlike the memory panel: there are three dozen settings and the
    summary above them would cost four rows of the only screen they have.
    """

    title = "management"
    hotkey = "g"
    takeover = True

    class Change(Message):
        """A setting has been given a new value, to send to the assistant."""

        def __init__(self, name: str, value: str) -> None:
            self.name = name
            self.value = value
            super().__init__()

    def __init__(self) -> None:
        super().__init__("panel-management")

    def compose_expanded(self) -> ComposeResult:
        yield SettingList()
        # Neither of these is laid out until a setting is being changed: the
        # menu for the six fields that have one, the editor for everything
        # else and for the menu's own way out.
        yield ChoiceList()
        yield Input(id="setting-editor")

    def interactive(self) -> Widget | None:
        return self.query_one(SettingList)

    def redraw_expanded(self) -> None:
        self.query_one(SettingList).set_config(self.data)

    def summary(self) -> str:
        if not self.data:
            return "  no data"
        values = self.data.get("values") or {}
        live = self.data.get("live") or {}
        restart = self.data.get("restart_required") or {}
        return "\n".join(
            [
                f"  chat  {values.get('chat_model', '?')}",
                f"  deep  {values.get('deep_model', '?')}",
                f"  voice {values.get('tts_voice', '?')} at {values.get('tts_speed', '?')}x",
                f"  {len(live)} live · {len(restart)} need a restart",
            ]
        )

    # ---- Editing ----

    def editing(self) -> bool:
        """True while either way in is open. Both are left by the same escape."""
        return self.has_class("-editing") or self.has_class("-choosing")

    def on_setting_list_edit(self, event: SettingList.Edit) -> None:
        """Enter on a setting: a menu where there is one, typing where there is not."""
        event.stop()
        options = choices_for(event.name)
        if options:
            self.open_menu(event.name, event.value, options)
        else:
            self.open_editor(event.value)

    def open_menu(self, setting: str, current: str, options: tuple[str, ...]) -> None:
        menu = self.query_one(ChoiceList)
        menu.open(setting, current, options)
        self.remove_class("-editing")
        self.add_class("-choosing")
        menu.focus()

    def open_editor(self, value: str) -> None:
        """Prefilled rather than blank.

        Most changes to a model name or a voice are an edit to what is set, and
        a blank box would mean retyping it from a row that may itself have been
        truncated.
        """
        editor = self.query_one("#setting-editor", Input)
        editor.value = value
        self.remove_class("-choosing")
        self.add_class("-editing")
        editor.focus()

    def on_choice_list_chosen(self, event: ChoiceList.Chosen) -> None:
        event.stop()
        if event.value == OTHER:
            # The menu is a shortcut, not a fence: OpenRouter carries four
            # hundred models and this list names five of them.
            self.open_editor(self.query_one(ChoiceList).current)
            return

        name = self.query_one(SettingList).editing
        self.close_editor()
        if name is not None:
            self.post_message(self.Change(name, event.value))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in the editor.

        Stopped here, and that is not tidiness: the app's own handler takes any
        Input.Submitted it is given and says it out loud, so an unstopped one
        would send the new model name to MINUS as a spoken line.
        """
        event.stop()
        name = self.query_one(SettingList).editing
        self.close_editor()
        if name is not None:
            self.post_message(self.Change(name, event.value))

    def close_editor(self) -> None:
        """Put the editor away, and put the keys back on the list.

        The focus move is the same hazard `collapse()` documents: focus can sit
        on a widget CSS has stopped laying out, where it goes on swallowing
        every key aimed at something else.
        """
        if not self.editing():
            return
        self.remove_class("-editing", "-choosing")
        settings = self.query_one(SettingList)
        settings.done_editing()
        settings.focus()

    def collapse(self) -> None:
        # Whatever put the panel away -- escape, another panel's hotkey -- an
        # editor left open would be hidden with a half-typed value still in it,
        # and would come back holding it the next time the panel was opened.
        self.close_editor()
        super().collapse()
