"""Media review: parser facts, optional frames, and the flags a human weighs."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from minus.triage.media_review import (
    MediaReview,
    _classify_streams,
    _title_coverage,
    _tokens,
    review_media,
)

BWRAP = shutil.which("bwrap")
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


def test_tokens_drop_filler_short_words_and_case():
    assert _tokens("The Matrix") == {"matrix"}
    assert _tokens("x-men 2") == {"men"}
    assert _tokens("A") == set()
    assert _tokens("") == set()


def test_title_coverage_needs_every_expected_token():
    assert _title_coverage("", ["anything"]) == 1.0
    assert _title_coverage("The Matrix", ["matrix"]) == 1.0
    assert _title_coverage("sample testsrc", ["sample"]) == 0.5
    assert _title_coverage("matrix reloaded", ["the matrix"]) == 0.5
    assert _title_coverage("sample", ["unrelated"]) == 0.0


def test_classify_streams_flags_data_and_attachments():
    streams = [
        {"codec_type": "video", "codec_name": "h264"},
        {"codec_type": "audio", "codec_name": "aac"},
        {"codec_type": "data", "codec_name": "bin_data"},
        {"codec_type": "attachment", "codec_name": "ttf"},
    ]

    flags, video, audio, notes = _classify_streams(streams)

    assert flags == ("media:attachment-stream", "media:data-stream")
    assert video == streams[0]
    assert audio == (streams[1],)
    assert len(notes) == 2


def test_classify_streams_accepts_a_container_with_no_video():
    flags, video, audio, notes = _classify_streams([{"codec_type": "audio", "codec_name": "flac"}])

    assert flags == ()
    assert video is None
    assert len(audio) == 1
    assert notes == []


def test_as_dict_is_plain_json_with_lists():
    review = MediaReview(
        available=True,
        file="movie.mkv",
        format="matroska,webm",
        duration=2.0,
        size=1024,
        video={"codec_type": "video"},
        audio=({"codec_type": "audio"},),
        streams=2,
        frames=({"at_seconds": 0.5, "path": "/tmp/x.jpg", "ok": True, "error": ""},),
        contact_sheet="/tmp/contact.jpg",
        flags=("media:data-stream",),
        notes=("note",),
        title="movie",
        expected_title="movie",
        title_coverage=1.0,
    )

    payload = review.as_dict()

    assert review.sampled == 1
    assert payload["audio"] == [{"codec_type": "audio"}]
    assert payload["frames"] == [{"at_seconds": 0.5, "path": "/tmp/x.jpg", "ok": True, "error": ""}]
    assert payload["flags"] == ["media:data-stream"]
    assert payload["notes"] == ["note"]


def test_no_media_files_is_unavailable(tmp_path):
    (tmp_path / "notes.txt").write_text("hello\n", encoding="utf-8")

    review = review_media(tmp_path)

    assert review.available is False
    assert review.file == ""
    assert review.frames == ()


def test_junk_video_is_a_parser_failure_not_an_exception(tmp_path):
    if BWRAP is None or FFPROBE is None:
        pytest.skip("bwrap/ffprobe not installed")
    (tmp_path / "movie.mkv").write_bytes(b"not a real matroska file")

    review = review_media(tmp_path)

    assert review.available is False
    assert review.file == "movie.mkv"
    assert "media:probe-failed" in review.flags
    assert review.notes


def test_a_real_sample_yields_frames_and_a_contact_sheet(tmp_path):
    if BWRAP is None or FFMPEG is None or FFPROBE is None:
        pytest.skip("bwrap/ffmpeg/ffprobe not installed")
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=2:size=160x120:rate=10",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(tmp_path / "sample.mp4"),
        ],
        check=True,
    )

    review = review_media(
        tmp_path,
        output_dir=tmp_path / "out",
        frames=3,
        expected_title="sample testsrc",
    )

    assert review.available is True
    assert review.format
    assert review.duration is not None
    assert 1.8 <= review.duration <= 2.2
    assert review.sampled >= 1
    assert "media:no-frames" not in review.flags
    assert review.contact_sheet
    assert Path(review.contact_sheet).exists()
    assert all(Path(frame["path"]).exists() for frame in review.frames)
