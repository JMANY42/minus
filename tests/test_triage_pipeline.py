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

from minus.triage.pipeline import TriageError, TriagePipeline

HASH = "a" * 40


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.addCleanup(self._temp.cleanup)
        self.quarantine = self.root / "quarantine"
        self.library = self.root / "library"
        self.pipeline = TriagePipeline(quarantine=self.quarantine, library=self.library)

    def stage(self, name: str = "Movie") -> Path:
        content = self.quarantine / HASH / "files" / name
        content.mkdir(parents=True)
        (content / "movie.mkv").write_bytes(b"payload")
        manifest = {
            "torrent_hash": HASH,
            "name": name,
            "content_name": name,
            "entries": [{"relative": f"{name}/movie.mkv", "size": 7, "sha256_head": "0" * 64}],
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
        self.assertEqual(saved["notes"], "")

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

        result = self.pipeline.promote(
            HASH,
            accept_review=True,
            accepted_by="tester",
            reason="checked the file list",
        )

        moved = Path(result["moved_to"])
        self.assertTrue(moved.is_dir())
        self.assertTrue((moved / "movie.mkv").is_file())
        provenance = json.loads(Path(result["provenance"]).read_text())
        manual = provenance["provenance"]["manual_acceptance"]
        self.assertEqual(manual["accepted_by"], "tester")
        self.assertEqual(manual["reason"], "checked the file list")
        self.assertFalse(manual["detonated"])
        self.assertEqual(manual["verdict"], "needs_review")

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
        self.assertTrue(result["promotion"]["can_accept_review"])
        self.assertIn("--accept-review", result["promotion"]["command"])
        self.assertEqual(result["files"][0]["relative"], "Movie/movie.mkv")
        self.assertIsInstance(result["media_probe"], list)


if __name__ == "__main__":
    unittest.main()
