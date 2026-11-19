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
from minus.services.qbittorrent import QBittorrentError, TorrentAlreadyAdded
from minus.triage.pipeline import TriageError, TriagePipeline
from minus.triage.trace import read_trace

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
            self.triage_review,
            self.triage_preview,
            self.triage_vm_review,
            self.triage_promote,
            self.triage_trace,
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

    def torrent_add(self, url: str, paused: bool = False) -> dict:
        """Add a magnet link or torrent URL to qBittorrent.

        Downloads land in qBittorrent's configured save path, which should be
        the quarantine incoming directory. The add is not paused by default:
        quarantine inspects the completed copy, and qBittorrent has no resume
        call in this client, so a paused add would never finish.

        Only URL strings are accepted (magnet:, http:, https:). A filesystem
        path is refused: MINUS never hands a local file to qBittorrent.

        A torrent already in the session is reported as `already_present`
        rather than an error: the hash is what the next stage needs, and the
        listing finds it either way.

        After adding, use list_torrents/qbittorrent_find to get the new hash,
        then triage_download to stage it into quarantine.

        Args:
            url: A magnet link or an http(s) URL of a .torrent file.
            paused: Add paused; off by default so the download can finish.
        """
        candidate = self._candidate(url, "torrent_add")
        client = self.pipeline.client
        if client is None:
            raise ToolExecutionError("qBittorrent is not configured.")
        try:
            answer = client.add_torrent([candidate], paused=paused)
        except TorrentAlreadyAdded:
            return {"status": "already_present", "url": candidate, "paused": paused}
        except QBittorrentError as exc:
            raise ToolExecutionError(str(exc)) from exc
        return {"status": answer, "url": candidate, "paused": paused}

    def torrent_ingest(
        self,
        url: str,
        expected_title: str = "",
        release: bool = False,
    ) -> dict:
        """Add a torrent, wait for it, quarantine it, scan it, and optionally release it.

        This runs the whole pipeline: the download lands in the quarantine
        incoming directory, waits for completion, is copied into quarantine,
        and is scanned. With a configured Windows guest the scan opens every
        staged file, so a movie is reviewed by Windows Media Player rather than
        treated as "nothing to detonate". `release=True` moves a `clean` verdict into the library
        and asks qBittorrent to remove the download copy. Nothing else is
        released: a `needs_review` item stays in quarantine until
        triage_review has looked at it and triage_promote is called with
        `accept_review` and a reason. Anything malicious is deleted and
        returned with an alert.

        Only URL strings are accepted (magnet:, http:, https:); a filesystem
        path is refused, exactly as with torrent_add. The wait is bounded
        (MINUS_TRIAGE_DOWNLOAD_TIMEOUT); a torrent that never finishes comes
        back as a failed result rather than hanging the conversation.

        Args:
            url: A magnet link or an http(s) URL of a .torrent file.
            expected_title: The query/title the user asked for, used by the
                media review to flag a likely wrong file. It is not a trust
                signal: a matching title does not make a file safe, and a
                mismatch only raises a flag for the reader.
            release: Promote the result only if the verdict is `clean`; off by
                default so a caller can review first.
        """
        candidate = self._candidate(url, "torrent_ingest")
        return self._guard(
            self.pipeline.ingest,
            candidate,
            expected_title=expected_title,
            release=release,
        )

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

        Static inspection always runs. With a configured Windows guest, review
        mode opens every staged file -- including a movie in Windows Media
        Player -- and records process, network, file, registry and Defender
        evidence; that is what can make a media-only torrent `clean`. Without a
        guest the verdict is needs_review, never clean.

        Args:
            torrent_hash: The hash that was staged by triage_download.
            detonate: Whether to run the sandbox/review stage.
        """
        return self._guard(self.pipeline.scan, torrent_hash, detonate=detonate)

    def triage_verdict(self, torrent_hash: str) -> dict:
        """Read the recorded verdict for a staged torrent.

        Args:
            torrent_hash: The hash that was scanned by triage_scan.
        """
        payload = self._guard(self.pipeline.verdict, torrent_hash)
        static = payload.get("static") or {}
        evidence = getattr(self.pipeline, "vm_review_evidence", None)
        sandbox = (
            evidence(torrent_hash, payload) if callable(evidence) else payload.get("sandbox") or {}
        )
        return {
            "torrent_hash": payload.get("torrent_hash"),
            "name": payload.get("name"),
            "verdict": payload.get("verdict"),
            "static_flags": static.get("flags", []),
            "files": len(static.get("files") or []),
            "detonated": bool(sandbox.get("detonated")),
            "reviewed": bool(sandbox.get("reviewed")),
            "opened": int(sandbox.get("opened") or 0),
            "notes": payload.get("notes", ""),
        }

    def triage_review(self, torrent_hash: str) -> dict:
        """Report facts for a human decision about a needs_review item.

        Static results and a bounded media probe, with whether manual
        acceptance is allowed and what would still block it. This never
        promotes: accepting is a separate, explicit triage_promote.

        Args:
            torrent_hash: The hash that was scanned.
        """
        return self._guard(self.pipeline.review, torrent_hash)

    def triage_preview(self, torrent_hash: str, frames: int = 6, expected_title: str = "") -> dict:
        """Generate a contact sheet and sampled frames for a staged torrent.

        The images are written under `quarantine/<hash>/preview/` and their
        absolute paths are returned, so the model or operator can open them
        and confirm the content is the one that was asked for. This never
        promotes: accepting is still a separate, explicit triage_promote.

        Args:
            torrent_hash: The hash that was staged.
            frames: How many frames to sample across the running time.
            expected_title: The title the user asked for, compared against the
                file's own title to flag a likely wrong file.
        """
        return self._guard(
            self.pipeline.preview,
            torrent_hash,
            frames=frames,
            expected_title=expected_title,
        )

    def triage_vm_review(self, torrent_hash: str, keep: str = "") -> dict:
        """Open every staged file in the Windows guest and report what happened.

        This is the movie-capable review: the guest copies the torrent from a
        read-only CD, opens each file with Windows Media Player, Paint, Notepad
        or the shell association, and records process, network, file, registry
        and Defender evidence. Files named in `keep` stay in the working set;
        everything else is moved to the guest's graveyard and never promoted.

        It changes no verdict and promotes nothing. Promotion of a
        `needs_review` item requires this evidence, so run it before
        triage_promote when the guest cannot make the torrent `clean`.

        Args:
            torrent_hash: The hash that was staged by triage_download.
            keep: Comma-separated staged paths or basenames to keep; empty uses
                the primary-media rule (videos, else audio, else every file).
        """
        selected = [item.strip() for item in keep.split(",") if item.strip()]
        payload = self._guard(
            self.pipeline.vm_review,
            torrent_hash,
            keep=selected or None,
        )
        return {
            "torrent_hash": payload.get("torrent_hash"),
            "name": payload.get("name"),
            "reviewed_at": payload.get("reviewed_at"),
            "selected": payload.get("selected", []),
            "engine": payload.get("engine"),
            "detonated": bool(payload.get("detonated")),
            "opened": int(payload.get("opened") or 0),
            "defender_detections": payload.get("defender_detections", []),
            "processes": payload.get("processes", []),
            "network_endpoints": payload.get("network_endpoints", []),
            "file_writes": payload.get("file_writes", []),
            "registry_writes": payload.get("registry_writes", []),
            "notes": payload.get("notes", ""),
        }

    def triage_promote(
        self,
        torrent_hash: str,
        name: str = "",
        accept_review: bool = False,
        reason: str = "",
        keep: str = "",
    ) -> dict:
        """Move clean content into the library, with its verdict as provenance.

        `clean` is the normal gate. `accept_review` releases only
        `needs_review` passive media, after VM review has opened or launched a
        file and observed no signals, and after explicit operator acceptance of
        the facts triage_review reported; `suspicious` and `malicious` still
        refuse. Only kept `.mp4` files reach the flat `library/`; everything else
        is graveyard, and provenance is written under
        `memory/library_provenance/`.

        Args:
            torrent_hash: The hash that was scanned.
            name: Optional library name; defaults to the torrent's own name.
            accept_review: Release a needs_review item after VM review.
            reason: Why the review was accepted, recorded as provenance.
            keep: Comma-separated staged paths or basenames to promote; empty
                keeps videos, else audio, else every file.
        """
        selected = [item.strip() for item in keep.split(",") if item.strip()]
        return self._guard(
            self.pipeline.promote,
            torrent_hash,
            name=name,
            accept_review=accept_review,
            reason=reason,
            keep=selected or None,
        )

    def triage_trace(self, torrent_hash: str = "", limit: int = 200) -> dict:
        """Read what the pipeline recorded for a torrent, newest events last.

        This is the audit trail: stage transitions, scan findings and the
        reasons a stage was skipped. It changes nothing.

        Args:
            torrent_hash: Restrict to one torrent's events; empty reads all.
            limit: Maximum events to return.
        """
        path = self.pipeline.trace_file
        if not path:
            return {"events": [], "count": 0, "reason": "tracing is not configured"}
        events = read_trace(path, torrent_hash=torrent_hash, limit=limit)
        return {
            "torrent_hash": torrent_hash,
            "events": events,
            "count": len(events),
            "trace_file": str(path),
        }

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
