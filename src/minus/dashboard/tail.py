"""Following the files MINUS writes, from a different process.

Everything the dashboard displays on the left of the screen already exists on
disk: the run log, the live conversation, the deep tier's write-ups. Reading
them rather than streaming them over the socket means the viewer still works
with the assistant stopped, which is exactly when you most want to read a log.

No textual import anywhere in this module. This is where the logic that is
worth testing lives, and it should not need a TUI to exercise it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from minus.prompts import FACTS_MARKER
from minus.services.json import read_json

# The run log's own format: a timestamp, then the level. Payloads logged with
# pretty_json span many lines, and only the first carries this prefix.
_LOG_PREFIX = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} (\w+) ")

LEVEL_STYLES = {
    "CRITICAL": "bold red",
    "ERROR": "red",
    "WARNING": "yellow",
    "INFO": "",
    "DEBUG": "bright_black",
}

# One read is capped at this. A first attach to a long-running service would
# otherwise pull megabytes into the UI thread at once.
MAX_READ_BYTES = 256 * 1024

# Enough of the head of a file to tell it apart from a different one that has
# taken its name. See LogTailer._read_header.
HEADER_BYTES = 256


def latest_log(directory: Path, prefix: str = "run") -> Path | None:
    """The newest log, by name.

    `run-%Y%m%d-%H%M%S-%f-<pid>.log` sorts lexicographically in the same order
    it sorts chronologically, so this needs no stat() per file -- and it picks
    up the *new* file when a restart starts one mid-session, which is the case
    that matters when the dashboard is watching a service.
    """
    logs = sorted(directory.glob(f"{prefix}-*.log"))
    return logs[-1] if logs else None


def visible(line: str) -> str:
    """What a terminal would have left on screen after drawing `line`.

    Only the text after the last carriage return: everything before it was
    overwritten in place. Spinners and progress bars are built entirely out of
    this, and without it each frame reads as a line of its own.

    One trailing `\\r` is dropped first, since a CRLF line ends with the `\\r`
    of its own terminator -- treating that as an overwrite would blank every
    line of a file that happens to use DOS endings.
    """
    if line.endswith("\r"):
        line = line[:-1]
    return line.rpartition("\r")[2]


class LogTailer:
    """Incremental reads of a growing file.

    Reads bytes rather than text on purpose. Opening in text mode applies
    universal newlines, which rewrites every `\\r` to `\\n` during the read --
    at which point a spinner frame is indistinguishable from a log line and no
    amount of work further down can tell them apart again. It also made the
    offset drift, since it was recovered by re-encoding the decoded text and a
    `\\r\\n` came back a byte short.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._offset = 0
        self._inode: int | None = None
        self._header = b""
        self._pending = b""

    def reset(self, path: Path | None) -> None:
        self.path = path
        self._offset = 0
        self._inode = None
        self._header = b""
        self._pending = b""

    @property
    def current(self) -> str:
        """The line being overwritten in place, if a writer is mid-line.

        A spinner never emits a newline until it stops, so this is where it
        lives -- shown on its own row rather than appended to the scrollback.
        """
        return visible(self._pending.decode("utf-8", errors="replace"))

    def _read_header(self) -> bytes:
        """The first few bytes, as a cheap identity check.

        The inode alone is not enough. Delete a file and write another with
        the same name and the kernel will happily hand back the inode number
        it just freed -- at which point a rotation looks like an append, and
        the offset we are holding points into the middle of unrelated content.
        Comparing the head of the file catches that, and truncate-and-rewrite
        with it, for one small read out of the page cache.
        """
        assert self.path is not None
        try:
            with self.path.open("rb") as handle:
                return handle.read(HEADER_BYTES)
        except OSError:
            return b""

    def poll(self) -> list[str]:
        """Whatever complete lines have appeared since the last call."""
        if self.path is None or not self.path.exists():
            return []

        stat = self.path.stat()
        header = self._read_header()

        # Truncated, or a different file wearing the same name. Either way the
        # offset being held describes content that is no longer there.
        #
        # The header test is continuation, not equality: a file that has grown
        # past 256 bytes since the last poll has a longer head than the one
        # recorded, and that is the ordinary case rather than a rotation. What
        # rules out a rotation is that the old head is still a prefix of the
        # new one.
        if self._offset and (
            stat.st_size < self._offset
            or stat.st_ino != self._inode
            or not header.startswith(self._header)
        ):
            self._offset = 0
            self._pending = b""

        self._inode = stat.st_ino
        self._header = header

        if stat.st_size <= self._offset:
            return []

        with self.path.open("rb") as handle:
            handle.seek(self._offset)
            raw = handle.read(MAX_READ_BYTES)
            self._offset += len(raw)

        # Held back as bytes, not text: a read can land in the middle of a
        # multi-byte character as easily as in the middle of a line, and
        # decoding the halves separately would replace both.
        data = self._pending + raw
        head, newline, tail = data.rpartition(b"\n")
        self._pending = tail if newline else data

        # Nothing before the last carriage return can still be seen, so it can
        # never be needed again. Dropping it is also what bounds this buffer
        # during a spinner that runs for minutes without a single newline.
        self._pending = self._pending.rpartition(b"\r")[2]

        if not newline:
            return []
        return [visible(line) for line in head.decode("utf-8", errors="replace").split("\n")]

    def backfill(self, lines: int = 400) -> list[str]:
        """The tail of the file, for a viewer that has just opened."""
        if self.path is None or not self.path.exists():
            return []

        stat = self.path.stat()
        with self.path.open("rb") as handle:
            if stat.st_size > MAX_READ_BYTES:
                handle.seek(stat.st_size - MAX_READ_BYTES)
                handle.readline()  # discard the partial line seeking landed in
            raw = handle.read()

        self._offset = stat.st_size
        self._inode = stat.st_ino
        self._header = self._read_header()

        # The same holdback poll() does. A file that ends mid-line -- which is
        # the normal state of one a spinner is writing to -- must not have its
        # unfinished last line committed to the scrollback.
        head, newline, tail = raw.rpartition(b"\n")
        self._pending = (tail if newline else raw).rpartition(b"\r")[2]
        if not newline:
            return []

        text = head.decode("utf-8", errors="replace")
        return [visible(line) for line in text.split("\n")][-lines:]


class LogLineStyler:
    """Assigns a level to each line, remembering the last one it saw.

    Stateful because it has to be: most lines in a MINUS log are continuations
    of a pretty_json payload and carry no prefix of their own. Styling those
    as INFO would make the body of an exception look unremarkable.
    """

    def __init__(self) -> None:
        self.level = "INFO"

    def style(self, line: str) -> str:
        match = _LOG_PREFIX.match(line)
        if match:
            self.level = match.group(1)
        return LEVEL_STYLES.get(self.level, "")


@dataclass
class Turn:
    """One line of conversation, ready to display."""

    role: str
    text: str


def _strip_facts(content: str) -> str:
    """Drop the recalled facts appended to every user message.

    Conversation._build_user_message appends a pretty_json list of facts to
    what the user said. It is there for the model, and in a transcript view it
    is several times longer than the message it is attached to.
    """
    return content.split(FACTS_MARKER, 1)[0].strip()


def render_messages(messages: list[dict], tool_width: int = 70) -> list[Turn]:
    """The parts of a transcript worth showing a person."""
    turns: list[Turn] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content") or ""

        if role == "system":
            continue
        if role == "user":
            turns.append(Turn("user", _strip_facts(content)))
        elif role == "tool":
            flat = " ".join(content.split())
            if len(flat) > tool_width:
                flat = flat[: tool_width - 1] + "…"
            turns.append(Turn("tool", flat))
        elif role == "assistant":
            for call in message.get("tool_calls") or []:
                turns.append(Turn("tool", f"→ {call['function']['name']}"))
            if content.strip():
                turns.append(Turn("assistant", content.strip()))

    return [turn for turn in turns if turn.text]


class ConversationReader:
    """Re-reads a conversation file whenever it changes.

    Cannot tail by offset: write_json replaces the file atomically, so the
    inode changes on *every* appended message and a held offset is immediately
    meaningless. That same property is what makes reading it safe -- a reader
    never sees a half-written transcript -- so the trade is a good one. The
    files are kilobytes.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._signature: tuple[Any, ...] | None = None
        self._turns: list[Turn] = []

    def reset(self, path: Path | None) -> None:
        self.path = path
        self._signature = None
        self._turns = []

    def poll(self) -> list[Turn] | None:
        """The whole conversation, or None if nothing has changed."""
        if self.path is None or not self.path.exists():
            return None

        stat = self.path.stat()
        signature = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
        if signature == self._signature:
            return None
        self._signature = signature

        try:
            payload = read_json(self.path)
        except (OSError, ValueError):
            # Mid-rename, most likely. The next poll will find it.
            self._signature = None
            return None

        self._turns = render_messages(payload.get("messages") or [])
        return self._turns


def latest_conversation(directory: Path) -> Path | None:
    """The most recently updated conversation file."""
    files = sorted(directory.glob("*.json"))
    return files[-1] if files else None


@dataclass
class DeepNote:
    path: Path
    title: str
    created_at: str
    detail: str


class DeepNoteReader:
    """The deep tier's write-ups, newest first."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._names: list[str] = []
        self.notes: list[DeepNote] = []

    def poll(self) -> bool:
        """Reload if the set of notes changed. True if it did."""
        if not self.directory.exists():
            return False

        names = sorted((path.name for path in self.directory.glob("*.json")), reverse=True)
        if names == self._names:
            return False
        self._names = names

        notes = []
        for name in names:
            path = self.directory / name
            try:
                payload = read_json(path)
            except (OSError, ValueError):
                continue
            notes.append(
                DeepNote(
                    path=path,
                    title=payload.get("title", path.stem),
                    created_at=payload.get("created_at", ""),
                    detail=payload.get("detail", ""),
                )
            )
        self.notes = notes
        return True
