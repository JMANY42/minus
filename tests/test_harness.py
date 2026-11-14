"""The harness, driven through Textual's pilot.

No model is called: the tests inject a fake Assistant (the object
`assembly.build_conversation` returns) whose Conversation answers locally and
emits the same observer events `ToolLoop` would. That keeps the assertions about
the harness's threading and rendering rather than about a network.
"""

from __future__ import annotations

from queue import Queue
from types import SimpleNamespace

import pytest

pytest.importorskip("textual")

from textual.widgets import Input, OptionList, RichLog, Static

from minus.config import Settings
from minus.harness import MODES, MinusHarness, run_harness


class FakeConversation:
    """Stands in for `core.agent.Conversation`, including its observer seam."""

    def __init__(self) -> None:
        self.observer = None
        self.turns: list[str] = []

    def reply(self, text: str) -> str:
        self.turns.append(text)
        if self.observer is not None:
            self.observer("tool_start", {"name": "read_file", "arguments": {"path": "notes.txt"}})
            self.observer("tool_end", {"name": "read_file", "result": {"content": "x" * 500}})
        return "a grounded answer"


def make_app() -> tuple[MinusHarness, FakeConversation]:
    conversation = FakeConversation()
    assistant = SimpleNamespace(conversation=conversation, results=Queue())
    return MinusHarness(Settings(), conversation=assistant), conversation


def log_text(app: MinusHarness, widget_id: str) -> str:
    log = app.query_one(f"#{widget_id}", RichLog)
    return "\n".join(strip.text for strip in log.lines)


async def test_mounts_with_all_modes_and_switches() -> None:
    app, _ = make_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.title == "MINUS"
        assert app.mode == "chat"

        for mode in MODES:
            app.select_mode(mode)
            await pilot.pause()
            assert app.mode == mode

        # The rail is a real OptionList: select an entry the way Enter does.
        rail = app.query_one("#mode-list", OptionList)
        rail.highlighted = MODES.index("vpn")
        rail.action_select()
        await pilot.pause()
        assert app.mode == "vpn"

        # And the slash command takes the same path.
        prompt = app.query_one("#prompt", Input)
        prompt.value = "/mode chat"
        await pilot.press("enter")
        await pilot.pause()
        assert app.mode == "chat"


async def test_help_writes_lines() -> None:
    app, _ = make_app()
    async with app.run_test(size=(120, 40)) as pilot:
        prompt = app.query_one("#prompt", Input)
        prompt.value = "/help"
        await pilot.press("enter")
        await pilot.pause()

        written = "\n".join(body for role, body in app.conversation_lines if role == "sys")
        assert "/mode <mode>" in written
        assert "/download" in written
        assert "/vpn status|connect|disconnect" in written


async def test_chat_turn_runs_on_a_worker_and_shows_the_reply() -> None:
    app, conversation = make_app()
    async with app.run_test(size=(120, 40)) as pilot:
        prompt = app.query_one("#prompt", Input)
        prompt.value = "hello"
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert conversation.turns == ["hello"]
        assert ("you", "hello") in app.conversation_lines
        assert ("minus", "a grounded answer") in app.conversation_lines


async def test_tool_events_become_compact_activity() -> None:
    app, _ = make_app()
    async with app.run_test(size=(120, 40)) as pilot:
        app.observer("tool_start", {"name": "read_file", "arguments": {"path": "notes.txt"}})
        app.observer(
            "tool_end",
            {"name": "read_file", "result": {"content": "x" * 1000}},
        )
        await pilot.pause()

        activity = log_text(app, "activity")
        assert "read_file" in activity
        assert "ok read_file" in activity

        cards = [body for role, body in app.conversation_lines if role == "tool"]
        assert cards[0] == "read_file path=notes.txt"
        assert cards[1] == "read_file ok"
        # The result is never copied onto the screen.
        assert "x" * 1000 not in "\n".join(app.activity_lines)


async def test_thinking_bar_expands_to_reasoning_and_tool_trace() -> None:
    app, _ = make_app()
    async with app.run_test(size=(120, 40)) as pilot:
        app.observer("thinking", {"round": 1})
        app.observer("thought", {"text": "I should inspect notes.txt"})
        app.observer("tool_start", {"name": "read_file", "arguments": {"path": "notes.txt"}})
        await pilot.pause()

        bar = str(app.query_one("#thinking-bar", Static).content)
        assert "reading" in bar
        assert "read_file" in bar

        trace = app.query_one("#thinking-trace", RichLog)
        assert not trace.has_class("-open")
        await pilot.click("#thinking-bar")
        await pilot.pause()
        assert trace.has_class("-open")
        assert "I should inspect notes.txt" in log_text(app, "thinking-trace")
        assert "reading · read_file" in log_text(app, "thinking-trace")
        await pilot.click("#thinking-bar")
        await pilot.pause()
        assert not trace.has_class("-open")


async def test_malicious_alert_renders_in_red_rail_and_transcript() -> None:
    app, _ = make_app()
    async with app.run_test(size=(120, 40)) as pilot:
        app.notify_event(
            "alert",
            {"alert": "ALERT: malicious torrent deadbeef was deleted from quarantine"},
        )
        await pilot.pause()

        assert any("ALERT" in line for line in app.alert_lines)
        assert "ALERT" in log_text(app, "alerts")
        assert any(role == "sys" and "ALERT" in body for role, body in app.conversation_lines)


async def test_pipeline_notification_without_alert_is_activity_only() -> None:
    app, _ = make_app()
    async with app.run_test(size=(120, 40)) as pilot:
        app.notify_event("stage", {"stage": "scan", "torrent_hash": "abc123"})
        await pilot.pause()

        assert any("abc123" in line for line in app.activity_lines)
        assert not app.alert_lines


def test_run_harness_refuses_without_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("minus.harness.app._is_tty", lambda: False)
    with pytest.raises(SystemExit) as raised:
        run_harness(Settings())
    assert "interactive terminal" in str(raised.value)
