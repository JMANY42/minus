"""Editing `.env` without destroying it.

A settings file that a person also edits by hand cannot be round-tripped
through a parser and rewritten -- that loses the comments explaining why a
value is what it is, the ordering, the blank lines, and any key this program
does not know about. So nothing is rewritten except the specific lines being
changed: everything else survives because it is never touched.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# `export FOO=` is valid in a .env and is what someone who also sources the
# file by hand will have written.
_ASSIGNMENT = re.compile(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)(\s*=)(.*)$")

_HEADER = "# --- written by the minus dashboard ---"

# Characters that make a bare value mean something other than itself once the
# file is sourced by a shell or read by a parser that honours quoting.
_NEEDS_QUOTING = set(" \t\"'#$`\\")


def quote(value: str) -> str:
    """A value safe to write on the right of an `=`.

    Only backslash and the double quote are escaped, because those are the
    only two python-dotenv unescapes. `\\$` in particular is *not* -- dotenv
    hands back the backslash as part of the value -- so escaping a dollar sign
    corrupts it rather than protecting it. Left bare it round-trips, since
    dotenv does not expand `$VAR` in the first place.
    """
    if value == "" or any(character in _NEEDS_QUOTING for character in value):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def update_env_file(
    path: Path | str,
    updates: dict[str, str],
    *,
    allowed: frozenset[str] | None = None,
) -> list[str]:
    """Set each key in `updates`, and return the names actually written.

    `allowed` is enforced here rather than only at the caller. This function
    is the last thing between a socket command and a file on disk that holds
    an API key, and a refusal is worth expressing where the write happens.
    """
    if allowed is not None:
        refused = set(updates) - set(allowed)
        if refused:
            raise ValueError(f"Refusing to write {', '.join(sorted(refused))} to {path}")

    path = Path(path)
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = original.splitlines()

    remaining = dict(updates)
    rewritten: list[str] = []

    for line in lines:
        match = _ASSIGNMENT.match(line)
        if match is None:
            rewritten.append(line)
            continue

        prefix, key, equals, _ = match.groups()
        if key in remaining:
            rewritten.append(f"{prefix}{key}{equals}{quote(remaining.pop(key))}")
        else:
            rewritten.append(line)

    if remaining:
        if rewritten and rewritten[-1].strip():
            rewritten.append("")
        if _HEADER not in original:
            rewritten.append(_HEADER)
        # Sorted so that repeated writes produce a stable file rather than one
        # whose diff depends on dict ordering.
        for key in sorted(remaining):
            rewritten.append(f"{key}={quote(remaining[key])}")

    _write(path, "\n".join(rewritten) + "\n")
    return sorted(updates)


def _write(path: Path, content: str) -> None:
    """Replace the file atomically, keeping its mode.

    Same temp-file-then-rename shape as services/json.py, for the same reason:
    a reader never sees a half-written file, and an interrupted write cannot
    leave the assistant with no API key. 0600 on a new file because of what
    this one tends to contain.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600

    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text(content, encoding="utf-8")
    os.chmod(temp_path, mode)
    os.replace(temp_path, path)
