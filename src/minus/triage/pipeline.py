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
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from minus.errors import MinusError
from minus.services.json import read_json, write_json
from minus.triage.airlock import inspect_tree
from minus.triage.library import promote_to_library
from minus.triage.review import ModelReviewer
from minus.triage.sandbox import DryRunRunner, SandboxReport, SandboxRunner
from minus.triage.scoring import CLEAN, MALICIOUS, decide
from minus.triage.static import HEAD_HASH_BYTES, analyse, sha256_file

logger = logging.getLogger(__name__)

# A torrent hash is a path component in everything below, so it is validated as
# one thing: hex, and nothing else.
_HASH = re.compile(r"\A[0-9a-fA-F]{6,64}\Z")

# A magnet may carry several `xt` entries; only the BitTorrent v1 SHA-1 form
# is a 40-hex hash, and that is the one qBittorrent lists.
_MAGNET_HASH = re.compile(r"xt=urn:btih:([0-9a-fA-F]{40})", re.IGNORECASE)

DEFAULT_MAX_BYTES = 5 * 1024**3


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
    client: Any | None = None
    sandbox: SandboxRunner = field(default_factory=DryRunRunner)
    max_bytes: int = DEFAULT_MAX_BYTES
    require_detonation: bool = True
    sandbox_timeout: float = 180.0
    vpn_interface: str = ""
    qbittorrent_vpn_interface: str = ""
    require_vpn: bool = True
    delete_malicious: bool = True
    # The harness attaches an observer here to render live progress and the
    # malicious-payload alert; the pipeline itself writes state to disk and
    # must work with no listener at all.
    notify: Callable[[str, dict], None] | None = None
    # The model stage reads code-shaped files before `decide` weighs them. It
    # can escalate a verdict, never clear one, and is absent when the caller
    # did not want a second model call.
    reviewer: ModelReviewer | None = None
    download_timeout: float = 3600.0
    poll_seconds: float = 5.0

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

    # ---- Stage 1: fetch ----

    def download(self, torrent_hash: str) -> dict:
        """Copy a completed torrent's content into quarantine, with a manifest.

        Refuses an incomplete torrent rather than staging half of one: a verdict
        on a partial download would describe files that do not exist.
        """
        if self.client is None:
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
                    raise TriageError(
                        "VPN enforcement is on but no interface is configured "
                        "(MINUS_QBITTORRENT_VPN_INTERFACE or MINUS_VPN_INTERFACE); "
                        "no VPN interface configured means no download is safe."
                    )
            except VpnError as exc:
                raise TriageError(str(exc)) from exc

        info = self.client.torrent(torrent_hash)
        progress = float(info.get("progress") or 0.0)
        if progress < 1.0:
            raise TriageError(
                f"Torrent {torrent_hash!r} is {progress:.0%} complete; refusing to triage it."
            )

        content = Path(str(info.get("content_path") or ""))
        if not str(content) or not content.exists():
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
            raise TriageError(f"Nothing to copy from {content}")

        for source in sources:
            size = source.stat().st_size
            total += size
            if total > self.max_bytes:
                shutil.rmtree(root, ignore_errors=True)
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

    def scan(self, torrent_hash: str, *, detonate: bool = True) -> dict:
        """Static analysis, optional detonation, and a verdict on disk."""
        manifest = _read_optional(self.manifest_path(torrent_hash))
        if manifest is None:
            raise TriageError(f"No manifest for {torrent_hash!r}; run the download stage first.")

        static = analyse(self.files_root(torrent_hash))
        airlock = inspect_tree(self.files_root(torrent_hash))

        run: SandboxReport | None = None
        notes = ""
        if detonate:
            # A runner that says why it cannot run is more useful than a generic
            # "not available": the operator needs to know what to install.
            reason = getattr(self.sandbox, "unavailable_reason", lambda: "")()
            if reason:
                notes = f"Sandbox {getattr(self.sandbox, 'name', '?')} is not available: {reason}"
            else:
                artifacts = [
                    path
                    for path in sorted(self.files_root(torrent_hash).rglob("*"))
                    if path.is_file() and not path.is_symlink()
                ]
                try:
                    run = self.sandbox.run(artifacts, timeout=self.sandbox_timeout)
                except Exception as exc:
                    from minus.triage.windows_vm import WindowsVmError

                    if not isinstance(exc, WindowsVmError):
                        raise
                    # A wedged guest is not evidence of cleanliness: leave the
                    # run empty so the verdict stays needs_review.
                    notes = f"Sandbox failed: {exc}"

        review = self.reviewer.review(self.files_root(torrent_hash)) if self.reviewer else None
        if review is not None and not review.available:
            note = f"Model review unavailable: {review.error}"
            notes = f"{notes}; {note}" if notes else note

        verdict = decide(
            static,
            run,
            require_detonation=self.require_detonation,
            review=review,
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
            "sandbox": (
                {
                    "engine": run.engine,
                    "detonated": run.detonated,
                    "defender_detections": list(run.defender_detections),
                    "processes": list(run.processes),
                    "network_endpoints": list(run.network_endpoints),
                    "file_writes": list(run.file_writes),
                    "registry_writes": list(run.registry_writes),
                    "notes": run.notes,
                }
                if run is not None
                else None
            ),
            "airlock": airlock,
            "model_review": review.as_dict() if review is not None else None,
            "verdict": verdict.as_dict(),
            "notes": notes,
        }
        write_json(self.verdict_path(torrent_hash), payload)

        # The model gets a summary, not every file: a 4,000-entry manifest in a
        # tool result is context spent on filenames.
        summary = {
            "torrent_hash": payload["torrent_hash"],
            "name": payload["name"],
            "verdict": payload["verdict"],
            "static_flags": static.flags,
            "files": len(static.files),
            "detonated": run.detonated if run is not None else False,
            "review_risk": review.risk if review is not None else "",
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
        self._notify("malicious_deleted", {"torrent_hash": torrent_hash.lower(), **result})
        return result

    # ---- Stage 3: read the verdict ----

    def verdict(self, torrent_hash: str) -> dict:
        payload = _read_optional(self.verdict_path(torrent_hash))
        if payload is None:
            raise TriageError(f"No verdict for {torrent_hash!r}; run the scan stage first.")
        return payload

    # ---- Stage 4: release ----

    def promote(self, torrent_hash: str, *, name: str = "") -> dict:
        """Move clean content into the library, with its verdict alongside.

        Only `clean` gets through. That label already means "detonated, and
        nothing was observed", so this is not a threshold of its own.
        """
        payload = self.verdict(torrent_hash)
        label = payload.get("verdict", {}).get("label")
        if label != CLEAN:
            reasons = payload.get("verdict", {}).get("reasons", [])
            raise TriageError(
                f"Refusing to promote {torrent_hash!r}: verdict is {label!r}. "
                f"Reasons: {'; '.join(reasons) or 'none recorded'}"
            )

        manifest = _read_optional(self.manifest_path(torrent_hash)) or {}
        content_name = manifest.get("content_name") or manifest.get("name") or "content"
        source = self.files_root(torrent_hash) / content_name
        if not source.exists():
            raise TriageError(f"Staged content is gone: {source}")

        provenance = {
            "torrent_hash": torrent_hash.lower(),
            "name": manifest.get("name"),
            "manifest": manifest,
            "verdict": payload.get("verdict"),
        }
        try:
            moved = promote_to_library(
                source,
                Path(self.library),
                name=name or str(manifest.get("name") or content_name),
                provenance=provenance,
            )
        except ValueError as exc:
            raise TriageError(str(exc)) from exc

        logger.info("Promoted %s to %s", torrent_hash, moved["moved_to"])
        result = {
            "torrent_hash": torrent_hash.lower(),
            "moved_to": moved["moved_to"],
            "provenance": moved["provenance"],
            "bytes": moved["bytes"],
        }
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

    def ingest(self, url: str, *, detonate: bool = True, wait: bool = True) -> dict:
        """Add, wait, stage, and scan one torrent as a single call.

        Nothing is promoted: the result is a verdict for the harness to show.
        A refusal or a timeout comes back as `status: failed` with the reason,
        because this is called from a conversation that needs to render the
        answer rather than trace a stack.
        """
        if self.client is None:
            return {
                "status": "failed",
                "error": "qBittorrent is not configured (set MINUS_QBITTORRENT_URL).",
            }

        try:
            before = {str(torrent.get("hash") or "").lower() for torrent in self.client.torrents()}
            self.client.add_torrent(
                [url],
                paused=False,
                save_path=str(Path(self.quarantine) / "incoming"),
            )
            torrent_hash = self.find_new_torrent(before, url)
            if wait:
                self.wait_for_completion(torrent_hash)
            staged = self.download(torrent_hash)
            summary = self.scan(torrent_hash, detonate=detonate)
        except TriageError as exc:
            return {"status": "failed", "error": str(exc)}

        verdict = summary.get("verdict") or {}
        return {
            "status": verdict.get("label") or "scanned",
            "torrent_hash": torrent_hash,
            "name": summary.get("name") or staged.get("name"),
            "verdict": verdict,
            "files": summary.get("files", staged.get("files")),
            "alert": summary.get("alert"),
            "error": None,
        }

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
