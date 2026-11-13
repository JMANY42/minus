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


if __name__ == "__main__":
    unittest.main()
