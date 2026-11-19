"""What a media file claims to be, read by ffprobe inside the airlock.

The Windows guest cannot answer this question. It handles Windows shapes
(`.exe`, `.bat`, `.lnk`, ...) and the harness uploads through WinRM, whose size
cap a 2 GiB movie cannot cross. Media is therefore checked by the parser the
user's own player would use: ffprobe under bubblewrap with the staged tree
read-only at `/input`, and a separate quarantined directory writable at
`/output` for the frames ffmpeg extracts.

Frames exist so a human -- or a vision-capable agent -- can verify the content
is what its name says. They are not evidence of safety: a valid container can
still carry a parser bug, and bwrap namespaces are containment, not a VM. The
review reports what the parser saw and lets the promotion gate decide.

Nothing here raises. A missing binary, a hung probe, junk bytes, and a frame
extraction that produced nothing are all facts a reviewer needs, so they are
returned as `MediaReview(available=False, ...)` and flags, never exceptions.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from minus.triage.airlock import run_isolated
from minus.triage.media import media_files

# Six frames is enough to spot a title card and a scene; more costs a seek each
# and makes the review slow without telling a human anything new.
FRAMES_DEFAULT = 6
MAX_FRAMES = 12
# Frames are evidence, not a screenshot gallery: one 640px edge keeps a contact
# sheet readable while bounding decode work on a long 4K file.
FRAME_MAX_EDGE = 640
FFPROBE_TIMEOUT = 10.0
FFMPEG_TIMEOUT = 12.0

VIDEO_SUFFIXES = frozenset(
    {
        ".mp4",
        ".m4v",
        ".mkv",
        ".avi",
        ".mov",
        ".webm",
        ".mpg",
        ".mpeg",
        ".wmv",
        ".flv",
        ".ts",
    }
)

# ffprobe's `format_name` is a comma-separated list of demuxer aliases, e.g.
# "mov,mp4,m4a,3gp,3g2,mj2". A file whose suffix promises one family and whose
# demuxer names another is either mislabelled or a polyglot worth looking at.
EXTENSION_FORMATS: dict[str, tuple[str, ...]] = {
    ".mp4": ("mp4", "mov"),
    ".m4v": ("mp4", "mov"),
    ".mkv": ("matroska", "webm"),
    ".avi": ("avi",),
    ".mov": ("mov", "mp4"),
    ".webm": ("webm", "matroska"),
    ".mpg": ("mpeg",),
    ".mpeg": ("mpeg",),
    ".wmv": ("asf", "wmv"),
    ".flv": ("flv",),
    ".ts": ("mpegts",),
}

# Words that appear in almost every release name; counting them as a title
# match would make the coverage check pass on nothing.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "the",
        "this",
        "to",
        "was",
        "were",
        "with",
    }
)


@dataclass(frozen=True)
class MediaReview:
    """The parser's answer, plus the frames a human can look at.

    `available` is false when the probe itself could not run or could not parse
    the file; the remaining fields are then still populated as far as they were
    known so a caller never has to special-case a missing review.
    """

    available: bool
    file: str
    format: str
    duration: float | None
    size: int
    video: dict | None
    audio: tuple[dict, ...]
    streams: int
    frames: tuple[dict, ...]
    contact_sheet: str
    flags: tuple[str, ...]
    notes: tuple[str, ...]
    title: str
    expected_title: str
    title_coverage: float

    @property
    def sampled(self) -> int:
        """How many extracted frames are actually on disk."""
        return sum(1 for frame in self.frames if frame.get("ok"))

    def as_dict(self) -> dict:
        """The wire shape: tuples become lists so this is plain JSON."""
        return {
            "available": self.available,
            "file": self.file,
            "format": self.format,
            "duration": self.duration,
            "size": self.size,
            "video": self.video,
            "audio": [dict(stream) for stream in self.audio],
            "streams": self.streams,
            "frames": [dict(frame) for frame in self.frames],
            "contact_sheet": self.contact_sheet,
            "flags": list(self.flags),
            "notes": list(self.notes),
            "title": self.title,
            "expected_title": self.expected_title,
            "title_coverage": self.title_coverage,
        }


def _tokens(text: str) -> set[str]:
    """Lowercase alphanumerics of length >= 2, minus English filler."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {word for word in words if len(word) >= 2 and word not in _STOPWORDS}


def _title_coverage(expected: str, candidates: list[str]) -> float:
    """Fraction of the expected title's tokens that any candidate contains."""
    expected_tokens = _tokens(expected)
    if not expected_tokens:
        return 1.0
    seen: set[str] = set()
    for candidate in candidates:
        seen |= _tokens(candidate)
    return len(expected_tokens & seen) / len(expected_tokens)


def _classify_streams(
    streams: list[dict],
) -> tuple[tuple[str, ...], dict | None, tuple[dict, ...], list[str]]:
    """Split ffprobe streams into (flags, video, audio, notes).

    A `data` or `attachment` stream is not media a player renders; it is a
    second file smuggled inside the container (fonts, executables, scripts), so
    it is flagged rather than ignored.
    """
    flags: set[str] = set()
    notes: list[str] = []
    video: dict | None = None
    audio: list[dict] = []
    for stream in streams:
        kind = stream.get("codec_type")
        if kind == "video" and video is None:
            video = stream
        elif kind == "audio":
            audio.append(stream)
        elif kind == "data":
            flags.add("media:data-stream")
            notes.append(f"data stream: {stream.get('codec_name') or 'unknown'}")
        elif kind == "attachment":
            flags.add("media:attachment-stream")
            notes.append(f"attachment stream: {stream.get('codec_name') or 'unknown'}")
    return tuple(sorted(flags)), video, tuple(audio), notes


def _float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _int(value: object) -> int | None:
    if not isinstance(value, (int, float, str)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _failed(
    file: str,
    reason: str,
    *,
    flags: tuple[str, ...] = (),
    expected_title: str = "",
    probe_failed: bool = True,
) -> MediaReview:
    """The shape every failure returns: unavailable, flagged, explained."""
    prefix = ("media:probe-failed",) if probe_failed else ()
    combined = tuple(dict.fromkeys((*prefix, *flags)))
    return MediaReview(
        available=False,
        file=file,
        format="",
        duration=None,
        size=0,
        video=None,
        audio=(),
        streams=0,
        frames=(),
        contact_sheet="",
        flags=combined,
        notes=(reason,),
        title="",
        expected_title=expected_title,
        title_coverage=_title_coverage(expected_title, []),
    )


def _grid(count: int) -> str:
    """The tile layout that holds `count` frames without leaving rows empty."""
    if count <= 2:
        return "2x1"
    if count <= 4:
        return "2x2"
    if count <= 6:
        return "3x2"
    return "3x3"


def _frame_error(run: dict) -> str:
    stderr = str(run.get("stderr") or "").strip()
    if stderr:
        return stderr[:200]
    if run.get("timed_out"):
        return "timed out"
    return f"exit {run.get('exit_code')}"


def review_media(
    root: Path,
    *,
    output_dir: Path | None = None,
    frames: int = FRAMES_DEFAULT,
    expected_title: str = "",
    timeout: float = FFPROBE_TIMEOUT,
) -> MediaReview:
    """Probe the first video under `root` and optionally sample frames from it.

    `root` is the staged tree, mounted read-only. `output_dir` must live under
    quarantine: `run_isolated` binds it writable at `/output`, and that is the
    only writable host path the parser gets.
    """
    try:
        return _review_media(
            Path(root),
            output_dir=Path(output_dir) if output_dir is not None else None,
            frames=frames,
            expected_title=expected_title,
            timeout=timeout,
        )
    except Exception as exc:  # the parser is hostile input's target; never raise
        return _failed("", f"media review failed: {exc}", expected_title=expected_title)


def _review_media(
    root: Path,
    *,
    output_dir: Path | None,
    frames: int,
    expected_title: str,
    timeout: float,
) -> MediaReview:
    files = media_files(root)
    if not files:
        return _failed(
            "",
            "no media files under the staged tree",
            expected_title=expected_title,
            probe_failed=False,
        )

    video_files = [path for path in files if path.suffix.lower() in VIDEO_SUFFIXES]
    # The largest video is the feature, not the sample or the trailer; a
    # season pack still yields one representative episode rather than the
    # alphabetically first artifact in the directory.
    path = max(video_files, key=lambda item: item.stat().st_size) if video_files else files[0]
    relative = path.relative_to(root).as_posix()
    suffix = path.suffix.lower()

    if shutil.which("ffprobe") is None:
        return _failed(relative, "ffprobe is not installed", expected_title=expected_title)

    run = run_isolated(
        root,
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            f"/input/{relative}",
        ],
        timeout=timeout,
    )
    if run["timed_out"]:
        return _failed(
            relative,
            f"ffprobe timed out after {timeout:g}s",
            expected_title=expected_title,
        )
    if run["exit_code"] != 0:
        return _failed(
            relative,
            f"ffprobe failed: {_frame_error(run)}",
            expected_title=expected_title,
        )

    try:
        payload = json.loads(run["stdout"])
    except json.JSONDecodeError as exc:
        return _failed(
            relative, f"ffprobe output was not JSON: {exc}", expected_title=expected_title
        )
    if not isinstance(payload, dict):
        return _failed(relative, "ffprobe output was not an object", expected_title=expected_title)

    format_info = payload.get("format") or {}
    streams = payload.get("streams") or []
    if not isinstance(format_info, dict):
        format_info = {}
    if not isinstance(streams, list):
        streams = []
    streams = [stream for stream in streams if isinstance(stream, dict)]

    format_name = str(format_info.get("format_name") or "")
    duration = _float(format_info.get("duration"))
    size = _int(format_info.get("size"))
    if not size:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
    tags = format_info.get("tags") or {}
    if not isinstance(tags, dict):
        tags = {}

    stream_flags, video, audio, notes = _classify_streams(streams)
    flags: set[str] = set(stream_flags)

    if suffix in VIDEO_SUFFIXES and video is None:
        flags.add("media:no-video-stream")
        notes.append("no video stream in a file that claims to be video")

    accepted = EXTENSION_FORMATS.get(suffix)
    if accepted and not any(token in format_name.lower() for token in accepted):
        flags.add("media:extension-mismatch")
        notes.append(f"{suffix} container is actually {format_name or 'unknown'}")

    title = str(tags.get("title") or "").strip()
    if not title and video is not None:
        video_tags = video.get("tags") or {}
        if isinstance(video_tags, dict):
            title = str(video_tags.get("title") or "").strip()
    if not title:
        title = path.stem

    tag_text = " ".join(str(value) for value in tags.values())
    coverage = _title_coverage(expected_title, [title, path.stem, tag_text])
    if len(_tokens(expected_title)) >= 2 and coverage < 0.4:
        flags.add("media:title-mismatch")
        notes.append(f"title covers {coverage:.0%} of the expected name")

    sampled: list[dict] = []
    contact_sheet = ""
    if output_dir is not None:
        if shutil.which("ffmpeg") is None:
            notes.append("ffmpeg is not installed: no frames sampled")
        elif video is not None and duration is not None and duration > 0:
            sampled, contact_sheet, frame_notes = _sample_frames(
                root,
                relative=relative,
                output_dir=output_dir,
                duration=duration,
                count=max(1, min(int(frames), MAX_FRAMES)),
            )
            notes.extend(frame_notes)
            if not any(frame["ok"] for frame in sampled):
                flags.add("media:no-frames")
        elif video is None:
            notes.append("no video stream: no frames to sample")
        else:
            notes.append("duration is unknown: no frames to sample")

    return MediaReview(
        available=True,
        file=relative,
        format=format_name,
        duration=duration,
        size=size,
        video=video,
        audio=audio,
        streams=len(streams),
        frames=tuple(sampled),
        contact_sheet=contact_sheet,
        flags=tuple(sorted(flags)),
        notes=tuple(notes),
        title=title,
        expected_title=expected_title,
        title_coverage=coverage,
    )


def _sample_frames(
    root: Path,
    *,
    relative: str,
    output_dir: Path,
    duration: float,
    count: int,
) -> tuple[list[dict], str, list[str]]:
    """Extract `count` mid-segment frames and one contact sheet.

    Returns (frames, contact_sheet_path, notes); a failure is recorded per frame
    and the sheet is simply absent, never an exception.
    """
    notes: list[str] = []
    frames: list[dict] = []
    for index in range(count):
        at_seconds = duration * (index + 0.5) / count
        target = output_dir / f"frame-{index:02d}.jpg"
        run = run_isolated(
            root,
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{at_seconds:.3f}",
                "-i",
                f"/input/{relative}",
                "-frames:v",
                "1",
                "-vf",
                f"scale=w='min({FRAME_MAX_EDGE},iw)':h=-2",
                "-q:v",
                "4",
                "-y",
                f"/output/frame-{index:02d}.jpg",
            ],
            timeout=FFMPEG_TIMEOUT,
            output_dir=output_dir,
        )
        ok = run["exit_code"] == 0 and target.exists()
        frames.append(
            {
                "at_seconds": round(at_seconds, 3),
                "path": str(target.resolve()),
                "ok": ok,
                "error": "" if ok else _frame_error(run),
            }
        )

    successes = sum(1 for frame in frames if frame["ok"])
    if successes < 2:
        return frames, "", notes

    sheet = output_dir / "contact.jpg"
    run = run_isolated(
        root,
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            "/output/frame-%02d.jpg",
            "-filter_complex",
            f"tile={_grid(successes)}:padding=4:margin=4",
            "-frames:v",
            "1",
            "-y",
            "/output/contact.jpg",
        ],
        timeout=FFMPEG_TIMEOUT,
        output_dir=output_dir,
    )
    if run["exit_code"] == 0 and sheet.exists():
        return frames, str(sheet.resolve()), notes

    notes.append(f"contact sheet failed: {_frame_error(run)}")
    return frames, "", notes


__all__ = [
    "EXTENSION_FORMATS",
    "FFMPEG_TIMEOUT",
    "FFPROBE_TIMEOUT",
    "FRAMES_DEFAULT",
    "FRAME_MAX_EDGE",
    "MAX_FRAMES",
    "VIDEO_SUFFIXES",
    "MediaReview",
    "review_media",
]
