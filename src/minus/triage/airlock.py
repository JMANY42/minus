"""The airlock: what quarantine staging left behind, and how to run inside it.

`inspect_tree` is the cheap half — permissions and archive metadata, no
extraction, no execution. It answers the questions that staging itself can
answer wrongly: a torrent is a stranger's filesystem, so its executable bits,
its loose permissions, and its archives are all untrusted input.

`run_isolated` is the other half: bubblewrap around a command with the staged
tree read-only at `/input`, an empty environment, and no host paths. Nothing in
this module decides "malicious"; it reports facts the verdict may weigh.

WHAT THIS DOES AND DOES NOT BUY
-------------------------------
A file sitting in a directory is not executable by itself. What can hurt the
host is (a) running or opening it, (b) a parser bug in something that reads it
(this module reads zip *directories* with Python's zipfile, and bounds that work
below), or (c) escaping the isolation if it is ever run. `run_isolated` uses
namespaces and drops capabilities, but it is not a virtual machine: the kernel is
the boundary, so a kernel bug or an un-seccomped syscall surface remains. Do not
present this as VM-grade containment.
"""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path

# A zip may claim to expand to anything; past this it is not worth inspecting.
MAX_EXPANDED_BYTES = 512 * 1024 * 1024
# Deflate ratios above this in a single member are characteristic of a bomb.
MAX_RATIO = 100.0
# Antivirus / rule engines this host may have. Presence is reported, not run.
ENGINES = ("clamscan", "yara")

# A torrent is an untrusted filesystem description. These caps bound the work a
# hostile one can ask of the inspector: millions of tiny files, a zip with
# millions of members, or an archive whose central directory is itself the
# attack. Without them the *inspection* is the denial of service.
MAX_FILES = 50_000
MAX_ZIP_MEMBERS = 10_000
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024

# One engine's console output is diagnostic context, not a data channel.
_MAX_OUTPUT_CHARS = 16384
_TRUNCATED = "...[truncated]"

# The host filesystem a sandboxed command is allowed to see: libraries and
# binaries only, read-only, and only those that exist on this distribution.
_HOST_BINDS = ("/usr", "/lib", "/lib64", "/bin", "/sbin")


def _engines_on_path() -> list[str]:
    return [engine for engine in ENGINES if shutil.which(engine) is not None]


def _inspect_zip(
    path: Path,
    relative: str,
    flags: set[str],
    notes: list[str],
    *,
    max_members: int = MAX_ZIP_MEMBERS,
    max_archive_bytes: int = MAX_ARCHIVE_BYTES,
) -> None:
    """Read an archive's directory only: names, sizes, flags. Never extract."""
    try:
        if path.stat().st_size > max_archive_bytes:
            flags.add("archive-too-large")
            notes.append(f"archive too large to inspect: {relative}")
            return

        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()

        if len(members) > max_members:
            flags.add("too-many-zip-members")
            notes.append(
                f"archive has {len(members)} members; inspected the first {max_members}"
            )
            members = members[:max_members]

        expanded = 0
        for info in members:
            # A backslash is a path separator on the platform that built the
            # archive even though the zip spec only defines `/`.
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or ".." in name.split("/"):
                flags.add("archive-traversal")
            if info.flag_bits & 0x1:
                flags.add("archive-encrypted")
            expanded += info.file_size
            if info.compress_size > 0 and info.file_size / info.compress_size > MAX_RATIO:
                flags.add("archive-bomb")
        if expanded > MAX_EXPANDED_BYTES:
            flags.add("archive-bomb")
    except Exception:
        # Corrupt or unsupported archives are a fact to record, not a reason to
        # abort a scan: the rest of the tree still deserves inspection, and a
        # file that calls itself a zip but is not one is suspicious in itself.
        notes.append(f"unreadable zip: {relative}")


def inspect_tree(
    root: Path,
    *,
    max_files: int = MAX_FILES,
    max_zip_members: int = MAX_ZIP_MEMBERS,
    max_archive_bytes: int = MAX_ARCHIVE_BYTES,
) -> dict:
    """Inspect a staged tree without following symlinks or opening content.

    Returns `flags` (sorted, deduplicated), the number of regular `files` seen,
    the malware `engines` present on PATH, and human-readable `notes`. Symlinks
    are skipped rather than followed: following them would inspect the host,
    which is exactly what staging promised not to do.
    """
    root = Path(root)
    if not root.is_dir():
        return {
            "flags": [],
            "files": 0,
            "engines": _engines_on_path(),
            "notes": [f"not a directory: {root}"],
        }

    flags: set[str] = set()
    notes: list[str] = []
    count = 0

    # Sorted for determinism: a scan report should not depend on inode order.
    paths = sorted(root.rglob("*"))
    if len(paths) > max_files:
        flags.add("too-many-files")
        notes.append(f"tree has at least {len(paths)} entries; inspected {max_files}")
        paths = paths[:max_files]

    for path in paths:
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        try:
            mode = path.stat().st_mode
        except OSError:
            notes.append(f"unreadable file: {relative}")
            continue
        count += 1

        # If the torrent carried an executable bit, staging must not keep it;
        # report it here rather than assume the staging code did.
        if mode & 0o111:
            flags.add("executable-bit")
        # Group/other access means another local user could read the payload.
        if mode & 0o077:
            flags.add("not-private")

        try:
            is_archive = zipfile.is_zipfile(path)
        except OSError:
            notes.append(f"unreadable file: {relative}")
            continue
        if is_archive:
            _inspect_zip(
                path,
                relative,
                flags,
                notes,
                max_members=max_zip_members,
                max_archive_bytes=max_archive_bytes,
            )

    engines = _engines_on_path()
    if not engines:
        notes.append("no malware engine installed: static checks only")

    return {
        "flags": sorted(flags),
        "files": count,
        "engines": engines,
        "notes": notes,
    }


def sandbox_command(root: Path, command: list[str]) -> list[str]:
    """The bubblewrap argv that runs `command` with `root` at read-only `/input`.

    Every namespace is unshared, capabilities are dropped, the environment is
    cleared, host binaries and libraries are read-only, and `/tmp` is a fresh
    tmpfs. `root` is read-only at `/input`. Without seccomp this is still
    namespace containment, not a VM: the kernel is the boundary.
    """
    if shutil.which("bwrap") is None:
        raise RuntimeError(
            "bubblewrap (bwrap) is not installed; refusing to run without an airlock"
        )

    argv = [
        "bwrap",
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--cap-drop",
        "ALL",
        "--clearenv",
        "--setenv",
        "PATH",
        "/usr/bin:/bin",
        "--setenv",
        "HOME",
        "/tmp",
    ]
    for base in _HOST_BINDS:
        if Path(base).exists():
            argv += ["--ro-bind", base, base]
    argv += [
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--ro-bind",
        str(root),
        "/input",
        "--chdir",
        "/input",
    ]
    return argv + list(command)


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def _truncate(text: str) -> str:
    return text if len(text) <= _MAX_OUTPUT_CHARS else text[:_MAX_OUTPUT_CHARS] + _TRUNCATED


def run_isolated(root: Path, command: list[str], *, timeout: float = 30.0) -> dict:
    """Run `command` under `sandbox_command`, always returning a result.

    A timeout is an observation ("it hung") rather than an exception; the child
    is killed, and `timed_out` carries the distinction so a caller never has to
    parse an error to learn it.
    """
    argv = sandbox_command(root, command)
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        # `subprocess.run` kills and reaps the child itself on timeout.
        return {
            "exit_code": None,
            "stdout": _truncate(_text(exc.stdout)),
            "stderr": _truncate(_text(exc.stderr)),
            "timed_out": True,
        }
    return {
        "exit_code": completed.returncode,
        "stdout": _truncate(completed.stdout),
        "stderr": _truncate(completed.stderr),
        "timed_out": False,
    }


__all__ = [
    "ENGINES",
    "MAX_ARCHIVE_BYTES",
    "MAX_EXPANDED_BYTES",
    "MAX_FILES",
    "MAX_RATIO",
    "MAX_ZIP_MEMBERS",
    "inspect_tree",
    "run_isolated",
    "sandbox_command",
]
