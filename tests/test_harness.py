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

from textual.events import MouseDown, MouseMove, MouseUp
from textual.widgets import Input, OptionList, RichLog, Static

from minus.config import Settings
from minus.harness import MODES, MinusHarness, run_harness
from minus.harness.explorer import ExplorerPane


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


def make_app(workspace=None) -> tuple[MinusHarness, FakeConversation]:
    conversation = FakeConversation()
    assistant = SimpleNamespace(conversation=conversation, results=Queue())
    return (
        MinusHarness(Settings(), workspace=workspace, conversation=assistant),
        conversation,
    )


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
        # The explorer's DirectoryTree has its own long-lived loader worker;
        # wait for the turn we just started, not for every worker in the app.
        await app.workers.wait_for_complete(
            [worker for worker in app.workers if worker.group == "chat"]
        )
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


async def test_ctrl_b_opens_the_explorer_and_docks_on_all_four_sides(tmp_path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
    app, _ = make_app(tmp_path)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.press("ctrl+b")
        await pilot.pause(0.1)

        explorer = app.query_one("#explorer", ExplorerPane)
        assert explorer.parent.id == "dock-left"
        assert app.query_one("#dock-left").has_class("-active")

        for button, dock in (
            ("#dock-right-btn", "dock-right"),
            ("#dock-top-btn", "dock-top"),
            ("#dock-bottom-btn", "dock-bottom"),
            ("#dock-left-btn", "dock-left"),
        ):
            await pilot.click(button)
            await pilot.pause()
            assert explorer.parent.id == dock
            assert app.query_one(f"#{dock}").has_class("-active")
            if dock in ("dock-top", "dock-bottom"):
                # A top/bottom dock spans the shell, and the code view spans
                # the dock rather than inheriting a side-pane width.
                assert explorer.size.width == app.query_one(f"#{dock}").size.width

        app.preview_file(tmp_path / "pkg" / "mod.py")
        await pilot.pause()
        assert "def hello" in log_text(app, "file-preview")
        preview = app.query_one("#file-preview", RichLog)
        tree_pane = app.query_one("#explorer-tree-pane")
        assert preview.size.width > tree_pane.size.width
        assert preview.size.height == app.query_one("#explorer-main").size.height

        await pilot.click("#nvim-btn")
        await pilot.pause()
        assert explorer.has_class("-expanded")
        assert app.query_one("#dock-left").has_class("-expanded")

        await pilot.press("ctrl+b")
        await pilot.pause()
        assert not app.query_one("#dock-left").has_class("-active")


async def test_dragging_the_dock_edge_resizes_and_leaves_chat_room(tmp_path) -> None:
    app, _ = make_app(tmp_path)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.press("ctrl+b")
        await pilot.pause(0.1)
        explorer = app.query_one("#explorer", ExplorerPane)
        dock = app.query_one("#dock-left")
        start = dock.size.width
        x = explorer.region.right - 1
        y = explorer.region.y + 2

        explorer.on_mouse_down(
            MouseDown(explorer, x, y, 0, 0, 1, False, False, False, screen_x=x, screen_y=y)
        )
        explorer.on_mouse_move(
            MouseMove(explorer, x, y, 0, 0, 1, False, False, False, screen_x=x + 8, screen_y=y)
        )
        await pilot.pause()
        assert dock.size.width > start

        explorer.on_mouse_move(
            MouseMove(explorer, x, y, 0, 0, 1, False, False, False, screen_x=x + 500, screen_y=y)
        )
        await pilot.pause()
        explorer.on_mouse_up(
            MouseUp(explorer, x, y, 0, 0, 1, False, False, False, screen_x=x, screen_y=y)
        )
        await pilot.pause()

        assert app.query_one("#center").size.width >= 25
        assert dock.size.width <= app.query_one("#body").size.width - 80


async def test_dragging_a_top_dock_leaves_the_body_readable(tmp_path) -> None:
    app, _ = make_app(tmp_path)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.press("ctrl+b")
        await pilot.pause(0.1)
        await pilot.click("#dock-top-btn")
        await pilot.pause()
        explorer = app.query_one("#explorer", ExplorerPane)
        dock = app.query_one("#dock-top")
        x = explorer.region.x + 2
        edge = explorer.region.bottom - 1
        explorer.on_mouse_down(
            MouseDown(
                explorer,
                x,
                edge,
                0,
                0,
                1,
                False,
                False,
                False,
                screen_x=x,
                screen_y=edge,
            )
        )
        explorer.on_mouse_move(
            MouseMove(
                explorer,
                x,
                edge,
                0,
                0,
                1,
                False,
                False,
                False,
                screen_x=x,
                screen_y=edge + 500,
            )
        )
        await pilot.pause()
        explorer.on_mouse_up(
            MouseUp(
                explorer,
                x,
                edge,
                0,
                0,
                1,
                False,
                False,
                False,
                screen_x=x,
                screen_y=edge + 500,
            )
        )
        await pilot.pause()

        assert app.query_one("#body").size.height >= 12
        assert dock.size.height <= app.query_one("#shell").size.height - 12


async def test_dragging_the_preview_divider_gives_code_more_space(tmp_path) -> None:
    path = tmp_path / "sample.py"
    path.write_text("\n".join(f"line_{n} = {n}" for n in range(80)), encoding="utf-8")
    app, _ = make_app(tmp_path)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.press("ctrl+b")
        await pilot.pause(0.1)
        app.preview_file(path)
        await pilot.pause()

        explorer = app.query_one("#explorer", ExplorerPane)
        pane = app.query_one("#explorer-tree-pane")
        preview = app.query_one("#file-preview", RichLog)
        tree_width = pane.size.width
        preview_width = preview.size.width
        boundary = pane.region.right
        y = explorer.region.y + 2

        explorer.on_mouse_down(
            MouseDown(
                explorer,
                boundary,
                y,
                0,
                0,
                1,
                False,
                False,
                False,
                screen_x=boundary,
                screen_y=y,
            )
        )
        explorer.on_mouse_move(
            MouseMove(
                explorer,
                boundary,
                y,
                0,
                0,
                1,
                False,
                False,
                False,
                screen_x=boundary - 4,
                screen_y=y,
            )
        )
        await pilot.pause()
        explorer.on_mouse_up(
            MouseUp(
                explorer,
                boundary - 4,
                y,
                0,
                0,
                1,
                False,
                False,
                False,
                screen_x=boundary - 4,
                screen_y=y,
            )
        )
        await pilot.pause()

        assert pane.size.width < tree_width
        assert preview.size.width > preview_width


async def test_diffs_button_shows_the_working_tree(tmp_path) -> None:
    import subprocess

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "test@example.test")
    git("config", "user.name", "Test")
    (tmp_path / "change.py").write_text("value = 1\n", encoding="utf-8")
    git("add", "change.py")
    git("commit", "-qm", "base")
    (tmp_path / "change.py").write_text("value = 2\n", encoding="utf-8")

    app, _ = make_app(tmp_path)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.press("ctrl+b")
        await pilot.pause(0.1)
        await pilot.click("#diffs-btn")
        await pilot.pause()

        diff = log_text(app, "file-preview")
        assert "diff --git" in diff
        assert "-value = 1" in diff
        assert "+value = 2" in diff
        assert app.query_one("#explorer", ExplorerPane).has_class("-preview")


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
