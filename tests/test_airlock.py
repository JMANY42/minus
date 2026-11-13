"""Airlock checks over real files and a real bubblewrap run.

Nothing here is a stand-in: the archives are built on disk, the modes are set
with `chmod`, and the isolation tests execute `/bin/sh` under the same argv
`sandbox_command` produces. If bubblewrap is not installed the execution tests
skip rather than pretend isolation happened.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

import pytest

from minus.triage.airlock import ENGINES, inspect_tree, run_isolated, sandbox_command
from minus.triage.pipeline import TriagePipeline

BWRAP = shutil.which("bwrap")
HASH = "b" * 40


class InspectTreeTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.addCleanup(self._temp.cleanup)

    def write(self, name: str, data: bytes = b"payload", mode: int = 0o600) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        os.chmod(path, mode)
        return path

    def archive(self, name: str, entries: list[tuple[str, bytes]]) -> Path:
        path = self.root / name
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for member, data in entries:
                archive.writestr(member, data)
        return path

    def test_an_executable_bit_is_reported(self):
        self.write("run.sh", b"#!/bin/sh\necho hi\n", 0o755)

        result = inspect_tree(self.root)

        self.assertIn("executable-bit", result["flags"])

    def test_group_or_other_access_is_not_private(self):
        self.write("notes.txt", b"hello\n", 0o644)

        result = inspect_tree(self.root)

        self.assertIn("not-private", result["flags"])
        self.assertNotIn("executable-bit", result["flags"])

    def test_a_private_file_has_no_flags(self):
        self.write("plain.bin", b"payload", 0o600)

        result = inspect_tree(self.root)

        self.assertEqual(result["flags"], [])
        self.assertEqual(result["files"], 1)

    def test_engines_and_notes_are_reported(self):
        self.write("plain.bin", b"payload", 0o600)

        result = inspect_tree(self.root)

        self.assertEqual([engine for engine in result["engines"] if engine not in ENGINES], [])
        self.assertIsInstance(result["notes"], list)

    def test_archive_traversal_member_is_flagged(self):
        self.archive("nasty.zip", [("../escape", b"x")])

        result = inspect_tree(self.root)

        self.assertIn("archive-traversal", result["flags"])
        self.assertNotIn("archive-bomb", result["flags"])

    def test_an_absolute_member_name_is_traversal(self):
        self.archive("absolute.zip", [("/etc/passwd", b"x")])

        self.assertIn("archive-traversal", inspect_tree(self.root)["flags"])

    def test_a_compressible_member_is_a_bomb_by_ratio(self):
        self.archive("bomb.zip", [("zeros.bin", b"\0" * (1024 * 1024))])

        result = inspect_tree(self.root)

        self.assertIn("archive-bomb", result["flags"])
        self.assertNotIn("archive-traversal", result["flags"])

    def test_a_plain_archive_is_neither_traversal_nor_bomb(self):
        self.archive("plain.zip", [("hello.txt", b"hello world\n")])

        result = inspect_tree(self.root)

        self.assertNotIn("archive-traversal", result["flags"])
        self.assertNotIn("archive-bomb", result["flags"])

    def test_symlinks_are_skipped(self):
        target = self.write("target.bin", b"payload", 0o600)
        os.symlink(target, self.root / "link.bin")

        result = inspect_tree(self.root)

        self.assertEqual(result["files"], 1)

    def test_a_missing_root_is_an_empty_answer(self):
        result = inspect_tree(self.root / "absent")

        self.assertEqual(result["files"], 0)
        self.assertEqual(result["flags"], [])


class SandboxCommandTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.addCleanup(self._temp.cleanup)

    def test_argv_unshares_everything_and_mounts_input_read_only(self):
        argv = sandbox_command(self.root, ["/bin/true"])

        self.assertIn("--unshare-all", argv)
        self.assertIn("--die-with-parent", argv)
        triples = [argv[index : index + 3] for index in range(len(argv) - 2)]
        self.assertIn(["--ro-bind", str(self.root), "/input"], triples)
        self.assertEqual(argv[argv.index("--chdir") + 1], "/input")
        self.assertEqual(argv[-1], "/bin/true")

def test_a_sandboxed_command_reads_the_staged_tree(tmp_path):
    if BWRAP is None:
        pytest.skip("bwrap is not installed")
    (tmp_path / "probe").write_text("probe contents\n")

    result = run_isolated(tmp_path, ["/bin/sh", "-c", "cat /input/probe"])

    assert result["timed_out"] is False
    assert result["exit_code"] == 0
    assert result["stdout"] == "probe contents\n"


def test_the_host_home_is_not_visible(tmp_path):
    if BWRAP is None:
        pytest.skip("bwrap is not installed")

    result = run_isolated(
        tmp_path, ["/bin/sh", "-c", "test -e /home && exit 1 || exit 0"]
    )

    assert result["timed_out"] is False
    assert result["exit_code"] == 0


def test_a_hanging_command_times_out_and_is_killed(tmp_path):
    if BWRAP is None:
        pytest.skip("bwrap is not installed")

    result = run_isolated(tmp_path, ["/bin/sh", "-c", "sleep 5"], timeout=0.2)

    assert result["timed_out"] is True
    assert result["exit_code"] is None


class PipelineAirlockTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.addCleanup(self._temp.cleanup)
        self.quarantine = self.root / "quarantine"
        self.library = self.root / "library"
        self.pipeline = TriagePipeline(quarantine=self.quarantine, library=self.library)

    def stage(self) -> Path:
        content = self.quarantine / HASH / "files" / "Movie"
        content.mkdir(parents=True)
        executable = content / "run.sh"
        executable.write_text("#!/bin/sh\necho hi\n")
        os.chmod(executable, 0o755)
        manifest = {
            "torrent_hash": HASH,
            "name": "Movie",
            "content_name": "Movie",
            "entries": [{"relative": "Movie/run.sh", "size": 20, "sha256_head": "0" * 64}],
        }
        (self.quarantine / HASH / "manifest.json").write_text(json.dumps(manifest))
        return content

    def test_scan_records_the_airlock_section(self):
        self.stage()

        result = self.pipeline.scan(HASH)

        saved = json.loads(self.pipeline.verdict_path(HASH).read_text())
        self.assertIn("airlock", saved)
        self.assertEqual(saved["airlock"]["files"], 1)
        self.assertIn("executable-bit", saved["airlock"]["flags"])
        self.assertIn("executable-bit", result["notes"])

    def test_summary_notes_join_at_most_eight_items(self):
        self.stage()

        result = self.pipeline.scan(HASH)

        self.assertLessEqual(len(result["notes"].split("; ")), 8)



class AirLockResourceCapTests(unittest.TestCase):
    """A hostile torrent must not be able to make the *inspection* expensive."""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.addCleanup(self._temp.cleanup)

    def test_a_large_tree_is_capped(self):
        for index in range(3):
            (self.root / f"f{index}.bin").write_text("x")

        report = inspect_tree(self.root, max_files=2)

        self.assertIn("too-many-files", report["flags"])
        self.assertEqual(report["files"], 2)
        self.assertTrue(any("inspected 2" in note for note in report["notes"]))

    def test_a_zip_with_many_members_is_capped(self):
        archive = self.root / "many.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            for index in range(3):
                handle.writestr(f"m{index}.txt", b"x")

        report = inspect_tree(self.root, max_zip_members=2)

        self.assertIn("too-many-zip-members", report["flags"])

    def test_an_oversized_archive_is_not_opened(self):
        archive = self.root / "big.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("payload.txt", b"x" * 4096)

        report = inspect_tree(self.root, max_archive_bytes=10)

        self.assertIn("archive-too-large", report["flags"])
        self.assertTrue(any("too large to inspect" in note for note in report["notes"]))

    def test_the_sandbox_drops_every_capability(self):
        argv = sandbox_command(self.root, ["/bin/true"])

        self.assertIn("--cap-drop", argv)
        self.assertIn("ALL", argv)
        self.assertIn("--unshare-all", argv)


if __name__ == "__main__":
    unittest.main()
