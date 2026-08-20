"""The dashboard's layout and keys, driven headlessly.

Textual's test pilot needs no terminal, so the widget tree and the key
handling are checked for real. Skipped where the extra is not installed,
matching how test_playback.py guards on sounddevice.
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("textual")

from rich.text import Text
from textual.widgets import Input, RichLog, Static

from minus.config import Settings
from minus.control.config_control import ConfigController
from minus.dashboard.app import MinusDashboard
from minus.dashboard.choices import CHOICES, MODELS, OTHER, STT_MODELS, VOICES, choices_for
from minus.dashboard.widgets import (
    AgentsPanel,
    ChoiceList,
    FactList,
    Heading,
    ManagementPanel,
    MemoryPanel,
    Panel,
    Setting,
    SettingList,
    ToolList,
    ToolsPanel,
    ViewerPane,
    ascii_bar,
    build_setting_rows,
    deep_elapsed,
    describe_change,
    human_bytes,
    render_turn,
    wrap_indented,
)
from minus.services.json import write_json

PANELS = [
    "panel-memory",
    "panel-hardware",
    "panel-tools",
    "panel-programs",
    "panel-agents",
    "panel-management",
]
HOTKEYS = {
    "m": "panel-memory",
    "h": "panel-hardware",
    "t": "panel-tools",
    "p": "panel-programs",
    "a": "panel-agents",
    "g": "panel-management",
}


@pytest.fixture
def app(tmp_path, monkeypatch):
    """A dashboard pointed at an empty project, with no assistant running."""
    monkeypatch.setenv("MINUS_PROJECT_ROOT", str(tmp_path))
    for name in ("logs", "memory/conversations", "memory/deep_notes"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    return MinusDashboard(tmp_path / "absent.sock")


def live_row(app) -> str:
    """The console's in-place line -- what a spinner is currently drawing."""
    return str(app.query_one("#console-live", Static).content)


def focus_within(app, panel_id: str) -> bool:
    """True if the keys are going to that panel, or to something inside it.

    Both count: a panel with an interactive expanded view hands focus to that
    view rather than keeping it on the frame.
    """
    focused = app.focused
    if focused is None:
        return False
    return focused.id == panel_id or any(node.id == panel_id for node in focused.ancestors)


@pytest.fixture
def connected(app):
    """A dashboard with a MINUS answering on the other end of the socket.

    `connect` is stubbed rather than the flag simply set: the real one runs on
    a worker thread and reports back that nothing is listening, which would
    otherwise land on top of the flag at whatever moment it finished.
    """
    app.connect = lambda: None
    app.connected = True
    return app


@pytest.fixture
def notes(tmp_path):
    """Two deep-think notes, the second long enough to need scrolling."""
    directory = tmp_path / "memory/deep_notes"
    write_json(
        directory / "20260810T010000Z-older.json",
        {"title": "older", "created_at": "20260810T010000Z", "detail": "short"},
    )
    write_json(
        directory / "20260810T020000Z-newer.json",
        {"title": "newer", "created_at": "20260810T020000Z", "detail": "line\n" * 200},
    )
    return directory


class TestLayout:
    async def test_the_screen_has_both_halves_and_all_five_panels(self, app):
        async with app.run_test() as pilot:
            assert pilot.app.query_one("#left")
            assert pilot.app.query_one("#right")
            for panel_id in PANELS:
                assert pilot.app.query_one(f"#{panel_id}")

    async def test_the_left_column_is_a_viewer_over_an_input(self, app):
        async with app.run_test() as pilot:
            assert pilot.app.query_one("#viewer")
            assert pilot.app.query_one("#prompt-input")

    async def test_the_input_is_one_line_in_a_bordered_pane(self, app):
        """It holds nothing but the Input now, so it needs nothing but the room."""
        async with app.run_test(size=(120, 40)) as pilot:
            # outer_size counts the border; size is what is left inside it.
            assert pilot.app.query_one("#prompt").outer_size.height == 3
            assert pilot.app.query_one("#prompt").size.height == 1
            assert pilot.app.query_one("#prompt-input").size.height == 1

    async def test_the_viewer_is_twice_the_height_of_the_console(self, app):
        """The two-thirds/one-third split, now against the console.

        The console takes the block the echo box used to, so opening it costs
        the viewer what the echo box already cost it.
        """
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("c")

            viewer = pilot.app.query_one("#viewer").size.height
            console = pilot.app.query_one("#console").size.height

            assert viewer == pytest.approx(2 * console, abs=2)

    async def test_the_viewer_takes_the_column_while_the_console_is_hidden(self, app):
        async with app.run_test(size=(120, 40)) as pilot:
            with_console_shut = pilot.app.query_one("#viewer").size.height
            await pilot.press("c")

            assert pilot.app.query_one("#viewer").size.height < with_console_shut

    async def test_the_halves_are_the_same_width(self, app):
        async with app.run_test(size=(120, 40)) as pilot:
            left = pilot.app.query_one("#left").size.width
            right = pilot.app.query_one("#right").size.width

            assert left == pytest.approx(right, abs=1)

    async def test_it_survives_a_small_terminal(self, app):
        async with app.run_test(size=(80, 24)) as pilot:
            assert pilot.app.query_one("#right").size.width > 0


class TestViewSwitching:
    async def test_starts_on_the_conversation(self, app):
        async with app.run_test() as pilot:
            assert pilot.app.query_one(ViewerPane).current == "conversation"

    async def test_the_ctrl_arrows_cycle_the_three_views(self, app):
        async with app.run_test() as pilot:
            viewer = pilot.app.query_one(ViewerPane)
            pilot.app.set_focus(None)

            await pilot.press("ctrl+right")
            assert viewer.current == "log"
            await pilot.press("ctrl+right")
            assert viewer.current == "deep"
            await pilot.press("ctrl+right")
            assert viewer.current == "conversation"
            await pilot.press("ctrl+left")
            assert viewer.current == "deep"

    async def test_the_bare_arrows_are_left_to_the_view(self, app):
        """They scroll a wide log line sideways instead of changing tab."""
        async with app.run_test() as pilot:
            viewer = pilot.app.query_one(ViewerPane)
            pilot.app.set_focus(None)

            await pilot.press("right")

            assert viewer.current == "conversation"

    async def test_ctrl_arrows_work_even_while_typing(self, app):
        """The bare arrows belong to the input's cursor when it has focus."""
        async with app.run_test() as pilot:
            pilot.app.query_one("#prompt-input").focus()
            await pilot.pause()

            await pilot.press("ctrl+right")

            assert pilot.app.query_one(ViewerPane).current == "log"

    async def test_the_number_keys_jump_directly(self, app):
        async with app.run_test() as pilot:
            pilot.app.set_focus(None)

            await pilot.press("3")

            assert pilot.app.query_one(ViewerPane).current == "deep"


class TestScrollingWithFocusNowhere:
    """The arrows still move the viewer when nothing holds the keys.

    Focus nowhere is how the dashboard opens and where escape puts you back,
    so the views' own scroll bindings never fire there -- which left the
    arrows doing nothing at all in the state the dashboard spends most of its
    time in.
    """

    async def fill(self, pilot):
        """Enough text in both logs to have somewhere to scroll to."""
        for view_id in ("view-conversation", "view-log"):
            log = pilot.app.query_one(f"#{view_id}", RichLog)
            for index in range(200):
                log.write(Text(f"{index} " + "x" * 400))
        await pilot.pause()

    async def test_left_and_right_scroll_the_log_sideways(self, app):
        async with app.run_test(size=(120, 40)) as pilot:
            pilot.app.set_focus(None)
            await self.fill(pilot)
            await pilot.press("2")
            log = pilot.app.query_one("#view-log")

            await pilot.press("right")
            await pilot.pause()
            scrolled = log.scroll_offset.x
            assert scrolled > 0

            await pilot.press("left")
            await pilot.pause()
            assert log.scroll_offset.x < scrolled

    @pytest.mark.parametrize(
        "key,view_id",
        [("1", "view-conversation"), ("2", "view-log"), ("3", "view-deep")],
    )
    async def test_up_and_down_scroll_whichever_view_is_showing(self, app, notes, key, view_id):
        async with app.run_test(size=(120, 40)) as pilot:
            pilot.app.set_focus(None)
            await self.fill(pilot)
            await pilot.press(key)
            view = pilot.app.query_one(f"#{view_id}")
            # Away from both ends first: the logs open pinned to the bottom
            # and the deep view opens at the top, so either one alone would
            # only ever prove the direction it had room to move in.
            view.scroll_to(y=10, animate=False)
            await pilot.pause()

            await pilot.press("down")
            await pilot.pause()
            assert view.scroll_offset.y > 10

            await pilot.press("up", "up")
            await pilot.pause()
            assert view.scroll_offset.y < 10

    async def test_the_arrows_are_left_to_whatever_does_have_focus(self, app):
        """Only the unfocused case is the app's: the input box keeps its own."""
        async with app.run_test(size=(120, 40)) as pilot:
            await self.fill(pilot)
            pilot.app.query_one("#prompt-input").focus()
            view = pilot.app.query_one("#view-conversation")
            view.scroll_to(y=10, animate=False)
            await pilot.pause()
            before = view.scroll_offset

            await pilot.press("down", "right")
            await pilot.pause()

            assert view.scroll_offset == before


class TestPanelExpansion:
    @pytest.mark.parametrize("key,panel_id", sorted(HOTKEYS.items()))
    async def test_each_hotkey_expands_its_panel(self, app, key, panel_id):
        async with app.run_test() as pilot:
            pilot.app.set_focus(None)

            await pilot.press(key)

            expanded = pilot.app.query_one(f"#{panel_id}")
            assert expanded.has_class("-expanded")
            for other in PANELS:
                if other != panel_id:
                    assert pilot.app.query_one(f"#{other}").has_class("-hidden")

    async def test_pressing_it_again_collapses(self, app):
        async with app.run_test() as pilot:
            pilot.app.set_focus(None)

            await pilot.press("m")
            await pilot.press("m")

            assert not pilot.app.query_one("#panel-memory").has_class("-expanded")
            assert not pilot.app.query_one("#panel-hardware").has_class("-hidden")

    async def test_escape_collapses(self, app):
        async with app.run_test() as pilot:
            pilot.app.set_focus(None)

            await pilot.press("h")
            await pilot.press("escape")

            assert not pilot.app.query_one("#panel-hardware").has_class("-expanded")

    async def test_a_second_panel_replaces_the_first(self, app):
        async with app.run_test() as pilot:
            pilot.app.set_focus(None)

            await pilot.press("m")
            await pilot.press("t")

            assert pilot.app.query_one("#panel-tools").has_class("-expanded")
            assert not pilot.app.query_one("#panel-memory").has_class("-expanded")

    async def test_escape_leaves_the_input(self, app):
        """`Vertical` is not focusable, so focusing the pane was a silent no-op."""
        async with app.run_test() as pilot:
            pilot.app.query_one("#prompt-input").focus()
            await pilot.pause()

            await pilot.press("escape")

            assert not isinstance(pilot.app.focused, Input)

    async def test_the_hotkeys_work_after_escape(self, app):
        """The whole point of stepping out: m/h/t/p/a stop being typed text."""
        async with app.run_test() as pilot:
            pilot.app.query_one("#prompt-input").focus()
            await pilot.pause()

            await pilot.press("escape")
            await pilot.press("m")

            assert pilot.app.query_one("#panel-memory").has_class("-expanded")
            assert pilot.app.query_one("#prompt-input").value == ""

    async def test_escape_lets_go_of_focus_entirely(self, app):
        """It used to hand focus to the viewer, which nothing had asked it to."""
        async with app.run_test() as pilot:
            pilot.app.query_one("#prompt-input").focus()
            await pilot.pause()

            await pilot.press("ctrl+right")  # to the log
            await pilot.press("escape")

            assert pilot.app.focused is None

    async def test_switching_views_keeps_focus_on_what_is_displayed(self, app):
        async with app.run_test() as pilot:
            await pilot.press("v")

            await pilot.press("ctrl+right")

            assert pilot.app.focused.id == "view-log"

    async def test_i_returns_to_the_input(self, app):
        async with app.run_test() as pilot:
            pilot.app.query_one("#prompt-input").focus()
            await pilot.pause()

            await pilot.press("escape")
            await pilot.press("i")

            assert isinstance(pilot.app.focused, Input)

    async def test_the_hotkeys_type_rather_than_expand_while_the_input_has_focus(self, app):
        async with app.run_test() as pilot:
            pilot.app.query_one("#prompt-input").focus()
            await pilot.pause()

            await pilot.press("m")

            assert pilot.app.query_one("#prompt-input").value == "m"
            assert not pilot.app.query_one("#panel-memory").has_class("-expanded")

    async def test_each_panel_title_still_shows_its_hotkey(self, app):
        """Unescaped, Textual reads [m] as content markup and drops it.

        Asserted against `_border_title`, the parsed Content that actually
        gets drawn -- `border_title` hands back the raw string, brackets and
        escape and all, so it would pass whether or not this works.
        """
        async with app.run_test() as pilot:
            for key, panel_id in HOTKEYS.items():
                panel = pilot.app.query_one(f"#{panel_id}", Panel)

                assert f"[{key}]" in panel._border_title.plain

    async def test_the_panels_without_an_expanded_view_say_they_have_none(self, app):
        """Scaffolding, deliberately. Filling one in is building the view."""
        async with app.run_test() as pilot:
            for panel in pilot.app.query(Panel):
                if panel.interactive() is None:
                    assert panel.options() == []
                    assert "(nothing here yet)" in str(panel.query_one("#options").content)


class TestDisconnected:
    async def test_it_opens_without_an_assistant_running(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()

            assert pilot.app.query_one("#statusbar").has_class("disconnected")

    async def test_it_keeps_looking_for_the_assistant(self, app):
        """Opened while MINUS is down -- the common case -- it must recover."""
        attempts = []
        app.connect = lambda: attempts.append(1)

        async with app.run_test() as pilot:
            attempts.clear()  # mounting connects once already
            pilot.app.connected = False
            pilot.app.reconnect()
            pilot.app.reconnect()

        assert len(attempts) == 2

    async def test_it_stops_looking_once_connected(self, app):
        attempts = []
        app.connect = lambda: attempts.append(1)

        async with app.run_test() as pilot:
            attempts.clear()  # mounting connects once already
            pilot.app.connected = True
            pilot.app.reconnect()

        assert attempts == []

    async def test_typing_a_line_reports_that_nothing_is_listening(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            pilot.app.query_one("#prompt-input").focus()

            await pilot.press("h", "i", "enter")
            await pilot.pause()

            assert pilot.app.query_one("#prompt-input").value == ""


class TestOpeningFocus:
    async def test_it_opens_holding_nothing(self, app):
        """Textual's AUTO_FOCUS would otherwise grab the first focusable view."""
        async with app.run_test() as pilot:
            await pilot.pause()

            assert pilot.app.focused is None

    async def test_a_hotkey_fires_rather_than_being_typed(self, app):
        """The whole reason not to open in the input box."""
        async with app.run_test() as pilot:
            await pilot.pause()

            await pilot.press("m")

            assert pilot.app.query_one("#panel-memory").has_class("-expanded")
            assert pilot.app.query_one("#prompt-input").value == ""


class TestViewerFocus:
    async def test_v_focuses_the_view_that_is_showing(self, app):
        async with app.run_test() as pilot:
            await pilot.press("v")

            assert pilot.app.focused.id == "view-conversation"

    async def test_shift_v_gives_the_viewer_the_screen(self, app):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("V")

            assert pilot.app.query_one("#body").has_class("-viewer-full")
            assert not pilot.app.query_one("#right").display
            assert not pilot.app.query_one("#prompt").display

    async def test_shift_v_again_gives_it_back(self, app):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("V")
            await pilot.press("V")

            assert not pilot.app.query_one("#body").has_class("-viewer-full")
            assert pilot.app.query_one("#right").display

    async def test_escape_leaves_fullscreen_before_anything_else(self, app):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("V")

            await pilot.press("escape")

            assert not pilot.app.query_one("#body").has_class("-viewer-full")

    async def test_enter_inside_the_viewer_toggles_fullscreen(self, app):
        """Focus sits on the RichLog, not the pane, so this walks ancestors."""
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("v")

            await pilot.press("enter")

            assert pilot.app.query_one("#body").has_class("-viewer-full")


class TestOneFocusAtATime:
    async def test_v_gives_the_viewer_back(self, app):
        """Every panel hotkey toggles, so the viewer key does too."""
        async with app.run_test() as pilot:
            await pilot.press("v")
            assert pilot.app.focused is not None

            await pilot.press("v")

            assert pilot.app.focused is None

    async def test_a_panel_hotkey_takes_focus_off_the_viewer(self, app):
        async with app.run_test() as pilot:
            await pilot.press("v")

            await pilot.press("m")

            assert focus_within(pilot.app, "panel-memory")
            assert not pilot.app.query_one("#viewer").has_pseudo_class("focus-within")

    async def test_a_panel_hotkey_takes_focus_off_the_console(self, app):
        async with app.run_test() as pilot:
            await pilot.press("c")

            await pilot.press("t")

            assert focus_within(pilot.app, "panel-tools")
            assert not pilot.app.query_one("#console").has_pseudo_class("focus-within")

    async def test_v_collapses_an_expanded_panel(self, app):
        """Only the input box is allowed to leave one enlarged behind it."""
        async with app.run_test() as pilot:
            await pilot.press("m")

            await pilot.press("v")

            assert not pilot.app.query_one("#panel-memory").has_class("-expanded")

    async def test_c_collapses_an_expanded_panel(self, app):
        async with app.run_test() as pilot:
            await pilot.press("m")

            await pilot.press("c")

            assert not pilot.app.query_one("#panel-memory").has_class("-expanded")

    async def test_the_input_leaves_the_panel_enlarged(self, app):
        """The one exception: you can type at it while still reading it."""
        async with app.run_test() as pilot:
            await pilot.press("m")

            await pilot.press("i")

            assert pilot.app.query_one("#panel-memory").has_class("-expanded")
            assert isinstance(pilot.app.focused, Input)


class TestTabbingThePanels:
    async def test_tab_reaches_the_right_hand_column(self, app):
        """A plain Vertical is not focusable, so the panels used to be skipped."""
        async with app.run_test() as pilot:
            pilot.app.query_one("#prompt-input").focus()
            await pilot.pause()

            await pilot.press("tab")

            assert isinstance(pilot.app.focused, Panel)

    async def test_enter_expands_the_focused_panel(self, app):
        async with app.run_test() as pilot:
            pilot.app.query_one("#panel-tools", Panel).focus()
            await pilot.pause()

            await pilot.press("enter")

            assert pilot.app.query_one("#panel-tools").has_class("-expanded")

    async def test_focusing_a_panel_takes_the_highlight_off_the_viewer(self, app):
        """Textual focus is exclusive; this pins that the viewer lets go of it."""
        async with app.run_test() as pilot:
            await pilot.press("v")

            pilot.app.query_one("#panel-memory", Panel).focus()
            await pilot.pause()

            assert pilot.app.focused.id == "panel-memory"
            assert not pilot.app.query_one("#viewer").has_pseudo_class("focus-within")


FACTS = [
    {"id": "f1", "attribute": "timezone", "value": "PST", "active": True},
    {"id": "f2", "attribute": "favorite_band", "value": "queen", "active": True},
    {"id": "f3", "attribute": "preferred_editor", "value": "vim", "active": True},
]


def remember(app, facts=FACTS) -> FactList:
    """Fill the memory panel as a `refresh_panels` from a live MINUS would."""
    app.query_one(MemoryPanel).update({"facts": facts, "conversations": 2, "deep_notes": 1})
    return app.query_one(FactList)


class TestFactList:
    """The expanded memory panel: `minus memory`, inside the dashboard."""

    async def test_expanding_memory_hands_the_keys_to_the_list(self, app):
        async with app.run_test() as pilot:
            remember(pilot.app)

            await pilot.press("m")

            assert pilot.app.focused is pilot.app.query_one(FactList)

    async def test_the_arrows_move_the_cursor(self, app):
        async with app.run_test() as pilot:
            facts = remember(pilot.app)
            await pilot.press("m")

            await pilot.press("down", "down")
            assert facts.cursor == 2
            await pilot.press("up")
            assert facts.cursor == 1

    async def test_the_cursor_stops_at_both_ends(self, app):
        async with app.run_test() as pilot:
            facts = remember(pilot.app)
            await pilot.press("m")

            await pilot.press("up")
            assert facts.cursor == 0
            await pilot.press("down", "down", "down", "down")
            assert facts.cursor == len(FACTS) - 1

    async def test_the_arrows_do_not_reach_the_viewer(self, app, notes):
        """The list owns them outright while it has focus, which is the point."""
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("3")  # the deep view, which scrolls and pages
            await pilot.pause()
            remember(pilot.app)
            await pilot.press("m")

            await pilot.press("down", "down")

            assert pilot.app.note_index == 0
            assert pilot.app.query_one("#view-deep").scroll_offset.y == 0

    async def test_space_marks_and_unmarks(self, app):
        async with app.run_test() as pilot:
            facts = remember(pilot.app)
            await pilot.press("m")

            await pilot.press("space")
            assert facts.marked == {"f1"}
            await pilot.press("down", "space")
            assert facts.marked == {"f1", "f2"}
            await pilot.press("space")
            assert facts.marked == {"f1"}

    async def test_one_d_only_asks(self, connected):
        """A hard delete with no history behind it takes saying twice."""
        forgotten = []

        async with connected.run_test() as pilot:
            pilot.app.forget_facts = forgotten.append
            facts = remember(pilot.app)
            await pilot.press("m")

            await pilot.press("space", "d")
            await pilot.pause()

            assert forgotten == []
            assert facts.pending

    async def test_the_second_d_forgets_what_is_marked(self, connected):
        forgotten = []

        async with connected.run_test() as pilot:
            pilot.app.forget_facts = forgotten.append
            remember(pilot.app)
            await pilot.press("m")

            await pilot.press("down", "space", "d", "d")
            await pilot.pause()

            assert forgotten == [["f2"]]

    async def test_it_forgets_every_mark_at_once(self, connected):
        forgotten = []

        async with connected.run_test() as pilot:
            pilot.app.forget_facts = forgotten.append
            remember(pilot.app)
            await pilot.press("m")

            await pilot.press("space", "down", "down", "space", "d", "d")
            await pilot.pause()

            assert forgotten == [["f1", "f3"]]

    async def test_d_with_nothing_marked_takes_what_is_under_the_cursor(self, connected):
        forgotten = []

        async with connected.run_test() as pilot:
            pilot.app.forget_facts = forgotten.append
            remember(pilot.app)
            await pilot.press("m")

            await pilot.press("down", "d", "d")
            await pilot.pause()

            assert forgotten == [["f2"]]

    async def test_moving_the_cursor_cancels_the_question(self, connected):
        forgotten = []

        async with connected.run_test() as pilot:
            pilot.app.forget_facts = forgotten.append
            facts = remember(pilot.app)
            await pilot.press("m")

            await pilot.press("d", "down", "d")
            await pilot.pause()

            assert forgotten == []
            assert facts.pending  # the last d asked again, about the new row

    async def test_leaving_the_list_cancels_it_too(self, app):
        """A `d` typed a minute later somewhere else is not the second half."""
        async with app.run_test() as pilot:
            facts = remember(pilot.app)
            await pilot.press("m")

            await pilot.press("d")
            await pilot.press("i")
            await pilot.pause()

            assert not facts.pending

    async def test_an_empty_store_has_nothing_to_forget(self, connected):
        forgotten = []

        async with connected.run_test() as pilot:
            pilot.app.forget_facts = forgotten.append
            remember(pilot.app, [])
            await pilot.press("m")

            await pilot.press("space", "d", "d")
            await pilot.pause()

            assert forgotten == []

    async def test_it_says_so_when_nothing_is_listening(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            said = []
            pilot.app.notice = lambda line, severity="information": said.append(line)
            remember(pilot.app)
            await pilot.press("m")

            await pilot.press("d", "d")
            await pilot.pause()

            assert any("not connected" in line for line in said)

    async def test_a_refresh_keeps_the_marks_it_still_has_facts_for(self, app):
        """Panels are repopulated on their own -- a rollover does it."""
        async with app.run_test() as pilot:
            facts = remember(pilot.app)
            await pilot.press("m")
            await pilot.press("space", "down", "space")

            remember(pilot.app, FACTS[1:])

            assert facts.marked == {"f2"}
            assert facts.cursor <= len(FACTS[1:]) - 1

    async def test_escape_collapses_it_and_takes_the_keys_back(self, app):
        """Focus cannot stay on a list CSS has stopped laying out."""
        async with app.run_test() as pilot:
            remember(pilot.app)
            await pilot.press("m")

            await pilot.press("escape")
            await pilot.pause()

            assert not pilot.app.query_one("#panel-memory").has_class("-expanded")
            assert pilot.app.focused.id == "panel-memory"
            assert not pilot.app.query_one("#panel-memory #expanded").display

    async def test_the_hotkeys_still_work_from_inside_the_list(self, app):
        """Its own bindings are three keys; the rest belong to the app."""
        async with app.run_test() as pilot:
            remember(pilot.app)
            await pilot.press("m")

            await pilot.press("t")

            assert pilot.app.query_one("#panel-tools").has_class("-expanded")
            assert not pilot.app.query_one("#panel-memory").has_class("-expanded")


class TestFactListRendering:
    """It draws its own window, so the drawing is worth pinning down."""

    async def test_the_footer_holds_the_bottom_line(self, app):
        async with app.run_test(size=(120, 40)) as pilot:
            facts = remember(pilot.app)
            await pilot.press("m")
            await pilot.pause()

            lines = facts.render().plain.splitlines()

            assert len(lines) == facts.size.height
            assert "d forget" in lines[-1]

    async def test_a_long_fact_is_cropped_rather_than_folded(self, app):
        """Folded, it pushed everything under it down and the footer off."""
        async with app.run_test(size=(120, 40)) as pilot:
            facts = remember(
                pilot.app,
                [{"id": "f1", "attribute": "routine", "value": "coffee " * 30, "active": True}],
            )
            await pilot.press("m")
            await pilot.pause()

            lines = facts.render().plain.splitlines()

            assert all(len(line) <= facts.size.width for line in lines)
            assert "d forget" in lines[-1]

    async def test_the_window_follows_the_cursor_off_the_bottom(self, app):
        async with app.run_test(size=(120, 40)) as pilot:
            many = [
                {"id": f"f{index}", "attribute": f"a{index}", "value": "x", "active": True}
                for index in range(200)
            ]
            facts = remember(pilot.app, many)
            await pilot.press("m")
            await pilot.press(*(["down"] * 199))
            await pilot.pause()

            lines = facts.render().plain.splitlines()

            assert " [ ] a199 = x" in lines[-2]
            assert " [ ] a0 = x" not in lines[0]

    async def test_an_inactive_fact_says_so(self, app):
        """Superseded facts are still in the store; forgetting one is the point."""
        async with app.run_test(size=(120, 40)) as pilot:
            facts = remember(
                pilot.app,
                [{"id": "f1", "attribute": "old_job", "value": "barista", "active": False}],
            )
            await pilot.press("m")
            await pilot.pause()

            assert "(inactive)" in facts.render().plain

    async def test_an_empty_store_says_so(self, app):
        async with app.run_test(size=(120, 40)) as pilot:
            facts = remember(pilot.app, [])
            await pilot.press("m")
            await pilot.pause()

            assert "no facts" in facts.render().plain


AGENTS = [
    {
        "key": "conversational",
        "title": "conversational",
        "note": "",
        "tools": [
            {"name": "escalate", "description": "Think harder.", "enabled": True},
            {"name": "get_current_time", "description": "What time it is.", "enabled": True},
            {"name": "read_workspace_file", "description": "Read a file.", "enabled": False},
        ],
    },
    {
        "key": "deep",
        "title": "deep think",
        "note": "",
        "tools": [{"name": "read_workspace_file", "description": "Read a file.", "enabled": True}],
    },
    {"key": "coding", "title": "coding", "note": "not implemented yet", "tools": []},
]


def equip(app, agents=AGENTS) -> ToolList:
    """Fill the tools panel as a `refresh_panels` from a live MINUS would."""
    app.query_one(ToolsPanel).update(agents)
    return app.query_one(ToolList)


class TestToolsPanel:
    """The tools panel's own keys: an agent per tab, a switch per tool."""

    async def test_it_opens_on_the_conversational_agent(self, app):
        async with app.run_test(size=(120, 40)) as pilot:
            tools = equip(pilot.app)
            await pilot.press("t")
            await pilot.pause()

            assert tools.agent()["key"] == "conversational"
            assert pilot.app.focused is tools

    async def test_right_moves_to_the_deep_agent(self, app):
        async with app.run_test(size=(120, 40)) as pilot:
            tools = equip(pilot.app)
            await pilot.press("t")

            await pilot.press("right")
            await pilot.pause()

            assert tools.agent()["key"] == "deep"
            assert "read_workspace_file" in tools.render().plain

    async def test_left_wraps_round_to_the_coding_agent(self, app):
        """The tabs cycle, exactly as the viewer's do."""
        async with app.run_test(size=(120, 40)) as pilot:
            tools = equip(pilot.app)
            await pilot.press("t")

            await pilot.press("left")
            await pilot.pause()

            assert tools.agent()["key"] == "coding"

    async def test_the_agent_with_no_tools_says_why(self, app):
        async with app.run_test(size=(120, 40)) as pilot:
            tools = equip(pilot.app)
            await pilot.press("t")

            await pilot.press("left")
            await pilot.pause()

            assert "not implemented yet" in tools.render().plain

    async def test_the_arrows_do_not_reach_the_viewer(self, app):
        """The list owns all four, which is what keeps them off the pane behind."""
        async with app.run_test(size=(120, 40)) as pilot:
            equip(pilot.app)
            await pilot.press("t")
            viewer = pilot.app.query_one(ViewerPane)
            before = viewer.index

            await pilot.press("left", "right", "up", "down")
            await pilot.pause()

            assert viewer.index == before

    async def test_space_switches_the_tool_under_the_cursor_off(self, connected):
        switched = []

        async with connected.run_test(size=(120, 40)) as pilot:
            pilot.app.switch_tool = lambda *args: switched.append(args)
            equip(pilot.app)
            await pilot.press("t")

            await pilot.press("space")
            await pilot.pause()

            assert switched == [("conversational", "escalate", False)]

    async def test_space_switches_a_switched_off_tool_back_on(self, connected):
        switched = []

        async with connected.run_test(size=(120, 40)) as pilot:
            pilot.app.switch_tool = lambda *args: switched.append(args)
            equip(pilot.app)
            await pilot.press("t")

            await pilot.press("down", "down", "space")
            await pilot.pause()

            assert switched == [("conversational", "read_workspace_file", True)]

    async def test_it_switches_the_tool_of_the_agent_that_is_showing(self, connected):
        """The same tool is in both tiers, and the two are switched separately."""
        switched = []

        async with connected.run_test(size=(120, 40)) as pilot:
            pilot.app.switch_tool = lambda *args: switched.append(args)
            equip(pilot.app)
            await pilot.press("t")

            await pilot.press("right", "space")
            await pilot.pause()

            assert switched == [("deep", "read_workspace_file", False)]

    async def test_the_checkbox_flips_without_waiting_for_the_socket(self, connected):
        """A key that looked dead until a round trip came back would read as broken."""
        async with connected.run_test(size=(120, 40)) as pilot:
            pilot.app.switch_tool = lambda *args: None
            tools = equip(pilot.app)
            await pilot.press("t")

            await pilot.press("space")
            await pilot.pause()

            assert tools.current()["enabled"] is False

    async def test_an_agent_with_no_tools_has_nothing_to_switch(self, connected):
        switched = []

        async with connected.run_test(size=(120, 40)) as pilot:
            pilot.app.switch_tool = lambda *args: switched.append(args)
            equip(pilot.app)
            await pilot.press("t")

            await pilot.press("left", "space")
            await pilot.pause()

            assert switched == []

    async def test_it_says_so_when_nothing_is_listening(self, app):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            said = []
            pilot.app.notice = lambda line, severity="information": said.append(line)
            tools = equip(pilot.app)
            await pilot.press("t")

            await pilot.press("space")
            await pilot.pause()

            assert any("not connected" in line for line in said)
            # And the optimistic flip is put back, since nothing took it.
            assert tools.current()["enabled"] is True

    async def test_a_refresh_keeps_the_reader_where_they_were(self, app):
        """Panels are repopulated after every toggle, and on their own."""
        async with app.run_test(size=(120, 40)) as pilot:
            tools = equip(pilot.app)
            await pilot.press("t")
            await pilot.press("right")

            equip(pilot.app)
            await pilot.pause()

            assert tools.agent()["key"] == "deep"

    async def test_the_hotkeys_still_work_from_inside_the_list(self, app):
        async with app.run_test(size=(120, 40)) as pilot:
            equip(pilot.app)
            await pilot.press("t")

            await pilot.press("m")

            assert pilot.app.query_one("#panel-memory").has_class("-expanded")
            assert not pilot.app.query_one("#panel-tools").has_class("-expanded")


class TestToolsPanelRendering:
    async def test_the_summary_counts_what_is_actually_on(self, app):
        async with app.run_test(size=(120, 40)) as pilot:
            equip(pilot.app)

            summary = pilot.app.query_one(ToolsPanel).summary()

            assert "2/3 on" in summary
            assert "not implemented yet" in summary

    async def test_without_an_assistant_there_is_nothing_to_show(self, app):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("t")
            await pilot.pause()

            assert "no data" in pilot.app.query_one(ToolsPanel).summary()
            assert "no data" in pilot.app.query_one(ToolList).render().plain

    async def test_the_tab_bar_shouts_the_agent_that_is_showing(self, app):
        async with app.run_test(size=(120, 40)) as pilot:
            tools = equip(pilot.app)
            await pilot.press("t")
            await pilot.pause()

            header = tools.render().plain.splitlines()[0]

            assert "CONVERSATIONAL" in header
            assert "deep think" in header

    async def test_a_narrow_panel_still_says_which_agent_is_showing(self, app):
        """Cropping the full bar would drop the far tab -- where the reader is."""
        async with app.run_test(size=(60, 20)) as pilot:
            tools = equip(pilot.app)
            await pilot.press("t")
            await pilot.press("left")
            await pilot.pause()

            header = tools.render().plain.splitlines()[0]

            assert "CODING" in header
            assert "(3/3)" in header

    async def test_the_footer_holds_the_bottom_line(self, app):
        async with app.run_test(size=(120, 40)) as pilot:
            tools = equip(pilot.app)
            await pilot.press("t")
            await pilot.pause()

            lines = tools.render().plain.splitlines()

            assert len(lines) == tools.size.height
            assert "space on/off" in lines[-1]

    async def test_the_cursor_row_is_written_out_underneath(self, app):
        """The rows are names alone; the description has to go somewhere."""
        async with app.run_test(size=(120, 40)) as pilot:
            tools = equip(pilot.app)
            await pilot.press("t")
            await pilot.pause()

            lines = tools.render().plain.splitlines()

            assert "Think harder." in lines[-3]

    async def test_a_long_row_is_cropped_rather_than_folded(self, app):
        async with app.run_test(size=(120, 40)) as pilot:
            tools = equip(
                pilot.app,
                [
                    {
                        "key": "conversational",
                        "title": "conversational",
                        "note": "",
                        "tools": [{"name": "x" * 300, "description": "y " * 300, "enabled": True}],
                    }
                ],
            )
            await pilot.press("t")
            await pilot.pause()

            lines = tools.render().plain.splitlines()

            assert all(len(line) <= tools.size.width for line in lines)
            assert "space on/off" in lines[-1]

    async def test_an_older_assistant_sends_a_flat_list(self, app):
        """It answered with the conversational tier's tools and nothing else."""
        async with app.run_test(size=(120, 40)) as pilot:
            tools = equip(
                pilot.app,
                [{"name": "get_current_time", "description": "What time it is."}],
            )
            await pilot.press("t")
            await pilot.pause()

            assert tools.agent()["key"] == "conversational"
            assert tools.current()["enabled"] is True
            assert "1/1 on" in pilot.app.query_one(ToolsPanel).summary()


FOLDERED = [
    {
        "key": "conversational",
        "title": "conversational",
        "note": "",
        "tools": [
            {"name": "escalate", "description": "Think harder.", "enabled": True},
            {
                "name": "add_google_event",
                "description": "Add an event.",
                "enabled": True,
                "category": "google calendar",
            },
            {
                "name": "delete_google_event",
                "description": "Delete an event.",
                "enabled": False,
                "category": "google calendar",
            },
            {
                "name": "read_workspace_file",
                "description": "Read a file.",
                "enabled": True,
                "category": "files",
            },
        ],
    },
    {
        "key": "deep",
        "title": "deep think",
        "note": "",
        "tools": [
            {
                "name": "read_workspace_file",
                "description": "Read a file.",
                "enabled": True,
                "category": "files",
            }
        ],
    },
]


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


class TestToolFolders:
    """The categories a tool registers with, drawn as folders that fold."""

    async def test_tools_are_grouped_under_their_category(self, app):
        async with app.run_test(size=(120, 40)) as pilot:
            tools = equip(pilot.app, FOLDERED)
            await pilot.press("t")
            await pilot.pause()

            assert [row.key for row in tools.rows] == [
                "folder:files",
                "tool:read_workspace_file",
                "folder:google calendar",
                "tool:add_google_event",
                "tool:delete_google_event",
                # Uncategorised, so under no heading -- and last, where it
                # cannot read as belonging to the folder above it.
                "tool:escalate",
            ]

    async def test_a_folder_says_how_many_of_its_tools_are_on(self, app):
        async with app.run_test(size=(120, 40)) as pilot:
            tools = equip(pilot.app, FOLDERED)
            await pilot.press("t")
            await pilot.pause()

            assert "google calendar  1/2 on" in tools.render().plain

    async def test_a_folder_indents_what_is_inside_it(self, app):
        """The indent is what says the row belongs to the heading above it."""
        async with app.run_test(size=(120, 40)) as pilot:
            tools = equip(pilot.app, FOLDERED)
            await pilot.press("t")
            await pilot.pause()

            drawn = tools.render().plain.splitlines()
            folder = next(line for line in drawn if "▾ files" in line)
            inside = next(line for line in drawn if "read_workspace_file" in line)
            loose = next(line for line in drawn if "escalate" in line and "[" in line)

            assert _indent(inside) > _indent(folder)
            # And the uncategorised one is not indented at all: it is in no
            # folder, so it hangs at the left with the headings.
            assert _indent(loose) == _indent(folder)

    async def test_space_closes_the_folder_under_the_cursor(self, app):
        async with app.run_test(size=(120, 40)) as pilot:
            tools = equip(pilot.app, FOLDERED)
            await pilot.press("t")

            await pilot.press("space")
            await pilot.pause()

            assert "read_workspace_file" not in tools.render().plain
            assert "▸ files" in tools.render().plain

    async def test_space_opens_it_again(self, app):
        async with app.run_test(size=(120, 40)) as pilot:
            tools = equip(pilot.app, FOLDERED)
            await pilot.press("t")

            await pilot.press("space", "space")
            await pilot.pause()

            assert "read_workspace_file" in tools.render().plain

    async def test_tab_folds_it_too(self, app):
        """Tab is the screen's focus key, and the list takes it while focused."""
        async with app.run_test(size=(120, 40)) as pilot:
            tools = equip(pilot.app, FOLDERED)
            await pilot.press("t")

            await pilot.press("tab")
            await pilot.pause()

            assert "read_workspace_file" not in tools.render().plain
            assert pilot.app.focused is tools

    async def test_tab_on_a_tool_closes_the_folder_holding_it(self, app):
        """And leaves the cursor on the heading, not on a row no longer drawn."""
        async with app.run_test(size=(120, 40)) as pilot:
            tools = equip(pilot.app, FOLDERED)
            await pilot.press("t")

            await pilot.press("down", "tab")
            await pilot.pause()

            assert "read_workspace_file" not in tools.render().plain
            assert tools.current_row().key == "folder:files"

    async def test_tab_on_an_uncategorised_tool_has_nothing_to_fold(self, app):
        async with app.run_test(size=(120, 40)) as pilot:
            tools = equip(pilot.app, FOLDERED)
            await pilot.press("t")
            await pilot.press("down", "down", "down", "down", "down")
            await pilot.pause()
            assert tools.current()["name"] == "escalate"

            await pilot.press("tab")
            await pilot.pause()

            assert tools.current()["name"] == "escalate"

    async def test_a_folder_row_has_no_switch(self, connected):
        """Space means fold on a heading, so nothing is sent for one."""
        switched = []

        async with connected.run_test(size=(120, 40)) as pilot:
            pilot.app.switch_tool = lambda *args: switched.append(args)
            equip(pilot.app, FOLDERED)
            await pilot.press("t")

            await pilot.press("space")
            await pilot.pause()

            assert switched == []

    async def test_a_tool_inside_a_folder_still_switches(self, connected):
        switched = []

        async with connected.run_test(size=(120, 40)) as pilot:
            pilot.app.switch_tool = lambda *args: switched.append(args)
            equip(pilot.app, FOLDERED)
            await pilot.press("t")

            await pilot.press("down", "space")
            await pilot.pause()

            assert switched == [("conversational", "read_workspace_file", False)]

    async def test_a_closed_folder_stays_closed_across_a_refresh(self, app):
        """Panels are repopulated after every toggle and on their own timer."""
        async with app.run_test(size=(120, 40)) as pilot:
            tools = equip(pilot.app, FOLDERED)
            await pilot.press("t")
            await pilot.press("space")

            equip(pilot.app, FOLDERED)
            await pilot.pause()

            assert "read_workspace_file" not in tools.render().plain
            assert tools.current_row().key == "folder:files"

    async def test_folding_is_per_agent(self, app):
        """The same folder is in both tiers; closing one leaves the other open."""
        async with app.run_test(size=(120, 40)) as pilot:
            tools = equip(pilot.app, FOLDERED)
            await pilot.press("t")
            await pilot.press("space")

            await pilot.press("right")
            await pilot.pause()

            assert "read_workspace_file" in tools.render().plain

    async def test_a_list_longer_than_the_panel_scrolls(self, app):
        """The window follows the cursor rather than running off the bottom."""
        many = [
            {
                "key": "conversational",
                "title": "conversational",
                "note": "",
                "tools": [
                    {
                        "name": f"tool_{index:02d}",
                        "description": "A tool.",
                        "enabled": True,
                        "category": "google calendar",
                    }
                    for index in range(40)
                ],
            }
        ]

        async with app.run_test(size=(80, 24)) as pilot:
            tools = equip(pilot.app, many)
            await pilot.press("t")
            await pilot.pause()
            assert "tool_39" not in tools.render().plain

            await pilot.press(*(["down"] * 40))
            await pilot.pause()

            drawn = tools.render().plain
            assert "tool_39" in drawn
            assert "tool_00" not in drawn
            assert len(drawn.splitlines()) == tools.size.height

    async def test_an_agent_listing_no_categories_draws_no_folders(self, app):
        """The panel before there were categories, and an older assistant now."""
        async with app.run_test(size=(120, 40)) as pilot:
            tools = equip(pilot.app)
            await pilot.press("t")
            await pilot.pause()

            assert all(not row.is_folder for row in tools.rows)


CONFIG = {
    "values": {
        "chat_model": "openai/gpt-oss-20b:nitro",
        "deep_model": "deepseek/deepseek-v4-flash-0731:nitro",
        "tts_voice": "am_puck",
        "tts_speed": 1.0,
        # The one live setting here with no menu behind it: what the free-text
        # editor is exercised through, now that the models and the voice open
        # a list instead.
        "max_tool_rounds": 7,
        "stt_model": "small.en",
        "embedding_dim": 384,
        "console_log_level": "INFO",
    },
    "live": {
        "chat_model": "openai/gpt-oss-20b:nitro",
        "deep_model": "deepseek/deepseek-v4-flash-0731:nitro",
        "tts_voice": "am_puck",
        "tts_speed": 1.0,
        "max_tool_rounds": 7,
    },
    "restart_required": {"stt_model": "small.en"},
    "blocked": {"embedding_dim": "The vector dimension is fixed when the table is created."},
    "not_applicable": {"console_log_level": "There is no console handler under a service."},
    "secrets": ["openrouter_api_key"],
}


def configure(app, described=CONFIG) -> SettingList:
    """Fill the management panel as a `refresh_panels` from a live MINUS would."""
    app.query_one(ManagementPanel).update(described)
    return app.query_one(SettingList)


def names(settings: SettingList) -> list[str]:
    return [row.name for row in settings.rows if isinstance(row, Setting)]


async def change(pilot, name: str) -> SettingList:
    """Put the cursor on one setting and press enter, whatever that opens."""
    settings = pilot.app.query_one(SettingList)
    settings.cursor = settings.index_of(name)
    await pilot.press("enter")
    await pilot.pause()
    return settings


class TestSettingRows:
    """What `get_config` looks like once it is a list."""

    def test_the_live_settings_come_first_and_can_be_edited(self):
        rows = build_setting_rows(CONFIG)

        first = next(row for row in rows if isinstance(row, Setting))
        assert first.name == "chat_model"
        assert first.value == "openai/gpt-oss-20b:nitro"
        assert first.editable

    def test_a_restart_required_setting_is_still_editable(self):
        """It is persisted; what it cannot do is take effect now, and it says so."""
        rows = {row.name: row for row in build_setting_rows(CONFIG) if isinstance(row, Setting)}

        assert rows["stt_model"].editable
        assert "restart" in rows["stt_model"].note

    def test_a_blocked_setting_carries_its_reason_instead(self):
        rows = {row.name: row for row in build_setting_rows(CONFIG) if isinstance(row, Setting)}

        assert not rows["embedding_dim"].editable
        assert rows["embedding_dim"].value == "384"
        assert "vector dimension" in rows["embedding_dim"].note

    def test_a_secret_is_listed_without_its_value(self):
        """That it exists is worth showing; what it is, is not ours to show."""
        rows = {row.name: row for row in build_setting_rows(CONFIG) if isinstance(row, Setting)}

        assert "openrouter_api_key" in rows
        assert "sk" not in rows["openrouter_api_key"].value
        assert not rows["openrouter_api_key"].editable

    def test_each_section_is_introduced(self):
        headings = [row.text for row in build_setting_rows(CONFIG) if isinstance(row, Heading)]

        assert headings == ["live", "restart required", "cannot be changed here"]

    def test_nothing_at_all_is_an_empty_list(self):
        assert build_setting_rows(None) == []

    def test_every_field_of_config_py_is_listed(self):
        """The panel is the whole file, or a reader is left wondering what is missing."""
        described = ConfigController(Settings(), {}).describe()

        listed = {row.name for row in build_setting_rows(described) if isinstance(row, Setting)}

        assert listed == set(Settings.model_fields)


class TestDescribeChange:
    """Three genuinely different answers; saying "done" to all three lies twice."""

    def test_an_applied_change_says_so(self):
        line, severity = describe_change("tts_voice", {"applied": ["tts_voice"], "persisted": []})

        assert "applied" in line
        assert severity == "information"

    def test_a_persisted_change_mentions_the_file(self):
        result = {"applied": ["tts_voice"], "persisted": ["MINUS_TTS_VOICE"]}

        assert ".env" in describe_change("tts_voice", result)[0]

    def test_a_restart_required_change_does_not_claim_to_have_applied(self):
        line, severity = describe_change("stt_model", {"restart_required": ["stt_model"]})

        assert "restart" in line
        assert severity == "warning"

    def test_a_refusal_carries_the_reason(self):
        result = {"rejected": {"embedding_dim": "would invalidate every embedding"}}

        line, severity = describe_change("embedding_dim", result)

        assert "would invalidate every embedding" in line
        assert severity == "error"


class TestManagementPanel:
    """The expanded panel: config.py, editable, over the socket."""

    async def test_expanding_hands_the_keys_to_the_list(self, app):
        async with app.run_test() as pilot:
            configure(pilot.app)

            await pilot.press("g")

            assert pilot.app.focused is pilot.app.query_one(SettingList)

    async def test_it_lists_every_setting_it_was_given(self, app):
        async with app.run_test() as pilot:
            settings = configure(pilot.app)

            assert set(names(settings)) == set(CONFIG["values"]) | {"openrouter_api_key"}

    async def test_the_arrows_step_over_the_headings(self, app):
        """There is nothing to do to a heading, so the cursor never sits on one."""
        async with app.run_test(size=(120, 40)) as pilot:
            settings = configure(pilot.app)
            await pilot.press("g")

            for _ in range(len(settings.rows) + 2):
                assert isinstance(settings.rows[settings.cursor], Setting)
                await pilot.press("down")

    async def test_the_cursor_stops_at_both_ends(self, app):
        async with app.run_test(size=(120, 40)) as pilot:
            settings = configure(pilot.app)
            await pilot.press("g")

            await pilot.press("up")
            assert names(settings)[0] == "chat_model"
            await pilot.press(*(["down"] * (len(settings.rows) + 3)))
            assert settings.current().name == "openrouter_api_key"

    async def test_enter_opens_the_editor_on_the_value_that_is_set(self, app):
        async with app.run_test(size=(120, 40)) as pilot:
            configure(pilot.app)
            await pilot.press("g")

            await change(pilot, "max_tool_rounds")

            editor = pilot.app.query_one("#setting-editor", Input)
            assert pilot.app.focused is editor
            assert editor.value == "7"
            assert editor.display

    async def test_submitting_sends_the_new_value(self, connected):
        async with connected.run_test(size=(120, 40)) as pilot:
            changed = []
            pilot.app.apply_setting = lambda name, value: changed.append((name, value))
            configure(pilot.app)
            await pilot.press("g")
            await change(pilot, "max_tool_rounds")

            pilot.app.query_one("#setting-editor", Input).value = "9"
            await pilot.press("enter")
            await pilot.pause()

            assert changed == [("max_tool_rounds", "9")]

    async def test_the_value_is_not_also_spoken(self, connected):
        """The app says any submitted Input out loud; this one must not reach it."""
        sent = []

        async with connected.run_test(size=(120, 40)) as pilot:
            pilot.app.send = lambda command, **params: sent.append(command)
            pilot.app.apply_setting = lambda name, value: None
            configure(pilot.app)
            await pilot.press("g")
            await change(pilot, "max_tool_rounds")

            await pilot.press("enter")
            await pilot.pause()

            assert sent == []

    async def test_applying_puts_the_editor_away(self, connected):
        async with connected.run_test(size=(120, 40)) as pilot:
            pilot.app.apply_setting = lambda name, value: None
            settings = configure(pilot.app)
            await pilot.press("g")
            await change(pilot, "max_tool_rounds")

            await pilot.press("enter")
            await pilot.pause()

            assert not pilot.app.query_one("#setting-editor").display
            assert pilot.app.focused is settings

    async def test_escape_cancels_the_edit_and_leaves_the_panel_open(self, app):
        async with app.run_test(size=(120, 40)) as pilot:
            settings = configure(pilot.app)
            await pilot.press("g")
            await change(pilot, "max_tool_rounds")

            await pilot.press("escape")
            await pilot.pause()

            assert not pilot.app.query_one("#panel-management").has_class("-editing")
            assert pilot.app.query_one("#panel-management").has_class("-expanded")
            assert pilot.app.focused is settings

    async def test_a_cancelled_edit_changes_nothing(self, connected):
        async with connected.run_test(size=(120, 40)) as pilot:
            changed = []
            pilot.app.apply_setting = lambda name, value: changed.append((name, value))
            configure(pilot.app)
            await pilot.press("g")
            await change(pilot, "max_tool_rounds")
            pilot.app.query_one("#setting-editor", Input).value = "9"

            await pilot.press("escape")
            await pilot.pause()

            assert changed == []

    async def test_the_next_escape_collapses_the_panel(self, app):
        async with app.run_test(size=(120, 40)) as pilot:
            configure(pilot.app)
            await pilot.press("g")
            await pilot.press("enter")

            await pilot.press("escape")
            await pilot.press("escape")
            await pilot.pause()

            assert not pilot.app.query_one("#panel-management").has_class("-expanded")

    async def test_the_hotkeys_are_typed_rather_than_fired_while_editing(self, app):
        """A value is text: `m` in the editor is the letter, not the memory panel."""
        async with app.run_test(size=(120, 40)) as pilot:
            configure(pilot.app)
            await pilot.press("g")
            await change(pilot, "max_tool_rounds")

            await pilot.press("m")
            await pilot.pause()

            assert not pilot.app.query_one("#panel-memory").has_class("-expanded")
            assert pilot.app.query_one("#setting-editor", Input).value.endswith("m")

    async def test_collapsing_while_editing_puts_the_editor_away(self, app):
        """Hidden with a half-typed value in it, it would come back holding it."""
        async with app.run_test(size=(120, 40)) as pilot:
            configure(pilot.app)
            await pilot.press("g")
            await change(pilot, "max_tool_rounds")

            pilot.app.action_expand("panel-management")
            await pilot.pause()

            assert not pilot.app.query_one("#panel-management").has_class("-editing")
            assert not pilot.app.query_one("#panel-management").has_class("-expanded")

    async def test_enter_on_a_setting_that_cannot_change_says_why(self, app):
        async with app.run_test(size=(120, 40)) as pilot:
            said = []
            pilot.app.notice = lambda line, severity="information": said.append(line)
            settings = configure(pilot.app)
            await pilot.press("g")
            settings.cursor = settings.index_of("embedding_dim")

            await pilot.press("enter")
            await pilot.pause()

            assert any("vector dimension" in line for line in said)
            assert not pilot.app.query_one("#panel-management").has_class("-editing")

    async def test_it_says_so_when_nothing_is_listening(self, app):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            said = []
            pilot.app.notice = lambda line, severity="information": said.append(line)
            configure(pilot.app)
            await pilot.press("g")
            await pilot.press("enter")

            await pilot.press("enter")
            await pilot.pause()

            assert any("not connected" in line for line in said)

    async def test_a_refresh_keeps_the_cursor_on_the_same_setting(self, app):
        """Every change repopulates the panel; the cursor must not wander."""
        async with app.run_test(size=(120, 40)) as pilot:
            settings = configure(pilot.app)
            await pilot.press("g")
            await pilot.press("down", "down")
            held = settings.current().name

            configure(pilot.app)

            assert settings.current().name == held

    async def test_the_summary_shows_what_is_set(self, app):
        async with app.run_test() as pilot:
            configure(pilot.app)

            summary = pilot.app.query_one(ManagementPanel).summary()

            assert "openai/gpt-oss-20b:nitro" in summary
            assert "am_puck" in summary
            assert "5 live" in summary

    async def test_without_an_assistant_there_is_nothing_to_show(self, app):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("g")
            await pilot.pause()

            assert "no data" in pilot.app.query_one(ManagementPanel).summary()
            assert "no settings" in pilot.app.query_one(SettingList).render().plain

    async def test_the_hotkeys_still_work_from_inside_the_list(self, app):
        async with app.run_test(size=(120, 40)) as pilot:
            configure(pilot.app)
            await pilot.press("g")

            await pilot.press("t")

            assert pilot.app.query_one("#panel-tools").has_class("-expanded")


class TestChoices:
    """The menus themselves. Data, but data that goes stale silently."""

    def test_only_the_six_fields_that_name_a_thing_have_one(self):
        assert set(CHOICES) == {
            "chat_model",
            "fact_extraction_model",
            "deep_model",
            "tts_voice",
            "stt_model",
            "stt_realtime_model",
        }

    def test_every_menu_is_for_a_real_setting(self):
        """A typo here would be a menu that never opens."""
        assert set(CHOICES) <= set(Settings.model_fields)

    def test_the_defaults_config_py_ships_are_offered(self):
        """Or the menu would not contain the assistant's own starting point."""
        for name in CHOICES:
            assert Settings.model_fields[name].default in choices_for(name), name

    def test_the_models_asked_for_are_in_the_list(self):
        assert "anthropic/claude-sonnet-5" in MODELS
        assert "anthropic/claude-opus-5" in MODELS
        assert "meta-llama/llama-4-maverick" in MODELS

    def test_the_voices_are_the_ones_kokoro_carries(self):
        """Every key of models/voices-v1.0.bin; a name not in it synthesizes nothing."""
        assert len(VOICES) == 54
        assert "am_puck" in VOICES
        assert tuple(sorted(VOICES)) == VOICES

    def test_the_speech_models_are_whisper_sizes(self):
        assert "tiny.en" in STT_MODELS
        assert "large-v3-turbo" in STT_MODELS
        assert len(set(STT_MODELS)) == len(STT_MODELS)

    def test_a_setting_without_a_menu_says_so(self):
        assert choices_for("relevance_threshold") == ()
        assert choices_for("not_a_setting") == ()


class TestChoiceMenus:
    """The six settings that name something out of a set open a menu."""

    async def test_a_model_opens_a_menu_rather_than_the_editor(self, app):
        async with app.run_test(size=(120, 40)) as pilot:
            configure(pilot.app)
            await pilot.press("g")

            await change(pilot, "chat_model")

            menu = pilot.app.query_one(ChoiceList)
            assert pilot.app.focused is menu
            assert menu.display
            assert not pilot.app.query_one("#setting-editor").display

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("chat_model", "anthropic/claude-opus-5"),
            ("deep_model", "anthropic/claude-sonnet-5"),
            ("tts_voice", "af_bella"),
            ("stt_model", "tiny.en"),
        ],
    )
    async def test_each_menu_offers_what_it_should(self, app, name, expected):
        async with app.run_test(size=(120, 40)) as pilot:
            configure(pilot.app)
            await pilot.press("g")

            await change(pilot, name)

            assert expected in pilot.app.query_one(ChoiceList).options

    async def test_the_models_asked_for_are_all_there(self, app):
        async with app.run_test(size=(120, 40)) as pilot:
            configure(pilot.app)
            await pilot.press("g")

            await change(pilot, "chat_model")

            offered = pilot.app.query_one(ChoiceList).options
            assert "anthropic/claude-sonnet-5" in offered
            assert "anthropic/claude-opus-5" in offered
            assert "meta-llama/llama-4-maverick" in offered
            # And what MINUS is running now, which is the point of a menu.
            assert "openai/gpt-oss-20b:nitro" in offered

    async def test_it_opens_on_the_value_that_is_set(self, app):
        """A menu of where you could go, not a list to find yourself in first."""
        async with app.run_test(size=(120, 40)) as pilot:
            configure(pilot.app)
            await pilot.press("g")

            await change(pilot, "tts_voice")

            menu = pilot.app.query_one(ChoiceList)
            assert menu.options[menu.cursor] == "am_puck"

    async def test_a_value_of_its_own_is_still_in_the_menu(self, app):
        """.env may hold any of OpenRouter's four hundred; the menu names five."""
        async with app.run_test(size=(120, 40)) as pilot:
            described = {
                **CONFIG,
                "values": {**CONFIG["values"], "chat_model": "x-ai/grok-4"},
                "live": {**CONFIG["live"], "chat_model": "x-ai/grok-4"},
            }
            configure(pilot.app, described)
            await pilot.press("g")

            await change(pilot, "chat_model")

            menu = pilot.app.query_one(ChoiceList)
            assert menu.options[menu.cursor] == "x-ai/grok-4"

    async def test_choosing_sends_it(self, connected):
        async with connected.run_test(size=(120, 40)) as pilot:
            changed = []
            pilot.app.apply_setting = lambda name, value: changed.append((name, value))
            configure(pilot.app)
            await pilot.press("g")
            await change(pilot, "tts_voice")

            await pilot.press("down", "enter")
            await pilot.pause()

            assert changed == [("tts_voice", "am_santa")]

    async def test_choosing_puts_the_menu_away(self, connected):
        async with connected.run_test(size=(120, 40)) as pilot:
            pilot.app.apply_setting = lambda name, value: None
            settings = configure(pilot.app)
            await pilot.press("g")
            await change(pilot, "tts_voice")

            await pilot.press("enter")
            await pilot.pause()

            assert not pilot.app.query_one("#setting-choices").display
            assert pilot.app.focused is settings

    async def test_escape_cancels_the_menu_and_keeps_the_panel(self, app):
        async with app.run_test(size=(120, 40)) as pilot:
            settings = configure(pilot.app)
            await pilot.press("g")
            await change(pilot, "deep_model")

            await pilot.press("escape")
            await pilot.pause()

            assert not pilot.app.query_one("#setting-choices").display
            assert pilot.app.query_one("#panel-management").has_class("-expanded")
            assert pilot.app.focused is settings

    async def test_a_cancelled_menu_changes_nothing(self, connected):
        async with connected.run_test(size=(120, 40)) as pilot:
            changed = []
            pilot.app.apply_setting = lambda name, value: changed.append((name, value))
            configure(pilot.app)
            await pilot.press("g")
            await change(pilot, "tts_voice")

            await pilot.press("down", "down", "escape")
            await pilot.pause()

            assert changed == []

    async def test_the_last_row_opens_the_editor_instead(self, app):
        """The menu is a shortcut, not a fence."""
        async with app.run_test(size=(120, 40)) as pilot:
            configure(pilot.app)
            await pilot.press("g")
            await change(pilot, "chat_model")
            menu = pilot.app.query_one(ChoiceList)
            menu.cursor = menu.options.index(OTHER)

            await pilot.press("enter")
            await pilot.pause()

            editor = pilot.app.query_one("#setting-editor", Input)
            assert pilot.app.focused is editor
            assert editor.value == "openai/gpt-oss-20b:nitro"
            assert not pilot.app.query_one("#setting-choices").display

    async def test_typing_another_value_sends_that_one(self, connected):
        async with connected.run_test(size=(120, 40)) as pilot:
            changed = []
            pilot.app.apply_setting = lambda name, value: changed.append((name, value))
            configure(pilot.app)
            await pilot.press("g")
            await change(pilot, "chat_model")
            menu = pilot.app.query_one(ChoiceList)
            menu.cursor = menu.options.index(OTHER)
            await pilot.press("enter")

            pilot.app.query_one("#setting-editor", Input).value = "x-ai/grok-4"
            await pilot.press("enter")
            await pilot.pause()

            assert changed == [("chat_model", "x-ai/grok-4")]

    async def test_a_threshold_still_takes_typing(self, app):
        """There is no set of sensible values for one, only a range."""
        async with app.run_test(size=(120, 40)) as pilot:
            configure(pilot.app)
            await pilot.press("g")

            await change(pilot, "tts_speed")

            assert not pilot.app.query_one("#setting-choices").display
            assert pilot.app.query_one("#setting-editor").display

    async def test_the_cursor_stops_at_both_ends(self, app):
        async with app.run_test(size=(120, 40)) as pilot:
            configure(pilot.app)
            await pilot.press("g")
            await change(pilot, "stt_model")
            menu = pilot.app.query_one(ChoiceList)

            await pilot.press(*(["up"] * (len(menu.options) + 2)))
            assert menu.cursor == 0
            await pilot.press(*(["down"] * (len(menu.options) + 2)))
            assert menu.cursor == len(menu.options) - 1


class TestChoiceListRendering:
    async def test_it_names_the_setting_it_is_a_menu_for(self, app):
        async with app.run_test(size=(120, 40)) as pilot:
            configure(pilot.app)
            await pilot.press("g")
            await change(pilot, "tts_voice")

            drawn = pilot.app.query_one(ChoiceList).render().plain

            assert "tts_voice" in drawn.splitlines()[0]
            assert "enter choose" in drawn.splitlines()[-1]

    async def test_the_value_that_is_set_is_marked(self, app):
        """The star says where you are; the cursor says where you would go."""
        async with app.run_test(size=(120, 40)) as pilot:
            configure(pilot.app)
            await pilot.press("g")
            await change(pilot, "tts_voice")
            menu = pilot.app.query_one(ChoiceList)

            await pilot.press("down")
            drawn = [line for line in menu.render().plain.splitlines() if line.strip()]

            assert any(line.startswith("  * am_puck") for line in drawn)

    async def test_it_fills_no_more_than_its_own_height(self, app):
        """Fifty-four voices in a panel that is twenty rows tall."""
        async with app.run_test(size=(80, 24)) as pilot:
            configure(pilot.app)
            await pilot.press("g")
            await change(pilot, "tts_voice")
            menu = pilot.app.query_one(ChoiceList)

            lines = menu.render().plain.splitlines()

            assert len(lines) == menu.size.height
            assert all(len(line) <= menu.size.width for line in lines)

    async def test_the_window_follows_the_cursor(self, app):
        async with app.run_test(size=(80, 24)) as pilot:
            configure(pilot.app)
            await pilot.press("g")
            await change(pilot, "tts_voice")
            menu = pilot.app.query_one(ChoiceList)

            await pilot.press(*(["down"] * (len(menu.options) - 1)))
            await pilot.pause()

            assert OTHER in menu.render().plain
            assert "af_alloy" not in menu.render().plain

    async def test_the_list_is_still_visible_behind_it(self, app):
        """Which setting is being changed stays on screen above the menu."""
        async with app.run_test(size=(120, 40)) as pilot:
            settings = configure(pilot.app)
            await pilot.press("g")
            await change(pilot, "tts_voice")

            assert settings.display
            assert settings.size.height > 0


class TestSettingListRendering:
    """It draws its own window, so the drawing is worth pinning down."""

    async def test_the_hint_holds_the_bottom_line(self, app):
        async with app.run_test(size=(120, 40)) as pilot:
            settings = configure(pilot.app)
            await pilot.press("g")
            await pilot.pause()

            lines = settings.render().plain.splitlines()

            assert len(lines) == settings.size.height
            assert "enter edit" in lines[-1]

    async def test_the_row_under_the_cursor_is_written_out_in_full(self, app):
        """The column is half a terminal wide; a model name is most of that."""
        async with app.run_test(size=(120, 40)) as pilot:
            settings = configure(pilot.app)
            await pilot.press("g")
            settings.cursor = settings.index_of("deep_model")
            await pilot.pause()

            detail = " ".join(settings.render().plain.splitlines()[-3:-1])

            assert "deepseek/deepseek-v4-flash-0731:nitro" in detail
            assert "applies immediately" in detail

    async def test_a_blocked_row_explains_itself_there_too(self, app):
        async with app.run_test(size=(120, 40)) as pilot:
            settings = configure(pilot.app)
            await pilot.press("g")
            settings.cursor = settings.index_of("console_log_level")
            await pilot.pause()

            detail = " ".join(settings.render().plain.splitlines()[-3:-1])

            assert "no console handler" in detail

    async def test_no_row_is_folded_onto_a_second_line(self, app):
        async with app.run_test(size=(80, 24)) as pilot:
            settings = configure(pilot.app)
            await pilot.press("g")
            await pilot.pause()

            lines = settings.render().plain.splitlines()

            assert len(lines) == settings.size.height
            assert all(len(line) <= settings.size.width for line in lines)

    async def test_the_window_follows_the_cursor_off_the_bottom(self, app):
        async with app.run_test(size=(120, 16)) as pilot:
            settings = configure(pilot.app)
            await pilot.press("g")
            await pilot.press(*(["down"] * len(settings.rows)))
            await pilot.pause()

            drawn = settings.render().plain

            assert "openrouter_api_key" in drawn
            assert "chat_model" not in drawn

    async def test_and_back_up_again(self, app):
        async with app.run_test(size=(120, 16)) as pilot:
            settings = configure(pilot.app)
            await pilot.press("g")
            await pilot.press(*(["down"] * len(settings.rows)))
            await pilot.press(*(["up"] * len(settings.rows)))
            await pilot.pause()

            assert "chat_model" in settings.render().plain

    async def test_the_editing_hint_replaces_the_moving_one(self, app):
        async with app.run_test(size=(120, 40)) as pilot:
            settings = configure(pilot.app)
            await pilot.press("g")

            await pilot.press("enter")
            await pilot.pause()

            assert "esc cancel" in settings.render().plain.splitlines()[-1]


class TakeoverPanel(Panel):
    """A panel that wants its expanded view to have the frame to itself."""

    title = "takeover"
    hotkey = "z"
    takeover = True

    def __init__(self) -> None:
        super().__init__("panel-takeover")


class TestTakeover:
    async def test_the_summary_steps_aside_while_it_is_expanded(self, app):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.app.query_one("#right").mount(TakeoverPanel())
            await pilot.pause()
            assert pilot.app.query_one("#panel-takeover #summary").display

            pilot.app.action_expand("panel-takeover")
            await pilot.pause()

            assert not pilot.app.query_one("#panel-takeover #summary").display
            assert pilot.app.query_one("#panel-takeover #expanded").display

    async def test_it_comes_back_when_the_panel_collapses(self, app):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.app.query_one("#right").mount(TakeoverPanel())
            await pilot.pause()

            pilot.app.action_expand("panel-takeover")
            await pilot.pause()
            pilot.app.action_expand("panel-takeover")
            await pilot.pause()

            assert pilot.app.query_one("#panel-takeover #summary").display

    async def test_a_panel_that_did_not_ask_keeps_its_summary(self, app):
        """The memory panel's counts stay above its fact list."""
        async with app.run_test(size=(120, 40)) as pilot:
            remember(pilot.app)

            await pilot.press("m")
            await pilot.pause()

            assert pilot.app.query_one("#panel-memory #summary").display


class TestEscapeLadder:
    async def test_escape_out_of_the_input_returns_to_the_panel(self, app):
        async with app.run_test() as pilot:
            pilot.app.query_one("#panel-agents", Panel).focus()
            await pilot.pause()
            await pilot.press("i")

            await pilot.press("escape")

            assert pilot.app.focused.id == "panel-agents"

    async def test_a_second_escape_leaves_the_panel_too(self, app):
        async with app.run_test() as pilot:
            pilot.app.query_one("#panel-agents", Panel).focus()
            await pilot.pause()
            await pilot.press("i")

            await pilot.press("escape")
            await pilot.press("escape")

            assert pilot.app.focused is None

    async def test_escape_leaves_the_input_before_collapsing_the_panel(self, app):
        """The other order threw the panel away while you were still typing."""
        async with app.run_test() as pilot:
            await pilot.press("m")
            await pilot.press("i")

            await pilot.press("escape")

            assert focus_within(pilot.app, "panel-memory")
            assert pilot.app.query_one("#panel-memory").has_class("-expanded")

    async def test_the_next_escape_collapses_it(self, app):
        async with app.run_test() as pilot:
            await pilot.press("m")
            await pilot.press("i")

            await pilot.press("escape")
            await pilot.press("escape")

            assert not pilot.app.query_one("#panel-memory").has_class("-expanded")
            # On the panel itself now: the list it was on is no longer laid out.
            assert pilot.app.focused.id == "panel-memory"

    async def test_and_the_third_lets_go(self, app):
        async with app.run_test() as pilot:
            await pilot.press("m")
            await pilot.press("i")

            for _ in range(3):
                await pilot.press("escape")

            assert pilot.app.focused is None

    async def test_escape_from_the_input_with_no_panel_behind_it_holds_nothing(self, app):
        async with app.run_test() as pilot:
            await pilot.press("i")

            await pilot.press("escape")

            assert pilot.app.focused is None


class TestDeepNotePaging:
    async def test_ctrl_arrows_page_between_notes(self, app, notes):
        async with app.run_test() as pilot:
            await pilot.press("3")
            await pilot.pause()
            assert pilot.app.note_index == 0

            await pilot.press("ctrl+down")

            assert pilot.app.note_index == 1

    async def test_the_bare_arrows_no_longer_page(self, app, notes):
        """They belong to the scroller now, which is what makes a long note readable."""
        async with app.run_test() as pilot:
            await pilot.press("3")
            await pilot.press("v")
            await pilot.pause()

            await pilot.press("down")

            assert pilot.app.note_index == 0

    async def test_the_deep_view_is_what_the_arrows_scroll(self, app, notes):
        async with app.run_test() as pilot:
            await pilot.press("3")
            await pilot.press("v")
            await pilot.pause()

            assert pilot.app.focused.id == "view-deep"
            assert pilot.app.focused.max_scroll_y > 0

    async def test_paging_returns_to_the_top_of_the_note(self, app, notes):
        async with app.run_test() as pilot:
            await pilot.press("3")
            await pilot.pause()
            view = pilot.app.query_one("#view-deep")
            view.scroll_to(y=20, animate=False)
            await pilot.pause()

            await pilot.press("ctrl+down")
            await pilot.pause()

            assert view.scroll_offset.y == 0

    async def test_paging_does_nothing_from_another_view(self, app, notes):
        async with app.run_test() as pilot:
            await pilot.press("1")
            await pilot.pause()

            await pilot.press("ctrl+down")

            assert pilot.app.note_index == 0


class TestConsole:
    async def test_it_is_hidden_until_c(self, app):
        async with app.run_test() as pilot:
            assert not pilot.app.query_one("#console").display

            await pilot.press("c")

            assert pilot.app.query_one("#console").display

    async def test_c_focuses_it_so_the_scroll_keys_land(self, app):
        async with app.run_test() as pilot:
            await pilot.press("c")

            assert pilot.app.focused.id == "console-body"

    async def test_c_again_hides_it_and_lets_go(self, app):
        async with app.run_test() as pilot:
            await pilot.press("c")

            await pilot.press("c")

            assert not pilot.app.query_one("#console").display
            assert pilot.app.focused is None

    async def test_escape_closes_it(self, app):
        async with app.run_test() as pilot:
            await pilot.press("c")

            await pilot.press("escape")

            assert not pilot.app.query_one("#console").display

    async def test_it_shows_what_minus_wrote_to_its_own_streams(self, app, tmp_path):
        """The same file-not-socket bargain the log viewer strikes."""
        (tmp_path / "logs/console-20260810-120000-000000-1.log").write_text(
            "Traceback (most recent call last):\n", encoding="utf-8"
        )

        async with app.run_test() as pilot:
            await pilot.press("c")
            await pilot.pause()

            written = pilot.app.query_one("#console-body").lines

            assert any("Traceback" in str(line) for line in written)

    async def test_it_does_not_follow_the_run_log(self, app, tmp_path):
        """Two prefixes, two viewers -- neither should show the other's file."""
        (tmp_path / "logs/run-20260810-120000-000000-1.log").write_text(
            "2026-08-10 12:00:00,000 INFO minus.cli: serving\n", encoding="utf-8"
        )

        async with app.run_test() as pilot:
            await pilot.press("c")
            await pilot.pause()

            written = pilot.app.query_one("#console-body").lines

            assert not any("serving" in str(line) for line in written)


class TestConsoleSpinner:
    """A spinner is one line being redrawn, not one line per frame."""

    def spin(self, tmp_path, frames: bytes) -> None:
        (tmp_path / "logs/console-20260810-120000-000000-1.log").write_bytes(frames)

    async def test_the_frames_do_not_become_lines(self, app, tmp_path):
        self.spin(tmp_path, "\r⠋ speak now\r⠙ speak now\r⠹ speak now".encode())

        async with app.run_test() as pilot:
            await pilot.press("c")
            await pilot.pause()

            assert pilot.app.query_one("#console-body").lines == []

    async def test_the_latest_frame_shows_on_its_own_row(self, app, tmp_path):
        self.spin(tmp_path, "\r⠋ speak now\r⠙ speak now".encode())

        async with app.run_test() as pilot:
            await pilot.press("c")
            await pilot.pause()

            assert live_row(pilot.app) == "⠙ speak now"

    async def test_a_finished_line_moves_into_the_log(self, app, tmp_path):
        self.spin(tmp_path, b"\rworking\rdone\n")

        async with app.run_test() as pilot:
            await pilot.press("c")
            await pilot.pause()

            written = pilot.app.query_one("#console-body").lines
            assert any("done" in str(line) for line in written)
            assert not any("working" in str(line) for line in written)
            assert live_row(pilot.app) == ""


class TestNoticingMinusDied:
    async def test_the_client_closing_marks_it_disconnected(self, app):
        """The reader thread's EOF is the only signal a killed MINUS can give."""
        async with app.run_test() as pilot:
            pilot.app.connected = True

            pilot.app._disconnected("MINUS closed the connection")
            await pilot.pause()

            assert not pilot.app.connected
            assert pilot.app.query_one("#statusbar").has_class("disconnected")

    async def test_the_dead_client_is_let_go_of(self, app):
        """Kept, it left `send` reaching into a connection that is gone."""
        async with app.run_test() as pilot:
            pilot.app.client = object()

            pilot.app._disconnected("gone")

            assert pilot.app.client is None

    async def test_the_callback_survives_a_torn_down_app(self, app):
        """It runs on the client's reader thread, which outlives the widgets."""
        async with app.run_test():
            pass

        app._on_client_closed("gone")  # must not raise


class TestStopKey:
    async def test_s_interrupts(self, app):
        sent = []
        app.send = lambda command, **params: sent.append(command)

        async with app.run_test() as pilot:
            await pilot.press("s")

            assert sent == ["interrupt"]

    async def test_c_no_longer_interrupts(self, app):
        sent = []
        app.send = lambda command, **params: sent.append(command)

        async with app.run_test() as pilot:
            await pilot.press("c")

            assert sent == []


class TestEndConversationKey:
    async def test_e_ends_the_conversation(self, app):
        sent = []
        app.send = lambda command, **params: sent.append(command)

        async with app.run_test() as pilot:
            pilot.app.connected = True

            await pilot.press("e")

            assert sent == ["end_conversation"]

    async def test_it_is_typed_rather_than_fired_while_the_input_has_focus(self, app):
        """It condenses a conversation; typing the letter `e` must not."""
        sent = []
        app.send = lambda command, **params: sent.append(command)

        async with app.run_test() as pilot:
            pilot.app.connected = True
            pilot.app.query_one("#prompt-input").focus()
            await pilot.pause()

            await pilot.press("e")

            assert sent == []
            assert pilot.app.query_one("#prompt-input").value == "e"

    async def test_it_says_so_when_nothing_is_running(self, app):
        sent = []
        app.send = lambda command, **params: sent.append(command)

        async with app.run_test() as pilot:
            await pilot.pause()
            said = []
            pilot.app.notice = lambda line, severity="information": said.append(line)

            await pilot.press("e")

            assert sent == []
            assert any("not connected" in line for line in said)


class TestNoticingARollover:
    """A conversation ending is only ever learned from the snapshot.

    MINUS answers `end_conversation` before it has condensed anything, and a
    silence rolls the conversation over with nobody having asked at all. Both
    show up here, as an id that is not the one from a moment ago.
    """

    def snapshot(self, conversation_id: str) -> dict:
        return {"phase": "listening", "conversation": {"id": conversation_id}}

    async def test_a_new_conversation_id_is_reported(self, app):
        async with app.run_test() as pilot:
            said = []
            pilot.app.refresh_panels = lambda: None
            pilot.app._apply_snapshot(self.snapshot("conv-1"))
            pilot.app.notice = lambda line, severity="information": said.append(line)

            pilot.app._apply_snapshot(self.snapshot("conv-2"))

            assert any("conv-2" in line for line in said)

    async def test_the_first_snapshot_is_not_a_rollover(self, app):
        """Connecting is not something happening; it is finding out what has."""
        async with app.run_test() as pilot:
            said = []
            pilot.app.refresh_panels = lambda: None
            pilot.app.notice = lambda line, severity="information": said.append(line)

            pilot.app._apply_snapshot(self.snapshot("conv-1"))

            assert said == []

    async def test_the_same_conversation_says_nothing(self, app):
        """Snapshots arrive on every phase change; only a change is news."""
        async with app.run_test() as pilot:
            said = []
            pilot.app.refresh_panels = lambda: None
            pilot.app._apply_snapshot(self.snapshot("conv-1"))
            pilot.app.notice = lambda line, severity="information": said.append(line)

            pilot.app._apply_snapshot(self.snapshot("conv-1"))

            assert said == []


class TestDeepTimer:
    """The clock has to run between snapshots, not only when one arrives.

    A snapshot is pushed on state edges, so its `elapsed_seconds` is a reading
    taken when the tier started -- which is why the timer used to stop a second
    or two in. Both readouts count from the wall-clock start time instead.
    """

    def thinking(self, seconds: float) -> dict:
        return {
            "phase": "listening",
            "deep": {
                "in_flight": True,
                "question": "why is this slow",
                # Frozen, exactly as a snapshot from `seconds` ago would be.
                "elapsed_seconds": 0.0,
                "started_at": time.time() - seconds,
            },
        }

    def test_it_counts_from_the_start_time(self):
        assert deep_elapsed(self.thinking(42)["deep"]) == pytest.approx(42, abs=1)

    def test_an_idle_tier_has_no_clock(self):
        assert deep_elapsed({"in_flight": False}) is None
        assert deep_elapsed(None) is None

    def test_an_assistant_too_old_to_send_one_still_shows_something(self):
        assert deep_elapsed({"in_flight": True, "elapsed_seconds": 3.0}) == 3.0

    async def test_the_status_bar_keeps_counting(self, app):
        async with app.run_test() as pilot:
            pilot.app.connected = True
            pilot.app.snapshot = self.thinking(42)

            pilot.app.tick()

            assert "deep 42s" in str(pilot.app.query_one("#statusbar", Static).content)

    async def test_the_agents_panel_keeps_counting(self, app):
        async with app.run_test() as pilot:
            panel = pilot.app.query_one(AgentsPanel)
            pilot.app.connected = True
            pilot.app._apply_snapshot(self.thinking(0))
            pilot.app.snapshot["deep"]["started_at"] = time.time() - 42

            pilot.app.tick()
            await pilot.pause()

            assert "42s" in panel.summary()
            assert "42s" in str(panel.query_one("#summary", Static).content)


class TestAgentsPanel:
    """The question the deep tier is on, in full: it used to be cut at 30."""

    LONG = (
        "why does the dashboard drop its connection whenever the assistant "
        "restarts in the middle of a long answer"
    )

    def thinking(self, question: str) -> dict:
        return {"deep": {"in_flight": True, "question": question, "started_at": time.time()}}

    def test_wrapping_hangs_every_line_under_the_first(self):
        wrapped = wrap_indented("one two three four", 10)

        assert wrapped == "  one two\n  three\n  four"
        assert all(len(line) <= 10 for line in wrapped.splitlines())

    def test_a_word_longer_than_the_column_is_folded_rather_than_dropped(self):
        assert "".join(wrap_indented("abcdefghij", 6).split()) == "abcdefghij"

    async def test_the_whole_question_is_there(self, app):
        async with app.run_test() as pilot:
            panel = pilot.app.query_one(AgentsPanel)

            panel.update(self.thinking(self.LONG))
            await pilot.pause()

            summary = str(panel.query_one("#summary", Static).content)
            assert " ".join(summary.split()[4:]) == self.LONG
            assert len(summary.splitlines()) > 2

    async def test_no_line_is_wider_than_the_panel(self, app):
        async with app.run_test() as pilot:
            panel = pilot.app.query_one(AgentsPanel)

            panel.update(self.thinking(self.LONG))
            await pilot.pause()

            width = panel.content_size.width
            assert all(len(line) <= width for line in panel.summary().splitlines())

    async def test_a_question_the_assistant_did_not_send_leaves_no_blank_line(self, app):
        async with app.run_test() as pilot:
            panel = pilot.app.query_one(AgentsPanel)

            panel.update(self.thinking(""))
            await pilot.pause()

            assert panel.summary().splitlines() == ["  deep tier: thinking 0s"]


class TestPromptPane:
    async def test_there_is_no_echo_box(self, app):
        """What you type shows up in the transcript above; twice was once too many."""
        async with app.run_test() as pilot:
            assert not pilot.app.query("#prompt-echo")


class TestSpeakerColumn:
    def test_the_label_and_the_speech_are_separate_columns(self):
        """Which is what lets a wrap hang under the speech rather than the wall."""
        grid = render_turn("assistant", "It is five.")

        assert len(grid.columns) == 2
        assert grid.columns[0].width == 5

    def test_each_speaker_is_styled_differently(self):
        styles = {
            role: render_turn(role, "x").columns[0].style for role in ("user", "assistant", "tool")
        }

        assert len({str(style) for style in styles.values()}) == 3

    def test_the_speech_itself_is_not_styled_like_the_speaker(self):
        grid = render_turn("user", "hello")

        assert grid.columns[0].style == "bold cyan"
        assert not grid.columns[1].style

    def test_an_unknown_role_still_renders(self):
        assert render_turn("system", "x").columns[0].width == 5


class TestMeters:
    def test_the_bar_is_drawn_in_plain_characters(self):
        """The VT font has no block-drawing glyphs, so neither does this."""
        bar = ascii_bar(50, width=10)

        assert bar == "[#####-----]   50%"
        assert all(character in "[]#- %0123456789" for character in bar)

    def test_an_unknown_value_is_shown_as_unknown(self):
        assert "?" in ascii_bar(None)

    @pytest.mark.parametrize("percent", [0, 100, -5, 150])
    def test_it_never_overflows_its_width(self, percent):
        assert ascii_bar(percent, width=10).count("#") <= 10

    def test_bytes_are_shown_in_units(self):
        assert human_bytes(1536) == "2K"
        assert human_bytes(3 * 1024**3) == "3G"
