"""The dashboard: a btop-style view of a running MINUS.

Left half: a viewer over the conversation, the run log, or the deep tier's
write-ups, switched with the arrow keys, with a text input beneath it. Right
half: management panels, each of which expands to fill the column when its
letter is pressed.

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
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
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
    HardwarePanel,
    MemoryPanel,
    Panel,
    ProgramsPanel,
    PromptPane,
    ToolsPanel,
    ViewerPane,
)
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

    # `show=False` on the noisier ones: the hint bar is written by hand, since
    # Textual's own footer would spend the width on bindings nobody needs to
    # be told about.
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("left", "cycle_view(-1)", "prev view", show=False),
        Binding("right", "cycle_view(1)", "next view", show=False),
        # Priority so they still reach the app while the Input has focus and
        # is using the bare arrows to move its cursor.
        Binding("ctrl+left", "cycle_view(-1)", "prev view", priority=True, show=False),
        Binding("ctrl+right", "cycle_view(1)", "next view", priority=True, show=False),
        Binding("1", "show_view(0)", "conversation", show=False),
        Binding("2", "show_view(1)", "log", show=False),
        Binding("3", "show_view(2)", "deep", show=False),
        Binding("m", "expand('panel-memory')", "memory", show=False),
        Binding("h", "expand('panel-hardware')", "hardware", show=False),
        Binding("t", "expand('panel-tools')", "tools", show=False),
        Binding("p", "expand('panel-programs')", "programs", show=False),
        Binding("a", "expand('panel-agents')", "agents", show=False),
        Binding("i", "focus_input", "input", show=False),
        Binding("escape", "leave", "back", priority=True, show=False),
        Binding("c", "interrupt", "interrupt", show=False),
        Binding("R", "restart", "restart minus", show=False),
        Binding("q", "quit", "quit", show=False),
    ]

    def __init__(self, socket_path: Path, unicode_borders: bool = False) -> None:
        super().__init__()
        self.socket_path = socket_path
        self.unicode_borders = unicode_borders

        self.log_tailer = LogTailer()
        self.styler = LogLineStyler()
        self.conversation_reader = ConversationReader()
        self.note_reader = DeepNoteReader(deep_notes_dir())
        self.note_index = 0
        self.metrics = SystemMetrics()

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
            with Vertical(id="right"):
                yield MemoryPanel()
                yield HardwarePanel()
                yield ToolsPanel()
                yield ProgramsPanel()
                yield AgentsPanel()
        yield Static(id="hints")

    def on_mount(self) -> None:
        if self.unicode_borders:
            self.query_one("#viewer").styles.border = ("solid", "ansi_bright_black")

        self.query_one("#hints", Static).update(
            "←/→ view · m h t p a panels · i input · esc back · c interrupt "
            "· R restart minus · q quits the dashboard, not MINUS"
        )

        self.attach_files()
        self.connect()

        # Inline timers for the cheap incremental reads: a capped read out of
        # the page cache is microseconds, and handing that to a worker thread
        # would cost more in message passing than it saves.
        self.set_interval(0.25, self.poll_log)
        self.set_interval(0.5, self.poll_conversation)
        self.set_interval(2.0, self.poll_notes)
        self.set_interval(2.0, self.poll_metrics)
        self.set_interval(1.0, self.redraw_status)
        self.set_interval(RECONNECT_SECONDS, self.reconnect)

        self.query_one("#prompt-input", Input).focus()

    # ---- Files ----

    def attach_files(self) -> None:
        """Point the readers at whatever is newest, and backfill the viewer."""
        self.log_tailer.reset(latest_log(logs_dir()))
        self.conversation_reader.reset(latest_conversation(conversations_dir()))

        view = self.query_one("#view-log", RichLog)
        for line in self.log_tailer.backfill():
            view.write(Text(line, style=self.styler.style(line)))

        self.poll_conversation()
        self.poll_notes()

    def poll_log(self) -> None:
        # A restart starts a new file; the newest name is the one to follow.
        newest = latest_log(logs_dir())
        if newest is not None and newest != self.log_tailer.path:
            self.log_tailer.reset(newest)
            self.query_one("#view-log", RichLog).write(Text(f"--- {newest.name} ---"))

        view = self.query_one("#view-log", RichLog)
        for line in self.log_tailer.poll():
            view.write(Text(line, style=self.styler.style(line)))

    def poll_conversation(self) -> None:
        newest = latest_conversation(conversations_dir())
        if newest is not None and newest != self.conversation_reader.path:
            self.conversation_reader.reset(newest)

        turns = self.conversation_reader.poll()
        if turns is None:
            return

        view = self.query_one("#view-conversation", RichLog)
        view.clear()
        styles = {"user": "bold", "assistant": "", "tool": "bright_black"}
        prefixes = {"user": "you  ", "assistant": "minus", "tool": "  ·  "}
        for turn in turns:
            view.write(Text(f"{prefixes[turn.role]} {turn.text}", style=styles.get(turn.role, "")))

    def poll_notes(self) -> None:
        if not self.note_reader.poll():
            return
        self.note_index = 0
        self.show_note()

    def show_note(self) -> None:
        notes = self.note_reader.notes
        body = self.query_one("#deep-body", Static)
        if not notes:
            body.update("No deep-think answers yet.")
            return

        self.note_index = max(0, min(self.note_index, len(notes) - 1))
        note = notes[self.note_index]
        body.update(
            f"{note.title}\n{note.created_at}"
            f"   ({self.note_index + 1}/{len(notes)}, ↑/↓ to move)\n\n{note.detail}"
        )

    def _fill(self, panel_type: type, data: Any) -> None:
        """Put data into a panel, from the UI thread.

        Always called through call_from_thread, and always doing its own query
        here rather than in the worker that produced the data: a worker can
        outlive the widget tree it was started against, and resolving the node
        over there raises NoMatches as the app tears down.
        """
        with contextlib.suppress(NoMatches):
            self.query_one(panel_type).update(data)

    def _echo(self, line: str) -> None:
        with contextlib.suppress(NoMatches):
            self.query_one(PromptPane).echo(line)

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
            client = ControlClient(self.socket_path, on_event=self._on_event).connect()
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

    def _disconnected(self, reason: str) -> None:
        self.connected = False
        self.snapshot = {}
        self.redraw_status()
        logger.info("Not connected: %s", reason)
        self.check_service()

    def _on_event(self, frame: dict) -> None:
        """Called on the client's reader thread."""
        if frame.get("event") == "status":
            self.call_from_thread(self._apply_snapshot, frame.get("data") or {})

    def _apply_snapshot(self, snapshot: dict) -> None:
        self.snapshot = snapshot
        self._fill(AgentsPanel, snapshot)
        self.redraw_status()

    @work(thread=True, exclusive=True, group="panels")
    def refresh_panels(self) -> None:
        if self.client is None:
            return
        try:
            tools = self.client.request("list_tools")
            facts = self.client.request("list_facts", limit=6)
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
        if deep.get("in_flight"):
            parts.append(f"deep {deep.get('elapsed_seconds', 0):.0f}s")
        if self.service_status.get("managed"):
            parts.append(f"unit {self.service_status.get('active')}")
        bar.update("   ".join(parts))

    # ---- Actions ----

    def action_cycle_view(self, step: int) -> None:
        self.query_one(ViewerPane).cycle(step)

    def action_show_view(self, index: int) -> None:
        self.query_one(ViewerPane).show(index)

    def action_expand(self, panel_id: str) -> None:
        """Give one panel the whole column, or give it back."""
        target = self.query_one(f"#{panel_id}", Panel)
        expanding = not target.has_class("-expanded")
        for panel in self.query(Panel):
            panel.remove_class("-expanded", "-hidden")
            if expanding and panel is not target:
                panel.add_class("-hidden")
        if expanding:
            target.add_class("-expanded")

    def action_focus_input(self) -> None:
        self.query_one("#prompt-input", Input).focus()

    def action_leave(self) -> None:
        """Escape: collapse a panel, or step out of the input box."""
        expanded = self.query(".panel.-expanded")
        if expanded:
            for panel in self.query(Panel):
                panel.remove_class("-expanded", "-hidden")
            return
        self.query_one(ViewerPane).focus_current()

    def action_interrupt(self) -> None:
        self.send("interrupt")

    def action_restart(self) -> None:
        self.restart_service()

    @work(thread=True, exclusive=True, group="restart")
    def restart_service(self) -> None:
        ok, message = service.restart()
        self.call_from_thread(self._echo, message)
        if ok:
            self.call_from_thread(self.connect)

    def on_key(self, event) -> None:
        """Up and down move between deep-think notes when that view is open."""
        if self.query_one(ViewerPane).current != "deep":
            return
        if isinstance(self.focused, Input):
            return
        if event.key in ("up", "down"):
            self.note_index += -1 if event.key == "up" else 1
            self.show_note()
            event.stop()

    # ---- Input ----

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return

        if not self.connected:
            self.query_one(PromptPane).echo("not connected -- MINUS is not running")
            return

        self.query_one(PromptPane).echo(f"you  {text}")
        self.send("say", text=text)

    @work(thread=True, group="send")
    def send(self, command: str, **params: Any) -> None:
        if self.client is None:
            return
        try:
            self.client.request(command, **params)
        except Exception as exc:
            self.call_from_thread(self._echo, f"failed: {exc}")
            self.call_from_thread(self._disconnected, str(exc))

    def on_unmount(self) -> None:
        if self.client is not None:
            self.client.close()


def run_dashboard(socket_path: Path, unicode_borders: bool = False) -> None:
    MinusDashboard(socket_path, unicode_borders=unicode_borders).run()
