"""Bridge jellyfin-web to MINUS's torrent search.

`server.Bridge` is the loopback API and the script it publishes; `install`
patches jellyfin-web's `index.html` so the page loads that script. Both are
deliberately outside the harness: here the browser is the caller, so the
interface is HTTP and a file patch rather than a Textual app.
"""

from minus.jellyfin.install import JellyfinError, install, remove, status
from minus.jellyfin.server import Bridge

__all__ = ["Bridge", "JellyfinError", "install", "remove", "status"]
