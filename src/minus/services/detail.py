"""Where the deep tier's written answers land.

Escalated answers come back in two channels. The short one is spoken. This is
the other one: the full write-up, which can run to pages and cite file paths
and line numbers -- all of it useless through a speech synthesizer and most of
it valuable on a screen.

Keeping the sink behind a protocol means the composition root decides where
that text goes. Today it is a file per answer; the dashboard in TODO.md is the
intended second implementation, and it will not require touching the tier that
produced the text.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import Path

from minus import paths
from minus.services.json import write_json

logger = logging.getLogger(__name__)

_UNSAFE = re.compile(r"[^a-z0-9]+")


def slugify(title: str, *, limit: int = 48) -> str:
    """A filesystem-safe stub of `title`, for eyeballing a directory listing."""
    slug = _UNSAFE.sub("-", title.lower()).strip("-")
    return slug[:limit].rstrip("-") or "untitled"


class FileDetailSink:
    """A DetailSink writing one JSON file per answer.

    JSON rather than raw markdown so the title and timestamp travel with the
    text, matching how condensed conversations are already stored. `write_json`
    is atomic, so a reader (a dashboard tailing the directory) never sees a
    half-written note.
    """

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = Path(directory) if directory is not None else paths.deep_notes_dir()

    def publish(self, title: str, detail: str) -> None:
        created_at = datetime.now(UTC)
        stamp = created_at.strftime("%Y%m%dT%H%M%SZ")
        path = self.directory / f"{stamp}-{slugify(title)}.json"

        try:
            write_json(
                path,
                {
                    "title": title,
                    "created_at": created_at.isoformat(),
                    "detail": detail,
                },
            )
        except OSError:
            # Consistent with the transcript writer: losing a note is bad, but
            # taking down a live conversation over it is worse.
            logger.exception("Could not write deep note to %s", path)
            return

        logger.info("Deep note written to %s", path)
