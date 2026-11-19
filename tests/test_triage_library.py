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
from minus.triage.library import choose_primary_files, promote_files_to_library, promote_to_library

STAMPED = re.compile(r"\A[0-9a-z-]+-\d{8}T\d{6}Z\Z")


def mode_of(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_promotes_only_the_mp4_out_of_a_release_directory(tmp_path):
    source = tmp_path / "downloads" / "Some Movie"
    (source / "extras").mkdir(parents=True)
    (source / "movie.mp4").write_bytes(b"payload")
    (source / "extras" / "notes.txt").write_bytes(b"notes")
    library = tmp_path / "library"

    result = promote_to_library(source, library, name="Some Movie", provenance={"torrent": "abc"})

    target = library / "movie.mp4"
    provenance_path = tmp_path / "memory" / "library_provenance" / "some-movie.provenance.json"
    assert not source.exists()
    assert result["moved_to"] == str(target)
    assert result["provenance"] == str(provenance_path)
    assert result["bytes"] == len(b"payload")
    assert result["files"] == ["movie.mp4"]
    assert target.read_bytes() == b"payload"
    assert mode_of(target) == 0o644
    assert mode_of(library) == 0o755
    assert sorted(p.name for p in library.iterdir()) == ["movie.mp4"]
    assert not (library / "extras").exists()

    provenance = read_json(result["provenance"])
    assert mode_of(provenance_path) == 0o600
    assert provenance["name"] == "Some Movie"
    assert provenance["target"] == str(library)
    assert provenance["files"] == ["movie.mp4"]
    assert provenance["provenance"] == {"torrent": "abc"}
    # Seconds precision, UTC: no microseconds in the recorded timestamp.
    assert datetime.fromisoformat(provenance["promoted_at"]).microsecond == 0


def test_promoting_the_same_file_name_again_gets_a_stamp(tmp_path):
    library = tmp_path / "library"

    first = tmp_path / "one"
    first.mkdir()
    (first / "file.mp4").write_bytes(b"first")
    promote_to_library(first, library, name="Same Name")

    second = tmp_path / "two"
    second.mkdir()
    (second / "file.mp4").write_bytes(b"second")
    result = promote_to_library(second, library, name="Same Name")

    target = library / "file.mp4"
    stamped = Path(result["moved_to"])
    assert stamped.parent == library
    assert STAMPED.match(stamped.stem)
    assert target.read_bytes() == b"first"
    assert stamped.read_bytes() == b"second"
    assert sorted(p.name for p in library.iterdir()) == sorted(["file.mp4", stamped.name])


def test_promotes_a_single_file_flat(tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    library = tmp_path / "library"

    result = promote_to_library(source, library, name="Clip")

    target = library / "clip.mp4"
    assert result["moved_to"] == str(target)
    assert result["bytes"] == 5
    assert target.read_bytes() == b"video"
    assert mode_of(target) == 0o644
    provenance = tmp_path / "memory" / "library_provenance" / "clip.provenance.json"
    assert mode_of(provenance) == 0o600


def test_a_non_mp4_directory_is_refused(tmp_path):
    source = tmp_path / "release"
    source.mkdir()
    (source / "movie.mkv").write_bytes(b"video")
    with pytest.raises(ValueError, match=r"No \.mp4"):
        promote_to_library(source, tmp_path / "library", name="Movie")
    assert source.is_dir()


def test_missing_source_is_refused(tmp_path):
    with pytest.raises(ValueError, match="missing source"):
        promote_to_library(tmp_path / "gone", tmp_path / "library", name="gone")
    assert not (tmp_path / "library").exists()


def test_primary_selection_keeps_video_over_release_furniture():
    entries = [
        {"relative": "Release/movie.mp4", "size": 100},
        {"relative": "Release/movie.srt", "size": 1},
        {"relative": "Release/cover.jpg", "size": 2},
        {"relative": "Release/notes.txt", "size": 3},
    ]

    assert choose_primary_files(entries) == ["Release/movie.mp4"]


def test_primary_selection_keeps_every_episode_and_falls_back_to_audio():
    episodes = [
        {"relative": "Show/e01.mkv"},
        {"relative": "Show/e02.mkv"},
        {"relative": "Show/e01.srt"},
    ]
    assert choose_primary_files(episodes) == ["Show/e01.mkv", "Show/e02.mkv"]

    album = [{"relative": "Album/01.flac"}, {"relative": "Album/cover.jpg"}]
    assert choose_primary_files(album) == ["Album/01.flac"]

    documents = [{"relative": "Docs/a.pdf"}, {"relative": "Docs/b.pdf"}]
    assert choose_primary_files(documents) == ["Docs/a.pdf", "Docs/b.pdf"]


def test_promote_files_copies_only_the_selected_files(tmp_path):
    root = tmp_path / "staged"
    (root / "Release").mkdir(parents=True)
    (root / "Release" / "movie.mp4").write_bytes(b"video")
    (root / "Release" / "cover.jpg").write_bytes(b"image")
    library = tmp_path / "library"

    result = promote_files_to_library(
        [
            (root / "Release" / "movie.mp4", "movie.mp4"),
        ],
        library,
        name="Movie",
        provenance={"selected": ["Release/movie.mp4"]},
    )

    target = library / "movie.mp4"
    assert target.read_bytes() == b"video"
    assert sorted(p.name for p in library.iterdir()) == ["movie.mp4"]
    assert result["files"] == ["movie.mp4"]
    assert read_json(result["provenance"])["files"] == ["movie.mp4"]
    # The source survives until the caller deletes the staged tree.
    assert (root / "Release" / "movie.mp4").is_file()


def test_promote_files_refuses_a_path_that_escapes_the_library(tmp_path):
    source = tmp_path / "movie.mp4"
    source.write_bytes(b"video")

    with pytest.raises(ValueError, match="Unsafe"):
        promote_files_to_library(
            [(source, "../escaped.mp4")],
            tmp_path / "library",
            name="Movie",
        )
    assert not (tmp_path / "library" / "escaped.mp4").exists()
