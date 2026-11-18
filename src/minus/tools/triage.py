"""Model-facing tools for the triage pipeline.

Registered under the `triage` category, and only when qBittorrent is configured
(`assembly.build_triage_tools`), because every flow starts at the torrent list.

Each tool is a thin stage wrapper: the pipeline owns the order and the state on
disk, so the model cannot promote something it never scanned — `promote` re-reads
the verdict rather than trusting the conversation.
"""

from __future__ import annotations

import logging
from typing import Any

from minus.errors import ToolExecutionError
from minus.services.qbittorrent import QBittorrentError
from minus.triage.pipeline import TriageError, TriagePipeline

logger = logging.getLogger(__name__)

CATEGORY = "triage"

# What a listing shows. A torrent's full API record is long and mostly duplicated
# in the files endpoint; these are the fields a decision needs.
LIST_FIELDS = ("hash", "name", "size", "progress", "state", "content_path", "save_path")


class TriageTools:
    """The torrent-to-library stages, as tools."""

    def __init__(self, pipeline: TriagePipeline) -> None:
        self.pipeline = pipeline

    def register(self, registry: Any) -> None:
        for tool in (
            self.list_torrents,
            self.qbittorrent_find,
            self.torrent_add,
            self.torrent_ingest,
            self.torrent_progress,
            self.triage_download,
            self.triage_scan,
            self.triage_verdict,
            self.triage_promote,
        ):
            registry.tool(tool, category=CATEGORY)

    # ---- Stages ----

    def list_torrents(self) -> dict:
        """List the torrents qBittorrent knows about, with progress and state.

        Use this to find the hash of a finished download before triaging it.
        """
        client = self.pipeline.client
        if client is None:
            raise ToolExecutionError("qBittorrent is not configured.")
        torrents = self._guard(client.torrents)
        return {
            "torrents": [
                {field: torrent.get(field) for field in LIST_FIELDS} for torrent in torrents[:50]
            ],
            "count": len(torrents),
        }

    def qbittorrent_find(
        self, query: str = "", only_complete: bool = True, limit: int = 10
    ) -> dict:
        """Find torrents already in qBittorrent whose name matches a query.

        Only searches the download client's own list. Finding a new torrent is
        the harness download mode's discovery path; this tool never queries an
        index site.

        Args:
            query: Case-insensitive substring of the torrent name; empty lists all.
            only_complete: Skip torrents that have not finished downloading.
            limit: Maximum matches to return.
        """
        matches = self._guard(
            self.pipeline.find_torrents,
            query,
            only_complete=only_complete,
            limit=limit,
        )
        return {"matches": matches, "count": len(matches)}

    def torrent_add(self, url: str, paused: bool = True) -> dict:
        """Add a magnet link or torrent URL to qBittorrent.

        Downloads land in qBittorrent's configured save path, which should be
        the quarantine incoming directory. paused=True by default, so metadata
        fetching and checksumming happen before any payload data flows.

        Only URL strings are accepted (magnet:, http:, https:). A filesystem
        path is refused: MINUS never hands a local file to qBittorrent.

        After adding, use list_torrents/qbittorrent_find to get the new hash,
        then triage_download to stage it into quarantine.

        Args:
            url: A magnet link or an http(s) URL of a .torrent file.
            paused: Add paused so the download can be inspected first.
        """
        candidate = self._candidate(url, "torrent_add")
        client = self.pipeline.client
        if client is None:
            raise ToolExecutionError("qBittorrent is not configured.")
        answer = self._guard(client.add_torrent, [candidate], paused=paused)
        return {"status": answer, "url": candidate, "paused": paused}

    def torrent_ingest(self, url: str) -> dict:
        """Add a torrent, wait for it, quarantine it, and scan it in one call.

        This runs the whole pipeline: the download lands in the quarantine
        incoming directory, waits for completion, is copied into quarantine,
        and is scanned. It never promotes the result -- a clean verdict still
        needs an explicit triage_promote, and anything malicious is deleted and
        returned with an alert.

        Only URL strings are accepted (magnet:, http:, https:); a filesystem
        path is refused, exactly as with torrent_add. The wait is bounded
        (MINUS_TRIAGE_DOWNLOAD_TIMEOUT); a torrent that never finishes comes
        back as a failed result rather than hanging the conversation.

        Args:
            url: A magnet link or an http(s) URL of a .torrent file.
        """
        candidate = self._candidate(url, "torrent_ingest")
        return self._guard(self.pipeline.ingest, candidate)

    def torrent_progress(self, torrent_hash: str) -> dict:
        """Report a torrent's download progress without changing anything.

        Args:
            torrent_hash: The torrent's info hash, from torrent_add or a listing.
        """
        client = self.pipeline.client
        if client is None:
            raise ToolExecutionError("qBittorrent is not configured.")
        info = self._guard(client.torrent, torrent_hash)
        return {
            "hash": info.get("hash"),
            "name": info.get("name"),
            "progress": info.get("progress"),
            "state": info.get("state"),
            "eta": info.get("eta"),
            "size": info.get("size"),
        }

    def triage_download(self, torrent_hash: str) -> dict:
        """Copy a completed torrent into quarantine and write its manifest.

        Refuses an incomplete torrent. This only copies files: nothing is
        executed, and nothing leaves quarantine.

        Args:
            torrent_hash: The torrent's info hash, from list_torrents.
        """
        return self._guard(self.pipeline.download, torrent_hash)

    def triage_scan(self, torrent_hash: str, detonate: bool = True) -> dict:
        """Inspect a staged torrent and record a verdict in quarantine.

        Static inspection always runs. A detonation only happens when a sandbox
        is configured; without one the verdict is needs_review, never clean.

        Args:
            torrent_hash: The hash that was staged by triage_download.
            detonate: Whether to run the sandbox stage.
        """
        return self._guard(self.pipeline.scan, torrent_hash, detonate=detonate)

    def triage_verdict(self, torrent_hash: str) -> dict:
        """Read the recorded verdict for a staged torrent.

        Args:
            torrent_hash: The hash that was scanned by triage_scan.
        """
        payload = self._guard(self.pipeline.verdict, torrent_hash)
        static = payload.get("static") or {}
        sandbox = payload.get("sandbox") or {}
        return {
            "torrent_hash": payload.get("torrent_hash"),
            "name": payload.get("name"),
            "verdict": payload.get("verdict"),
            "static_flags": static.get("flags", []),
            "files": len(static.get("files") or []),
            "detonated": bool(sandbox.get("detonated")),
            "notes": payload.get("notes", ""),
        }

    def triage_promote(self, torrent_hash: str, name: str = "") -> dict:
        """Move clean content into the library, with its verdict as provenance.

        Refuses any verdict other than clean.

        Args:
            torrent_hash: The hash that was scanned.
            name: Optional library name; defaults to the torrent's own name.
        """
        return self._guard(self.pipeline.promote, torrent_hash, name=name)

    # ---- Errors ----

    @staticmethod
    def _candidate(url: str, tool: str) -> str:
        candidate = url.strip() if isinstance(url, str) else ""
        if not candidate:
            raise ToolExecutionError(
                f"{tool} needs a magnet:? or http(s) URL; got an empty string."
            )
        if not candidate.startswith(("magnet:?", "http://", "https://")):
            raise ToolExecutionError(
                f"{tool} needs a magnet:? or http(s) URL; filesystem paths and "
                f"other schemes are not accepted: {candidate!r}"
            )
        return candidate

    @staticmethod
    def _guard(call, *args, **kwargs):
        try:
            return call(*args, **kwargs)
        except (QBittorrentError, TriageError) as exc:
            # ToolError family, so the loop tells the model "retry with valid
            # arguments" rather than treating it as a crash.
            raise ToolExecutionError(str(exc)) from exc
