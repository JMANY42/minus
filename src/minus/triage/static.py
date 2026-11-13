"""Static inspection of downloaded files: what is inside, before anything runs.

Everything here is deterministic and offline. The questions are deliberately
narrow — what kind of file is this, is it a Windows executable, is it signed,
does it look packed, does its name lie about what it is — because those are the
signals that hold up without a sandbox. Nothing here decides "malicious": the
verdict function (`triage/scoring.py`) refuses to call a file clean without a
detonation, and this module never claims otherwise.
"""

from __future__ import annotations

import hashlib
import math
import struct
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

# Read at most this much of one file for hashing and entropy. A 40 GB disc image
# in a torrent should not be read twice into memory, and the head of a file is
# where the executable's structure lives.
SAMPLE_BYTES = 4 * 1024 * 1024
# The hash reported for a file is over its first 16 MiB, and is named that way
# everywhere it travels, so nobody mistakes it for a whole-file digest.
HEAD_HASH_BYTES = SAMPLE_BYTES * 4

MZ_MAGIC = b"MZ"
PE_SIGNATURE = b"PE\0\0"

EXECUTABLE_SUFFIXES = {
    ".exe",
    ".dll",
    ".scr",
    ".com",
    ".pif",
    ".cpl",
    ".ocx",
    ".sys",
    ".drv",
    ".msi",
}
SCRIPT_SUFFIXES = {".bat", ".cmd", ".ps1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".hta", ".jar"}
DOCUMENT_SUFFIXES = {
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".rtf",
    ".txt",
    ".jpg",
    ".jpeg",
    ".png",
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".zip",
    ".rar",
    ".7z",
}
MACRO_SUFFIXES = {".docm", ".xlsm", ".pptm", ".dotm", ".xlam"}

# Weight of each flag in the static score. Weights are a judgement, not a
# measurement: a double extension outweighs an unsigned binary because one is a
# naming lie and the other is merely common.
FLAG_WEIGHTS = {
    "windows-pe": 1,
    "elf-binary": 1,
    "macho-binary": 1,
    "unsigned-pe": 1,
    "high-entropy": 2,
    "packed-section": 3,
    "double-extension": 4,
    "macro-enabled-document": 3,
    "script": 2,
    "shortcut": 3,
    "archive": 1,
    "no-extension": 1,
}


class StaticError(Exception):
    """A file could not be inspected at all."""


@dataclass
class FileReport:
    relative: str
    size: int
    sha256: str
    kind: str
    entropy: float
    flags: list[str] = field(default_factory=list)
    pe: dict | None = None


@dataclass
class StaticReport:
    root: str
    files: list[FileReport]
    flags: list[str]
    score: int

    @property
    def total_bytes(self) -> int:
        return sum(file.size for file in self.files)


def sha256_file(path: Path, *, limit: int | None = None) -> str:
    digest = hashlib.sha256()
    read = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            if limit is not None:
                remaining = limit - read
                if remaining <= 0:
                    break
                chunk = chunk[:remaining]
            digest.update(chunk)
            read += len(chunk)
    return digest.hexdigest()


def shannon_entropy(data: bytes) -> float:
    """Bits per byte, 0..8. Random or compressed data sits near 8."""
    if not data:
        return 0.0
    counts = Counter(data)
    total = len(data)
    entropy = 0.0
    for count in counts.values():
        probability = count / total
        entropy -= probability * math.log2(probability)
    return entropy


def detect_kind(header: bytes, name: str) -> str:
    """What the file is, from magic bytes first and the name second."""
    if header.startswith(MZ_MAGIC):
        return "pe"
    if header.startswith(b"\x7fELF"):
        return "elf"
    if header[:4] in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf"):
        return "macho"
    if header.startswith(b"%PDF"):
        return "pdf"
    if header.startswith(b"PK\x03\x04"):
        return "zip"
    if header.startswith(b"Rar!"):
        return "rar"
    if header.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "7z"
    if header.startswith(b"\xd0\xcf\x11\xe0"):
        return "ole"
    if header.startswith(b"#!"):
        return "script"

    suffix = Path(name).suffix.lower()
    if suffix in EXECUTABLE_SUFFIXES:
        return "executable"
    if suffix in SCRIPT_SUFFIXES:
        return "script"
    if suffix in MACRO_SUFFIXES:
        return "ole"
    if suffix in {".lnk"}:
        return "shortcut"
    if suffix in {".zip", ".rar", ".7z", ".tar", ".gz"}:
        return "archive"
    return "unknown"


def parse_pe(data: bytes) -> dict | None:
    """The parts of a PE header worth reporting. None if it does not parse.

    Offsets are from the PE/COFF specification: DOS header, `e_lfanew` at 0x3c,
    COFF header 20 bytes, optional header whose size the COFF header declares,
    then the section table.
    """
    if len(data) < 0x40 or not data.startswith(MZ_MAGIC):
        return None

    try:
        e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
        if data[e_lfanew : e_lfanew + 4] != PE_SIGNATURE:
            return None
        coff = e_lfanew + 4
        if coff + 20 > len(data):
            return None

        machine, section_count = struct.unpack_from("<HH", data, coff)
        _timestamp = struct.unpack_from("<I", data, coff + 4)[0]
        optional_size = struct.unpack_from("<H", data, coff + 16)[0]
        characteristics = struct.unpack_from("<H", data, coff + 18)[0]

        optional = coff + 20
        magic = struct.unpack_from("<H", data, optional)[0]
        subsystem = struct.unpack_from("<H", data, optional + 68)[0]
        signed = _has_security_directory(data, optional, magic)

        sections: list[dict] = []
        table = optional + optional_size
        for index in range(min(section_count, 96)):
            offset = table + index * 40
            if offset + 40 > len(data):
                break
            name = data[offset : offset + 8].rstrip(b"\0").decode("latin-1")
            virtual_size, _virtual_address, raw_size, raw_pointer = struct.unpack_from(
                "<IIII", data, offset + 8
            )
            body = data[raw_pointer : raw_pointer + min(raw_size, SAMPLE_BYTES)]
            sections.append(
                {
                    "name": name,
                    "raw_size": raw_size,
                    "virtual_size": virtual_size,
                    "entropy": round(shannon_entropy(body), 3),
                }
            )
    except (struct.error, IndexError):
        return None

    return {
        "machine": {0x14C: "x86", 0x8664: "x64", 0xAA64: "arm64"}.get(machine, hex(machine)),
        "subsystem": subsystem,
        "sections": sections,
        "signed": signed,
        "dll": bool(characteristics & 0x2000),
    }


def _has_security_directory(data: bytes, optional: int, magic: int) -> bool:
    """Whether the optional header declares a non-empty certificate table."""
    directories = optional + (96 if magic == 0x10B else 112)
    count_offset = optional + (92 if magic == 0x10B else 108)
    try:
        count = struct.unpack_from("<I", data, count_offset)[0]
        if count <= 4:
            return False
        _address, size = struct.unpack_from("<II", data, directories + 4 * 8)
    except (struct.error, IndexError):
        return False
    return size > 0


def _name_flags(name: str) -> list[str]:
    flags: list[str] = []
    suffix = Path(name).suffix.lower()
    parts = [part for part in name.lower().split(".") if part]

    if not suffix:
        flags.append("no-extension")
    if suffix in EXECUTABLE_SUFFIXES and any(
        f".{part}" in DOCUMENT_SUFFIXES for part in parts[:-1]
    ):
        flags.append("double-extension")
    if suffix in MACRO_SUFFIXES:
        flags.append("macro-enabled-document")
    return flags


def inspect(path: Path, relative: str | None = None) -> FileReport:
    """Inspect one file. `relative` is what to report it as (a manifest name)."""
    relative = relative or path.name
    size = path.stat().st_size
    with path.open("rb") as handle:
        head = handle.read(SAMPLE_BYTES)

    kind = detect_kind(head, path.name)
    flags = _name_flags(path.name)
    pe = parse_pe(head) if kind == "pe" else None

    if kind in ("pe", "executable"):
        flags.append("windows-pe")
    elif kind == "elf":
        flags.append("elf-binary")
    elif kind == "macho":
        flags.append("macho-binary")
    elif kind in ("zip", "rar", "7z", "archive"):
        flags.append("archive")
    elif kind in ("script",):
        flags.append("script")
    elif kind == "shortcut":
        flags.append("shortcut")

    entropy = shannon_entropy(head)
    if entropy >= 7.2:
        flags.append("high-entropy")

    if pe is not None:
        if not pe["signed"]:
            flags.append("unsigned-pe")
        packed = any(
            section["entropy"] >= 7.5 and section["raw_size"] > 4096 for section in pe["sections"]
        )
        if packed:
            flags.append("packed-section")

    return FileReport(
        relative=relative,
        size=size,
        sha256=sha256_file(path, limit=HEAD_HASH_BYTES) if size > 0 else "",
        kind=kind,
        entropy=round(entropy, 3),
        flags=sorted(set(flags)),
        pe=pe,
    )


def analyse(root: Path) -> StaticReport:
    """Inspect every regular file under `root`; symlinks are never followed."""
    root = Path(root)
    if not root.is_dir():
        raise StaticError(f"Not a directory: {root}")

    files: list[FileReport] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        files.append(inspect(path, relative=str(path.relative_to(root))))

    flags = sorted({flag for report in files for flag in report.flags})
    score = sum(FLAG_WEIGHTS.get(flag, 1) for flag in flags)
    return StaticReport(root=str(root), files=files, flags=flags, score=score)
