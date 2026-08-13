"""The dashboard: a btop-style view of a running MINUS.

Left half: a viewer over the conversation, the run log, or the deep tier's
write-ups, switched with the arrow keys, with a text input beneath it and a
console below that, hidden until it is asked for. Right half: management
panels, each of which expands to fill the column when its letter is pressed.

Reads come from disk and writes go over the socket, so the whole screen except
the input box still works with the assistant stopped -- which is exactly when
a log is most worth reading.
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import Any, ClassVar

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.widget import Widget
from textual.widgets import Input, RichLog, Static

from minus.control.client import ControlClient, NotRunning
from minus.dashboard import service
from minus.dashboard.tail import (
    ConversationReader,
    DeepNoteReader,
    LogLineStyler,
    LogTailer,
    latest_conversation,
    latest_log,
)
from minus.dashboard.widgets import (
    AgentsPanel,
    ConsolePane,
    FactList,
    HardwarePanel,
    ManagementPanel,
    MemoryPanel,
    Panel,
    ProgramsPanel,
    PromptPane,
    ToolsPanel,
    ViewerPane,
    deep_elapsed,
    render_turn,
)
from minus.logging_config import CONSOLE_PREFIX
from minus.paths import conversations_dir, deep_notes_dir, logs_dir
from minus.system.metrics import SystemMetrics

logger = logging.getLogger(__name__)

# How often to look for an assistant that was not there a moment ago. A fixed
# interval rather than a backoff: connecting to a unix socket that is absent
# fails immediately and costs nothing, so there is no pressure to back off.
RECONNECT_SECONDS = 3.0


class MinusDashboard(App):
    """The whole screen."""

    CSS_PATH = "theme.tcss"
    TITLE = "MINUS"

    # Textual focuses the first focusable widget on mount by default. Opening
    # with focus nowhere is the whole point: it is what makes m, h, t and the
    # rest keys rather than typed letters.
    AUTO_FOCUS = None

    # `show=False` on the noisier ones: the hint bar is written by hand, since
    # Textual's own footer would spend the width on bindings nobody needs to
    # be told about.
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        # The bare arrows are left to whatever has focus, so that a wide log
        # line can be scrolled sideways. Switching view takes the modifier.
        # Priority so they still reach the app while the Input has focus and
        # is using the bare arrows to move its cursor.
        Binding("ctrl+left", "cycle_view(-1)", "prev view", priority=True, show=False),
        Binding("ctrl+right", "cycle_view(1)", "next view", priority=True, show=False),
        # The deep view's own scroller owns the bare arrows, so paging between
        # notes takes the modifier -- same trade, and for the same reason, as
        # the view keys above.
        Binding("ctrl+up", "cycle_note(-1)", "prev note", priority=True, show=False),
        Binding("ctrl+down", "cycle_note(1)", "next note", priority=True, show=False),
        # With focus nowhere -- how the dashboard opens, and where escape puts
        # you back -- there is no view to leave the bare arrows to, so the app
        # scrolls the showing one on their behalf.
        Binding("left", "scroll_view('left')", "scroll left", show=False),
        Binding("right", "scroll_view('right')", "scroll right", show=False),
        Binding("up", "scroll_view('up')", "scroll up", show=False),
        Binding("down", "scroll_view('down')", "scroll down", show=False),
        Binding("1", "show_view(0)", "conversation", show=False),
        Binding("2", "show_view(1)", "log", show=False),
        Binding("3", "show_view(2)", "deep", show=False),
        Binding("m", "expand('panel-memory')", "memory", show=False),
        Binding("h", "expand('panel-hardware')", "hardware", show=False),
        Binding("t", "expand('panel-tools')", "tools", show=False),
        Binding("p", "expand('panel-programs')", "programs", show=False),
        Binding("a", "expand('panel-agents')", "agents", show=False),
        Binding("g", "expand('panel-management')", "management", show=False),
        Binding("v", "focus_viewer", "viewer", show=False),
        Binding("V", "toggle_fullscreen", "viewer fullscreen", show=False),
        # Deliberately not priority: Input binds enter to submit at its own
        # level, and must keep it.
        Binding("enter", "expand_focused", "expand", show=False),
        Binding("i", "focus_input", "input", show=False),
        Binding("escape", "leave", "back", priority=True, show=False),
        Binding("c", "toggle_console", "console", show=False),
        Binding("s", "interrupt", "stop", show=False),
        Binding("e", "end_conversation", "end conversation", show=False),
        Binding("R", "restart", "restart minus", show=False),
        Binding("q", "quit", "quit", show=False),
    ]

    def __init__(self, socket_path: Path, unicode_borders: bool = False) -> None:
        super().__init__()
        self.socket_path = socket_path
        self.unicode_borders = unicode_borders

        self.log_tailer = LogTailer()
        self.styler = LogLineStyler()
        self.console_tailer = LogTailer()
        self.conversation_reader = ConversationReader()
        self.note_reader = DeepNoteReader(deep_notes_dir())
        self.note_index = 0
        self.metrics = SystemMetrics()
        # What escape falls back to when it leaves the input box. See
        # on_descendant_focus.
        self._prior_focus: Widget | None = None

        self.client: ControlClient | None = None
        self.connected = False
        self.snapshot: dict = {}
        self.service_status: dict = {"managed": False, "reason": "not checked"}

    # ---- Layout ----

    def compose(self) -> ComposeResult:
        yield Static(id="statusbar")
        with Horizontal(id="body"):
            with Vertical(id="left"):
                yield ViewerPane()
                yield PromptPane()
                yield ConsolePane()
            with Vertical(id="right"):
                yield MemoryPanel()
                yield HardwarePanel()
                yield ToolsPanel()
                yield ProgramsPanel()
                yield AgentsPanel()
                yield ManagementPanel()
        yield Static(id="hints")

    def on_mount(self) -> None:
        if self.unicode_borders:
            for pane in ("#viewer", "#prompt", "#console"):
                self.query_one(pane).styles.border = ("solid", "ansi_bright_black")

        self.query_one("#hints", Static).update(
            "ctrl+←/→ view · arrows scroll · v/V viewer · m h t p a g panels "
            "· enter expand · i input "
            "· esc back · s stop · e end conversation · c console · R restart minus "
            "· q quit"
        )

        self.attach_files()
        self.connect()

        # Inline timers for the cheap incremental reads: a capped read out of
        # the page cache is microseconds, and handing that to a worker thread
        # would cost more in message passing than it saves.
        self.set_interval(0.25, self.poll_log)
        self.set_interval(0.25, self.poll_console)
        self.set_interval(0.5, self.poll_conversation)
        self.set_interval(2.0, self.poll_notes)
        self.set_interval(2.0, self.poll_metrics)
        self.set_interval(1.0, self.tick)
        self.set_interval(RECONNECT_SECONDS, self.reconnect)

        # Holding nothing, so that the letter keys are keys. Opening in the
        # input box meant every one of them was typed instead of pressed.
        self.set_focus(None)

    # ---- Files ----

    def attach_files(self) -> None:
        """Point the readers at whatever is newest, and backfill the viewer."""
        self.log_tailer.reset(latest_log(logs_dir()))
        self.console_tailer.reset(latest_log(logs_dir(), prefix=CONSOLE_PREFIX))
        self.conversation_reader.reset(latest_conversation(conversations_dir()))

        view = self.query_one("#view-log", RichLog)
        for line in self.log_tailer.backfill():
            view.write(Text(line, style=self.styler.style(line)))

        console = self.query_one(ConsolePane)
        for line in self.console_tailer.backfill():
            console.write(line)
        # A console file is usually opened mid-line, with a spinner running.
        console.show_live(self.console_tailer.current)

        self.poll_conversation()
        self.poll_notes()

    def poll_log(self) -> None:
        # Suppressed for the same reason _fill's is, and it is the interval
        # timers that need it most: they keep firing while the screen is being
        # torn down, at which point the view they write to is already gone.
        with contextlib.suppress(NoMatches):
            # A restart starts a new file; the newest name is the one to follow.
            newest = latest_log(logs_dir())
            if newest is not None and newest != self.log_tailer.path:
                self.log_tailer.reset(newest)
                self.query_one("#view-log", RichLog).write(Text(f"--- {newest.name} ---"))

            view = self.query_one("#view-log", RichLog)
            for line in self.log_tailer.poll():
                view.write(Text(line, style=self.styler.style(line)))

    def poll_console(self) -> None:
        """Follow what MINUS writes to its own stdout and stderr.

        Polled whether or not the pane is showing, so that pressing `c` after
        something has already gone wrong shows the thing that went wrong.
        """
        with contextlib.suppress(NoMatches):
            console = self.query_one(ConsolePane)

            newest = latest_log(logs_dir(), prefix=CONSOLE_PREFIX)
            if newest is not None and newest != self.console_tailer.path:
                self.console_tailer.reset(newest)
                console.write(f"--- {newest.name} ---")

            for line in self.console_tailer.poll():
                console.write(line)

            # Whatever is still being overwritten goes on its own row rather
            # than into the log, so a spinner spins instead of scrolling.
            console.show_live(self.console_tailer.current)

    def poll_conversation(self) -> None:
        newest = latest_conversation(conversations_dir())
        if newest is not None and newest != self.conversation_reader.path:
            self.conversation_reader.reset(newest)

        turns = self.conversation_reader.poll()
        if turns is None:
            return

        with contextlib.suppress(NoMatches):
            view = self.query_one("#view-conversation", RichLog)
            view.clear()
            for turn in turns:
                view.write(render_turn(turn.role, turn.text))

    def poll_notes(self) -> None:
        if not self.note_reader.poll():
            return
        self.note_index = 0
        self.show_note()

    def show_note(self) -> None:
        notes = self.note_reader.notes
        with contextlib.suppress(NoMatches):
            body = self.query_one("#deep-body", Static)
            if not notes:
                body.update("No deep-think answers yet.")
                return

            self.note_index = max(0, min(self.note_index, len(notes) - 1))
            note = notes[self.note_index]
            body.update(
                f"{note.title}\n{note.created_at}"
                f"   ({self.note_index + 1}/{len(notes)}, ctrl+↑/↓ to move)\n\n{note.detail}"
            )
            # Back to the top, or a short note opens scrolled to wherever the
            # last long one was left.
            self.query_one("#view-deep", VerticalScroll).scroll_home(animate=False)

    def _fill(self, panel_type: type, data: Any) -> None:
        """Put data into a panel, from the UI thread.

        Always called through call_from_thread, and always doing its own query
        here rather than in the worker that produced the data: a worker can
        outlive the widget tree it was started against, and resolving the node
        over there raises NoMatches as the app tears down.
        """
        with contextlib.suppress(NoMatches):
            self.query_one(panel_type).update(data)

    def notice(self, line: str, severity: str = "information") -> None:
        """Tell the user something they asked for the result of.

        Twice over, on purpose: a toast so it is seen at all, and a console
        line so it is still there a minute later when they wonder what it
        said. The suppress is the same hazard as _fill's -- a worker can
        outlive the widget tree that it is reporting to.
        """
        with contextlib.suppress(NoMatches):
            self.query_one(ConsolePane).write(f"dash: {line}")
        self.notify(line, severity=severity)

    @work(thread=True, exclusive=True, group="metrics")
    def poll_metrics(self) -> None:
        # nvidia-smi initialises NVML and takes a few hundred milliseconds, so
        # this one genuinely does belong off the event loop.
        sample = self.metrics.sample()
        self.call_from_thread(self._fill, HardwarePanel, sample)

    # ---- The socket ----

    @work(thread=True, exclusive=True, group="connect")
    def connect(self) -> None:
        try:
            client = ControlClient(
                self.socket_path,
                on_event=self._on_event,
                on_close=self._on_client_closed,
            ).connect()
            client.subscribe()
        except NotRunning as exc:
            self.call_from_thread(self._disconnected, str(exc))
            return

        self.client = client
        self.call_from_thread(self._connected)

    def reconnect(self) -> None:
        """Pick the assistant back up when it comes back.

        The dashboard is opened at least as often while MINUS is down as while
        it is up -- to read the log that says why -- and `systemctl restart`
        drops the connection by design. Without this, both leave a dashboard
        that is permanently wrong until it is restarted itself.
        """
        if not self.connected:
            self.connect()

    def _connected(self) -> None:
        self.connected = True
        self.refresh_panels()
        self.check_service()

    def _on_client_closed(self, reason: str) -> None:
        """MINUS went away without being asked to.

        Called on the client's reader thread, which outlives the widget tree
        during teardown -- call_from_thread raises once the app has stopped,
        and there is nothing left to tell by then anyway.
        """
        with contextlib.suppress(RuntimeError):
            self.call_from_thread(self._disconnected, reason)

    def _disconnected(self, reason: str) -> None:
        self.connected = False
        self.snapshot = {}
        # Dropped rather than kept: the socket behind it is gone, and holding
        # it left `send` reaching into a dead connection until the next
        # successful connect replaced it.
        self.client = None
        self.redraw_status()
        logger.info("Not connected: %s", reason)
        self.check_service()

    def _on_event(self, frame: dict) -> None:
        """Called on the client's reader thread."""
        if frame.get("event") == "status":
            self.call_from_thread(self._apply_snapshot, frame.get("data") or {})

    def _apply_snapshot(self, snapshot: dict) -> None:
        previous = (self.snapshot.get("conversation") or {}).get("id")
        self.snapshot = snapshot
        self._fill(AgentsPanel, snapshot)
        self.redraw_status()

        current = (snapshot.get("conversation") or {}).get("id")
        if previous and current and current != previous:
            # Whatever ended it -- `e`, or a silence long enough for MINUS to
            # roll over on its own while nobody was watching. The counts in the
            # memory panel have moved either way.
            self.notice(f"conversation ended; now recording to {current}")
            self.refresh_panels()

    @work(thread=True, exclusive=True, group="panels")
    def refresh_panels(self) -> None:
        if self.client is None:
            return
        try:
            tools = self.client.request("list_tools")
            # All of them, up to the handler's own ceiling: the panel counts
            # what it is given, so asking for six made it report six, and the
            # expanded list has every one of them to arrow through.
            facts = self.client.request("list_facts", limit=500)
            notes = self.client.request("list_deep_notes", limit=200)
            conversations = self.client.request("list_conversations", limit=500)
            snapshot = self.client.request("get_status")
        except (NotRunning, OSError) as exc:
            self.call_from_thread(self._disconnected, str(exc))
            return

        self.call_from_thread(self._fill, ToolsPanel, tools)
        self.call_from_thread(
            self._fill,
            MemoryPanel,
            {"facts": facts, "deep_notes": len(notes), "conversations": len(conversations)},
        )
        self.call_from_thread(self._apply_snapshot, snapshot)

    @work(thread=True, exclusive=True, group="service")
    def check_service(self) -> None:
        status = service.status()
        self.call_from_thread(self._set_service, status)

    def _set_service(self, status: dict) -> None:
        self.service_status = status
        self.redraw_status()

    # ---- Status bar ----

    def tick(self) -> None:
        """Once a second, redraw what moves without anything arriving.

        Only the deep tier's clock so far. Its panel is otherwise redrawn when
        a snapshot lands, and snapshots land on state edges -- so between the
        two edges of one escalation nothing redraws it at all, and a timer that
        counts is the one thing that has to.
        """
        self.redraw_status()
        if (self.snapshot.get("deep") or {}).get("in_flight"):
            with contextlib.suppress(NoMatches):
                self.query_one(AgentsPanel).redraw()

    def redraw_status(self) -> None:
        try:
            bar = self.query_one("#statusbar", Static)
        except NoMatches:
            # Reached from a worker's completion callback, which can land
            # after the screen has gone.
            return

        if not self.connected:
            bar.add_class("disconnected")
            reason = self.service_status.get("reason", "")
            bar.update(f" MINUS  ● DISCONNECTED   {reason}")
            return

        bar.remove_class("disconnected")
        phase = self.snapshot.get("phase", "?")
        deep = self.snapshot.get("deep") or {}
        conversation = self.snapshot.get("conversation") or {}

        parts = [f" MINUS  ● {phase}"]
        if conversation.get("id"):
            parts.append(f"conversation {conversation['id']}")
        elapsed = deep_elapsed(deep)
        if elapsed is not None:
            parts.append(f"deep {elapsed:.0f}s")
        if self.service_status.get("managed"):
            parts.append(f"unit {self.service_status.get('active')}")
        bar.update("   ".join(parts))

    # ---- Actions ----

    def action_cycle_view(self, step: int) -> None:
        self.query_one(ViewerPane).cycle(step)

    def action_show_view(self, index: int) -> None:
        self.query_one(ViewerPane).show(index)

    def action_scroll_view(self, direction: str) -> None:
        """A bare arrow with focus nowhere: scroll what the viewer is showing.

        Guarded on focus rather than left to binding precedence. A focused
        widget that does not bind an arrow itself -- the input box has no use
        for up and down, and a panel has no use for any of them -- would
        otherwise let the key fall through to here and scroll a pane nobody
        was pointing at.
        """
        if self.focused is not None:
            return
        self.query_one(ViewerPane).scroll_current(direction)

    def _collapse_panels(self) -> None:
        for panel in self.query(Panel):
            panel.collapse()

    def action_expand(self, panel_id: str) -> None:
        """Give one panel the whole column, or give it back.

        Focus follows, which is what stops the left half from still looking
        focused once a panel has been opened over here -- and the panel decides
        where it lands, because a panel with something interactive in its
        expanded view wants the keys going there rather than to the frame.
        """
        target = self.query_one(f"#{panel_id}", Panel)
        expanding = not target.has_class("-expanded")
        self._collapse_panels()
        if not expanding:
            target.focus()
            return

        for panel in self.query(Panel):
            if panel is not target:
                panel.add_class("-hidden")
        target.expand()

    def action_focus_input(self) -> None:
        """The one move that may leave a panel enlarged behind it."""
        self.query_one("#prompt-input", Input).focus()

    def action_focus_viewer(self) -> None:
        """Take the viewer, or give it back.

        Toggling because every panel hotkey toggles: pressing the key for what
        you are already looking at should put it down again.
        """
        self._collapse_panels()
        viewer = self.query_one(ViewerPane)
        if viewer.viewing():
            self.set_focus(None)
            return
        viewer.focus_current()

    def action_toggle_fullscreen(self) -> None:
        """Give the viewer the screen, or give the screen back."""
        self.query_one("#body").toggle_class("-viewer-full")
        self.query_one(ViewerPane).focus_current()

    def action_toggle_console(self) -> None:
        self._collapse_panels()
        console = self.query_one(ConsolePane)
        if console.toggle_class("-shown"):
            self.query_one("#console-body", RichLog).focus()
        elif self.focused is not None and self.focused.id == "console-body":
            # Hiding what has focus would leave the letter keys going nowhere.
            self.set_focus(None)

    def action_expand_focused(self) -> None:
        """Enter: give whatever is focused the room, in whichever half it lives.

        Walks up from the focused widget rather than testing it directly: in
        the viewer the focused thing is the RichLog or the scroller inside the
        pane, and in a panel it may one day be a list inside it.
        """
        node: Widget | None = self.focused
        while node is not None:
            if isinstance(node, Panel):
                self.action_expand(node.id)
                return
            if isinstance(node, ViewerPane):
                self.action_toggle_fullscreen()
                return
            node = node.parent if isinstance(node.parent, Widget) else None

    def action_leave(self) -> None:
        """Escape: undo whatever last took over the screen, then let go.

        Ordered so that one key walks back out the way you came in. It never
        moves focus *to* anything except the panel the input box was entered
        from, which is what makes leaving a panel two presses rather than a
        round trip.

        The input box is stepped out of *before* the panel behind it is
        collapsed. The other order looks equivalent and is not: it threw away
        the panel you had opened while you were still typing into the box in
        front of it.
        """
        body = self.query_one("#body")
        if body.has_class("-viewer-full"):
            body.remove_class("-viewer-full")
            return

        if isinstance(self.focused, Input):
            prior = self._prior_focus
            if prior is not None and prior.focusable:
                prior.focus()
            else:
                self.set_focus(None)
            return

        if self.query(".panel.-expanded"):
            self._collapse_panels()
            return

        console = self.query_one(ConsolePane)
        if console.has_class("-shown"):
            self.action_toggle_console()
            return

        self._prior_focus = None
        self.set_focus(None)

    def on_descendant_focus(self, event) -> None:
        """Remember what to put focus back on when escape leaves the input.

        Panels only -- the panel itself, or whatever inside one had the keys,
        so that stepping out of the input box and back returns you to your
        place in an expanded panel's list rather than to the frame around it.
        Remembering any widget at all sounds more general and is worse: the
        last thing focused before the input box is usually a view inside the
        viewer, and escape would then hand the viewer back the focus it is not
        supposed to take without being asked for by name.
        """
        widget = event.widget
        if isinstance(widget, Panel) or any(isinstance(node, Panel) for node in widget.ancestors):
            self._prior_focus = widget

    def action_help_quit(self) -> None:
        """Textual binds ctrl+C to this to say how you really quit.

        Overridden because its own version reports the first `quit` binding it
        finds, which is Textual's ctrl+Q -- a key this app does not advertise
        and does not want to. The real one is q.
        """
        self.notify("Press q to quit", title="Do you want to quit?")

    def action_interrupt(self) -> None:
        self.send("interrupt")

    def action_end_conversation(self) -> None:
        """Wrap the current conversation up now and open a fresh one.

        Announced on the way out, unlike `interrupt`: stopping a reply is
        audible the instant it happens, while this looks like nothing at all
        from the outside. MINUS answers the command before it has done the
        work -- condensing is two model calls -- so the conversation it ends up
        in is reported later, by _apply_snapshot, from the status event.
        """
        if not self.connected:
            self.notice("not connected -- MINUS is not running", severity="warning")
            return
        self.notice("ending the conversation; condensing and extracting facts…")
        self.send("end_conversation")

    def action_restart(self) -> None:
        self.restart_service()

    @work(thread=True, exclusive=True, group="restart")
    def restart_service(self) -> None:
        ok, message = service.restart()
        self.call_from_thread(self.notice, message)
        if ok:
            self.call_from_thread(self.connect)

    def action_cycle_note(self, step: int) -> None:
        """Page between deep-think notes.

        The bare arrows used to do this from an App.on_key handler, which only
        ever worked while focus was nowhere: give #view-deep focus and its own
        scroll bindings consume them first. They are the right owner -- a note
        longer than the pane has to be scrollable -- so paging moved to ctrl.
        """
        if self.query_one(ViewerPane).current != "deep":
            return
        self.note_index += step
        self.show_note()

    # ---- Input ----

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return

        if not self.connected:
            self.notice("not connected -- MINUS is not running", severity="warning")
            return

        # Not echoed anywhere: the conversation view above shows the turn as
        # soon as MINUS writes it to the transcript.
        self.send("say", text=text)

    def on_fact_list_delete(self, event: FactList.Delete) -> None:
        """The memory panel has asked for facts to be forgotten.

        Over the socket rather than into the database: the store is open on the
        assistant's side, and two writers to one sqlite file is a race the
        dashboard has no business starting.
        """
        if not self.connected:
            self.notice("not connected -- MINUS is not running", severity="warning")
            return
        self.forget_facts(event.ids)

    @work(thread=True, group="forget")
    def forget_facts(self, ids: list[str]) -> None:
        if self.client is None:
            return
        try:
            result = self.client.request("delete_facts", ids=ids)
        except Exception as exc:
            self.call_from_thread(self.notice, f"failed to forget: {exc}", "error")
            self.call_from_thread(self._disconnected, str(exc))
            return

        self.call_from_thread(self.notice, f"forgot {result.get('deleted', len(ids))} fact(s)")
        # Redrawn from the store rather than from the assumption that it did
        # what it was asked, which is also what puts the count in the summary
        # right.
        self.call_from_thread(self.refresh_panels)

    @work(thread=True, group="send")
    def send(self, command: str, **params: Any) -> None:
        if self.client is None:
            return
        try:
            self.client.request(command, **params)
        except Exception as exc:
            self.call_from_thread(self.notice, f"failed: {exc}", "error")
            self.call_from_thread(self._disconnected, str(exc))

    def on_unmount(self) -> None:
        if self.client is not None:
            self.client.close()


def run_dashboard(socket_path: Path, unicode_borders: bool = False) -> None:
    MinusDashboard(socket_path, unicode_borders=unicode_borders).run()
