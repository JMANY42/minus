"""Following MINUS's files from another process.

Imports `minus.dashboard.tail` only, never the app, so this passes whether or
not the dashboard extra is installed -- which is the reason that package's
__init__ is empty.
"""

from __future__ import annotations

from minus.core.prompts import FACTS_MARKER
from minus.dashboard.tail import (
    ConversationReader,
    DeepNoteReader,
    LogLineStyler,
    LogTailer,
    latest_conversation,
    latest_log,
    render_messages,
)
from minus.services.json import write_json


class TestLatestLog:
    def test_picks_the_newest_by_name(self, tmp_path):
        """The run-<timestamp>-<pid> format sorts chronologically already."""
        for name in ("run-20260810-120000-000000-1.log", "run-20260810-235959-000000-2.log"):
            (tmp_path / name).write_text("x", encoding="utf-8")

        assert latest_log(tmp_path).name == "run-20260810-235959-000000-2.log"

    def test_ignores_the_dashboards_own_log(self, tmp_path):
        """Otherwise the log viewer ends up watching itself."""
        (tmp_path / "run-20260810-120000-000000-1.log").write_text("x", encoding="utf-8")
        (tmp_path / "dash-20260810-235959-000000-2.log").write_text("x", encoding="utf-8")

        assert latest_log(tmp_path).name.startswith("run-")

    def test_returns_nothing_for_an_empty_directory(self, tmp_path):
        assert latest_log(tmp_path) is None


class TestLogTailer:
    def test_reads_only_what_is_new(self, tmp_path):
        path = tmp_path / "run-1.log"
        path.write_text("one\ntwo\n", encoding="utf-8")
        tailer = LogTailer(path)

        assert tailer.poll() == ["one", "two"]
        assert tailer.poll() == []

        with path.open("a", encoding="utf-8") as handle:
            handle.write("three\n")

        assert tailer.poll() == ["three"]

    def test_holds_back_a_partial_line(self, tmp_path):
        """A read can land mid-line; showing half a message is worse than waiting."""
        path = tmp_path / "run-1.log"
        path.write_text("complete\npart", encoding="utf-8")
        tailer = LogTailer(path)

        assert tailer.poll() == ["complete"]

        with path.open("a", encoding="utf-8") as handle:
            handle.write("ial\n")

        assert tailer.poll() == ["partial"]

    def test_starts_over_when_the_file_is_replaced(self, tmp_path):
        path = tmp_path / "run-1.log"
        path.write_text("first run\n", encoding="utf-8")
        tailer = LogTailer(path)
        tailer.poll()

        path.unlink()
        path.write_text("second run\n", encoding="utf-8")

        assert tailer.poll() == ["second run"]

    def test_starts_over_when_the_file_is_truncated(self, tmp_path):
        path = tmp_path / "run-1.log"
        path.write_text("a long first line\n", encoding="utf-8")
        tailer = LogTailer(path)
        tailer.poll()

        path.write_text("tiny\n", encoding="utf-8")

        assert tailer.poll() == ["tiny"]

    def test_a_missing_file_is_not_an_error(self, tmp_path):
        assert LogTailer(tmp_path / "absent.log").poll() == []

    def test_backfill_returns_the_tail_and_sets_the_offset(self, tmp_path):
        path = tmp_path / "run-1.log"
        path.write_text("".join(f"line {index}\n" for index in range(100)), encoding="utf-8")
        tailer = LogTailer(path)

        assert tailer.backfill(lines=5) == [f"line {index}" for index in range(95, 100)]
        assert tailer.poll() == []


class TestLogLineStyler:
    def test_styles_by_level(self):
        styler = LogLineStyler()

        assert "red" in styler.style("2026-08-10 15:57:13,374 ERROR minus.cli: boom")
        assert styler.style("2026-08-10 15:57:13,374 INFO minus.cli: fine") == ""

    def test_continuation_lines_inherit_the_level(self):
        """Most lines in a MINUS log are the body of a pretty_json payload."""
        styler = LogLineStyler()
        styler.style("2026-08-10 15:57:13,374 ERROR minus.cli: it failed")

        assert "red" in styler.style('  File "thing.py", line 3')
        assert "red" in styler.style("    raise ValueError")


class TestRenderMessages:
    def test_drops_the_system_prompt(self):
        turns = render_messages(
            [{"role": "system", "content": "You are MINUS."}, {"role": "user", "content": "hi"}]
        )

        assert [turn.role for turn in turns] == ["user"]

    def test_strips_the_recalled_facts_from_a_user_turn(self):
        """They are for the model, and dwarf the message they hang off."""
        content = f'what music do I like\n\n{FACTS_MARKER}\n[\n  "Likes jazz"\n]'

        turns = render_messages([{"role": "user", "content": content}])

        assert turns[0].text == "what music do I like"

    def test_collapses_a_tool_result_to_one_line(self):
        turns = render_messages(
            [{"role": "tool", "content": "a\nb\nc " + "x" * 200, "tool_call_id": "1"}]
        )

        assert "\n" not in turns[0].text
        assert len(turns[0].text) <= 70

    def test_shows_a_tool_call_by_name(self):
        turns = render_messages(
            [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": "1", "function": {"name": "get_current_time", "arguments": "{}"}}
                    ],
                }
            ]
        )

        assert turns[0].text == "→ get_current_time"

    def test_keeps_an_assistant_reply(self):
        turns = render_messages([{"role": "assistant", "content": "It is five."}])

        assert turns[0].role == "assistant"
        assert turns[0].text == "It is five."


class TestConversationReader:
    def write(self, path, messages):
        write_json(path, {"conversation_id": "c1", "messages": messages})

    def test_reads_a_conversation(self, tmp_path):
        path = tmp_path / "c1.json"
        self.write(path, [{"role": "user", "content": "hello"}])

        turns = ConversationReader(path).poll()

        assert [turn.text for turn in turns] == ["hello"]

    def test_returns_none_when_nothing_changed(self, tmp_path):
        path = tmp_path / "c1.json"
        self.write(path, [{"role": "user", "content": "hello"}])
        reader = ConversationReader(path)
        reader.poll()

        assert reader.poll() is None

    def test_notices_a_new_message_despite_the_inode_changing(self, tmp_path):
        """write_json replaces the file, so an offset-based tail would break."""
        path = tmp_path / "c1.json"
        self.write(path, [{"role": "user", "content": "hello"}])
        reader = ConversationReader(path)
        reader.poll()

        self.write(
            path,
            [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}],
        )
        turns = reader.poll()

        assert [turn.text for turn in turns] == ["hello", "hi"]

    def test_a_missing_file_is_not_an_error(self, tmp_path):
        assert ConversationReader(tmp_path / "absent.json").poll() is None

    def test_latest_conversation_picks_the_newest_name(self, tmp_path):
        for name in ("20260810T010000Z-a.json", "20260810T020000Z-b.json"):
            (tmp_path / name).write_text("{}", encoding="utf-8")

        assert latest_conversation(tmp_path).name == "20260810T020000Z-b.json"


class TestDeepNoteReader:
    def test_reads_notes_newest_first(self, tmp_path):
        for stamp, title in (("20260810T010000Z-a", "older"), ("20260810T020000Z-b", "newer")):
            write_json(
                tmp_path / f"{stamp}.json",
                {"title": title, "created_at": stamp, "detail": "words"},
            )
        reader = DeepNoteReader(tmp_path)

        assert reader.poll() is True
        assert [note.title for note in reader.notes] == ["newer", "older"]

    def test_reports_no_change_when_nothing_was_added(self, tmp_path):
        write_json(tmp_path / "a.json", {"title": "x", "detail": "y"})
        reader = DeepNoteReader(tmp_path)
        reader.poll()

        assert reader.poll() is False

    def test_a_missing_directory_is_not_an_error(self, tmp_path):
        assert DeepNoteReader(tmp_path / "absent").poll() is False

    def test_a_corrupt_note_is_skipped_rather_than_fatal(self, tmp_path):
        (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
        write_json(tmp_path / "good.json", {"title": "fine", "detail": "y"})
        reader = DeepNoteReader(tmp_path)

        reader.poll()

        assert [note.title for note in reader.notes] == ["fine"]
