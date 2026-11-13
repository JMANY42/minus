"""Promotion into the library, over real directories in a temp tree.

The move is filesystem behavior: atomic rename, modes, and a provenance file
next to the content. Faking any of it would test the mock, so these cases use
real files and assert on what the filesystem can show.
"""

from __future__ import annotations

import re
import stat
from datetime import datetime
from pathlib import Path

import pytest

from minus.services.json import read_json
from minus.triage.library import promote_to_library

STAMPED = re.compile(r"\A[0-9a-z-]+-\d{8}T\d{6}Z\Z")


def mode_of(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_promotes_a_directory_with_library_modes(tmp_path):
    source = tmp_path / "downloads" / "Some Movie"
    (source / "extras").mkdir(parents=True)
    (source / "movie.mkv").write_bytes(b"payload")
    (source / "extras" / "notes.txt").write_bytes(b"notes")
    library = tmp_path / "library"

    result = promote_to_library(source, library, name="Some Movie", provenance={"torrent": "abc"})

    target = library / "some-movie"
    assert not source.exists()
    assert result == {
        "moved_to": str(target),
        "provenance": str(library / "some-movie.provenance.json"),
        "bytes": len(b"payload") + len(b"notes"),
    }
    assert (target / "movie.mkv").read_bytes() == b"payload"
    assert (target / "extras" / "notes.txt").read_bytes() == b"notes"

    assert mode_of(target) == 0o755
    assert mode_of(target / "extras") == 0o755
    assert mode_of(target / "movie.mkv") == 0o644
    assert mode_of(target / "extras" / "notes.txt") == 0o644

    provenance = read_json(result["provenance"])
    assert mode_of(library / "some-movie.provenance.json") == 0o600
    assert provenance["name"] == "Some Movie"
    assert provenance["source"] == str(source)
    assert provenance["target"] == str(target)
    assert provenance["provenance"] == {"torrent": "abc"}
    # Seconds precision, UTC: no microseconds in the recorded timestamp.
    assert datetime.fromisoformat(provenance["promoted_at"]).microsecond == 0


def test_promoting_the_same_name_again_gets_a_stamp(tmp_path):
    library = tmp_path / "library"

    first = tmp_path / "one"
    first.mkdir()
    (first / "file.bin").write_bytes(b"first")
    promote_to_library(first, library, name="Same Name")

    second = tmp_path / "two"
    second.mkdir()
    (second / "file.bin").write_bytes(b"second")
    result = promote_to_library(second, library, name="Same Name")

    target = library / "same-name"
    stamped = Path(result["moved_to"])
    assert stamped.parent == library
    assert STAMPED.match(stamped.name)
    assert target.is_dir()
    assert stamped.is_dir()
    assert (target / "file.bin").read_bytes() == b"first"
    assert (stamped / "file.bin").read_bytes() == b"second"
    assert read_json(stamped.with_name(stamped.name + ".provenance.json"))["provenance"] == {}


def test_promotes_a_single_file(tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    library = tmp_path / "library"

    result = promote_to_library(source, library, name="Clip")

    target = library / "clip"
    assert result["moved_to"] == str(target)
    assert result["bytes"] == 5
    assert target.read_bytes() == b"video"
    assert mode_of(target) == 0o644
    assert mode_of(library / "clip.provenance.json") == 0o600


def test_missing_source_is_refused(tmp_path):
    with pytest.raises(ValueError, match="missing source"):
        promote_to_library(tmp_path / "gone", tmp_path / "library", name="gone")
    assert not (tmp_path / "library").exists()
