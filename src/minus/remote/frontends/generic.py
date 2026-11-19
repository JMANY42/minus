"""The fallback for any page that can take a `<script>` tag.

There is no universal place to put it: an app may serve a template, a bundle,
or a file the package manager owns, and guessing wrong is worse than asking.
So this frontend has no file operations at all -- it publishes the tag for a
human to paste, and refuses install/status/remove with a message that says so
rather than pretending it looked.
"""

from __future__ import annotations

from pathlib import Path

from minus.remote.frontends import FrontendError

_ASSET = Path(__file__).resolve().parent.parent / "web" / "minus.js"

_NO_INSTALLER = (
    "no installer; paste the tag from `minus bridge tag --frontend generic` into your page's <head>"
)


class GenericFrontend:
    """Tag-only frontend: the bridge can serve it, but nothing is patched."""

    name = "generic"

    def asset_path(self) -> Path:
        return _ASSET

    def script_tag(self, base_url: str) -> str:
        url = f"{base_url.rstrip('/')}/minus.js"
        return f'<script defer src="{url}" data-minus-remote="1"></script>'

    def install(self, web_dir: Path) -> bool:
        raise FrontendError(_NO_INSTALLER)

    def status(self, web_dir: Path) -> bool:
        raise FrontendError(_NO_INSTALLER)

    def remove(self, web_dir: Path) -> bool:
        raise FrontendError(_NO_INSTALLER)

    def notes(self) -> str:
        return (
            "no file layout to patch: print the tag with `minus bridge tag` and "
            "paste it before </head> yourself"
        )
