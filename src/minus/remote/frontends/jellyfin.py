"""Patch jellyfin-web's index.html to load the MINUS bridge script.

jellyfin-web serves a static bundle, so putting a script into that page means
editing the one file it always serves: `index.html`. That file belongs to the
package manager and is replaced by every upgrade, so the patch has to be
small, reversible, and identifiable afterwards. Hence one marked `<script>`
tag, a single `.minus-backup` copy written the first time, and a `remove` that
restores that backup when there is one instead of trying to reverse a string
edit it cannot see.

The marker used to be `data-minus-jellyfin`. It is now `data-minus-remote` so
the tag says what it is on any app, but `install` still recognizes the old one:
a page patched by an earlier build must be replaced, not patched a second time,
or the browser would load the client twice.

Permissions are not swallowed: `/usr/share/jellyfin/web` belongs to root, and
a silent failure would leave the user believing the bridge was installed. The
error names sudo because that is the only fix.
"""

from __future__ import annotations

import re
from pathlib import Path

from minus.remote.frontends import FrontendError

# Where the CLI's tag command and this installer assume the bridge listens.
DEFAULT_BASE_URL = "http://127.0.0.1:8099"
SCRIPT_URL = f"{DEFAULT_BASE_URL}/minus.js"
MARKER = "data-minus-remote"
LEGACY_MARKER = "data-minus-jellyfin"
BACKUP_SUFFIX = ".minus-backup"

# Any marked tag, including the whitespace it was inserted with, so a re-run
# replaces rather than accumulates and a removal leaves no blank line behind.
# Both markers match: a legacy tag is still ours to replace or remove.
_TAG_RE = re.compile(
    r"[ \t]*<script\b[^>]*\b(?:data-minus-remote|data-minus-jellyfin)\b[^>]*>\s*</script>\s*",
    re.IGNORECASE,
)
_HEAD_RE = re.compile(r"</head\s*>", re.IGNORECASE)

_ASSET = Path(__file__).resolve().parent.parent / "web" / "minus.js"


def index_html(web_dir: Path) -> Path:
    return Path(web_dir) / "index.html"


def _tag(script_url: str) -> str:
    return f'<script defer src="{script_url}" {MARKER}="1"></script>'


def _script_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/minus.js"


def status(web_dir: Path) -> bool:
    """Whether a bridge tag is present; a missing index.html is "not"."""
    try:
        return bool(_TAG_RE.search(index_html(web_dir).read_text(encoding="utf-8")))
    except OSError:
        return False


def install(web_dir: Path, script_url: str = SCRIPT_URL) -> bool:
    """Insert the marked script tag; True when the file changed.

    Idempotent: two installs leave exactly one tag, and the backup still holds
    the bytes from before the first one. A tag carrying the legacy marker is
    stripped with the current one, so upgrading in place cannot double-load the
    client.
    """
    path = index_html(web_dir)
    try:
        original = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FrontendError(f"cannot read {path}: {exc}") from exc

    # Strip every marked tag first, so a file that was patched by hand or by an
    # older bridge still ends up with one tag in the canonical position.
    unpatched = _TAG_RE.sub("", original)
    tag = _tag(script_url)
    anchor = _HEAD_RE.search(unpatched)
    if anchor is None:
        # No head to anchor on is unusual for jellyfin-web, but appending is
        # still better than refusing to install into a page that loads scripts.
        patched = unpatched.rstrip() + "\n" + tag + "\n"
    else:
        patched = unpatched[: anchor.start()] + tag + "\n" + unpatched[anchor.start() :]

    backup = path.with_name(path.name + BACKUP_SUFFIX)
    if not backup.exists():
        # Written before the edit so a crash between the two still leaves a
        # restorable copy; on a re-run the existing backup is the older one.
        _write(backup, unpatched, path)
    if patched == original:
        return False
    _write(path, patched, path)
    return True


def remove(web_dir: Path) -> bool:
    """Restore the backup when present, else strip the tag; True when changed."""
    path = index_html(web_dir)
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    if backup.exists():
        try:
            restored = backup.read_text(encoding="utf-8")
        except OSError as exc:
            raise FrontendError(f"cannot read {backup}: {exc}") from exc
        _write(path, restored, path)
        try:
            backup.unlink()
        except PermissionError as exc:
            raise FrontendError(
                f"cannot remove {backup}: permission denied. Re-run with sudo "
                f"(e.g. sudo minus bridge remove --web-dir {path.parent})"
            ) from exc
        except OSError as exc:
            raise FrontendError(f"cannot remove {backup}: {exc}") from exc
        return True

    try:
        current = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FrontendError(f"cannot read {path}: {exc}") from exc
    stripped = _TAG_RE.sub("", current)
    if stripped == current:
        return False
    _write(path, stripped, path)
    return True


def _write(path: Path, text: str, owned: Path) -> None:
    """Write, turning a permission failure into the one remedy that works."""
    try:
        path.write_text(text, encoding="utf-8")
    except PermissionError as exc:
        raise FrontendError(
            f"cannot write {path}: permission denied. Re-run with sudo "
            f"(e.g. sudo minus bridge install --web-dir {owned.parent})"
        ) from exc
    except OSError as exc:
        raise FrontendError(f"cannot write {path}: {exc}") from exc


class JellyfinFrontend:
    """jellyfin-web's static bundle, patched in place."""

    name = "jellyfin"

    def asset_path(self) -> Path:
        return _ASSET

    def script_tag(self, base_url: str) -> str:
        return _tag(_script_url(base_url))

    def install(self, web_dir: Path) -> bool:
        return install(web_dir)

    def status(self, web_dir: Path) -> bool:
        return status(web_dir)

    def remove(self, web_dir: Path) -> bool:
        return remove(web_dir)

    def notes(self) -> str:
        return (
            "one script tag is written into index.html; re-run install after a "
            "jellyfin-web upgrade replaces that file"
        )
