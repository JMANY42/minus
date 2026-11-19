"""Reusable remote control for MINUS's torrent search.

Three layers, each importable on its own: `DownloadService` is the behaviour
and knows nothing about HTTP, `HttpBridge` is a loopback transport around one
service, and `frontends` describes the page that may load the script. Keeping
them apart is what lets another agent call the service in-process, another
process call it over the socket, and a web app (Jellyfin or anything else)
load the same browser client.

`FRONTENDS`/`get_frontend` are re-exported here so callers do not have to know
the package layout just to name the default frontend.
"""

from minus.remote.frontends import FRONTENDS, Frontend, FrontendError, get_frontend
from minus.remote.http import HttpBridge
from minus.remote.service import DownloadService

__all__ = [
    "FRONTENDS",
    "DownloadService",
    "Frontend",
    "FrontendError",
    "HttpBridge",
    "get_frontend",
]
