"""The dashboard's layout and keys, driven headlessly.

Textual's test pilot needs no terminal, so the widget tree and the key
handling are checked for real. Skipped where the extra is not installed,
matching how test_playback.py guards on sounddevice.
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("textual")

from textual.widgets import Input, Static

from minus.dashboard.app import MinusDashboard
from minus.dashboard.widgets import (
    AgentsPanel,
    FactList,
    MemoryPanel,
    Panel,
    ViewerPane,
    ascii_bar,
    deep_elapsed,
    human_bytes,
    render_turn,
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

            assert pilot.app.focused.id == "panel-tools"
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
