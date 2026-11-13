"""Static secret scanning for the checkout and anything it downloads.

The scanner exists so that a cleanup pass can be checked mechanically, and so
that a future `minus doctor` can say "no key-shaped strings in the tree"
without anyone reading files by hand.

Two rules shape it:

  * A finding names the file, the line and the pattern -- never the matched
    text. A scanner whose output quotes the secret has copied the secret into
    the terminal, the shell history and the issue report. Callers only ever
    receive (path, line, kind).
  * It is deliberately pattern-matching, not entropy-scoring. A high-entropy
    heuristic on a music library or a model file reports thousands of false
    positives; these six shapes are the ones that actually appear in a
    checkout, and each is cheap to recognise.

Standard library only: this runs from the CLI before the optional extras are
installed, and a scanner that cannot start is a scanner nobody runs.
"""

from __future__ import annotations

import re
from pathlib import Path

# Directories whose contents are either not source (caches), not the
# developer's work (checkouts), or runtime data that legitimately holds
# arbitrary text. Walking them costs seconds and reports nothing useful --
# semantic_memory.db in particular would be scanned as one long binary blob.
SKIP_DIRS = (
    ".git",
    ".venv",
    ".venv.old",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "target",
    "memory",
    "logs",
    "quarantine",
    "library",
)

# (kind, pattern). The names are part of the output, so they are stable rather
# than pretty: a report line is `file:line kind`.
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # OpenAI/DeepSeek style keys, including the `sk-proj-` form.
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    # Classic GitHub personal access tokens.
    ("github_token", re.compile(r"\bghp_[A-Za-z0-9]{20,}")),
    # Slack bot/user/app/refresh/rotation/webhook tokens.
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    # PEM private keys, whatever the algorithm.
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    # AWS access key IDs.
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    # The common case that no prefix covers: a quoted literal assigned to a
    # secret-looking name. 16 characters is the shortest length that is a
    # credential rather than an example ("password" is 8).
    (
        "secret_assignment",
        re.compile(
            r"(?i)\b(api[_-]?key|secret|password|token)\b\s*[:=]\s*['\"]([^'\n\"]{16,})['\"]"
        ),
    ),
)

# Files are capped so that a stray multi-gigabyte artefact cannot make a scan
# look hung. No source file is 2 MiB; keys appear in text.
_MAX_BYTES = 2 * 1024 * 1024
# A NUL byte in the first block means binary; decoding straight into text
# would still "work" with errors=ignore, but matched byte soup would produce
# nonsense findings.
_SNIFF_BYTES = 8 * 1024


def scan_file(path: Path) -> list[dict]:
    """Return findings for one file, or [] if it is clean or unreadable.

    A finding is ``{"path": str, "line": int, "kind": str}``. The matched
    text is deliberately absent: it is the secret.
    """
    try:
        with path.open("rb") as handle:
            data = handle.read(_MAX_BYTES)
    except OSError:
        # An unreadable file (permissions, a broken symlink, a socket) is not
        # a finding; walking a tree must not die on one.
        return []

    if b"\x00" in data[:_SNIFF_BYTES]:
        return []

    text = data.decode("utf-8", errors="ignore")
    findings: list[dict] = []
    for kind, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            findings.append(
                {
                    "path": str(path),
                    "line": text.count("\n", 0, match.start()) + 1,
                    "kind": kind,
                }
            )
    findings.sort(key=lambda item: (item["line"], item["kind"]))
    return findings


def scan_repository(root: Path) -> list[dict]:
    """Scan every regular file under *root*, skipping the directories above.

    Sorted by path, then line, then kind, so two runs produce identical output
    and a diff between runs is a real change rather than walk order.
    """
    findings: list[dict] = []
    for path in root.rglob("*"):
        # A skipped directory must take its whole subtree with it. Checking the
        # *parts* of the path we were handed (not just the name) is what does
        # that: rglob yields every file under `.venv/` individually.
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if not path.is_file():
            continue
        findings.extend(scan_file(path))

    findings.sort(key=lambda item: (item["path"], item["line"], item["kind"]))
    return findings
