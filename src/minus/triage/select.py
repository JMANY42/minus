"""Choosing which torrent rows to hand to the triage pipeline.

qBittorrent's torrent list is a wide, loosely typed JSON array; the pipeline
only cares about a handful of fields and only about content that is actually
finished. This module is the narrow waist between the two: it filters, orders
and trims rows so that a caller (a tool, a CLI command) never has to reason
about incomplete downloads again.

Ordering is deliberate. Complete torrents come first, largest progress first,
then by name; a partial torrent ordering above a complete one is never useful
when the caller is about to ask "which one should I scan next?".
"""

from __future__ import annotations

TORRENT_FIELDS = ("hash", "name", "size", "progress", "state", "content_path")


def select_torrents(
    rows: list[dict],
    query: str,
    *,
    only_complete: bool = True,
    limit: int = 10,
) -> list[dict]:
    """Filter and rank `rows`, returning only `TORRENT_FIELDS` in each result.

    `query` is a case-insensitive substring of the name; an empty query matches
    everything. With `only_complete`, anything below 100% progress is dropped
    before ranking, so a caller asking for "complete" never sees a partial row
    even when `limit` would otherwise have room for one.
    """
    needle = query.casefold()
    matched: list[tuple[dict, float, str]] = []

    for row in rows:
        name = str(row.get("name") or "")
        if needle and needle not in name.casefold():
            continue
        progress = float(row.get("progress") or 0.0)
        if only_complete and progress < 1.0:
            continue
        matched.append((row, progress, name.casefold()))

    matched.sort(key=lambda item: (-item[1], item[2]))

    return [
        {field: row.get(field) for field in TORRENT_FIELDS}
        for row, _progress, _name in matched[:limit]
    ]
