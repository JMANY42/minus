"""A small interactive harness: modes on the left, conversation in the middle.

This is not the dashboard. The dashboard watches a MINUS that is already
running and is allowed to work with it stopped; the harness *is* the agent, so
everything it shows comes from objects it built. That is why the model calls run
on Textual workers and the observers marshal back with `call_from_thread`: the
UI thread owns the widgets, and a tool loop that painted them directly would
corrupt the screen the moment two events arrived close together.

The five modes are deliberately not five screens. They share one transcript, one
activity rail and one input, because a session moves between them -- a torrent
is downloaded, triaged, then discussed -- and losing the transcript on every
switch would make that impossible. The mode only decides what a submitted line
means and what the rails are showing.
"""

from __future__ import annotations

import contextlib
import logging
import sys
import threading
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any, ClassVar

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.widgets import Button, DirectoryTree, Input, OptionList, RichLog, Static

from minus.config import Settings
from minus.harness.agent import (
    DeepResultWatcher,
    format_value,
    payload_summary,
    summarize_arguments,
    tool_line,
)
from minus.harness.approvals import ThreadApprover
from minus.harness.explorer import ExplorerPane

logger = logging.getLogger(__name__)

MODES: tuple[str, ...] = ("chat", "code", "download", "vpn", "sys")

HELP = """commands:
  /help                     this list
  /mode <mode>              chat | code | download | vpn | sys
  /model [name]             show or switch the conversational model
  /clear                    clear this scrollback (mode transcripts stay)
  /quit                     leave
  /vpn status|connect|disconnect
  /download <query|magnet|url>
                            find the best torrent, download, scan, release clean
  /triage <hash>            scan a staged torrent
  /promote <hash> [name]    release a clean torrent into the library
  /doctor                   security posture, in short
  /tools                    what the current tier may call
  /thoughts                 open or close the reasoning and tool trace
  /files                    open or close the file explorer (ctrl+b)
  /diffs                    show the working-tree diff in the explorer"""

KEY_HINTS = (
    "enter send   /help commands   tab modes   ctrl+b files   "
    "ctrl+t thoughts   esc clear focus   ctrl+c quit"
)

# The label each role is drawn under. Rich styles rather than Textual CSS,
# because these are text runs inside a RichLog, not widgets.
ROLE_STYLES: dict[str, tuple[str, str]] = {
    "you": ("you", "bold #7dd3fc"),
    "minus": ("minus", "bold #86efac"),
    "tool": ("tool", "#7b8494"),
    "sys": ("sys", "#c4b5fd"),
}

VALUE = "#d7dce4"
DIM = "#6b7280"
ALERT = "#fb7185"
OK = "#86efac"


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def _is_tty() -> bool:
    """Whether both halves of a terminal are present.

    Kept as a function, not an inline expression, so a test can replace the
    answer without pretending pytest's captured stdin is a terminal.
    """
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


class MinusHarness(App[None]):
    """The whole harness."""

    CSS_PATH = "theme.tcss"
    TITLE = "MINUS"
    AUTO_FOCUS = "#prompt"

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        # Escape has to work while the Input owns the keyboard, which is where
        # focus starts. Input does not bind it, so a normal binding reaches the
        # app; priority would also fire while an approval card is up, where the
        # card's own escape means "deny".
        Binding("escape", "clear_focus", "clear focus", show=False),
        Binding("tab", "focus_modes", "modes", show=False),
        # The Input binds ctrl+b/ctrl+t for line editing; these are app-level
        # views and must win while the prompt has focus.
        Binding("ctrl+b", "toggle_explorer", "files", show=False, priority=True),
        Binding("ctrl+t", "toggle_thoughts", "thoughts", show=False, priority=True),
        # Textual's own ctrl+c opens the quit help; the hint bar promises quit.
        Binding("ctrl+c", "quit", "quit", show=False, priority=True),
    ]

    def __init__(
        self,
        settings: Settings,
        *,
        workspace: Path | None = None,
        conversation: Any | None = None,
    ) -> None:
        """`conversation` is the assembled Assistant, injectable for tests.

        It is the result of `assembly.build_conversation`, not a bare
        Conversation: the deep tier's queue lives beside it. Building it here is
        lazy -- a harness opened on `sys` has no reason to open a memory
        database -- but it is built at most once.
        """
        super().__init__()
        self.settings = settings
        self.workspace = Path(workspace) if workspace is not None else Path.cwd()
        self.assistant = conversation
        self.mode = "chat"

        self.conversation_lines: list[tuple[str, str]] = []
        self.activity_lines: list[str] = []
        self.alert_lines: list[str] = []
        self.thought_lines: list[tuple[str, str]] = []

        # The thinking bar is deliberately plain state: the loop emits events
        # faster than a widget should be rebuilt, and the spinner timer only
        # needs to know whether something is in flight.
        self._thinking_label = "ready"
        self._thinking_active = False
        self._thinking_frame = 0
        self._thinking_timer: Any | None = None
        self.thoughts_open = False
        self.explorer_visible = False
        self.explorer_preview_path: Path | None = None
        self._explorer: ExplorerPane | None = None
        self._explorer_mounting = False

        self._ui_thread: int | None = None
        self._chat_lock = threading.Lock()
        self._deep_watcher: DeepResultWatcher | None = None
        self._pipeline: Any | None = None
        self._coding: Any | None = None
        self._seen_alerts: set[str] = set()

        self.vpn_interface = ""
        self.vpn_link_up = False
        self.vpn_carrying = False
        self.vpn_healthy: bool | None = None
        self.vpn_detail = ""

    # ---- Layout ----

    def compose(self) -> ComposeResult:
        yield Static(id="topbar")
        with Horizontal(id="body"):
            with Vertical(id="left"):
                yield Static("MODES", classes="rail-title")
                yield OptionList(*MODES, id="mode-list")
            with Vertical(id="dock-left", classes="dock dock-h"):
                pass
            with Vertical(id="center"):
                yield RichLog(id="conversation", wrap=True, markup=False)
                # The bar is the collapsed form; clicking it reveals the
                # reasoning/tool trace above the input instead of pushing
                # the conversation into a separate screen.
                yield Static("ready", id="thinking-bar")
                yield RichLog(id="thinking-trace", wrap=True, markup=False)
            with Vertical(id="right"):
                yield Static("ACTIVITY", classes="rail-title")
                yield RichLog(id="activity", wrap=True, markup=False)
                yield Static("ALERTS", classes="rail-title")
                yield RichLog(id="alerts", wrap=True, markup=False)
        with Vertical(id="bottom"):
            yield Input(placeholder="message · /help", id="prompt")
            yield Static(KEY_HINTS, id="hints")

    def on_mount(self) -> None:
        self._ui_thread = threading.get_ident()
        self._refresh_vpn_state()
        self.refresh_topbar()
        self._sync_rail()
        self._append("sys", "ready · /help for commands")
        self._trace("trace · thinking, reads and commands", style=DIM)
        self._render_thinking_bar()
        self._thinking_timer = self.set_interval(0.12, self._tick_thinking)
        if self.assistant is not None:
            self._bind_chat()
        self._autoconnect_vpn()

    def on_unmount(self) -> None:
        if self._deep_watcher is not None:
            self._deep_watcher.stop()
        if self._thinking_timer is not None:
            self._thinking_timer.stop()

    # ---- Focus and mode selection ----

    def action_clear_focus(self) -> None:
        self.set_focus(None)

    def action_focus_modes(self) -> None:
        rail = self.query_one("#mode-list", OptionList)
        if rail.has_focus:
            self.query_one("#prompt", Input).focus()
        else:
            rail.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        # Options are added as bare strings, so the prompt is the mode name.
        self.select_mode(str(event.option.prompt))

    def _sync_rail(self) -> None:
        with contextlib.suppress(NoMatches, ValueError):
            self.query_one("#mode-list", OptionList).highlighted = MODES.index(self.mode)

    def select_mode(self, mode: str) -> None:
        """Change what the input means, and refresh the rails that depend on it."""
        wanted = mode.strip().lower()
        if not wanted:
            return
        if wanted not in MODES:
            self._append("sys", f"unknown mode {wanted!r} · {', '.join(MODES)}")
            return

        changed = wanted != self.mode
        self.mode = wanted
        self._sync_rail()
        self.refresh_topbar()
        if changed:
            self._append("sys", f"mode: {wanted}")
        self._enter_mode(wanted)

    def _enter_mode(self, mode: str) -> None:
        if mode == "code":
            # Built on the first turn rather than here. Constructing the client
            # is cheap, but failing it (no API key yet) is not worth a screen
            # full of traceback when the user only looked at the mode list.
            self._append("sys", f"workspace: {self.workspace} · /help")
        elif mode == "download":
            self._ensure_pipeline()
        elif mode == "vpn":
            self._render_vpn()
        elif mode == "sys":
            self._render_sys()

    # ---- Header and rail state ----

    def refresh_topbar(self) -> None:
        if self.vpn_healthy is None:
            vpn_label, vpn_style = "vpn ?", DIM
        elif self.vpn_healthy:
            vpn_label, vpn_style = f"vpn up ({self.vpn_interface})", OK
        elif self.vpn_interface:
            vpn_label, vpn_style = f"vpn down ({self.vpn_interface})", ALERT
        else:
            vpn_label, vpn_style = "vpn off", DIM

        qbit_on = bool(self.settings.qbittorrent_url)
        qbit_label = "qbit on" if qbit_on else "qbit off"

        line = Text()
        line.append("MINUS", style="bold #f8fafc")
        line.append("   ")
        line.append(self.mode, style="bold #93c5fd")
        line.append("   model ", style=DIM)
        line.append(self.settings.chat_model, style=VALUE)
        line.append("   ")
        line.append(vpn_label, style=vpn_style)
        line.append("   ")
        line.append(qbit_label, style=OK if qbit_on else DIM)
        line.append("   ")
        line.append(str(self.workspace), style=DIM)
        with contextlib.suppress(NoMatches):
            self.query_one("#topbar", Static).update(line)

    def _refresh_vpn_state(self) -> None:
        """One read-only probe, with every failure turned into a rail sentence."""
        self.vpn_interface = str(self.settings.vpn_interface or "")
        try:
            from minus.services import vpn

            tunnel_status = getattr(vpn, "tunnel_status", None)
            if callable(tunnel_status):
                snapshot = tunnel_status(self.settings)
            else:
                snapshot = vpn.status(self.settings.vpn_interface)
        except Exception as exc:
            self.vpn_link_up = False
            self.vpn_carrying = False
            self.vpn_healthy = False
            self.vpn_detail = f"probe failed: {exc}"
            return

        # The orchestrator's `tunnel_status` returns TunnelStatus, but accepting
        # a mapping too costs nothing and keeps the header working against a
        # provider that reports one.
        def field(name: str, default: Any) -> Any:
            if isinstance(snapshot, dict):
                return snapshot.get(name, default)
            return getattr(snapshot, name, default)

        self.vpn_interface = str(field("interface", "") or self.vpn_interface)
        self.vpn_link_up = bool(field("link_up", False))
        self.vpn_carrying = bool(field("carrying_traffic", False))
        self.vpn_healthy = bool(field("healthy", self.vpn_link_up and self.vpn_carrying))
        self.vpn_detail = str(field("detail", ""))

    def _render_vpn(self) -> None:
        self._refresh_vpn_state()
        self.refresh_topbar()
        state = "up" if self.vpn_link_up else "down"
        carrying = "carrying traffic" if self.vpn_carrying else "not carrying traffic"
        interface = self.vpn_interface or "no interface"
        self._append("sys", f"vpn: {interface} {state} · {carrying} · {self.vpn_detail}")
        self._activity(f"vpn: {interface} {state}")

        provider = (self.settings.vpn_provider or "").lower()
        if provider == "airvpn" and not (
            self.settings.airvpn_username and self.settings.airvpn_password
        ):
            missing = "AirVPN credentials missing (MINUS_AIRVPN_USERNAME/PASSWORD)"
            self._activity(f"vpn: {missing}")
            self._append("sys", f"vpn: {missing}")

    # ---- Writing to the screen ----

    def _render_line(self, role: str, body: str) -> Text:
        label, style = ROLE_STYLES.get(role, (role, VALUE))
        line = Text()
        line.append(f"{label:<7}", style=style)
        line.append(body, style=DIM if role == "tool" else VALUE)
        return line

    def _append(self, role: str, body: str) -> None:
        """Add one transcript line, and remember it for callers and tests.

        The list is the same content the RichLog drew. Keeping it is what lets
        `/clear` and a test read a conversation without scraping Strip objects.
        """
        body = str(body)
        self.conversation_lines.append((role, body))
        with contextlib.suppress(NoMatches):
            self.query_one("#conversation", RichLog).write(self._render_line(role, body))

    def _activity(self, body: str, style: str = DIM) -> None:
        body = _clip(body, 120)
        self.activity_lines.append(body)
        with contextlib.suppress(NoMatches):
            self.query_one("#activity", RichLog).write(Text(body, style=style))

    def _show_alert(self, body: str) -> None:
        text = str(body).strip()
        label = text if text.upper().startswith("ALERT") else f"ALERT: {text}"
        if label in self._seen_alerts:
            return
        self._seen_alerts.add(label)
        self.alert_lines.append(label)
        with contextlib.suppress(NoMatches):
            self.query_one("#alerts", RichLog).write(Text(label, style=f"bold {ALERT}"))
        # The rail is easy to miss; the transcript is where the user is looking.
        self._append("sys", label)

    # ---- Thinking bar and trace ----

    def on_click(self, event: events.Click) -> None:
        widget = event.widget
        if widget is not None and getattr(widget, "id", "") == "thinking-bar":
            self.toggle_thoughts()

    def action_toggle_thoughts(self) -> None:
        self.toggle_thoughts()

    def toggle_thoughts(self) -> None:
        """Open or close the reasoning/tool trace above the input."""
        try:
            panel = self.query_one("#thinking-trace", RichLog)
        except NoMatches:
            return
        self.thoughts_open = not self.thoughts_open
        if self.thoughts_open:
            # A hidden RichLog does not retain writes reliably; the list is the
            # source of truth, and opening replays it with its styles.
            panel.clear()
            for text, style in self.thought_lines[-250:]:
                panel.write(Text(text, style=style))
            panel.add_class("-open")
            panel.scroll_end(animate=False)
        else:
            panel.remove_class("-open")
        self._render_thinking_bar()

    def _tick_thinking(self) -> None:
        if not self._thinking_active:
            return
        self._thinking_frame += 1
        self._render_thinking_bar()

    def _render_thinking_bar(self) -> None:
        spinner = "|/-\\"[self._thinking_frame % 4] if self._thinking_active else "·"
        hint = "click to collapse" if self.thoughts_open else "click for trace"
        style = "#7dd3fc" if self._thinking_active else DIM
        try:
            self.query_one("#thinking-bar", Static).update(
                Text(f"{spinner} {self._thinking_label}  ·  {hint}", style=style)
            )
        except NoMatches:
            return

    def _set_status(self, label: str, active: bool = True) -> None:
        """Set the one-line state; called on the UI thread."""
        self._thinking_label = _clip(label, 88)
        self._thinking_active = active
        if not active:
            self._thinking_frame = 0
        self._render_thinking_bar()

    def _trace(self, line: str, style: str = DIM) -> None:
        """Append one line to the trace, whether or not the panel is open."""
        text = _clip(str(line), 400)
        self.thought_lines.append((text, style))
        self.thought_lines = self.thought_lines[-250:]
        if not self.thoughts_open:
            return
        try:
            self.query_one("#thinking-trace", RichLog).write(Text(text, style=style))
        except NoMatches:
            return

    # ---- File explorer ----

    def action_toggle_explorer(self) -> None:
        """Show or hide the left dock; a second `ctrl+b` is the close.

        Synchronous on purpose. An async toggle leaves a window between the
        key press and the completed mount in which `explorer_visible` is still
        false, so a quick second press would try to open again instead of
        closing. The dock is also never emptied on hide -- only its active
        class is removed -- so reopening is a class change, not a remount.
        """
        if self.explorer_visible:
            self._hide_explorer()
        else:
            self._show_explorer()

    def _show_explorer(self) -> None:
        if self._explorer is None:
            # The DirectoryTree scans on mount, so it is built only when the
            # explorer is first opened; a harness that never opens files pays
            # nothing for the tree.
            self._explorer = ExplorerPane(self.workspace)
        explorer = self._explorer
        target = self.query_one("#dock-left", Vertical)
        if explorer.parent is None and not self._explorer_mounting:
            # Not awaited: hiding only removes a CSS class, and the mount is
            # scheduled before the next paint. `_explorer_mounting` stops a
            # rapid open/close/open from scheduling the same widget twice.
            target.mount(explorer)
            self._explorer_mounting = True
        target.add_class("-active")
        self.explorer_visible = True
        self._set_status("files · left", False)

    def _hide_explorer(self) -> None:
        self.query_one("#dock-left", Vertical).remove_class("-active")
        self.explorer_visible = False
        self._set_status("files hidden", False)
        # Focus can be inside the tree or preview; leaving it there would send
        # the next keystroke to a hidden widget.
        if self._focus_is_in_explorer():
            self.query_one("#prompt", Input).focus()

    def _focus_is_in_explorer(self) -> bool:
        focus: Any = self.focused
        while focus is not None:
            if focus is self._explorer:
                return True
            focus = getattr(focus, "parent", None)
        return False

    def preview_file(self, path: Path) -> None:
        path = Path(path)
        if not path.is_file():
            self._append("sys", f"file: {path} is not a regular file")
            return
        if self._explorer is None or not self.explorer_visible:
            self._append("sys", "open the file explorer with ctrl+b first")
            return
        explorer = self._explorer
        explorer.show_file(path)
        self.explorer_preview_path = path
        try:
            relative = path.relative_to(self.workspace)
        except ValueError:
            relative = path
        self._set_status(f"reading · {_clip(str(relative), 70)}", False)
        self._trace(f"file · {relative}", style=VALUE)

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        event.stop()
        self.preview_file(Path(event.path))

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "diffs-btn" and self._explorer is not None:
            self._explorer.show_diff(self.explorer_preview_path)
            self._set_status("diffs · working tree", False)

    @staticmethod
    def _tool_verb(name: str) -> str:
        if name in ("read_file", "read_workspace_file", "list_files", "list_workspace_files"):
            return "reading"
        if name in ("grep", "glob", "qbittorrent_find", "list_torrents"):
            return "searching"
        if name == "run_shell":
            return "running"
        if name in ("write_file", "apply_patch"):
            return "writing"
        return "working"

    def _ui(self, callback: Callable[[], None]) -> None:
        """Run a UI mutation on Textual's thread, from here or from a worker."""
        try:
            if self._ui_thread is not None and threading.get_ident() != self._ui_thread:
                self.call_from_thread(callback)
            else:
                callback()
        except Exception:
            # A rendering failure must never escape into a worker: the whole
            # reason observers are optional is that UI bugs are not agent bugs.
            logger.debug("Harness UI callback failed", exc_info=True)

    # ---- Tool and pipeline events ----

    def observer(self, event: str, payload: dict[str, Any]) -> None:
        """`ToolLoop` traffic, from whichever thread ran the tool."""
        try:
            data = dict(payload or {})
        except (TypeError, ValueError):
            data = {}
        self._ui(partial(self._render_event, event, data))

    def _render_event(self, event: str, payload: dict[str, Any]) -> None:
        if event == "thinking":
            round_number = payload.get("round")
            label = f"thinking · round {round_number}" if round_number else "thinking"
            if payload.get("forced"):
                label = "thinking · final answer"
            self._set_status(label, True)
            self._trace(label)
            return

        if event == "thought":
            text = str(payload.get("text") or "").strip()
            if text:
                self._trace("thought", style="#c4b5fd")
                for line in text.splitlines()[:12]:
                    self._trace(f"  {_clip(line, 240)}", style="#9aa4b2")
            self._set_status("reasoning", True)
            return

        if event == "answer":
            self._set_status("ready", False)
            text = str(payload.get("text") or "").strip()
            if text:
                self._trace(f"answer · {_clip(text, 160)}", style=OK)
            return

        name = str(payload.get("name") or "tool")
        if event == "tool_start":
            card = tool_line(name, payload.get("arguments"))
            verb = self._tool_verb(name)
            summary = summarize_arguments(payload.get("arguments"))
            self._append("tool", card)
            self._activity(f"tool {card}")
            self._set_status(f"{verb} · {name} {summary}".strip(), True)
            self._trace(f"{verb} · {card}", style=VALUE)
        elif event == "tool_end":
            self._append("tool", f"{name} ok")
            self._activity(f"ok {name}", style=OK)
            self._set_status("thinking", True)
            self._trace(f"ok · {name}", style=OK)
        elif event == "tool_error":
            error = _clip(str(payload.get("error") or "failed"), 80)
            self._append("tool", f"{name} error · {error}")
            self._activity(f"error {name} · {error}", style=ALERT)
            self._set_status("recovering", True)
            self._trace(f"error · {name} · {error}", style=ALERT)

    def notify_event(self, kind: str, payload: dict[str, Any]) -> None:
        """The triage pipeline's notification hook; usable from any thread.

        An alert gets the red rail block and a transcript line because a
        malicious verdict is the one event that must not be missed. Everything
        else is one activity line: stage progress is useful, but not worth
        pushing the conversation out of the way.
        """
        try:
            data = dict(payload or {})
        except (TypeError, ValueError):
            data = {"detail": str(payload)}

        verdict = data.get("verdict")
        malicious = (
            kind.lower() in ("alert", "malicious")
            or bool(data.get("alert"))
            or (isinstance(verdict, dict) and verdict.get("label") == "malicious")
        )
        if malicious:
            body = str(data.get("alert") or "").strip()
            if not body:
                subject = data.get("torrent_hash") or data.get("name") or "payload"
                body = f"malicious {subject}"
            self._ui(partial(self._show_alert, body))
            self._ui(partial(self._set_status, "alert · malicious payload", False))
            self._ui(partial(self._trace, body, style=ALERT))
            return

        summary = payload_summary(data)
        line = f"{kind}: {summary}" if kind else summary
        self._ui(partial(self._activity, line))
        self._ui(partial(self._set_status, line, kind not in ("verdict", "promoted")))
        self._ui(partial(self._trace, line))

    # ---- Chat mode ----

    def _bind_chat(self) -> None:
        if self.assistant is None:
            return
        conversation = getattr(self.assistant, "conversation", None)
        if conversation is not None:
            try:
                conversation.observer = self.observer
            except Exception:  # pragma: no cover - a fake may refuse attributes
                logger.debug("Conversation would not take an observer", exc_info=True)
        if self._deep_watcher is None:
            results = getattr(self.assistant, "results", None)
            if results is not None:
                self._deep_watcher = DeepResultWatcher(results, self._publish_deep)
                self._deep_watcher.start()

    def _ensure_chat(self) -> Any | None:
        with self._chat_lock:
            if self.assistant is not None:
                self._bind_chat()
                return getattr(self.assistant, "conversation", None)
            try:
                from minus.assembly import build_conversation

                self.assistant = build_conversation(self.settings)
            except Exception as exc:
                logger.exception("Could not build the conversation")
                self._ui(partial(self._append, "sys", f"chat unavailable: {exc}"))
                self._ui(partial(self._set_status, "unavailable", False))
                return None
            self._bind_chat()
            return getattr(self.assistant, "conversation", None)

    def _chat_submit(self, text: str) -> None:
        self._append("you", text)
        # Building the conversation loads the embedding model on first use.
        # That belongs in the worker: the spinner must keep moving while it
        # happens, and a 40-second first turn must not freeze the screen.
        self._set_status("thinking", True)
        self._trace(f"you · {_clip(text, 160)}", style=VALUE)

        def work() -> None:
            conversation = self._ensure_chat()
            if conversation is None:
                return
            try:
                answer = conversation.reply(text)
            except Exception as exc:
                self._ui(partial(self._append, "sys", f"chat failed: {exc}"))
                self._ui(partial(self._set_status, f"failed · {_clip(str(exc), 60)}", False))
                return
            self._ui(lambda: self._append("minus", answer or "(no reply)"))
            self._ui(partial(self._set_status, "ready", False))

        self._spawn(work, "chat")

    def _publish_deep(self, result: Any) -> None:
        if isinstance(result, dict):
            spoken = str(result.get("spoken") or "")
            detail = str(result.get("detail") or "")
            question = str(result.get("question") or "")
        else:
            spoken = str(getattr(result, "spoken", "") or "")
            detail = str(getattr(result, "detail", "") or "")
            question = str(getattr(result, "question", "") or "")
        if not spoken and not detail:
            return

        def render() -> None:
            if spoken:
                self._append("minus", spoken)
            head = _clip(question, 48) if question else "deep answer"
            self._activity(
                f"deep: {head} · {len(detail)} chars of detail" if detail else f"deep: {head}"
            )
            self._trace(f"deep · {head}", style=OK)
            self._set_status("ready", False)

        self._ui(render)

    # ---- Code mode ----

    def _ensure_coding(self) -> Any | None:
        if self._coding is not None:
            return self._coding
        try:
            from minus.coding.session import CodingSession

            self._coding = CodingSession(
                self.settings,
                self.workspace,
                approver=ThreadApprover(self),
                echo=lambda _line: None,
                observer=self.observer,
            )
        except Exception as exc:
            logger.exception("Could not build the coding session")
            self._append("sys", f"code unavailable: {exc}")
            return None
        return self._coding

    def _code_submit(self, text: str) -> None:
        coding = self._ensure_coding()
        if coding is None:
            return
        self._append("you", text)
        self._set_status("thinking · code", True)
        self._trace(f"you · {_clip(text, 160)}", style=VALUE)

        def work() -> None:
            try:
                answer = coding.run_turn(text)
            except Exception as exc:
                self._ui(partial(self._append, "sys", f"code failed: {exc}"))
                self._ui(partial(self._set_status, f"failed · {_clip(str(exc), 60)}", False))
                return
            self._ui(lambda: self._append("minus", answer or "(no reply)"))
            self._ui(partial(self._set_status, "ready", False))

        self._spawn(work, "code")

    # ---- Download mode ----

    def _ensure_pipeline(self) -> Any | None:
        if self._pipeline is not None:
            return self._pipeline
        try:
            from minus.assembly import build_triage_pipeline

            pipeline = build_triage_pipeline(self.settings)
        except (ImportError, AttributeError):
            self._append("sys", "download unavailable: this build has no triage pipeline")
            return None
        except Exception as exc:
            logger.exception("Could not build the triage pipeline")
            self._append("sys", f"download unavailable: {exc}")
            return None

        if pipeline is None:
            message = "qBittorrent is not configured (set MINUS_QBITTORRENT_URL)"
            self._activity(f"download: {message}")
            self._append("sys", f"download: {message}")
            return None

        try:
            pipeline.notify = self.notify_event
        except Exception:  # pragma: no cover - a pipeline that refuses the hook
            logger.debug("Triage pipeline would not take a notify hook", exc_info=True)
        self._pipeline = pipeline
        return pipeline

    def _download(self, target: str) -> None:
        """A magnet/URL is fetched directly; anything else is searched first."""
        target = " ".join(target.split())
        if not target:
            self._append("sys", "download: needs a query, magnet link or .torrent URL")
            return
        if target.startswith(("magnet:?", "http://", "https://")):
            self._ingest_url(target)
        else:
            self._download_query(target)

    def _ingest_url(self, target: str) -> None:
        pipeline = self._ensure_pipeline()
        if pipeline is None:
            return
        ingest = getattr(pipeline, "ingest", None)
        if not callable(ingest):
            self._append(
                "sys",
                "download unavailable: the pipeline has no URL/magnet ingest in this build",
            )
            return
        self._append("sys", f"download: {_clip(target, 100)}")
        self._set_status("working · download", True)
        self._trace(f"download · {_clip(target, 120)}", style=VALUE)

        def work() -> None:
            try:
                # Starting the downloader belongs in the worker: `provision`
                # waits for the WebUI, and that wait must not freeze the UI.
                if self.settings.qbittorrent_url:
                    from minus.scripts.qbt import ensure_running

                    ensure_running(self.settings)
                result = ingest(target, detonate=True)
            except Exception as exc:
                self._ui(partial(self._append, "sys", f"download failed: {exc}"))
                self._ui(partial(self._set_status, "download failed", False))
                return
            self._ui(partial(self._render_stage_result, result))

        self._spawn(work, "download")

    def _download_query(self, query: str) -> None:
        """Search, rank, then run the winner through the quarantine pipeline."""
        if not self.settings.discovery_enabled:
            self._append("sys", "download: discovery is off (MINUS_DISCOVERY_ENABLED=false)")
            return
        pipeline = self._ensure_pipeline()
        if pipeline is None:
            return
        ingest = getattr(pipeline, "ingest", None)
        if not callable(ingest):
            self._append(
                "sys",
                "download unavailable: the pipeline has no URL/magnet ingest in this build",
            )
            return
        self._append("sys", f"search: {_clip(query, 100)}")
        self._set_status("working · searching", True)
        self._trace(f"search · {_clip(query, 120)}", style=VALUE)

        def work() -> None:
            try:
                from minus.triage.discovery import choose_best, discover

                found = discover(self.settings, query)
                for line in found.summary_lines(3):
                    self._ui(partial(self._append, "sys", line))
                best = choose_best(found, min_score=self.settings.discovery_min_score)
                if best is None:
                    self._ui(partial(self._set_status, "search · no pick", False))
                    self._ui(
                        partial(
                            self._append,
                            "sys",
                            "search: no candidate cleared the threshold; refine the query",
                        )
                    )
                    return
                item = best.candidate
                if not item.download_url:
                    raise ValueError("the selected candidate has no magnet or .torrent URL")
                self._ui(partial(self._append, "sys", f"pick: {best.describe()}"))
                self._ui(partial(self._trace, f"pick · {item.title}", style=OK))

                if self.settings.qbittorrent_url:
                    from minus.scripts.qbt import ensure_running

                    ensure_running(self.settings)
                result = ingest(item.download_url, detonate=True)
                status = str(result.get("status") or "")
                if status == "clean" and self.settings.discovery_auto_promote:
                    try:
                        promoted = pipeline.promote(result["torrent_hash"], name=item.title)
                    except Exception as exc:
                        self._ui(partial(self._append, "sys", f"promote failed: {exc}"))
                    else:
                        self._ui(
                            partial(
                                self._append,
                                "sys",
                                f"library: {promoted.get('moved_to')}",
                            )
                        )
                elif status == "clean":
                    self._ui(
                        partial(
                            self._append,
                            "sys",
                            "safety: clean; left in quarantine (auto-promote is off)",
                        )
                    )
                elif status in ("needs_review", "suspicious"):
                    self._ui(
                        partial(
                            self._append,
                            "sys",
                            f"safety: {status}; left in quarantine",
                        )
                    )
                    torrent_hash = str(result.get("torrent_hash") or "")
                    if status == "needs_review" and torrent_hash:
                        self._ui(
                            partial(
                                self._append,
                                "sys",
                                f"review: minus triage review {torrent_hash}",
                            )
                        )
                        self._ui(
                            partial(
                                self._append,
                                "sys",
                                f"release: minus triage promote {torrent_hash} "
                                "--accept-review --yes",
                            )
                        )
                elif status == "malicious":
                    self._ui(partial(self._append, "sys", "safety: malicious; payload deleted"))
                self._ui(partial(self._render_stage_result, result))
            except Exception as exc:
                self._ui(partial(self._append, "sys", f"download failed: {exc}"))
                self._ui(partial(self._set_status, "download failed", False))

        self._spawn(work, "download")

    def _scan(self, torrent_hash: str) -> None:
        pipeline = self._ensure_pipeline()
        if pipeline is None:
            return
        self._append("sys", f"triage: {torrent_hash}")
        self._set_status("working · scanning", True)
        self._trace(f"scan · {_clip(torrent_hash, 80)}", style=VALUE)

        def work() -> None:
            try:
                result = pipeline.scan(torrent_hash)
            except Exception as exc:
                self._ui(partial(self._append, "sys", f"triage failed: {exc}"))
                self._ui(partial(self._set_status, "scan failed", False))
                return
            self._ui(partial(self._render_stage_result, result))

        self._spawn(work, "triage")

    def _promote(self, torrent_hash: str, name: str = "") -> None:
        pipeline = self._ensure_pipeline()
        if pipeline is None:
            return
        self._append("sys", f"promote: {torrent_hash}")
        self._set_status("working · promote", True)
        self._trace(f"promote · {_clip(torrent_hash, 80)}", style=VALUE)

        def work() -> None:
            try:
                result = pipeline.promote(torrent_hash, name=name)
            except Exception as exc:
                self._ui(partial(self._append, "sys", f"promote failed: {exc}"))
                self._ui(partial(self._set_status, "promote failed", False))
                return
            self._ui(partial(self._render_stage_result, result))

        self._spawn(work, "promote")

    def _render_stage_result(self, result: Any) -> None:
        self._set_status("ready", False)
        if not isinstance(result, dict):
            self._append("sys", format_value(result, 120))
            return
        alert = result.get("alert")
        if alert:
            self._show_alert(str(alert))
        shown = 0
        for key, value in result.items():
            if key == "alert":
                continue
            if shown >= 12:
                self._append("sys", "…")
                break
            rendered = format_value(value, 72)
            self._append("sys", f"{key}: {rendered}")
            self._activity(f"{key}: {format_value(value, 48)}")
            shown += 1

    # ---- VPN mode ----

    def _autoconnect_vpn(self) -> None:
        """Bring the default tunnel up in the background when the harness opens.

        A first start should not need `/vpn connect` before a torrent can be
        fetched, so `vpn_autoconnect` is honoured here rather than only on an
        explicit command. The work goes to a worker because AirVPN can take
        seconds; `services.vpn.autoconnect` leaves a live tunnel alone, so a
        second start cannot spawn a second client. An ordinary `Settings()`
        with no credentials returns without touching the network, which is
        what keeps the harness usable on a box with no VPN.
        """
        if not self.settings.vpn_autoconnect:
            return
        if (self.settings.vpn_provider or "").lower() != "airvpn":
            return
        if not (self.settings.airvpn_username and self.settings.airvpn_password):
            return
        if self.vpn_healthy:
            return

        self._set_status("working · vpn autoconnect", True)
        self._trace("vpn autoconnect", style=VALUE)

        def work() -> None:
            try:
                from minus.services import vpn

                result = vpn.autoconnect(self.settings)
                if result.get("connected"):
                    summary = f"up on {result.get('interface') or 'the tunnel'}"
                    binding = result.get("binding")
                    if isinstance(binding, dict) and not binding.get("bound"):
                        reason = binding.get("error") or binding.get("reason")
                        summary += f" (qBittorrent: {reason or 'not bound'})"
                else:
                    summary = str(result.get("detail") or "not connected")
            except Exception as exc:
                summary = f"failed: {exc}"
            self._ui(lambda: self._append("sys", f"vpn autoconnect: {summary}"))
            self._ui(partial(self._set_status, "vpn autoconnect", False))
            self._ui(partial(self._trace, f"vpn autoconnect · {summary}", style=VALUE))
            self._ui(self._refresh_vpn_and_header)

        self._spawn(work, "vpn")

    def _vpn_command(self, argument: str) -> None:
        action = (argument or "status").strip().lower()
        if action not in ("status", "connect", "disconnect"):
            self._append("sys", "vpn: use status | connect | disconnect")
            return
        if action == "status":
            self._render_vpn()
            return

        provider = (self.settings.vpn_provider or "").lower()
        if (
            action == "connect"
            and provider == "airvpn"
            and not (self.settings.airvpn_username and self.settings.airvpn_password)
        ):
            missing = "AirVPN credentials missing (MINUS_AIRVPN_USERNAME/PASSWORD)"
            self._activity(f"vpn: {missing}")
            self._append("sys", f"vpn: {missing}")
            return

        self._set_status(f"working · vpn {action}", True)
        self._trace(f"vpn {action}", style=VALUE)

        def work() -> None:
            try:
                from minus.services import vpn

                operation = getattr(vpn, action, None)
                if not callable(operation):
                    raise AttributeError(f"{action} is not available in this build")
                result = operation(self.settings)
                if action == "connect":
                    binding = vpn.bind_downloads(self.settings, result)
                    if not binding.get("bound"):
                        reason = binding.get("error") or binding.get("reason") or "not bound"
                        result = f"{result} (qBittorrent: {reason})"
                summary = (
                    payload_summary(result)
                    if isinstance(result, dict)
                    else format_value(result, 100)
                )
            except Exception as exc:
                summary = f"failed: {exc}"
            self._ui(lambda: self._append("sys", f"vpn {action}: {summary}"))
            self._ui(partial(self._set_status, f"vpn {action}", False))
            self._ui(partial(self._trace, f"vpn {action} · {summary}", style=VALUE))
            self._ui(self._refresh_vpn_and_header)

        self._spawn(work, "vpn")

    def _refresh_vpn_and_header(self) -> None:
        self._refresh_vpn_state()
        self.refresh_topbar()

    # ---- Sys mode ----

    def _tool_names(self) -> list[str]:
        conversation = (
            getattr(self.assistant, "conversation", None) if self.assistant is not None else None
        )
        tools = getattr(conversation, "tools", None)
        if tools is not None and hasattr(tools, "names"):
            return list(tools.names())
        try:
            from minus.tools import registry

            return list(registry.names())
        except Exception:  # pragma: no cover - a broken tool import is not fatal here
            return []

    def _render_sys(self) -> None:
        self._refresh_vpn_state()
        self.refresh_topbar()
        try:
            from minus.doctor import collect_posture

            posture = collect_posture(self.settings)
            dangerous = posture.get("dangerous") or []
            self._append("sys", f"security: {len(dangerous)} issue(s)")
            for item in dangerous[:4]:
                self._append("sys", _clip(str(item), 110))
        except Exception as exc:
            self._append("sys", f"security: unavailable ({exc})")

        names = self._tool_names()
        shown = ", ".join(names[:12]) + (f", +{len(names) - 12} more" if len(names) > 12 else "")
        self._append("sys", f"tools: {len(names)} · {shown or 'none'}")
        interface = self.vpn_interface or "no interface"
        self._append("sys", f"vpn: {interface} · {self.vpn_detail or 'not probed'}")

    def _render_doctor(self) -> None:
        try:
            from minus.doctor import collect_posture, format_posture

            rendered = format_posture(collect_posture(self.settings))
        except Exception as exc:
            self._append("sys", f"doctor failed: {exc}")
            return
        for line in rendered.splitlines():
            self._append("sys", line)

    def _render_tools(self) -> None:
        names = self._tool_names()
        self._append("sys", f"{len(names)} tool(s)")
        for start in range(0, len(names), 6):
            self._append("sys", "  " + ", ".join(names[start : start + 6]))

    # ---- Commands ----

    async def _command(self, line: str) -> None:
        name, _, argument = line.partition(" ")
        name = name.strip().lower()
        argument = argument.strip()

        if name == "/help":
            for entry in HELP.splitlines():
                self._append("sys", entry)
        elif name == "/mode":
            if not argument:
                self._append("sys", f"mode: {self.mode} · {', '.join(MODES)}")
            else:
                self.select_mode(argument)
        elif name == "/model":
            self._command_model(argument)
        elif name == "/clear":
            self._clear_view()
        elif name in ("/quit", "/exit"):
            self.exit()
        elif name == "/vpn":
            self._vpn_command(argument)
        elif name == "/download":
            if not argument:
                self._append(
                    "sys",
                    "/download needs a query, a magnet link or an http(s) .torrent URL",
                )
            else:
                self._download(argument)
        elif name == "/triage":
            if not argument:
                self._append("sys", "/triage needs a torrent hash")
            else:
                self._scan(argument.split()[0])
        elif name == "/promote":
            parts = argument.split(maxsplit=1)
            if not parts:
                self._append("sys", "/promote needs a torrent hash")
            else:
                self._promote(parts[0], parts[1] if len(parts) > 1 else "")
        elif name == "/doctor":
            self._render_doctor()
        elif name == "/tools":
            self._render_tools()
        elif name == "/thoughts":
            self.toggle_thoughts()
        elif name == "/files":
            self.action_toggle_explorer()
        elif name == "/diffs":
            if not self.explorer_visible:
                self.action_toggle_explorer()
            if self._explorer is not None:
                self._explorer.show_diff(self.explorer_preview_path)
                self._set_status("diffs · working tree", False)
        else:
            self._append("sys", f"unknown command {name} · /help")

    def _command_model(self, argument: str) -> None:
        if not argument:
            self._append("sys", f"model: {self.settings.chat_model}")
            return
        try:
            from minus.scripts import models

            changes = models.resolve(self.settings, conversational=argument)
            self.settings.chat_model = changes["chat_model"]
        except Exception as exc:
            self._append("sys", f"model: {exc}")
            return
        self.refresh_topbar()
        self._append("sys", f"model: {self.settings.chat_model} (this session)")

    def _clear_view(self) -> None:
        self.conversation_lines.clear()
        with contextlib.suppress(NoMatches):
            self.query_one("#conversation", RichLog).clear()
        if self.mode == "code" and self._coding is not None:
            try:
                self._coding.transcript.replace([])
            except Exception:  # pragma: no cover - a fake session has no transcript
                logger.debug("Coding transcript could not be cleared", exc_info=True)
        self._append("sys", "cleared")

    # ---- Input ----

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        line = event.value.strip()
        event.input.value = ""
        if not line:
            return
        if line.startswith("/"):
            await self._command(line)
            return
        if self.mode == "chat":
            self._chat_submit(line)
        elif self.mode == "code":
            self._code_submit(line)
        elif self.mode == "download":
            # In this mode the line is the thing to fetch; a magnet/URL goes
            # straight to the pipeline and anything else is searched first.
            self._download(line)
        else:
            self._append("sys", f"{self.mode} mode takes commands; /help")

    def _spawn(self, work: Callable[[], None], group: str) -> None:
        """Run blocking agent/tool work off the UI thread.

        `exit_on_error=False` because every worker already catches its own
        failure and writes it to the transcript; letting Textual render a
        traceback would replace the screen instead.
        """
        try:
            self.run_worker(work, thread=True, group=group, exit_on_error=False)
        except Exception as exc:
            self._append("sys", f"{group}: could not start ({exc})")


def run_harness(
    settings: Settings,
    *,
    workspace: Path | None = None,
    model: str | None = None,
) -> None:
    """Open the interactive harness.

    Refuses without a terminal rather than starting Textual against a pipe,
    which would accept keystrokes nobody can send and draw a screen nobody can
    see. `model` is validated by the same resolver the `minus model` command
    uses, so a bad name is refused before the UI opens rather than on the first
    turn.
    """
    if not _is_tty():
        raise SystemExit(
            "minus harness needs an interactive terminal; run it from a shell "
            "(use `minus voice --no-mic` for a typed session without Textual)."
        )

    if model:
        from minus.scripts import models

        try:
            changes = models.resolve(settings, conversational=model)
        except ValueError as exc:
            raise SystemExit(f"model: {exc}") from exc
        settings.chat_model = changes["chat_model"]

    MinusHarness(settings, workspace=workspace).run()
