"""What the bridge needs to know about the page that will call it.

A frontend is a page, not a transport: `jellyfin-web` ships a static bundle
whose `index.html` can be patched in place, while any other app only gets a
copy-pasteable `<script>` tag. Keeping the two behind one protocol means the
CLI and the HTTP layer never branch on "which app" -- they ask for a tag, an
asset, or an install, and a frontend that cannot do one says so.

The registry is explicit rather than discovered by import magic: the valid
names are what `get_frontend` lists in its error, so adding one is a line here
and nothing else.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from minus.errors import MinusError


class FrontendError(MinusError):
    """A frontend could not be resolved, or has no file installer."""


class Frontend(Protocol):
    """The page-side half of a bridge.

    `asset_path` is the single browser client under `remote/web/`; `script_tag`
    renders it for a page that has to be edited by hand; the three file
    operations return whether they changed anything, which is what lets the
    CLI say "installed" versus "already patched" without re-reading the page.
    """

    name: str

    def asset_path(self) -> Path: ...

    def script_tag(self, base_url: str) -> str: ...

    def install(self, web_dir: Path) -> bool: ...

    def status(self, web_dir: Path) -> bool: ...

    def remove(self, web_dir: Path) -> bool: ...

    def notes(self) -> str: ...


# Imported after the protocol and error exist: the frontend modules import
# `FrontendError` from this package, so importing them at the top would ask a
# partially initialized module for a name it has not defined yet.
from minus.remote.frontends.generic import GenericFrontend  # noqa: E402
from minus.remote.frontends.jellyfin import JellyfinFrontend  # noqa: E402

FRONTENDS: dict[str, Frontend] = {
    "jellyfin": JellyfinFrontend(),
    "generic": GenericFrontend(),
}


def get_frontend(name: str) -> Frontend:
    """Resolve a frontend by the name the CLI exposes."""
    try:
        return FRONTENDS[name]
    except KeyError:
        valid = ", ".join(sorted(FRONTENDS))
        raise FrontendError(f"unknown frontend {name!r}; choose one of: {valid}") from None
