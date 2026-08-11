"""The dashboard's layout and keys, driven headlessly.

Textual's test pilot needs no terminal, so the widget tree and the key
handling are checked for real. Skipped where the extra is not installed,
matching how test_playback.py guards on sounddevice.
"""

from __future__ import annotations

import pytest

pytest.importorskip("textual")

from minus.dashboard.app import MinusDashboard
from minus.dashboard.widgets import Panel, ViewerPane, ascii_bar, human_bytes

PANELS = ["panel-memory", "panel-hardware", "panel-tools", "panel-programs", "panel-agents"]
HOTKEYS = {
    "m": "panel-memory",
    "h": "panel-hardware",
    "t": "panel-tools",
    "p": "panel-programs",
    "a": "panel-agents",
}


@pytest.fixture
def app(tmp_path, monkeypatch):
    """A dashboard pointed at an empty project, with no assistant running."""
    monkeypatch.setenv("MINUS_PROJECT_ROOT", str(tmp_path))
    for name in ("logs", "memory/conversations", "memory/deep_notes"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    return MinusDashboard(tmp_path / "absent.sock")


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

    async def test_the_viewer_is_twice_the_height_of_the_input(self, app):
        """The requested split: two thirds over one third."""
        async with app.run_test(size=(120, 40)) as pilot:
            viewer = pilot.app.query_one("#viewer").size.height
            prompt = pilot.app.query_one("#prompt").size.height

            assert viewer == pytest.approx(2 * prompt, abs=2)

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

    async def test_the_arrows_cycle_the_three_views(self, app):
        async with app.run_test() as pilot:
            viewer = pilot.app.query_one(ViewerPane)
            pilot.app.set_focus(None)

            await pilot.press("right")
            assert viewer.current == "log"
            await pilot.press("right")
            assert viewer.current == "deep"
            await pilot.press("right")
            assert viewer.current == "conversation"
            await pilot.press("left")
            assert viewer.current == "deep"

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

    async def test_the_hotkeys_type_rather_than_expand_while_the_input_has_focus(self, app):
        async with app.run_test() as pilot:
            pilot.app.query_one("#prompt-input").focus()
            await pilot.pause()

            await pilot.press("m")

            assert pilot.app.query_one("#prompt-input").value == "m"
            assert not pilot.app.query_one("#panel-memory").has_class("-expanded")

    async def test_every_panel_says_it_has_no_options_yet(self, app):
        """Scaffolding, deliberately. Filling one in is returning a list."""
        async with app.run_test() as pilot:
            for panel in pilot.app.query(Panel):
                assert panel.options() == []


class TestDisconnected:
    async def test_it_opens_without_an_assistant_running(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()

            assert pilot.app.query_one("#statusbar").has_class("disconnected")

    async def test_typing_a_line_reports_that_nothing_is_listening(self, app):
        async with app.run_test() as pilot:
            await pilot.pause()
            pilot.app.query_one("#prompt-input").focus()

            await pilot.press("h", "i", "enter")
            await pilot.pause()

            assert pilot.app.query_one("#prompt-input").value == ""


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
