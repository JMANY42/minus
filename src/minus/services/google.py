"""One Google grant, shared by every Google API MINUS talks to.

Tasks and Calendar are two APIs and one authorization: the same client, the
same consent screen, the same refresh token, and -- once the scopes are asked
for together -- the same access token. Holding the token here rather than in
each API client is what keeps that true. Two clients holding their own copies
would refresh twice for one grant, and would drift apart on which one had
noticed a revocation.

Google's own client libraries were the obvious first choice and are the wrong
shape for this program. `google-auth-oauthlib` wants to own a credentials file,
open a browser and write a `token.json` next to it; `minus serve` runs under
systemd with no browser, no tty and one configuration file. So the durable half
of the grant -- the refresh token -- lives in `.env` alongside every other
credential, and this module buys a short-lived access token with it whenever
the one it holds has expired. Nothing is written to disk at runtime.

That also keeps the dependency footprint at httpx, which `openai` already
brings in. Between them the two APIs are a dozen REST calls; wrapping them was
cheaper than taking on the discovery-document client and its transitive auth
stack.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import httpx

from minus.errors import GoogleAuthError, GoogleError

logger = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"

# Read *and* write, for both APIs. The read-only variants cannot add, edit,
# move or delete, which is four fifths of what is built on this.
TASKS_SCOPE = "https://www.googleapis.com/auth/tasks"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"
SCOPES = (TASKS_SCOPE, CALENDAR_SCOPE)

# Asked for together, in one consent screen, so that one refresh token covers
# both. Scopes are not additive after the fact: a token issued when MINUS only
# knew about tasks stays a tasks-only token forever, however many scopes a
# later build wants. That is why adding an API means re-running `minus
# google-auth`, and why the 403 below is worth naming precisely.
SCOPE_PARAMETER = " ".join(SCOPES)

# Access tokens last an hour. Renewed early so a call that starts just before
# the boundary is not the one that discovers it.
_EXPIRY_MARGIN_SECONDS = 60.0

# What Google says when the token is valid but was not granted the scope the
# request needs -- which is exactly the state an existing MINUS install is in
# the first time it reaches for a calendar.
_INSUFFICIENT_SCOPE = "ACCESS_TOKEN_SCOPE_INSUFFICIENT"


class GoogleCredentials:
    """One account's grant: the refresh token, and the access token it buys."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        *,
        http: httpx.Client | None = None,
        timeout: float = 15.0,
    ) -> None:
        if not (client_id and client_secret and refresh_token):
            raise GoogleAuthError(
                "Google needs MINUS_GOOGLE_CLIENT_ID, MINUS_GOOGLE_CLIENT_SECRET "
                "and MINUS_GOOGLE_REFRESH_TOKEN in .env. Run `minus google-auth` to get them."
            )
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._http = http if http is not None else httpx.Client(timeout=timeout)
        # The conversation thread and the deep tier's worker can both be in
        # here, and two simultaneous refreshes would each be valid but the
        # second would be pointless. One lock over the token, held only for the
        # refresh itself -- the API calls below are not serialized.
        self._lock = threading.Lock()
        self._access_token = ""
        self._expires_at = 0.0

    # ---- Auth ----

    def access_token(self, *, force: bool = False) -> str:
        """A valid access token, refreshing only when the held one is spent."""
        with self._lock:
            if not force and self._access_token and time.monotonic() < self._expires_at:
                return self._access_token

            payload = self._post_form(
                TOKEN_URL,
                {
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": self._refresh_token,
                    "grant_type": "refresh_token",
                },
            )
            token = payload.get("access_token")
            if not token:
                raise GoogleAuthError("Google returned no access token for that refresh token.")

            self._access_token = str(token)
            expires_in = float(payload.get("expires_in", 3600))
            self._expires_at = time.monotonic() + max(expires_in - _EXPIRY_MARGIN_SECONDS, 0.0)
            logger.debug("Refreshed the Google access token, good for %ss", expires_in)
            return self._access_token

    def _post_form(self, url: str, data: dict[str, str]) -> dict:
        try:
            response = self._http.post(url, data=data)
        except httpx.HTTPError as exc:
            raise GoogleAuthError(f"Could not reach Google to refresh the token: {exc}") from exc

        payload = payload_of(response)
        if response.status_code >= 400:
            # invalid_grant is the one worth naming: it means the refresh token
            # was revoked, expired (a Testing-mode consent screen expires them
            # in seven days) or belongs to a different client.
            error = payload.get("error", response.status_code)
            detail = payload.get("error_description", response.text[:200])
            raise GoogleAuthError(f"Google refused the refresh token ({error}): {detail}")
        return payload

    # ---- Requests ----

    def request(self, method: str, url: str, **kwargs: Any) -> dict:
        """One authenticated call, retried once if the token went stale.

        A token can expire between the clock check above and the request
        landing -- a long GC pause is enough -- and it can be revoked outright.
        Both look like a 401, and both are answered the same way: get a new
        token and try once more. Only once, so a genuinely rejected credential
        fails rather than looping.
        """
        response = self._send(method, url, self.access_token(), **kwargs)
        if response.status_code == 401:
            logger.debug("Google rejected the access token; refreshing and retrying once")
            response = self._send(method, url, self.access_token(force=True), **kwargs)

        payload = payload_of(response)
        if response.status_code >= 400:
            raise _failure(response, payload)
        return payload

    def _send(self, method: str, url: str, token: str, **kwargs: Any) -> httpx.Response:
        try:
            return self._http.request(
                method, url, headers={"Authorization": f"Bearer {token}"}, **kwargs
            )
        except httpx.HTTPError as exc:
            raise GoogleError(f"Could not reach Google: {exc}") from exc

    def close(self) -> None:
        self._http.close()


def payload_of(response: httpx.Response) -> dict:
    """A response body as a dict, whatever came back.

    A 204 from delete has no body and an outage can return HTML; neither is a
    reason to raise a JSON error over an HTTP one the caller can explain.
    """
    if not response.content:
        return {}
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _failure(response: httpx.Response, payload: dict) -> GoogleError:
    error = payload.get("error")
    if not isinstance(error, dict):
        return GoogleError(f"Google returned HTTP {response.status_code}: {response.text[:200]}")

    message = error.get("message") or response.text[:200]
    if _INSUFFICIENT_SCOPE in str(error):
        # A valid token that was granted less than this build needs. Reported
        # as an auth failure with the actual remedy, because the API message
        # ("Request had insufficient authentication scopes") sounds like a
        # transient permissions problem and is not one.
        return GoogleAuthError(
            "This Google account was connected before MINUS asked for calendar access, "
            "so its token cannot reach the calendar. Run `minus google-auth` again to "
            "re-approve with both scopes."
        )
    return GoogleError(
        f"Google refused the request ({error.get('status', response.status_code)}): {message}"
    )
