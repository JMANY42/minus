"""Torrent in, verdict out: quarantine, inspect, detonate, judge, promote.

The order is the point. Content is copied out of the download directory before
anything touches it, nothing is ever executed on this host, and content only
reaches the library when a verdict says `clean` — which, per
`triage/scoring.py`, requires a detonation that observed nothing.

Every stage leaves a file behind: `manifest.json` before analysis, `verdict.json`
after it. Both are JSON written atomically through `services/json.py`, so a
dashboard or a second tool can read them without racing a writer.

`ingest` chains add -> wait -> download -> scan for the harness; it deliberately
stops before promotion. An attached `notify` observer sees each stage, including
the malicious-payload deletion, without being able to break the pipeline.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shlex
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from minus.errors import MinusError
from minus.services.json import read_json, write_json
from minus.services.qbittorrent import QBittorrentError
from minus.triage.airlock import inspect_tree
from minus.triage.library import choose_primary_files, promote_files_to_library
from minus.triage.media import clean_review_blockers, manual_review_blockers, probe_media
from minus.triage.media_review import MediaReview, review_media
from minus.triage.review import ModelReviewer
from minus.triage.sandbox import DryRunRunner, SandboxReport, SandboxRunner
from minus.triage.scoring import CLEAN, MALICIOUS, NEEDS_REVIEW, Verdict, decide
from minus.triage.static import HEAD_HASH_BYTES, analyse, sha256_file
from minus.triage.trace import TraceEvent, Tracer, new_op_id, trace_span

logger = logging.getLogger(__name__)

# A torrent hash is a path component in everything below, so it is validated as
# one thing: hex, and nothing else.
_HASH = re.compile(r"\A[0-9a-fA-F]{6,64}\Z")

# A magnet may carry several `xt` entries; only the BitTorrent v1 SHA-1 form
# is a 40-hex hash, and that is the one qBittorrent lists.
_MAGNET_HASH = re.compile(r"xt=urn:btih:([0-9a-fA-F]{40})", re.IGNORECASE)

DEFAULT_MAX_BYTES = 5 * 1024**3

# A container that carries a data or attachment stream, or whose bytes are not
# the format its name claims, is a second file inside the first. A detonation
# cannot clear that shape, so a `clean` verdict carrying one of these is held at
# `needs_review`: parser facts are advisory, but this particular anomaly is not
# something a human should have to notice for themselves.
CLEAN_BLOCKING_MEDIA_FLAGS = frozenset(
    {
        "media:data-stream",
        "media:attachment-stream",
        "media:extension-mismatch",
        "media:no-video-stream",
    }
)


class TriageError(MinusError):
    """The pipeline refused, or could not continue."""


def _utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _malicious_alert(torrent_hash: str, name: Any) -> str:
    return (
        f"ALERT: malicious torrent {torrent_hash} ({name}) was "
        "deleted from quarantine; verdict kept in the rejected record."
    )


def _torrent_matches(torrent: dict, url: str) -> bool:
    """Whether a listed torrent is plausibly the one `url` asked for."""
    wanted = url.lower()
    magnet = str(torrent.get("magnet_uri") or torrent.get("magnet") or "").lower()
    if magnet and (wanted in magnet or magnet in wanted):
        return True
    named = _MAGNET_HASH.search(magnet)
    if named is not None:
        listed = _MAGNET_HASH.search(url)
        if listed is not None and named.group(1).lower() == listed.group(1).lower():
            return True
    name = str(torrent.get("name") or "").lower()
    return bool(name) and name in wanted


def _read_optional(path: Path) -> dict | None:
    """`read_json`, but a missing file is an answer rather than an exception."""
    return read_json(path) if path.is_file() else None


def _harden_staged_dirs(root: Path, directory: Path) -> None:
    """Make every directory MINUS created under `root` private to this user.

    Staging copies a stranger's tree, so the umask is not trusted for either
    files or the directories that hold them: another local user being able to
    read or enter quarantine defeats the point of quarantining it.
    """
    os.chmod(root, 0o700)
    current = directory
    while current != root and root in current.parents:
        os.chmod(current, 0o700)
        current = current.parent


@dataclass
class TriagePipeline:
    """One quarantine, one library, and the stages between them."""

    quarantine: Path
    library: Path
    # Provenance is evidence, not media; keep it out of the Jellyfin-facing
    # library. None means `<library parent>/memory/library_provenance`.
    provenance_dir: Path | None = None
    client: Any | None = None
    sandbox: SandboxRunner = field(default_factory=DryRunRunner)
    max_bytes: int = DEFAULT_MAX_BYTES
    require_detonation: bool = True
    sandbox_timeout: float = 180.0
    vpn_interface: str = ""
    qbittorrent_vpn_interface: str = ""
    require_vpn: bool = True
    delete_malicious: bool = True
    # Once content is released or a malicious payload rejected, the copy
    # qBittorrent downloaded is redundant; asking it to remove the torrent (and
    # its bytes) keeps the download directory from growing a second library.
    # A cleanup failure is reported in the result, never raised.
    auto_cleanup: bool = True
    # The harness attaches an observer here to render live progress and the
    # malicious-payload alert; the pipeline itself writes state to disk and
    # must work with no listener at all.
    notify: Callable[[str, dict], None] | None = None
    # The model stage reads code-shaped files before `decide` weighs them. It
    # can escalate a verdict, never clear one, and is absent when the caller
    # did not want a second model call.
    reviewer: ModelReviewer | None = None
    # Media sampling runs ffprobe/ffmpeg inside the airlock after the static
    # pass. It is advisory, and it costs a probe plus one decode per frame, so
    # a caller can turn it off and `media_frames` bounds the work when it is on.
    media_enabled: bool = True
    media_frames: int = 6
    # A real Windows guest can open a movie in Windows Media Player and watch
    # the same process/network/file/registry telemetry a launchable sample
    # gets. That is the only way a media-only torrent can earn a `clean`
    # verdict; the host-side ffprobe pass is evidence, not a detonation. It is
    # on by default because promotion of anything not VM-reviewed is refused.
    vm_review_media: bool = True
    download_timeout: float = 3600.0
    poll_seconds: float = 5.0
    # An observer like `notify`, but with a durable, ordered record behind it:
    # `JsonlTracer(settings.triage_trace_path)` when the caller wants to replay
    # a run afterwards. Absent means the pipeline behaves exactly as before.
    tracer: Tracer | None = None

    # ---- Paths and tracing ----

    @property
    def trace_file(self) -> Path | None:
        """Where trace events land, when the tracer writes to a file."""
        return getattr(self.tracer, "path", None)

    def _trace(
        self,
        op_id: str,
        stage: str,
        event: str,
        torrent_hash: str = "",
        *,
        payload: dict[str, Any] | None = None,
        **data: Any,
    ) -> None:
        """Emit one event if a tracer is attached, never at the pipeline's expense.

        A tracer is a reporter, not a dependency: even a broken custom tracer
        must not turn a completed stage into an exception, so its errors are
        logged at debug and dropped. `payload` exists because a data key can
        be named `torrent_hash` (the model wants it in both places for the
        skip case), which `**data` cannot spell without colliding.
        """
        if self.tracer is None:
            return
        merged = dict(payload or {})
        merged.update(data)
        try:
            self.tracer.emit(
                TraceEvent(
                    stage=stage,
                    event=event,
                    op_id=op_id,
                    torrent_hash=torrent_hash,
                    data=merged,
                )
            )
        except Exception:
            logger.debug("triage tracer failed for %s/%s", stage, event, exc_info=True)

    # ---- Notifications ----

    def _notify(self, kind: str, payload: dict) -> None:
        """Tell an attached harness about a stage, never at its expense.

        A dashboard failing to render must not turn a completed scan into an
        exception, so observer errors are logged and swallowed.
        """
        if self.notify is None:
            return
        try:
            self.notify(kind, payload)
        except Exception:
            logger.debug("triage observer failed for %s", kind, exc_info=True)

    # ---- qBittorrent cleanup ----

    def _cleanup_download(self, torrent_hash: str) -> dict:
        """Ask qBittorrent to drop a torrent and the bytes it downloaded.

        Housekeeping happens after the decision, never as part of it: the
        content is already in the library or already rejected, so a client
        failure is reported in the result rather than raised at the caller.
        `needs_review` is deliberately not cleaned up -- those bytes are what
        a human is being asked to look at.
        """
        if not self.auto_cleanup:
            return {"skipped": True, "reason": "auto cleanup is disabled"}
        if self.client is None:
            return {"skipped": True, "reason": "qBittorrent is not configured"}
        try:
            self.client.delete_torrent(torrent_hash.lower(), delete_files=True)
        except Exception as exc:
            logger.warning("Could not remove torrent %s from qBittorrent: %s", torrent_hash, exc)
            return {"deleted": False, "error": str(exc)}
        return {"deleted": True}

    # ---- Paths ----

    def root(self, torrent_hash: str) -> Path:
        if not _HASH.match(torrent_hash or ""):
            raise TriageError(f"Not a torrent hash: {torrent_hash!r}")
        return Path(self.quarantine) / torrent_hash.lower()

    def files_root(self, torrent_hash: str) -> Path:
        return self.root(torrent_hash) / "files"

    def manifest_path(self, torrent_hash: str) -> Path:
        return self.root(torrent_hash) / "manifest.json"

    def verdict_path(self, torrent_hash: str) -> Path:
        return self.root(torrent_hash) / "verdict.json"

    def vm_review_path(self, torrent_hash: str) -> Path:
        return self.root(torrent_hash) / "vm_review.json"

    # ---- Selection ----

    def _review_artifacts(self, torrent_hash: str) -> list[Path]:
        """Every non-empty staged file, in the order the review ISO uses."""
        root = self.files_root(torrent_hash)
        return [
            path
            for path in sorted(root.rglob("*"))
            if path.is_file() and not path.is_symlink() and path.stat().st_size > 0
        ]

    def _selected_files(self, manifest: dict, keep: list[str] | None = None) -> list[str]:
        """Manifest-relative paths to promote; everything else is graveyard.

        Explicit `keep` entries are matched against the manifest's relative
        paths or their basenames, so a caller can say `movie.mp4` without
        repeating the release directory. With no explicit list the primary
        media rule applies: videos, else audio, else every file.
        """
        entries = [entry for entry in (manifest.get("entries") or []) if entry.get("relative")]
        if keep:
            selected: list[str] = []
            for item in keep:
                wanted = str(item).replace("\\", "/").strip().lstrip("./")
                matches = [
                    str(entry["relative"])
                    for entry in entries
                    if str(entry["relative"]) == wanted
                    or Path(str(entry["relative"])).name == wanted
                ]
                if not matches:
                    raise TriageError(f"No staged file matches keep entry: {item!r}")
                selected.extend(match for match in matches if match not in selected)
            if selected:
                return selected
        selected = choose_primary_files(entries)
        if not selected:
            raise TriageError("Manifest lists no files to promote.")
        return selected

    def _guest_keep_names(
        self,
        torrent_hash: str,
        manifest: dict,
        selected: list[str],
    ) -> set[str]:
        """The sanitized names the guest should keep out of its graveyard."""
        from minus.triage.windows_vm import guest_name

        root = self.files_root(torrent_hash)
        order = {
            path.relative_to(root): index
            for index, path in enumerate(self._review_artifacts(torrent_hash))
        }
        names: set[str] = set()
        for relative in selected:
            path = Path(relative)
            index = order.get(path)
            if index is not None:
                names.add(guest_name(index, root / path))
        return names

    def vm_review_evidence(self, torrent_hash: str, payload: dict) -> dict:
        """The verdict's sandbox facts, or the standalone review file's.

        `triage vm` deliberately does not rewrite the verdict; it writes
        `vm_review.json` beside it. Promotion and review facts must see that
        evidence, otherwise a successful review would be invisible to the gate.
        """
        sandbox = dict(payload.get("sandbox") or {})
        review = _read_optional(self.vm_review_path(torrent_hash)) or {}
        for key in (
            "engine",
            "completed",
            "detonated",
            "opened",
            "defender_detections",
            "processes",
            "network_endpoints",
            "file_writes",
            "registry_writes",
            "notes",
        ):
            if key in review and sandbox.get(key) in (None, False, 0, "", []):
                sandbox[key] = review[key]
        if review:
            sandbox["reviewed"] = True
            sandbox.setdefault("selected", review.get("selected") or [])
        return sandbox

    @staticmethod
    def _sandbox_payload(
        run: SandboxReport | None,
        *,
        reviewed: bool = False,
    ) -> dict | None:
        if run is None:
            return None
        return {
            "engine": run.engine,
            "completed": run.completed,
            "detonated": run.detonated,
            "opened": run.opened,
            "reviewed": reviewed,
            "defender_detections": list(run.defender_detections),
            "processes": list(run.processes),
            "network_endpoints": list(run.network_endpoints),
            "file_writes": list(run.file_writes),
            "registry_writes": list(run.registry_writes),
            "notes": run.notes,
        }

    def vm_review(
        self,
        torrent_hash: str,
        *,
        keep: list[str] | None = None,
        op_id: str = "",
    ) -> dict:
        """Open every staged file in the Windows guest, changing no verdict.

        This is the explicit review pass for a torrent whose verdict already
        exists (or whose files were staged without a detonation). `scan` runs
        the same mode automatically when a guest is configured; this method
        exists so an operator can re-run it and read per-file evidence without
        rescanning static analysis. It never promotes.
        """
        manifest = _read_optional(self.manifest_path(torrent_hash))
        if manifest is None:
            raise TriageError(f"No manifest for {torrent_hash!r}; run the download stage first.")

        checker = getattr(self.sandbox, "review_unavailable_reason", None)
        reason = checker() if callable(checker) else "the configured sandbox has no review mode"
        if reason:
            raise TriageError(reason)

        selected = self._selected_files(manifest, keep)
        keep_names = self._guest_keep_names(torrent_hash, manifest, selected)
        artifacts = self._review_artifacts(torrent_hash)
        op_id = op_id or new_op_id("vm-review", torrent_hash)
        with trace_span(
            self.tracer,
            op_id=op_id,
            stage="vm_review",
            event="review",
            torrent_hash=torrent_hash,
            artifacts=len(artifacts),
            kept=len(keep_names),
        ) as span:
            run = self.sandbox.run(
                artifacts,
                timeout=self.sandbox_timeout,
                review=True,
                keep=keep_names,
            )
            span.update(
                {
                    "detonated": run.detonated,
                    "opened": run.opened,
                    "engine": run.engine,
                }
            )
        payload = {
            "torrent_hash": torrent_hash.lower(),
            "name": manifest.get("name"),
            "reviewed_at": _utc(),
            "selected": selected,
            **(self._sandbox_payload(run, reviewed=True) or {}),
        }
        write_json(self.vm_review_path(torrent_hash), payload)
        return payload

    # ---- Stage 1: fetch ----

    def download(self, torrent_hash: str, *, op_id: str = "", expected_title: str = "") -> dict:
        """Copy a completed torrent's content into quarantine, with a manifest.

        Refuses an incomplete torrent rather than staging half of one: a verdict
        on a partial download would describe files that do not exist.

        `expected_title` is the name the user asked for, kept in the manifest so
        a later media sample can compare the container's title against it
        without the caller having to remember what the query was.
        """
        op_id = op_id or new_op_id("download", torrent_hash)
        with trace_span(
            self.tracer,
            op_id=op_id,
            stage="download",
            event="stage",
            torrent_hash=torrent_hash,
        ) as result:
            summary = self._download(torrent_hash, op_id=op_id, expected_title=expected_title)
            result.update(summary)
            return summary

    def _download(self, torrent_hash: str, *, op_id: str, expected_title: str = "") -> dict:
        if self.client is None:
            self._trace(
                op_id,
                "download",
                "refused",
                torrent_hash,
                reason="qBittorrent is not configured",
            )
            raise TriageError("qBittorrent is not configured (set MINUS_QBITTORRENT_URL).")

        # Validate before anything else: the hash becomes a path component, and
        # a client call is not worth making for something that cannot be staged.
        root = self.root(torrent_hash)

        # Fail closed: enforcement on with no interface named is a
        # misconfiguration, not permission to fetch over the raw route. The
        # qBittorrent binding wins when both are set: its process is the one
        # that downloads, so its own setting is the stronger assertion.
        if self.require_vpn:
            from minus.services.vpn import VpnError, require_torrent_tunnel, require_tunnel

            try:
                if self.qbittorrent_vpn_interface:
                    require_torrent_tunnel(self.client, self.qbittorrent_vpn_interface)
                elif self.vpn_interface:
                    require_tunnel(self.vpn_interface)
                else:
                    self._trace(
                        op_id,
                        "vpn",
                        "preflight.refused",
                        torrent_hash,
                        reason="no VPN interface configured",
                    )
                    raise TriageError(
                        "VPN enforcement is on but no interface is configured "
                        "(MINUS_QBITTORRENT_VPN_INTERFACE or MINUS_VPN_INTERFACE); "
                        "no VPN interface configured means no download is safe."
                    )
            except VpnError as exc:
                self._trace(
                    op_id,
                    "vpn",
                    "preflight.refused",
                    torrent_hash,
                    reason=str(exc),
                )
                raise TriageError(str(exc)) from exc
            self._trace(
                op_id,
                "vpn",
                "preflight.done",
                torrent_hash,
                interface=self.qbittorrent_vpn_interface or self.vpn_interface,
            )

        info = self.client.torrent(torrent_hash)
        progress = float(info.get("progress") or 0.0)
        if progress < 1.0:
            self._trace(
                op_id,
                "download",
                "refused",
                torrent_hash,
                reason="torrent is incomplete",
                progress=progress,
            )
            raise TriageError(
                f"Torrent {torrent_hash!r} is {progress:.0%} complete; refusing to triage it."
            )

        content = Path(str(info.get("content_path") or ""))
        if not str(content) or not content.exists():
            self._trace(
                op_id,
                "download",
                "refused",
                torrent_hash,
                reason="content is not on disk",
                content=str(content),
            )
            raise TriageError(
                f"qBittorrent reports content at {content}, which is not on disk. "
                "Is the download directory visible to MINUS?"
            )

        if root.exists():
            # Re-staging is deliberate: a previous manifest for the same hash
            # describes the same bytes, and leaving two would be ambiguous.
            shutil.rmtree(root)
        files_dir = root / "files"
        files_dir.mkdir(parents=True)
        _harden_staged_dirs(root, files_dir)

        entries: list[dict] = []
        total = 0

        sources = (
            sorted(path for path in content.rglob("*") if path.is_file() and not path.is_symlink())
            if content.is_dir()
            else [content]
        )
        if not sources:
            self._trace(
                op_id,
                "download",
                "refused",
                torrent_hash,
                reason="nothing to copy",
                content=str(content),
            )
            raise TriageError(f"Nothing to copy from {content}")

        for source in sources:
            size = source.stat().st_size
            total += size
            if total > self.max_bytes:
                shutil.rmtree(root, ignore_errors=True)
                self._trace(
                    op_id,
                    "download",
                    "refused",
                    torrent_hash,
                    reason="content exceeds the staging cap",
                    total_bytes=total,
                    max_bytes=self.max_bytes,
                )
                raise TriageError(
                    f"Content exceeds the {self.max_bytes} byte cap; refusing to stage it."
                )

            relative = (
                Path(content.name) / source.relative_to(content)
                if content.is_dir()
                else Path(content.name)
            )
            destination = files_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            _harden_staged_dirs(root, destination.parent)
            shutil.copy2(source, destination)
            # Never let a torrent's permission bits survive staging.
            os.chmod(destination, 0o600)
            entries.append(
                {
                    "relative": str(relative),
                    "size": size,
                    # Head hash, named as such: a 40 GB image is not read twice.
                    "sha256_head": sha256_file(destination, limit=HEAD_HASH_BYTES),
                }
            )

        manifest = {
            "torrent_hash": torrent_hash.lower(),
            "name": info.get("name") or content.name,
            "content_name": content.name,
            "size": info.get("size"),
            "save_path": info.get("save_path"),
            "staged_at": _utc(),
            "total_bytes": total,
            "expected_title": expected_title,
            "entries": entries,
        }
        write_json(self.manifest_path(torrent_hash), manifest)
        logger.info(
            "Staged %s into %s (%d file(s), %d bytes)", torrent_hash, root, len(entries), total
        )
        summary = {
            "torrent_hash": torrent_hash.lower(),
            "name": manifest["name"],
            "files": len(entries),
            "total_bytes": total,
            "quarantine": str(root),
        }
        self._notify("staged", summary)
        return summary

    # ---- Stage 2: inspect ----

    def scan(self, torrent_hash: str, *, detonate: bool = True, op_id: str = "") -> dict:
        """Static analysis, optional detonation, and a verdict on disk."""
        op_id = op_id or new_op_id("scan", torrent_hash)
        with trace_span(
            self.tracer,
            op_id=op_id,
            stage="scan",
            event="inspect",
            torrent_hash=torrent_hash,
            detonate=detonate,
        ) as result:
            summary = self._scan(torrent_hash, detonate=detonate, op_id=op_id)
            result.update(summary)
            return summary

    def _scan(self, torrent_hash: str, *, detonate: bool, op_id: str) -> dict:
        manifest = _read_optional(self.manifest_path(torrent_hash))
        if manifest is None:
            self._trace(
                op_id,
                "scan",
                "refused",
                torrent_hash,
                reason="no manifest; run the download stage first",
            )
            raise TriageError(f"No manifest for {torrent_hash!r}; run the download stage first.")

        static = analyse(self.files_root(torrent_hash))
        airlock = inspect_tree(self.files_root(torrent_hash))
        self._trace(
            op_id,
            "static",
            "static.done",
            torrent_hash,
            score=static.score,
            flags=list(static.flags),
            files=len(static.files),
        )

        # Media sampling is advisory and its parser is attack surface: it runs
        # in the airlock, and every failure is a note rather than an exception.
        # A scan without media facts is still a scan; a scan that dies because
        # ffprobe is missing is not.
        media: MediaReview | None = None
        media_notes: list[str] = []
        if self.media_enabled:
            try:
                with trace_span(
                    self.tracer,
                    op_id=op_id,
                    stage="media",
                    event="sample",
                    torrent_hash=torrent_hash,
                ) as span:
                    media = review_media(
                        self.files_root(torrent_hash),
                        output_dir=self.root(torrent_hash) / "review" / "frames",
                        frames=self.media_frames,
                        expected_title=manifest.get("expected_title") or "",
                    )
                    span.update(
                        {
                            "available": media.available,
                            "flags": list(media.flags),
                            "frames": media.sampled,
                            "duration_seconds": media.duration,
                        }
                    )
            except Exception as exc:
                media = None
                media_notes.append(f"Media review failed: {exc}")
            if media is not None and media.flags:
                media_notes.append("Media flags: " + ", ".join(sorted(media.flags)))

        run: SandboxReport | None = None
        reviewed = False
        notes = ""
        if detonate:
            # A runner that says why it cannot run is more useful than a generic
            # "not available": the operator needs to know what to install.
            artifacts = [
                path
                for path in sorted(self.files_root(torrent_hash).rglob("*"))
                if path.is_file() and not path.is_symlink()
            ]
            review_reason = getattr(
                self.sandbox, "review_unavailable_reason", lambda: "review unsupported"
            )()
            use_review = bool(self.vm_review_media and artifacts and not review_reason)
            if use_review:
                # A media-only torrent has nothing Windows can execute, but it
                # does have a parser path: the player. Review mode opens every
                # file with a Windows application and watches the same
                # telemetry, so this is the detonation for a movie.
                try:
                    selected = self._selected_files(manifest)
                    keep = self._guest_keep_names(torrent_hash, manifest, selected)
                    with trace_span(
                        self.tracer,
                        op_id=op_id,
                        stage="sandbox",
                        event="review",
                        torrent_hash=torrent_hash,
                        artifacts=len(artifacts),
                    ) as span:
                        run = self.sandbox.run(
                            artifacts,
                            timeout=self.sandbox_timeout,
                            review=True,
                            keep=keep,
                        )
                        reviewed = True
                        span.update(
                            {
                                "detonated": run.detonated,
                                "opened": run.opened,
                                "engine": run.engine,
                                "notes": run.notes,
                            }
                        )
                except Exception as exc:
                    from minus.triage.windows_vm import WindowsVmError

                    if not isinstance(exc, WindowsVmError):
                        raise
                    self._trace(
                        op_id,
                        "sandbox",
                        "sandbox.failed",
                        torrent_hash,
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
                    # A wedged guest is not evidence of cleanliness: leave the
                    # run empty so the verdict stays needs_review.
                    notes = f"Sandbox review failed: {exc}"
            else:
                skipper = getattr(self.sandbox, "skip_reason", None)
                skipped = skipper(artifacts) if callable(skipper) else ""
                reason = getattr(self.sandbox, "unavailable_reason", lambda: "")()
                if skipped:
                    self._trace(
                        op_id,
                        "sandbox",
                        "sandbox.skipped",
                        torrent_hash,
                        reason=skipped,
                    )
                    notes = f"Sandbox {getattr(self.sandbox, 'name', '?')} skipped: {skipped}"
                elif reason:
                    notes = (
                        f"Sandbox {getattr(self.sandbox, 'name', '?')} is not available: {reason}"
                    )
                else:
                    try:
                        with trace_span(
                            self.tracer,
                            op_id=op_id,
                            stage="sandbox",
                            event="run",
                            torrent_hash=torrent_hash,
                            artifacts=len(artifacts),
                        ) as span:
                            run = self.sandbox.run(artifacts, timeout=self.sandbox_timeout)
                            span.update(
                                {
                                    "detonated": run.detonated,
                                    "engine": run.engine,
                                    "notes": run.notes,
                                }
                            )
                    except Exception as exc:
                        from minus.triage.windows_vm import WindowsVmError

                        if not isinstance(exc, WindowsVmError):
                            raise
                        self._trace(
                            op_id,
                            "sandbox",
                            "sandbox.failed",
                            torrent_hash,
                            error=str(exc),
                            error_type=type(exc).__name__,
                        )
                        # A wedged guest is not evidence of cleanliness: leave
                        # the run empty so the verdict stays needs_review.
                        notes = f"Sandbox failed: {exc}"

        review = self.reviewer.review(self.files_root(torrent_hash)) if self.reviewer else None
        if review is not None and not review.available:
            note = f"Model review unavailable: {review.error}"
            notes = f"{notes}; {note}" if notes else note
        for note in media_notes:
            notes = f"{notes}; {note}" if notes else note
        self._trace(
            op_id,
            "review",
            "review.done",
            torrent_hash,
            risk=review.risk if review is not None else "",
            available=review.available if review is not None else False,
            error=review.error if review is not None else "",
        )

        verdict = decide(
            static,
            run,
            require_detonation=self.require_detonation,
            review=review,
        )
        # Structural media anomalies survive a detonation: review mode may open
        # the container in a player, but the player only consumes the streams it
        # understands, so "clean" says nothing about a second file hidden inside
        # it. The verdict is held at needs_review and the flags join the
        # reasons, so the record says why.
        if verdict.label == CLEAN and media is not None:
            blockers = sorted(set(media.flags) & CLEAN_BLOCKING_MEDIA_FLAGS)
            if blockers:
                verdict = Verdict(
                    NEEDS_REVIEW,
                    verdict.score,
                    verdict.detonated,
                    verdict.reasons + tuple(blockers),
                )
                note = "Media flags block a clean release: " + ", ".join(blockers)
                notes = f"{notes}; {note}" if notes else note
        self._trace(
            op_id,
            "verdict",
            "verdict.done",
            torrent_hash,
            label=verdict.label,
            score=verdict.score,
            reasons=list(verdict.reasons),
        )

        payload = {
            "torrent_hash": torrent_hash.lower(),
            "name": manifest.get("name"),
            "scanned_at": _utc(),
            "static": {
                "root": static.root,
                "score": static.score,
                "flags": static.flags,
                "total_bytes": static.total_bytes,
                "files": [
                    {
                        "relative": file.relative,
                        "size": file.size,
                        "kind": file.kind,
                        "entropy": file.entropy,
                        "flags": file.flags,
                        "pe": file.pe,
                    }
                    for file in static.files
                ],
            },
            "sandbox": self._sandbox_payload(run, reviewed=reviewed),
            "airlock": airlock,
            "model_review": review.as_dict() if review is not None else None,
            "media": media.as_dict() if media is not None else None,
            "verdict": verdict.as_dict(),
            "notes": notes,
        }
        write_json(self.verdict_path(torrent_hash), payload)
        if reviewed and run is not None:
            write_json(
                self.vm_review_path(torrent_hash),
                {
                    "torrent_hash": torrent_hash.lower(),
                    "name": manifest.get("name"),
                    "reviewed_at": _utc(),
                    "selected": self._selected_files(manifest),
                    **(self._sandbox_payload(run, reviewed=True) or {}),
                },
            )

        # The model gets a summary, not every file: a 4,000-entry manifest in a
        # tool result is context spent on filenames.
        summary = {
            "torrent_hash": payload["torrent_hash"],
            "name": payload["name"],
            "verdict": payload["verdict"],
            "static_flags": static.flags,
            "files": len(static.files),
            "detonated": run.detonated if run is not None else False,
            "reviewed": reviewed,
            "opened": run.opened if run is not None else 0,
            "review_risk": review.risk if review is not None else "",
            "media_flags": list(media.flags) if media is not None else [],
            "frames": [frame["path"] for frame in media.frames if frame.get("ok")]
            if media is not None
            else [],
            "duration_seconds": media.duration if media is not None else None,
            "contact_sheet": media.contact_sheet if media is not None else "",
            "notes": "; ".join(
                (([notes] if notes else []) + airlock["flags"] + airlock["notes"])[:8]
            ),
        }

        # A snapshot: the malicious branch below mutates `summary`, and an
        # observer that kept the dict should see the verdict as it was judged.
        self._notify("verdict", dict(summary))

        if verdict.label == MALICIOUS and self.delete_malicious:
            summary.update(self.reject_malicious(torrent_hash, payload))
            summary["alert"] = _malicious_alert(torrent_hash, payload.get("name"))

        return summary

    def reject_malicious(self, torrent_hash: str, payload: dict) -> dict:
        """Delete a malicious payload now, keeping the verdict as evidence.

        The bytes are the hazard; the record is not. What survives is a JSON
        document with the hash, the verdict and the file *names* -- never a copy
        of the content -- and the staged tree is removed in one `rmtree`.
        """
        root = self.root(torrent_hash)
        rejected_dir = Path(self.quarantine) / "rejected"
        rejected_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(rejected_dir, 0o700)

        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        record_path = rejected_dir / f"{torrent_hash.lower()}-{stamp}.json"
        record = {
            "torrent_hash": torrent_hash.lower(),
            "name": payload.get("name"),
            "rejected_at": _utc(),
            "verdict": payload.get("verdict"),
            "static_flags": (payload.get("static") or {}).get("flags", []),
            "file_names": [
                entry.get("relative")
                for entry in (payload.get("static") or {}).get("files", [])
                if entry.get("relative")
            ],
        }
        write_json(record_path, record)
        shutil.rmtree(root, ignore_errors=True)

        reasons = "; ".join((payload.get("verdict") or {}).get("reasons", [])[:5])
        logger.critical(
            "MALICIOUS torrent %s (%s) deleted from quarantine: %s",
            torrent_hash,
            payload.get("name"),
            reasons or "no reasons recorded",
        )
        result = {
            "deleted": True,
            "rejected_record": str(record_path),
            "alert": _malicious_alert(torrent_hash, payload.get("name")),
        }
        result["cleanup"] = self._cleanup_download(torrent_hash)
        self._notify("malicious_deleted", {"torrent_hash": torrent_hash.lower(), **result})
        return result

    # ---- Stage 3: read the verdict ----

    def verdict(self, torrent_hash: str) -> dict:
        payload = _read_optional(self.verdict_path(torrent_hash))
        if payload is None:
            raise TriageError(f"No verdict for {torrent_hash!r}; run the scan stage first.")
        return payload

    # ---- Stage 4: release ----

    def review(self, torrent_hash: str) -> dict:
        """Facts for a human deciding whether to accept a `needs_review` item.

        Static results and a bounded, sandboxed ffprobe pass over media files.
        No promotion happens here; `promote(..., accept_review=True)` is the
        separate, explicit act.
        """
        payload = self.verdict(torrent_hash)
        static = payload.get("static") or {}
        airlock = payload.get("airlock") or {}
        label = (payload.get("verdict") or {}).get("label")
        sandbox = self.vm_review_evidence(torrent_hash, payload)
        blockers = manual_review_blockers(payload)
        accept = label == NEEDS_REVIEW and not blockers
        vm_reviewed = bool(sandbox.get("detonated") or int(sandbox.get("opened") or 0) > 0)
        result = {
            "torrent_hash": torrent_hash.lower(),
            "name": payload.get("name"),
            "verdict": payload.get("verdict"),
            "detonated": bool(sandbox.get("detonated")),
            "vm_reviewed": vm_reviewed,
            "opened": int(sandbox.get("opened") or 0),
            "static_flags": static.get("flags", []),
            "files": (static.get("files") or [])[:200],
            "engines": airlock.get("engines", []),
            "media": payload.get("media"),
            "media_probe": probe_media(self.files_root(torrent_hash)),
            "promotion": {
                "label": label,
                "can_accept_review": accept and vm_reviewed,
                "manual_blockers": blockers,
                "vm_reviewed": vm_reviewed,
                "command": (
                    "minus triage promote "
                    f"{torrent_hash.lower()} --accept-review --yes"
                    + (
                        " --keep "
                        + shlex.quote(",".join(str(item) for item in sandbox.get("selected") or []))
                        if sandbox.get("selected")
                        else ""
                    )
                    if accept and vm_reviewed
                    else None
                ),
            },
        }
        op_id = new_op_id("review", torrent_hash)
        self._trace(
            op_id,
            "review",
            "review.done",
            torrent_hash,
            can_accept_review=accept,
            verdict=payload.get("verdict"),
        )
        return result

    def preview(
        self,
        torrent_hash: str,
        *,
        frames: int | None = None,
        expected_title: str = "",
    ) -> dict:
        """Sample frames from a staged torrent on demand, changing nothing else.

        The scan's contact sheet is bounded and may be stale; a reviewer who
        wants one more look should not have to rescan to get it. This writes
        frames and `preview/media.json` under quarantine and never touches the
        verdict, never promotes, and never deletes: it is a viewer, not a stage
        in the decision.
        """
        manifest = _read_optional(self.manifest_path(torrent_hash))
        if manifest is None:
            raise TriageError(f"No manifest for {torrent_hash!r}; run the download stage first.")

        count = self.media_frames if frames is None else frames
        output_dir = self.root(torrent_hash) / "preview"
        media = review_media(
            self.files_root(torrent_hash),
            output_dir=output_dir,
            frames=count,
            expected_title=expected_title or manifest.get("expected_title") or "",
        )
        write_json(output_dir / "media.json", media.as_dict())
        self._trace(
            new_op_id("preview", torrent_hash),
            "preview",
            "media.done",
            torrent_hash,
            flags=list(media.flags),
            frames=media.sampled,
            contact_sheet=media.contact_sheet,
        )
        return {
            "torrent_hash": torrent_hash.lower(),
            "name": manifest.get("name"),
            "media": media.as_dict(),
            "frames": [frame["path"] for frame in media.frames if frame.get("ok")],
            "contact_sheet": media.contact_sheet,
            "flags": list(media.flags),
        }

    def promote(
        self,
        torrent_hash: str,
        *,
        name: str = "",
        accept_review: bool = False,
        accepted_by: str = "",
        reason: str = "",
        keep: list[str] | None = None,
        op_id: str = "",
    ) -> dict:
        """Move clean MP4 content into the flat library, with provenance outside it.

        `clean` is the normal gate. `accept_review` is the explicit manual
        release for `needs_review` only: the operator has seen the facts and
        accepts the risk, and the provenance records that no detonation
        happened. A manual release now also requires VM review evidence: the
        guest must have opened or launched a file and observed no signals.

        `keep` selects what reaches the library; without it, videos are reviewed,
        else audio, else every file. Only `.mp4` is copied into `library/`; a
        selection with no `.mp4` is refused. Everything else is graveyard: the
        guest moved it aside, and the staged copy is deleted after the release.
        Provenance goes to `memory/library_provenance/`, never beside the MP4s.
        `suspicious` and `malicious` are never releasable this way.
        """
        op_id = op_id or new_op_id("promote", torrent_hash)
        with trace_span(
            self.tracer,
            op_id=op_id,
            stage="promote",
            event="release",
            torrent_hash=torrent_hash,
            accept_review=accept_review,
            keep=list(keep or ()),
        ) as result:
            moved = self._promote(
                torrent_hash,
                name=name,
                accept_review=accept_review,
                accepted_by=accepted_by,
                reason=reason,
                keep=keep,
                op_id=op_id,
            )
            result.update(moved)
            return moved

    def _promote(
        self,
        torrent_hash: str,
        *,
        name: str,
        accept_review: bool,
        accepted_by: str,
        reason: str,
        keep: list[str] | None,
        op_id: str,
    ) -> dict:
        payload = self.verdict(torrent_hash)
        label = payload.get("verdict", {}).get("label")
        evidence = self.vm_review_evidence(torrent_hash, payload)
        manual: dict | None = None
        if label == CLEAN:
            if not (
                evidence.get("completed") is not False
                and (evidence.get("detonated") or int(evidence.get("opened") or 0) > 0)
            ):
                self._trace(
                    op_id,
                    "promote",
                    "refused",
                    torrent_hash,
                    reason="clean verdict without VM evidence",
                    label=label,
                )
                raise TriageError(
                    f"Refusing to promote {torrent_hash!r}: the verdict is clean but there is "
                    "no VM detonation or review evidence. Run `minus triage scan` or "
                    f"`minus triage vm {torrent_hash.lower()}` first."
                )
            linux_active = clean_review_blockers(payload)
            if linux_active:
                self._trace(
                    op_id,
                    "promote",
                    "refused",
                    torrent_hash,
                    reason="Windows detonation did not exercise Linux-active content",
                    label=label,
                    blockers=linux_active[:6],
                )
                raise TriageError(
                    f"Refusing to promote {torrent_hash!r}: the Windows detonation did not "
                    "exercise Linux-active content: " + "; ".join(linux_active[:6])
                )
        if label != CLEAN:
            reasons = payload.get("verdict", {}).get("reasons", [])
            if not accept_review:
                self._trace(
                    op_id,
                    "promote",
                    "refused",
                    torrent_hash,
                    reason="verdict is not clean",
                    label=label,
                    verdict_reasons=reasons,
                )
                raise TriageError(
                    f"Refusing to promote {torrent_hash!r}: verdict is {label!r}. "
                    f"Reasons: {'; '.join(reasons) or 'none recorded'}"
                )
            if label == MALICIOUS:
                self._trace(
                    op_id,
                    "promote",
                    "refused",
                    torrent_hash,
                    reason="verdict is malicious",
                    label=label,
                )
                raise TriageError(
                    f"Refusing to manually promote {torrent_hash!r}: the verdict is malicious."
                )
            if label != NEEDS_REVIEW:
                self._trace(
                    op_id,
                    "promote",
                    "refused",
                    torrent_hash,
                    reason="verdict is not needs_review",
                    label=label,
                )
                raise TriageError(
                    f"Refusing to manually promote {torrent_hash!r}: verdict is {label!r}, "
                    "not needs_review."
                )
            blockers = manual_review_blockers(payload)
            if blockers:
                self._trace(
                    op_id,
                    "promote",
                    "refused",
                    torrent_hash,
                    reason="manual acceptance is only for passive media",
                    label=label,
                    blockers=blockers[:6],
                )
                raise TriageError(
                    f"Refusing to manually promote {torrent_hash!r}: manual acceptance is "
                    "only for passive media. Blockers: " + "; ".join(blockers[:6])
                )
            sandbox = evidence
            review_evidence = bool(
                sandbox.get("completed") is not False
                and (sandbox.get("detonated") or int(sandbox.get("opened") or 0) > 0)
            )
            if not review_evidence:
                self._trace(
                    op_id,
                    "promote",
                    "refused",
                    torrent_hash,
                    reason="no VM review evidence",
                    label=label,
                )
                raise TriageError(
                    f"Refusing to manually promote {torrent_hash!r}: no VM review evidence. "
                    "Run `minus triage vm "
                    f"{torrent_hash.lower()}` (or triage_vm_review) first; a manual release "
                    "of passive media now requires the guest to have opened or launched it."
                )
            manual = {
                "accepted_at": _utc(),
                "accepted_by": accepted_by or "local operator",
                "reason": reason or "manual acceptance of a needs_review verdict",
                "detonated": bool(sandbox.get("detonated")),
                "vm_reviewed": review_evidence,
                "verdict": label,
                "reasons": reasons,
            }

        manifest = _read_optional(self.manifest_path(torrent_hash)) or {}
        content_name = manifest.get("content_name") or manifest.get("name") or "content"
        content_root = self.files_root(torrent_hash) / content_name
        if not content_root.exists():
            self._trace(
                op_id,
                "promote",
                "refused",
                torrent_hash,
                reason="staged content is gone",
                source=str(content_root),
            )
            raise TriageError(f"Staged content is gone: {content_root}")

        selected = self._selected_files(manifest, keep)
        reviewed_selected = [str(item) for item in (evidence.get("selected") or [])]
        if reviewed_selected:
            extra = [item for item in selected if item not in reviewed_selected]
            if extra:
                self._trace(
                    op_id,
                    "promote",
                    "refused",
                    torrent_hash,
                    reason="promotion selection includes files the VM review did not keep",
                    extra=extra[:6],
                )
                raise TriageError(
                    f"Refusing to promote {torrent_hash!r}: the selection includes files the "
                    "VM review did not keep: "
                    + "; ".join(extra[:6])
                    + ". Pass the same --keep list used for review, or review again."
                )
        mp4_selected = [
            relative for relative in selected if Path(relative).suffix.lower() == ".mp4"
        ]
        if not mp4_selected:
            self._trace(
                op_id,
                "promote",
                "refused",
                torrent_hash,
                reason="no .mp4 files selected",
                selected=selected[:6],
            )
            raise TriageError(
                f"Refusing to promote {torrent_hash!r}: no .mp4 files selected. "
                "The library accepts only .mp4; pass --keep with an .mp4 file."
            )
        sources: list[tuple[Path, str]] = []
        for relative in mp4_selected:
            source = self.files_root(torrent_hash) / relative
            if not source.is_file():
                self._trace(
                    op_id,
                    "promote",
                    "refused",
                    torrent_hash,
                    reason="selected file is gone",
                    source=str(source),
                )
                raise TriageError(f"Selected file is gone: {source}")
            # The library is flat: a movie is `library/<name>.mp4`, not a
            # release directory. Basenames are unique enough for a movie; a
            # collision gets a timestamp in promote_files_to_library.
            sources.append((source, source.name))

        entries = [entry for entry in (manifest.get("entries") or []) if entry.get("relative")]
        graveyard = [
            str(entry["relative"])
            for entry in entries
            if str(entry["relative"]) not in mp4_selected
        ]
        provenance = {
            "torrent_hash": torrent_hash.lower(),
            "name": manifest.get("name"),
            "manifest": manifest,
            "verdict": payload.get("verdict"),
            "selected": mp4_selected,
            "graveyard": graveyard,
            "vm_review": self.vm_review_evidence(torrent_hash, payload),
        }
        if manual is not None:
            provenance["manual_acceptance"] = manual
        try:
            moved = promote_files_to_library(
                sources,
                Path(self.library),
                name=name or str(manifest.get("name") or content_name),
                provenance=provenance,
                provenance_dir=self.provenance_dir,
            )
        except ValueError as exc:
            self._trace(op_id, "promote", "refused", torrent_hash, reason=str(exc))
            raise TriageError(str(exc)) from exc

        # The selected files were copied into the library; the staged copy is
        # now the graveyard. Delete it so the host does not keep a second copy
        # of the torrent (the guest already moved its non-kept files aside and
        # its overlay dies with the run).
        shutil.rmtree(self.files_root(torrent_hash), ignore_errors=True)

        logger.info("Promoted %s to %s", torrent_hash, moved["moved_to"])
        result = {
            "torrent_hash": torrent_hash.lower(),
            "moved_to": moved["moved_to"],
            "provenance": moved["provenance"],
            "bytes": moved["bytes"],
            "files": moved.get("files", []),
            "graveyard": graveyard,
        }
        result["cleanup"] = self._cleanup_download(torrent_hash)
        self._notify("promoted", result)
        return result

    # ---- Whole-flow helpers ----

    def wait_for_completion(
        self,
        torrent_hash: str,
        *,
        timeout: float | None = None,
        poll: float | None = None,
    ) -> dict:
        """Poll until the torrent reports 100%, or raise naming its progress.

        The wait is bounded on purpose: a torrent that never finishes is an
        answer the harness can show, not a reason to hang a conversation.
        """
        if self.client is None:
            raise TriageError("qBittorrent is not configured (set MINUS_QBITTORRENT_URL).")

        limit = self.download_timeout if timeout is None else timeout
        interval = self.poll_seconds if poll is None else poll
        deadline = time.monotonic() + max(0.0, limit)
        while True:
            info = self.client.torrent(torrent_hash)
            progress = float(info.get("progress") or 0.0)
            if progress >= 1.0:
                return info
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TriageError(
                    f"Torrent {torrent_hash!r} is {progress:.0%} complete after "
                    f"{limit:g}s; giving up rather than waiting forever."
                )
            time.sleep(min(max(0.0, interval), remaining))

    def find_new_torrent(
        self,
        before: set[str],
        url: str,
        *,
        timeout: float = 60.0,
        poll: float = 2.0,
    ) -> str:
        """The hash qBittorrent just created for `url`.

        A magnet names its own hash, so that case is answered without polling.
        An HTTP `.torrent` URL does not, so the torrent list is polled for a
        hash that was not there before and whose name or magnet matches.
        """
        if self.client is None:
            raise TriageError("qBittorrent is not configured (set MINUS_QBITTORRENT_URL).")

        named = _MAGNET_HASH.search(url or "")
        if named is not None:
            return named.group(1).lower()

        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            fresh = [
                (str(torrent.get("hash") or "").lower(), torrent)
                for torrent in self.client.torrents()
                if str(torrent.get("hash") or "").lower() not in before
            ]
            for torrent_hash, torrent in fresh:
                if _torrent_matches(torrent, url):
                    return torrent_hash
            # Only one torrent appeared after the add, so it is the one we
            # caused even when its metadata does not mention the URL.
            if len(fresh) == 1 and fresh[0][0]:
                return fresh[0][0]

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TriageError(
                    f"qBittorrent did not report a new torrent for {url!r} within {timeout:g}s."
                )
            time.sleep(min(max(0.0, poll), remaining))

    def ingest(
        self,
        url: str,
        *,
        detonate: bool = True,
        wait: bool = True,
        expected_title: str = "",
        release: bool = False,
        name: str = "",
    ) -> dict:
        """Add, wait, stage, scan, and optionally release one torrent.

        `release=True` promotes only a `clean` verdict into the library and lets
        the cleanup remove the download copy. `needs_review` is deliberately
        left in quarantine: releasing passive media after a detonation-free scan
        is a separate, explicit `promote(accept_review=True)` after review, not
        something an ingest flag may decide on the operator's behalf.

        A refusal or a timeout comes back as `status: failed` with the reason,
        because this is called from a conversation that needs to render the
        answer rather than trace a stack. `expected_title` is the query the
        torrent was picked for, carried into the manifest so media sampling can
        compare the container's title against what was asked for.
        """
        if self.client is None:
            return {
                "status": "failed",
                "error": "qBittorrent is not configured (set MINUS_QBITTORRENT_URL).",
            }

        op_id = new_op_id("ingest", url)
        torrent_hash = ""
        stage = "add"
        try:
            # A magnet names its own hash, so it can be matched against the
            # list qBittorrent already returns -- adding it again would either
            # fail or duplicate the bytes. There is deliberately no equivalent
            # guess for an HTTP URL: its hash is only known after the add.
            # The same list read gives the `before` set, because a second read
            # or a per-hash probe would be a second source of truth for what
            # was present.
            before = {str(torrent.get("hash") or "").lower() for torrent in self.client.torrents()}
            named = _MAGNET_HASH.search(url or "")
            if named is not None:
                torrent_hash = named.group(1).lower()

            if torrent_hash and torrent_hash in before:
                self._trace(
                    op_id,
                    "add",
                    "skipped",
                    torrent_hash,
                    payload={"reason": "already present", "torrent_hash": torrent_hash},
                )
            else:
                self.client.add_torrent(
                    [url],
                    paused=False,
                    save_path=str(Path(self.quarantine) / "incoming"),
                )
                self._trace(op_id, "add", "add.done", torrent_hash, url=url)
                torrent_hash = self.find_new_torrent(before, url)
            stage = "find"
            self._trace(op_id, "find", "find.done", torrent_hash, url=url)
            if wait:
                stage = "wait"
                info = self.wait_for_completion(torrent_hash)
                self._trace(
                    op_id,
                    "wait",
                    "wait.done",
                    torrent_hash,
                    progress=info.get("progress"),
                    size=info.get("size"),
                )
            stage = "download"
            staged = self.download(torrent_hash, op_id=op_id, expected_title=expected_title)
            stage = "scan"
            summary = self.scan(torrent_hash, detonate=detonate, op_id=op_id)
        except (TriageError, QBittorrentError) as exc:
            self._trace(
                op_id,
                stage,
                "failed",
                torrent_hash,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            # The hash is still useful to a caller: a timeout leaves the
            # download in qBittorrent, and naming it is what lets the harness
            # report it (or a later retry resume it) instead of orphaning it.
            return {
                "status": "failed",
                "torrent_hash": torrent_hash or None,
                "stage": stage,
                "error": str(exc),
                "error_type": type(exc).__name__,
            }

        verdict = summary.get("verdict") or {}
        result = {
            "status": verdict.get("label") or "scanned",
            "torrent_hash": torrent_hash,
            "op_id": op_id,
            "name": summary.get("name") or staged.get("name"),
            "verdict": verdict,
            "files": summary.get("files", staged.get("files")),
            "alert": summary.get("alert"),
            "error": None,
        }
        if release:
            result["release"] = self._release_ingested(
                torrent_hash,
                verdict=verdict,
                name=name,
                op_id=op_id,
            )
        return result

    def _release_ingested(
        self,
        torrent_hash: str,
        *,
        verdict: dict,
        name: str,
        op_id: str,
    ) -> dict:
        """Promote a just-scanned torrent only when the verdict already allows it.

        This is the automatic half of the gate, not a second gate: the decision
        still comes from `promote`, which re-reads the verdict and refuses a
        `needs_review` item unless the caller asked for manual acceptance. An
        ingest caller that passed `release=True` did not, so a review item stays
        in quarantine and its result says why.
        """
        label = verdict.get("label")
        if label != CLEAN:
            self._trace(
                op_id,
                "release",
                "skipped",
                torrent_hash,
                reason="verdict is not clean",
                label=label,
            )
            return {
                "requested": True,
                "promoted": False,
                "reason": f"verdict is {label}; left in quarantine",
            }
        try:
            moved = self.promote(
                torrent_hash,
                name=name,
                reason="clean verdict",
            )
        except TriageError as exc:
            self._trace(
                op_id,
                "release",
                "refused",
                torrent_hash,
                reason=str(exc),
                label=label,
            )
            return {
                "requested": True,
                "promoted": False,
                "reason": str(exc),
            }
        return {"requested": True, "promoted": True, **moved}

    def find_torrents(
        self,
        query: str = "",
        *,
        only_complete: bool = True,
        limit: int = 10,
    ) -> list[dict]:
        """Torrents already in qBittorrent whose name matches `query`.

        Deliberately only searches what the user already added: MINUS does not
        browse indexes, and cannot be asked to find content to infringe.
        """
        if self.client is None:
            raise TriageError("qBittorrent is not configured (set MINUS_QBITTORRENT_URL).")

        from minus.triage.select import select_torrents

        return select_torrents(
            self.client.torrents(),
            query,
            only_complete=only_complete,
            limit=limit,
        )
