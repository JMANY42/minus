"""A cheap parser smoke test for staged media, inside the airlock.

This is not a detonation and cannot make anything `clean`. It runs `ffprobe`
under `airlock.run_isolated`: read-only input, no network, a fresh /tmp, and a
deadline. A valid container that ffprobe can open is evidence that the file is
a real media container; a crash, a timeout, or a parser rejection is evidence
worth showing a human who is about to accept the risk manually.

The parser itself is attack surface, which is why it runs inside the airlock and
not on the host. bwrap namespaces are not VM-grade containment; a kernel or
ffmpeg bug is still a boundary failure. That is exactly why the result is
advisory and the promotion it feeds is explicitly manual.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from minus.triage.airlock import run_isolated

MEDIA_SUFFIXES = frozenset(
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
        ".mp3",
        ".flac",
        ".m4a",
        ".aac",
        ".ogg",
        ".opus",
        ".wav",
    }
)

# What a human may accept without a detonation. Deliberately passive: video,
# audio, images, subtitles and plain text. PDFs, Office files, archives,
# executables and scripts are parser/active surface and need a real detonation.
PASSIVE_SUFFIXES = MEDIA_SUFFIXES | frozenset(
    {
        ".srt",
        ".ass",
        ".ssa",
        ".vtt",
        ".sub",
        ".txt",
        ".nfo",
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".gif",
    }
)

# Flags that say "this is active content or an archive", so a static-only
# scan is not enough for a manual release.
MANUAL_BLOCKING_FLAGS = frozenset(
    {
        "windows-pe",
        "elf-binary",
        "macho-binary",
        "unsigned-pe",
        "packed-section",
        "double-extension",
        "macro-enabled-document",
        "script",
        "shortcut",
        "archive",
        "archive-traversal",
        "archive-encrypted",
        "archive-bomb",
        "archive-too-large",
        "too-many-zip-members",
        "too-many-files",
        "no-extension",
        "executable-bit",
    }
)


LINUX_ACTIVE_SUFFIXES = frozenset(
    {".sh", ".bash", ".zsh", ".fish", ".py", ".pyw", ".pl", ".rb", ".lua", ".jar"}
)

# Media structures a detonation cannot clear. A container carrying `data` or
# `attachment` streams is a second file inside the first, and bytes that do not
# match the format their suffix promises are either mislabelled or a polyglot.
# `media:probe-failed`, `media:no-frames` and `media:title-mismatch` stay visible
# in the payload but are deliberately not here: a parser that could not answer,
# or a title that does not look like the query, is a question for the human, not
# proof of hiding.
MEDIA_BLOCKING_FLAGS = frozenset(
    {
        "media:data-stream",
        "media:attachment-stream",
        "media:extension-mismatch",
    }
)


def clean_review_blockers(payload: dict) -> list[str]:
    """Files a Windows detonation did not exercise and Linux could execute.

    The guest harness launches Windows shapes (`.exe`, `.bat`, `.ps1`, `.msi`,
    `.lnk`, `.scr`, `.com`); it never runs ELF, Mach-O, or Linux scripts. A
    Windows `clean` verdict is therefore not evidence for those files, even
    when another launchable artifact in the same torrent was detonated.
    """
    static = payload.get("static") or {}
    blockers: list[str] = []
    for entry in static.get("files") or []:
        relative = str(entry.get("relative") or "")
        kind = str(entry.get("kind") or "")
        suffix = Path(relative).suffix.lower()
        if kind in ("elf", "macho") or suffix in LINUX_ACTIVE_SUFFIXES:
            blockers.append(f"file:{relative} ({kind or suffix or 'unknown'})")
    return blockers


def manual_review_blockers(payload: dict) -> list[str]:
    """Why a `needs_review` payload cannot be released by manual acceptance.

    A movie with a high entropy flag is fine: compressed video is expected to
    be noisy. A PE, script, archive, PDF or Office document is not, because the
    static scan cannot prove it passive. Those need a detonation.
    """
    static = payload.get("static") or {}
    airlock = payload.get("airlock") or {}
    flags = set(static.get("flags") or []) | set(airlock.get("flags") or [])
    blockers = [f"flag:{flag}" for flag in sorted(flags & MANUAL_BLOCKING_FLAGS)]

    # Media flags are already namespaced (`media:...`), so they are reported as
    # they are; prefixing them again would make the two lists unreadable side by
    # side, and the operator is comparing exactly those two lists.
    media = payload.get("media") or {}
    media_flags = set(media.get("flags") or [])
    blockers.extend(sorted(media_flags & MEDIA_BLOCKING_FLAGS))

    files = static.get("files") or []
    if not files:
        blockers.append("no files were staged")
    for entry in files:
        relative = str(entry.get("relative") or "")
        suffix = Path(relative).suffix.lower()
        if suffix not in PASSIVE_SUFFIXES:
            blockers.append(f"file:{relative} ({suffix or 'no extension'})")
    return blockers


# A season pack can have dozens of files. Five probes is enough to show that
# the archive opens and keeps the review bounded at seconds, not minutes.
MAX_PROBES = 5
PROBE_TIMEOUT = 8.0


def media_files(root: Path) -> list[Path]:
    """Regular media files under `root`, sorted; symlinks are never followed."""
    root = Path(root)
    return [
        path
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink() and path.suffix.lower() in MEDIA_SUFFIXES
    ]


def probe_media(
    root: Path,
    *,
    limit: int = MAX_PROBES,
    timeout: float = PROBE_TIMEOUT,
) -> list[dict]:
    """Run ffprobe on the first few media files and report what it saw.

    Every failure is a fact (`failed`, `timed_out`, `unavailable`), never an
    exception: a review command must still print the rest of the verdict when
    one file cannot be parsed.
    """
    root = Path(root)
    files = media_files(root)
    if not files:
        return []
    if shutil.which("ffprobe") is None:
        return [{"probe": "unavailable", "reason": "ffprobe is not installed"}]

    results: list[dict] = []
    for path in files[: max(1, limit)]:
        relative = path.relative_to(root).as_posix()
        item: dict = {"file": relative, "probe": "failed"}
        try:
            run = run_isolated(
                root,
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=format_name,duration,size",
                    "-of",
                    "json",
                    relative,
                ],
                timeout=timeout,
            )
        except Exception as exc:  # bwrap missing, or the command could not start
            item.update(probe="unavailable", reason=str(exc))
            results.append(item)
            break

        item["timed_out"] = run["timed_out"]
        item["exit_code"] = run["exit_code"]
        if run["timed_out"]:
            item["probe"] = "timed_out"
        elif run["exit_code"] == 0:
            try:
                payload = json.loads(run["stdout"])
            except json.JSONDecodeError:
                item["probe"] = "unparsable"
            else:
                item["probe"] = "ok"
                item["format"] = payload.get("format", {})
        else:
            item["stderr"] = (run["stderr"] or "")[:200]
        results.append(item)

    skipped = len(files) - len(results)
    if skipped > 0:
        results.append({"probe": "skipped", "reason": f"{skipped} more media file(s) not probed"})
    return results
