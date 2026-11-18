"""The media probe under bubblewrap: parser facts, never a verdict."""

from __future__ import annotations

import shutil

import pytest

from minus.triage.media import PROBE_TIMEOUT, probe_media

BWRAP = shutil.which("bwrap")
FFPROBE = shutil.which("ffprobe")


def test_non_media_files_are_not_probed(tmp_path):
    (tmp_path / "notes.txt").write_text("hello\n", encoding="utf-8")
    assert probe_media(tmp_path) == []


def test_probe_reports_a_parser_failure_without_raising(tmp_path):
    if BWRAP is None or FFPROBE is None:
        pytest.skip("bwrap/ffprobe not installed")
    (tmp_path / "movie.mkv").write_bytes(b"not a real matroska file")

    result = probe_media(tmp_path, timeout=PROBE_TIMEOUT)

    assert result[0]["file"] == "movie.mkv"
    assert result[0]["probe"] == "failed"
    assert result[0]["timed_out"] is False


def test_probe_is_bounded_and_says_what_it_skipped(tmp_path):
    if BWRAP is None or FFPROBE is None:
        pytest.skip("bwrap/ffprobe not installed")
    for index in range(3):
        (tmp_path / f"episode-{index}.mkv").write_bytes(b"not a video")

    result = probe_media(tmp_path, limit=1, timeout=PROBE_TIMEOUT)

    assert result[0]["file"] == "episode-0.mkv"
    assert result[-1]["probe"] == "skipped"
    assert "2 more" in result[-1]["reason"]
