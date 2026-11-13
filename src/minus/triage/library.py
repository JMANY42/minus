"""Promoting already-cleared content into the library, without a verdict.

`TriagePipeline.promote` is the gate: it will not release anything short of a
`clean` verdict. This module is the other way in -- content the operator has
already vouched for -- and it exists separately so that "move these bytes into
the library and record where they came from" can be reused without dragging the
scanner and the sandbox along.

The move is arranged so the library never contains a half-copied tree. Same
filesystem: one `os.replace`, which is atomic. Different filesystem: the tree
is copied (and fsynced) into a hidden staging directory inside the library,
that directory is renamed into place atomically, and only then is the source
removed. The expensive case is the one the fast path exists to avoid; the slow
path still has to leave a coherent library if the process dies midway.
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path

from minus.services.detail import slugify
from minus.services.json import write_json

logger = logging.getLogger(__name__)


def _utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _tree_bytes(source: Path) -> int:
    """The size that is about to move, for the caller's log line and tests."""
    if source.is_file():
        return source.stat().st_size
    return sum(
        path.stat().st_size
        for path in source.rglob("*")
        if path.is_file() and not path.is_symlink()
    )


def _copy_file(source: Path, destination: Path) -> None:
    with source.open("rb") as reader, destination.open("xb") as writer:
        shutil.copyfileobj(reader, writer)
        writer.flush()
        # The staged tree is renamed into the library next; a crash after the
        # rename must not leave a file whose contents are still in page cache.
        os.fsync(writer.fileno())
    os.chmod(destination, 0o644)


def _copy_tree(source: Path, incoming: Path) -> None:
    """Build `incoming` as a copy of `source`, with library file modes."""
    if source.is_file():
        _copy_file(source, incoming)
        return

    incoming.mkdir()
    os.chmod(incoming, 0o755)
    for root, dirs, files in os.walk(source):
        root_path = Path(root)
        current = incoming / root_path.relative_to(source)
        os.chmod(current, 0o755)
        for name in dirs:
            child = current / name
            child.mkdir()
            os.chmod(child, 0o755)
        for name in files:
            _copy_file(root_path / name, current / name)


def _normalise_modes(target: Path) -> None:
    """Library content is readable by others; the source's modes do not travel.

    Applied after a same-filesystem rename too, so the two paths into the
    library produce the same result: directories 0o755, files 0o644.
    """
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


def promote_to_library(
    source: Path,
    library: Path,
    *,
    name: str,
    provenance: dict | None = None,
) -> dict:
    """Move `source` into `library` under `name`, recording where it came from.

    `source` may be a file or a directory and must already exist. The target is
    `library/<slug>`; a name already taken gets a UTC timestamp suffix rather
    than replacing the earlier promotion, because both are different content
    that the operator asked to keep.
    """
    source = Path(source)
    library = Path(library)

    if not source.exists():
        raise ValueError(f"Cannot promote missing source: {source}")

    if not library.exists():
        library.mkdir(parents=True)
        os.chmod(library, 0o755)

    target = library / slugify(name)
    if target.exists():
        target = library / f"{target.name}-{_utc_stamp()}"

    moved = _tree_bytes(source)
    try:
        os.replace(source, target)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        # Crossing filesystems: stage inside the library so the final rename is
        # still a same-device, atomic operation.
        incoming = library / f".incoming-{uuid.uuid4().hex}"
        try:
            _copy_tree(source, incoming)
            os.replace(incoming, target)
        except BaseException:
            if incoming.is_dir():
                shutil.rmtree(incoming, ignore_errors=True)
            else:
                incoming.unlink(missing_ok=True)
            raise
        if source.is_dir():
            shutil.rmtree(source)
        else:
            source.unlink()
        logger.info("Copied %s across filesystems into %s", source, target)

    _normalise_modes(target)

    provenance_path = library / f"{target.name}.provenance.json"
    write_json(
        provenance_path,
        {
            "promoted_at": _utc(),
            "name": name,
            "source": str(source),
            "target": str(target),
            "provenance": provenance or {},
        },
    )
    # `write_json` makes a file private by making its directory private too,
    # which is right for transcripts and wrong for a library other tools browse.
    os.chmod(library, 0o755)

    return {
        "moved_to": str(target),
        "provenance": str(provenance_path),
        "bytes": moved,
    }
