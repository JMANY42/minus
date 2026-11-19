"""The qBittorrent Web API, as few calls as this needs.

The pipeline uses log in, add, list, list files and delete; provisioning adds
the preferences write, version and transfer info. The API is session-cookie
based (a POST to `/api/v2/auth/login` sets `SID`), which `httpx.Client`'s
cookie jar handles, so there is no token juggling here.

The client is injected (`client=`) so tests can drive a `MockTransport`; the
constructor builds one only when asked to talk to a real instance.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from minus.errors import MinusError

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 10.0


class QBittorrentError(MinusError):
    """qBittorrent refused, or was not there."""


class TorrentAlreadyAdded(QBittorrentError):
    """The torrent's infohash is already in the session.

    qBittorrent 5.x answers HTTP 409 here; that is an answer about the torrent,
    not a transport failure, so callers can treat it as success and go on to
    find the existing torrent rather than reporting a broken client.
    """


class QBittorrentClient:
    """A minimal qBittorrent Web API client."""

    def __init__(
        self,
        base_url: str,
        username: str = "",
        password: str = "",
        *,
        client: Any | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self._client = client
        self._logged_in = False

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = httpx.Client(base_url=self.base_url, timeout=TIMEOUT_SECONDS)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    # ---- Auth ----

    def login(self) -> None:
        """Authenticate, or raise. Doubles as the reachability check."""
        try:
            response = self.client.post(
                "/api/v2/auth/login",
                data={"username": self.username, "password": self.password},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise QBittorrentError(f"Cannot reach qBittorrent at {self.base_url}: {exc}") from exc

        # Older releases answer "Ok." / "Fails." with HTTP 200, so the body is
        # the only signal. qBittorrent 5.3 answers 204 with an empty body on
        # success, which is why a strict "Ok." check left a real install unable
        # to log in even though curl succeeded.
        body = response.text.strip()
        if response.status_code == 204 or body == "Ok.":
            self._logged_in = True
            return

        raise QBittorrentError(
            "qBittorrent refused the login. Check MINUS_QBITTORRENT_USERNAME and "
            "MINUS_QBITTORRENT_PASSWORD, and that the Web UI is enabled."
        )

    def _ensure_login(self) -> None:
        if not self._logged_in:
            self.login()

    # ---- Writing ----

    def add_torrent(
        self,
        urls: list[str],
        *,
        paused: bool = True,
        save_path: str | None = None,
    ) -> str:
        """Add one or more magnet/torrent URLs, and return the API's answer.

        Only URL strings are accepted; there is no file/multipart upload path.
        `save_path` overrides qBittorrent's configured save path for this one
        torrent, so the pipeline can land a download in quarantine without
        changing what every other torrent does. The API answers "Ok." on
        success and "Fails." on refusal, with HTTP 200 in both cases -- the
        body is the only signal for those two, so it is checked here and
        returned rather than discarded. A duplicate infohash is the one answer
        that comes as a status code instead, and raises `TorrentAlreadyAdded`.
        """
        self._ensure_login()
        form = {
            "urls": "\n".join(urls),
            "paused": "true" if paused else "false",
        }
        if save_path is not None:
            form["savepath"] = save_path
        try:
            response = self.client.post("/api/v2/torrents/add", data=form)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # A 409 is qBittorrent saying "I have this infohash already" -- an
            # answer, not a transport failure -- so it gets its own type and the
            # response body, rather than being flattened into "it failed".
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 409:
                body = exc.response.text.strip() or "Conflict"
                raise TorrentAlreadyAdded(f"qBittorrent already has this torrent: {body}") from exc
            raise QBittorrentError(f"Adding a torrent failed: {exc}") from exc

        body = response.text.strip()
        if body == "Ok.":
            return body
        # qBittorrent 5.x answers with a JSON summary instead of "Ok.":
        # {"added_torrent_ids": [...], "success_count": 1, ...}. Treating that
        # as a refusal rejected a torrent the client had already accepted.
        if body.startswith("{"):
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict) and (
                payload.get("added_torrent_ids")
                or payload.get("success_count")
                # A .torrent URL is fetched by qBittorrent itself, so the
                # immediate answer is "pending", not "added". It is still an
                # acceptance; refusing it rejected a download already queued.
                or payload.get("pending_count")
            ):
                return body
        raise QBittorrentError(f"qBittorrent refused the torrent: {body}")

    def set_preferences(self, prefs: dict) -> None:
        """Apply a partial preferences update.

        The API takes one form field, `json`, holding a compact JSON object:
        only the keys in `prefs` are changed, and everything else keeps its
        current value. That is what makes this safe to call on a qBittorrent
        the user also configures by hand.
        """
        self._ensure_login()
        payload = json.dumps(prefs, separators=(",", ":"))
        try:
            response = self.client.post("/api/v2/app/setPreferences", data={"json": payload})
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise QBittorrentError(f"Writing preferences failed: {exc}") from exc

    def delete_torrent(self, torrent_hash: str, delete_files: bool = True) -> None:
        """Remove a torrent, and optionally the bytes it downloaded."""
        self._ensure_login()
        try:
            response = self.client.post(
                "/api/v2/torrents/delete",
                data={
                    "hashes": torrent_hash,
                    "deleteFiles": "true" if delete_files else "false",
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise QBittorrentError(f"Deleting torrent {torrent_hash!r} failed: {exc}") from exc

    # ---- Reading ----

    def version(self) -> str:
        """The running qBittorrent's version string."""
        self._ensure_login()
        try:
            response = self.client.get("/api/v2/app/version")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise QBittorrentError(f"Reading the version failed: {exc}") from exc
        return response.text.strip()

    def transfer_info(self) -> dict:
        """Global transfer rates and connection status."""
        self._ensure_login()
        try:
            response = self.client.get("/api/v2/transfer/info")
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise QBittorrentError(f"Reading transfer info failed: {exc}") from exc
        except ValueError as exc:
            raise QBittorrentError(f"qBittorrent returned non-JSON: {exc}") from exc

        if not isinstance(payload, dict):
            raise QBittorrentError(
                f"qBittorrent returned unexpected transfer info: {type(payload).__name__}"
            )
        return payload

    def torrents(self, **filters: str) -> list[dict]:
        """Every torrent, or those matching the API's own filters."""
        self._ensure_login()
        try:
            response = self.client.get("/api/v2/torrents/info", params=filters or None)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise QBittorrentError(f"Listing torrents failed: {exc}") from exc
        except ValueError as exc:
            raise QBittorrentError(f"qBittorrent returned non-JSON: {exc}") from exc

        return payload if isinstance(payload, list) else []

    def torrent(self, torrent_hash: str) -> dict:
        """One torrent by hash, or raise naming what was asked for."""
        found = self.torrents(hashes=torrent_hash)
        if not found:
            raise QBittorrentError(f"No torrent with hash {torrent_hash!r}")
        return found[0]

    def preferences(self) -> dict:
        """The application settings, of which only one matters here.

        `current_network_interface` is qBittorrent's own binding: an empty
        string means "all interfaces". Downloads happen in qBittorrent's
        process, so this is the only way from outside to see which device it
        actually uses.
        """
        self._ensure_login()
        try:
            response = self.client.get("/api/v2/app/preferences")
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise QBittorrentError(f"Reading preferences failed: {exc}") from exc
        except ValueError as exc:
            raise QBittorrentError(f"qBittorrent returned non-JSON: {exc}") from exc

        if not isinstance(payload, dict):
            raise QBittorrentError(
                f"qBittorrent returned unexpected preferences: {type(payload).__name__}"
            )
        return payload

    def files(self, torrent_hash: str) -> list[dict]:
        """The files inside a torrent, with the API's own progress field."""
        self._ensure_login()
        try:
            response = self.client.get("/api/v2/torrents/files", params={"hash": torrent_hash})
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise QBittorrentError(f"Listing files for {torrent_hash!r} failed: {exc}") from exc
        except ValueError as exc:
            raise QBittorrentError(f"qBittorrent returned non-JSON: {exc}") from exc

        return payload if isinstance(payload, list) else []
