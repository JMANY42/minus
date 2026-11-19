"""Pipeline stages over real files: staging refusals, verdicts, promotion.

No stand-in client or sandbox: the cases here are the ones the real code can
reach on its own — a missing client, a staged manifest, a dry-run verdict, and
the promotion refusal that follows from it.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from minus.triage.media_review import MediaReview
from minus.triage.pipeline import TriageError, TriagePipeline
from minus.triage.sandbox import SandboxReport

HASH = "a" * 40


def media_review(
    *,
    flags: tuple[str, ...] = ("media:probe-failed",),
    frames: tuple[dict, ...] | None = None,
    contact_sheet: str = "/tmp/contact.jpg",
    duration: float = 120.0,
) -> MediaReview:
    """A controlled parser answer: the tests never invoke ffmpeg or bwrap."""
    if frames is None:
        frames = (
            {"at_seconds": 12.0, "path": "/tmp/frame-00.jpg", "ok": True, "error": ""},
            {"at_seconds": 24.0, "path": "/tmp/frame-01.jpg", "ok": False, "error": "boom"},
        )
    return MediaReview(
        available=True,
        file="Movie/movie.mp4",
        format="matroska,webm",
        duration=duration,
        size=7,
        video={"codec_type": "video"},
        audio=(),
        streams=1,
        frames=frames,
        contact_sheet=contact_sheet,
        flags=flags,
        notes=(),
        title="Movie",
        expected_title="",
        title_coverage=1.0,
    )


class DetonatedSandbox:
    """A guest that ran and saw nothing: the only way `decide` says clean."""

    name = "fake-guest"

    def run(self, artifacts: list[Path], *, timeout: float) -> SandboxReport:
        return SandboxReport(engine=self.name, detonated=True)


class ReviewSandbox:
    """A guest that opens every artifact and records what it saw."""

    name = "review-guest"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def review_unavailable_reason(self) -> str:
        return ""

    def run(self, artifacts, *, timeout, review=False, keep=None):
        self.calls.append({"artifacts": len(artifacts), "review": review, "keep": keep})
        return SandboxReport(
            engine=self.name,
            detonated=True,
            opened=len(artifacts),
            notes="reviewed: opened every artifact",
        )


class FakeClient:
    """A qBittorrent that reports one complete torrent on disk."""

    def __init__(self, content: Path) -> None:
        self.content = content

    def torrent(self, torrent_hash: str) -> dict:
        return {
            "progress": 1.0,
            "content_path": str(self.content),
            "name": "Movie",
            "size": 7,
            "save_path": str(self.content.parent),
        }


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.addCleanup(self._temp.cleanup)
        self.quarantine = self.root / "quarantine"
        self.library = self.root / "library"
        self.pipeline = TriagePipeline(quarantine=self.quarantine, library=self.library)
        self.media = media_review()
        patcher = mock.patch(
            "minus.triage.pipeline.review_media", side_effect=lambda *a, **k: self.media
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def stage(self, name: str = "Movie") -> Path:
        content = self.quarantine / HASH / "files" / name
        content.mkdir(parents=True)
        (content / "movie.mp4").write_bytes(b"payload")
        manifest = {
            "torrent_hash": HASH,
            "name": name,
            "content_name": name,
            "entries": [{"relative": f"{name}/movie.mp4", "size": 7, "sha256_head": "0" * 64}],
        }
        (self.quarantine / HASH / "manifest.json").write_text(json.dumps(manifest))
        return content

    def test_download_without_a_client_is_refused(self):
        with self.assertRaises(TriageError) as caught:
            self.pipeline.download(HASH)
        self.assertIn("not configured", str(caught.exception))

    def test_scanning_without_a_manifest_names_the_missing_stage(self):
        with self.assertRaises(TriageError) as caught:
            self.pipeline.scan(HASH)
        self.assertIn("download stage first", str(caught.exception))

    def test_a_dry_run_verdict_is_needs_review_and_writes_the_file(self):
        self.stage()

        result = self.pipeline.scan(HASH)

        self.assertEqual(result["verdict"]["label"], "needs_review")
        self.assertFalse(result["detonated"])
        self.assertIn("no detonation", " ".join(result["verdict"]["reasons"]))
        saved = json.loads(self.pipeline.verdict_path(HASH).read_text())
        self.assertEqual(saved["torrent_hash"], HASH)
        self.assertIn("Media flags: media:probe-failed", saved["notes"])
        self.assertEqual(saved["media"]["flags"], ["media:probe-failed"])
        self.assertEqual(result["media_flags"], ["media:probe-failed"])
        self.assertEqual(result["frames"], ["/tmp/frame-00.jpg"])
        self.assertEqual(result["duration_seconds"], 120.0)
        self.assertEqual(result["contact_sheet"], "/tmp/contact.jpg")

    def test_a_clean_verdict_carrying_a_data_stream_stays_needs_review(self):
        self.stage()
        self.pipeline.sandbox = DetonatedSandbox()
        self.media = media_review(flags=("media:data-stream",))

        result = self.pipeline.scan(HASH)

        self.assertEqual(result["verdict"]["label"], "needs_review")
        self.assertIn("media:data-stream", result["verdict"]["reasons"])
        saved = json.loads(self.pipeline.verdict_path(HASH).read_text())
        self.assertIn("media:data-stream", saved["verdict"]["reasons"])
        self.assertIn("Media flags block a clean release", saved["notes"])
        self.assertEqual(saved["media"]["flags"], ["media:data-stream"])

    def test_a_media_failure_is_a_note_not_an_exception(self):
        self.stage()

        def explode(*args, **kwargs):
            raise RuntimeError("ffprobe exploded")

        with mock.patch("minus.triage.pipeline.review_media", side_effect=explode):
            result = self.pipeline.scan(HASH)

        self.assertEqual(result["verdict"]["label"], "needs_review")
        self.assertEqual(result["media_flags"], [])
        self.assertEqual(result["frames"], [])
        self.assertEqual(result["contact_sheet"], "")
        saved = json.loads(self.pipeline.verdict_path(HASH).read_text())
        self.assertIsNone(saved["media"])
        self.assertIn("Media review failed: ffprobe exploded", saved["notes"])

    def test_preview_writes_frames_and_media_json_without_touching_the_verdict(self):
        self.stage()

        result = self.pipeline.preview(HASH, frames=2)

        self.assertEqual(result["frames"], ["/tmp/frame-00.jpg"])
        self.assertEqual(result["contact_sheet"], "/tmp/contact.jpg")
        self.assertEqual(result["flags"], ["media:probe-failed"])
        self.assertEqual(result["name"], "Movie")
        saved = json.loads((self.quarantine / HASH / "preview" / "media.json").read_text())
        self.assertEqual(saved["flags"], ["media:probe-failed"])
        self.assertFalse(self.pipeline.verdict_path(HASH).exists())

    def test_preview_without_a_manifest_is_refused(self):
        with self.assertRaises(TriageError) as caught:
            self.pipeline.preview(HASH)
        self.assertIn("download stage first", str(caught.exception))

    def test_download_records_the_expected_title_in_the_manifest(self):
        content = self.root / "incoming" / "Movie"
        content.mkdir(parents=True)
        (content / "movie.mp4").write_bytes(b"payload")
        self.pipeline.client = FakeClient(content)
        self.pipeline.require_vpn = False

        self.pipeline.download(HASH, expected_title="The Great Gatsby")

        manifest = json.loads(self.pipeline.manifest_path(HASH).read_text())
        self.assertEqual(manifest["expected_title"], "The Great Gatsby")

    def test_promotion_refuses_anything_not_clean(self):
        self.stage()
        self.pipeline.scan(HASH)

        with self.assertRaises(TriageError) as caught:
            self.pipeline.promote(HASH)

        self.assertIn("needs_review", str(caught.exception))
        self.assertFalse(self.library.exists())

    def test_accept_review_releases_needs_review_with_provenance(self):
        self.stage()
        self.pipeline.scan(HASH)
        payload = json.loads(self.pipeline.verdict_path(HASH).read_text())
        payload["sandbox"] = {
            "engine": "test-guest",
            "detonated": True,
            "opened": 1,
            "reviewed": True,
        }
        self.pipeline.verdict_path(HASH).write_text(json.dumps(payload))

        result = self.pipeline.promote(
            HASH,
            accept_review=True,
            accepted_by="tester",
            reason="checked the file list",
        )

        moved = Path(result["moved_to"])
        self.assertTrue(moved.is_file())
        self.assertEqual(moved.name, "movie.mp4")
        provenance = json.loads(Path(result["provenance"]).read_text())
        manual = provenance["provenance"]["manual_acceptance"]
        self.assertEqual(manual["accepted_by"], "tester")
        self.assertEqual(manual["reason"], "checked the file list")
        self.assertTrue(manual["detonated"])
        self.assertTrue(manual["vm_reviewed"])
        self.assertEqual(manual["verdict"], "needs_review")

    def test_accept_review_without_vm_evidence_is_refused(self):
        self.stage()
        self.pipeline.scan(HASH)

        with self.assertRaises(TriageError) as caught:
            self.pipeline.promote(HASH, accept_review=True)

        self.assertIn("no VM review evidence", str(caught.exception))
        self.assertFalse(self.library.exists())

    def test_accept_review_refuses_malicious(self):
        self.stage()
        (self.quarantine / HASH / "verdict.json").write_text(
            json.dumps(
                {
                    "torrent_hash": HASH,
                    "name": "Movie",
                    "verdict": {"label": "malicious", "reasons": ["test"]},
                }
            )
        )

        with self.assertRaises(TriageError) as caught:
            self.pipeline.promote(HASH, accept_review=True)

        self.assertIn("malicious", str(caught.exception))
        self.assertFalse(self.library.exists())

    def test_clean_promotion_refuses_linux_active_content(self):
        content = self.stage()
        (content / "run.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        (self.quarantine / HASH / "verdict.json").write_text(
            json.dumps(
                {
                    "torrent_hash": HASH,
                    "name": "Movie",
                    "verdict": {"label": "clean", "reasons": ["detonated"]},
                    "sandbox": {"detonated": True},
                    "static": {
                        "flags": ["script"],
                        "files": [{"relative": "Movie/run.sh", "kind": "script"}],
                    },
                }
            )
        )

        with self.assertRaises(TriageError) as caught:
            self.pipeline.promote(HASH)

        self.assertIn("Linux-active", str(caught.exception))
        self.assertFalse(self.library.exists())

    def test_accept_review_refuses_active_content(self):
        self.stage()
        (self.quarantine / HASH / "files" / "Movie" / "tool.exe").write_bytes(b"MZ" + b"\0" * 64)
        self.pipeline.scan(HASH)

        with self.assertRaises(TriageError) as caught:
            self.pipeline.promote(HASH, accept_review=True)

        self.assertIn("passive media", str(caught.exception))
        self.assertFalse(self.library.exists())

    def test_accept_review_refuses_suspicious(self):
        self.stage()
        (self.quarantine / HASH / "verdict.json").write_text(
            json.dumps(
                {
                    "torrent_hash": HASH,
                    "name": "Movie",
                    "verdict": {"label": "suspicious", "reasons": ["test"]},
                }
            )
        )

        with self.assertRaises(TriageError) as caught:
            self.pipeline.promote(HASH, accept_review=True)

        self.assertIn("not needs_review", str(caught.exception))
        self.assertFalse(self.library.exists())

    def test_review_exposes_the_files_and_the_exact_release_command(self):
        self.stage()
        self.pipeline.scan(HASH)

        result = self.pipeline.review(HASH)

        self.assertEqual(result["verdict"]["label"], "needs_review")
        self.assertFalse(result["detonated"])
        self.assertFalse(result["promotion"]["can_accept_review"])
        self.assertIsNone(result["promotion"]["command"])
        self.assertFalse(result["vm_reviewed"])
        self.assertEqual(result["files"][0]["relative"], "Movie/movie.mp4")
        self.assertIsInstance(result["media_probe"], list)

    def test_review_offers_release_once_the_vm_reviewed_the_item(self):
        self.stage()
        self.pipeline.scan(HASH)
        payload = json.loads(self.pipeline.verdict_path(HASH).read_text())
        payload["sandbox"] = {
            "engine": "test-guest",
            "detonated": True,
            "opened": 1,
            "reviewed": True,
        }
        self.pipeline.verdict_path(HASH).write_text(json.dumps(payload))

        result = self.pipeline.review(HASH)

        self.assertTrue(result["vm_reviewed"])
        self.assertTrue(result["promotion"]["can_accept_review"])
        self.assertIn("--accept-review", result["promotion"]["command"])

    def test_scan_opens_media_in_the_guest_and_records_the_evidence(self):
        self.stage()
        sandbox = ReviewSandbox()
        self.pipeline.sandbox = sandbox
        self.media = media_review(flags=())

        result = self.pipeline.scan(HASH)

        self.assertTrue(result["reviewed"])
        self.assertEqual(result["opened"], 1)
        self.assertEqual(result["verdict"]["label"], "clean")
        self.assertTrue(sandbox.calls[0]["review"])
        self.assertEqual(sandbox.calls[0]["keep"], {"00-movie.mp4"})
        payload = json.loads(self.pipeline.verdict_path(HASH).read_text())
        self.assertTrue(payload["sandbox"]["reviewed"])
        self.assertEqual(payload["sandbox"]["opened"], 1)
        self.assertTrue(self.pipeline.vm_review_path(HASH).is_file())

    def test_vm_review_writes_evidence_without_changing_the_verdict(self):
        self.stage()
        self.pipeline.scan(HASH)
        sandbox = ReviewSandbox()
        self.pipeline.sandbox = sandbox

        result = self.pipeline.vm_review(HASH, keep=["movie.mp4"])

        self.assertTrue(result["detonated"])
        self.assertEqual(result["opened"], 1)
        self.assertEqual(result["selected"], ["Movie/movie.mp4"])
        self.assertTrue(sandbox.calls[0]["keep"] == {"00-movie.mp4"})
        saved = json.loads(self.pipeline.vm_review_path(HASH).read_text())
        self.assertEqual(saved["opened"], 1)
        verdict = json.loads(self.pipeline.verdict_path(HASH).read_text())
        self.assertEqual(verdict["verdict"]["label"], "needs_review")

    def test_promote_keeps_the_video_and_graveyards_the_release_furniture(self):
        content = self.stage()
        (content / "cover.jpg").write_bytes(b"image")
        (content / "subs.srt").write_bytes(b"subs")
        manifest = json.loads(self.pipeline.manifest_path(HASH).read_text())
        manifest["entries"] += [
            {"relative": "Movie/cover.jpg", "size": 5, "sha256_head": "0" * 64},
            {"relative": "Movie/subs.srt", "size": 4, "sha256_head": "0" * 64},
        ]
        self.pipeline.manifest_path(HASH).write_text(json.dumps(manifest))
        self.pipeline.scan(HASH)
        payload = json.loads(self.pipeline.verdict_path(HASH).read_text())
        payload["sandbox"] = {
            "engine": "test-guest",
            "detonated": True,
            "opened": 3,
            "reviewed": True,
        }
        self.pipeline.verdict_path(HASH).write_text(json.dumps(payload))

        result = self.pipeline.promote(HASH, accept_review=True)

        moved = Path(result["moved_to"])
        self.assertTrue(moved.is_file())
        self.assertEqual(moved.name, "movie.mp4")
        self.assertFalse((moved.parent / "cover.jpg").exists())
        self.assertFalse((moved.parent / "subs.srt").exists())
        self.assertEqual(result["files"], ["movie.mp4"])
        self.assertEqual(result["graveyard"], ["Movie/cover.jpg", "Movie/subs.srt"])
        self.assertFalse((self.quarantine / HASH / "files" / "Movie").exists())

    def test_standalone_vm_review_is_the_evidence_a_manual_release_needs(self):
        content = self.stage()
        (content / "cover.jpg").write_bytes(b"image")
        manifest = json.loads(self.pipeline.manifest_path(HASH).read_text())
        manifest["entries"].append(
            {"relative": "Movie/cover.jpg", "size": 5, "sha256_head": "0" * 64}
        )
        self.pipeline.manifest_path(HASH).write_text(json.dumps(manifest))
        self.pipeline.scan(HASH)
        verdict = json.loads(self.pipeline.verdict_path(HASH).read_text())
        self.assertFalse((verdict.get("sandbox") or {}).get("detonated"))
        self.assertEqual(verdict["verdict"]["label"], "needs_review")

        sandbox = ReviewSandbox()
        self.pipeline.sandbox = sandbox
        reviewed = self.pipeline.vm_review(HASH, keep=["movie.mp4"])
        self.assertTrue(reviewed["detonated"])
        self.assertEqual(reviewed["selected"], ["Movie/movie.mp4"])
        kept = sandbox.calls[0]["keep"]
        self.assertEqual(len(kept), 1)
        self.assertTrue(next(iter(kept)).endswith("-movie.mp4"))
        self.assertTrue(sandbox.calls[0]["review"])
        self.assertTrue(self.pipeline.vm_review_path(HASH).is_file())
        saved_review = json.loads(self.pipeline.vm_review_path(HASH).read_text())
        self.assertEqual(saved_review["opened"], 2)

        result = self.pipeline.promote(HASH, accept_review=True, keep=["movie.mp4"])

        moved = Path(result["moved_to"])
        self.assertTrue(moved.is_file())
        self.assertEqual(moved.name, "movie.mp4")
        self.assertEqual([path.name for path in moved.parent.iterdir()], ["movie.mp4"])
        self.assertEqual(result["files"], ["movie.mp4"])
        self.assertEqual(result["graveyard"], ["Movie/cover.jpg"])
        self.assertFalse((self.quarantine / HASH / "files" / "Movie").exists())
        provenance = json.loads(Path(result["provenance"]).read_text())
        manual = provenance["provenance"]["manual_acceptance"]
        self.assertTrue(manual["detonated"])
        self.assertTrue(manual["vm_reviewed"])

    def test_promote_single_file_torrent_uses_the_filename(self):
        content = self.quarantine / HASH / "files" / "Movie.mp4"
        content.parent.mkdir(parents=True)
        content.write_bytes(b"video")
        manifest = {
            "torrent_hash": HASH,
            "name": "Movie",
            "content_name": "Movie.mp4",
            "entries": [{"relative": "Movie.mp4", "size": 5, "sha256_head": "0" * 64}],
        }
        self.pipeline.manifest_path(HASH).write_text(json.dumps(manifest))
        self.pipeline.verdict_path(HASH).write_text(
            json.dumps(
                {
                    "torrent_hash": HASH,
                    "name": "Movie",
                    "static": {
                        "flags": [],
                        "files": [{"relative": "Movie.mp4", "kind": "unknown"}],
                    },
                    "sandbox": {
                        "engine": "test-guest",
                        "detonated": True,
                        "opened": 1,
                        "reviewed": True,
                    },
                    "verdict": {"label": "clean", "score": 0, "detonated": True, "reasons": []},
                }
            )
        )

        result = self.pipeline.promote(HASH)

        moved = Path(result["moved_to"])
        self.assertTrue(moved.is_file())
        self.assertEqual(moved.name, "Movie.mp4")
        self.assertFalse(content.exists())

    def test_promote_refuses_a_selection_the_review_did_not_keep(self):
        content = self.stage()
        (content / "cover.jpg").write_bytes(b"image")
        manifest = json.loads(self.pipeline.manifest_path(HASH).read_text())
        manifest["entries"].append(
            {"relative": "Movie/cover.jpg", "size": 5, "sha256_head": "0" * 64}
        )
        self.pipeline.manifest_path(HASH).write_text(json.dumps(manifest))
        self.pipeline.scan(HASH)
        self.pipeline.vm_review_path(HASH).write_text(
            json.dumps(
                {
                    "torrent_hash": HASH,
                    "name": "Movie",
                    "selected": ["Movie/movie.mp4"],
                    "engine": "test-guest",
                    "completed": True,
                    "detonated": True,
                    "opened": 2,
                }
            )
        )

        with self.assertRaises(TriageError) as caught:
            self.pipeline.promote(HASH, accept_review=True, keep=["cover.jpg"])

        self.assertIn("did not keep", str(caught.exception))
        self.assertFalse(self.library.exists())

    def test_review_command_carries_the_selection_the_guest_kept(self):
        self.stage()
        self.pipeline.scan(HASH)
        self.pipeline.vm_review_path(HASH).write_text(
            json.dumps(
                {
                    "torrent_hash": HASH,
                    "name": "Movie",
                    "selected": ["Movie/movie.mp4"],
                    "engine": "test-guest",
                    "completed": True,
                    "detonated": True,
                    "opened": 1,
                }
            )
        )

        result = self.pipeline.review(HASH)

        self.assertTrue(result["promotion"]["can_accept_review"])
        self.assertIn("--keep Movie/movie.mp4", result["promotion"]["command"])

    def test_clean_promotion_without_vm_evidence_is_refused(self):
        self.stage()
        self.pipeline.verdict_path(HASH).write_text(
            json.dumps(
                {
                    "torrent_hash": HASH,
                    "name": "Movie",
                    "static": {"flags": [], "files": [{"relative": "Movie/movie.mp4"}]},
                    "sandbox": None,
                    "verdict": {"label": "clean", "score": 0, "detonated": False, "reasons": []},
                }
            )
        )

        with self.assertRaises(TriageError) as caught:
            self.pipeline.promote(HASH)

        self.assertIn("no VM detonation or review evidence", str(caught.exception))
        self.assertFalse(self.library.exists())


if __name__ == "__main__":
    unittest.main()
