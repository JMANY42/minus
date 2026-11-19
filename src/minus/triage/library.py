"""Promoting already-cleared content into the library, without a verdict.

`TriagePipeline.promote` is the gate: it will not release anything short of a
`clean` verdict. This module is the other way in -- content the operator has
already vouched for -- and it exists separately so that "copy these bytes into
the library and record where they came from" can be reused without dragging the
scanner and the sandbox along.

The library is deliberately flat and deliberately MP4-only: it is what a Jellyfin
library points at, and a media scanner should see movies, not release folders,
artwork, subtitles or provenance sidecars. Provenance is written under
``memory/library_provenance/`` instead. Selected files are copied (and fsynced)
into a hidden staging directory next to the library, then renamed into place one
at a time; a crash cannot leave a half-written MP4 in the library.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from minus.services.detail import slugify
from minus.services.json import write_json

logger = logging.getLogger(__name__)

# What a library is for: the thing a person asked for, not the release's
# artwork, subtitles and NFO. The review still opens every file; only MP4s are
# promoted because that is the format the library is kept in.
MP4_SUFFIX = ".mp4"
VIDEO_SUFFIXES = frozenset(
    {".mp4", ".m4v", ".mkv", ".avi", ".mov", ".webm", ".mpg", ".mpeg", ".wmv", ".flv", ".ts"}
)
AUDIO_SUFFIXES = frozenset({".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav"})


def choose_primary_files(entries: list[dict]) -> list[str]:
    """The manifest entries worth reviewing, in manifest order.

    Every video is kept, not just the largest: a season pack is one torrent and
    every episode is the content. Artwork, subtitles and URL text are release
    furniture, so they stay out of the keep set. A torrent with no video keeps
    its audio, and one with neither keeps every file. `promote` then narrows the
    result to `.mp4`; this function only decides what the review should treat as
    the primary content.
    """

    def suffix(entry: dict) -> str:
        return Path(str(entry.get("relative") or "")).suffix.lower()

    for allowed in (VIDEO_SUFFIXES, AUDIO_SUFFIXES):
        selected = [
            str(entry["relative"])
            for entry in entries
            if entry.get("relative") and suffix(entry) in allowed
        ]
        if selected:
            return selected
    return [str(entry["relative"]) for entry in entries if entry.get("relative")]


def _utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _copy_file(source: Path, destination: Path) -> None:
    with source.open("rb") as reader, destination.open("xb") as writer:
        shutil.copyfileobj(reader, writer)
        writer.flush()
        # The staged file is renamed into the library next; a crash after the
        # rename must not leave a file whose contents are still in page cache.
        os.fsync(writer.fileno())
    os.chmod(destination, 0o644)


def _normalise_modes(target: Path) -> None:
    """Library content is readable by others; the source's modes do not travel."""
    if target.is_file():
        os.chmod(target, 0o644)
        return
    os.chmod(target, 0o755)
    for root, _dirs, files in os.walk(target):
        root_path = Path(root)
        if root_path != target:
            os.chmod(root_path, 0o755)
        for name in files:
            os.chmod(root_path / name, 0o644)


def _provenance_path(library: Path, provenance_dir: Path | None, name: str) -> Path:
    base = (
        Path(provenance_dir)
        if provenance_dir is not None
        else Path(library).parent / "memory" / "library_provenance"
    )
    base.mkdir(parents=True, exist_ok=True)
    os.chmod(base, 0o700)
    slug = slugify(name)
    path = base / f"{slug}.provenance.json"
    if path.exists():
        path = base / f"{slug}-{_utc_stamp()}.provenance.json"
    return path


def promote_files_to_library(
    sources: list[tuple[Path, str]],
    library: Path,
    *,
    name: str,
    provenance: dict | None = None,
    provenance_dir: Path | None = None,
) -> dict:
    """Copy selected MP4s into the flat library, with provenance outside it.

    `sources` pairs each real staged file with the name it should have in the
    library. Non-MP4 entries are refused rather than silently copied: the
    library is a movies view, not a second quarantine. Provenance is written to
    `provenance_dir` (default `memory/library_provenance/`), never beside the
    media, so a Jellyfin scan sees only `.mp4` files.
    """
    library = Path(library)
    pairs = [(Path(source), Path(relative)) for source, relative in sources]
    if not pairs:
        raise ValueError("No files selected for promotion")

    mp4_pairs = [
        (source, relative) for source, relative in pairs if source.suffix.lower() == MP4_SUFFIX
    ]
    if not mp4_pairs:
        raise ValueError("No .mp4 files selected; the library only accepts .mp4")

    missing = [str(source) for source, _ in mp4_pairs if not source.is_file()]
    if missing:
        raise ValueError(f"Cannot promote missing source: {missing[0]}")

    if not library.exists():
        library.mkdir(parents=True)
        os.chmod(library, 0o755)

    staging = Path(tempfile.mkdtemp(prefix=".library-incoming-", dir=library.parent))
    os.chmod(staging, 0o755)
    moved = 0
    try:
        for source, relative in mp4_pairs:
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"Unsafe library-relative path: {relative!r}")
            destination = staging / source.name
            if destination.suffix.lower() != MP4_SUFFIX:
                raise ValueError(f"Not an .mp4 source: {source.name!r}")
            if destination.exists():
                destination = staging / f"{destination.stem}-{_utc_stamp()}{destination.suffix}"
            _copy_file(source, destination)
            moved += source.stat().st_size

        targets: list[Path] = []
        for staged in sorted(staging.iterdir()):
            target = library / staged.name
            if target.exists():
                target = library / f"{target.stem}-{_utc_stamp()}{target.suffix}"
            os.replace(staged, target)
            _normalise_modes(target)
            targets.append(target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    provenance_path = _provenance_path(library, provenance_dir, name)
    file_names = [target.name for target in targets]
    write_json(
        provenance_path,
        {
            "promoted_at": _utc(),
            "name": name,
            "source": str(mp4_pairs[0][0].parent) if len(mp4_pairs) == 1 else "multiple",
            "target": str(library),
            "files": file_names,
            "provenance": provenance or {},
        },
    )
    os.chmod(library, 0o755)

    return {
        "moved_to": str(targets[0]) if len(targets) == 1 else str(library),
        "provenance": str(provenance_path),
        "bytes": moved,
        "files": file_names,
        "targets": [str(target) for target in targets],
    }


def promote_to_library(
    source: Path,
    library: Path,
    *,
    name: str,
    provenance: dict | None = None,
    provenance_dir: Path | None = None,
) -> dict:
    """Promote the MP4s under `source` into the flat library.

    A file source must itself be an `.mp4`; a directory source is searched
    recursively for `.mp4` files, and the source tree is removed only after the
    copies succeed. This is the operator-vouched entry point; the verdict gate
    still lives in `TriagePipeline.promote`.
    """
    source = Path(source)
    if not source.exists():
        raise ValueError(f"Cannot promote missing source: {source}")

    if source.is_file():
        files = [source] if source.suffix.lower() == MP4_SUFFIX else []
    else:
        files = [
            path
            for path in sorted(source.rglob("*"))
            if path.is_file() and not path.is_symlink() and path.suffix.lower() == MP4_SUFFIX
        ]
    if not files:
        raise ValueError(f"No .mp4 files under {source}")

    result = promote_files_to_library(
        [(path, path.name) for path in files],
        library,
        name=name,
        provenance=provenance,
        provenance_dir=provenance_dir,
    )
    if source.is_dir():
        shutil.rmtree(source)
    else:
        source.unlink()
    return result
