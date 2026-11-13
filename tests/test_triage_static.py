"""Static inspection: what can be known about a file without running it.

The PE fixture is built byte by byte from the PE/COFF layout, because the parser
is the part most likely to be wrong in a way a real-world file would hide. Every
assertion here is about structure the format guarantees, not about a heuristic.
"""

from __future__ import annotations

import os
import struct
import tempfile
import unittest
from pathlib import Path

from minus.triage.static import (
    FLAG_WEIGHTS,
    FileReport,
    analyse,
    detect_kind,
    inspect,
    parse_pe,
    shannon_entropy,
)


def make_pe(section: bytes = bytes(512)) -> bytes:
    """A minimal but structurally valid PE32+ image with one section."""
    data = bytearray(0x40)
    data[0:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x40)
    data += b"PE\0\0"
    optional_size = 0xE0
    data += struct.pack("<HHIIIHH", 0x8664, 1, 0, 0, 0, optional_size, 0x0022)

    optional = bytearray(optional_size)
    struct.pack_into("<H", optional, 0, 0x20B)  # PE32+
    struct.pack_into("<H", optional, 68, 2)  # Windows GUI subsystem
    struct.pack_into("<I", optional, 108, 16)  # NumberOfRvaAndSizes
    struct.pack_into("<II", optional, 112 + 32, 0, 0)  # no certificate table
    data += optional

    raw_pointer = len(data) + 40
    data += struct.pack("<8sIIII", b".text", len(section), 0x1000, len(section), raw_pointer)
    data += bytes(16)  # rest of the section header
    data += section
    return bytes(data)


class ParserTests(unittest.TestCase):
    def test_parses_a_well_formed_image(self):
        pe = parse_pe(make_pe())

        self.assertIsNotNone(pe)
        self.assertEqual(pe["machine"], "x64")
        self.assertEqual(pe["subsystem"], 2)
        self.assertFalse(pe["signed"])
        self.assertFalse(pe["dll"])
        self.assertEqual([section["name"] for section in pe["sections"]], [".text"])

    def test_a_truncated_header_is_refused_rather_than_guessed(self):
        self.assertIsNone(parse_pe(b"MZ" + bytes(10)))
        self.assertIsNone(parse_pe(make_pe()[:0x60]))

    def test_a_non_pe_file_is_refused(self):
        self.assertIsNone(parse_pe(b"\x7fELF" + bytes(64)))

    def test_entropy_separates_zeros_from_noise(self):
        self.assertEqual(shannon_entropy(bytes(4096)), 0.0)
        self.assertGreater(shannon_entropy(os.urandom(4096)), 7.5)

    def test_magic_beats_the_extension(self):
        self.assertEqual(detect_kind(b"MZ" + bytes(60), "notes.txt"), "pe")
        self.assertEqual(detect_kind(b"%PDF-1.7", "it.pdf"), "pdf")
        self.assertEqual(detect_kind(b"PK\x03\x04", "pack.zip"), "zip")
        self.assertEqual(detect_kind(b"hello", "run.bat"), "script")


class InspectTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.addCleanup(self._temp.cleanup)

    def write(self, name: str, data: bytes) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_an_unsigned_executable_carries_the_expected_flags(self):
        report = inspect(self.write("setup.exe", make_pe()))

        self.assertEqual(report.kind, "pe")
        for flag in ("windows-pe", "unsigned-pe"):
            self.assertIn(flag, report.flags)
        self.assertEqual(report.pe["machine"], "x64")
        self.assertGreater(report.size, 0)
        self.assertEqual(len(report.sha256), 64)

    def test_a_double_extension_is_a_flag_of_its_own(self):
        report = inspect(self.write("holiday.mp4.exe", make_pe()))
        self.assertIn("double-extension", report.flags)
        self.assertGreaterEqual(FLAG_WEIGHTS["double-extension"], 3)

    def test_a_macro_document_is_flagged_even_without_a_parser(self):
        report = inspect(self.write("invoice.docm", b"\xd0\xcf\x11\xe0" + bytes(64)))
        self.assertIn("macro-enabled-document", report.flags)

    def test_a_file_with_no_extension_is_not_silently_normal(self):
        report = inspect(self.write("mystery", b"random bytes"))
        self.assertIn("no-extension", report.flags)

    def test_a_compressed_section_reads_as_packed(self):
        report = inspect(self.write("packed.exe", make_pe(os.urandom(8192))))
        self.assertIn("packed-section", report.flags)
        self.assertIn("high-entropy", report.flags)

    def test_analyse_unions_flags_and_ignores_symlinks(self):
        self.write("a.exe", make_pe())
        self.write("b.docm", b"\xd0\xcf\x11\xe0" + bytes(64))
        outside = self.root.parent / f"outside-{os.getpid()}.txt"
        outside.write_text("secret")
        self.addCleanup(outside.unlink)
        (self.root / "link.txt").symlink_to(outside)

        report = analyse(self.root)

        names = [file.relative for file in report.files]
        self.assertIn("a.exe", names)
        self.assertNotIn("link.txt", names)
        self.assertIn("windows-pe", report.flags)
        self.assertIn("macro-enabled-document", report.flags)
        self.assertEqual(
            report.score,
            sum(FLAG_WEIGHTS.get(flag, 1) for flag in report.flags),
        )
        self.assertIsInstance(report.files[0], FileReport)


if __name__ == "__main__":
    unittest.main()
