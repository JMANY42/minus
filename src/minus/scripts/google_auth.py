"""One-time Google consent, from a terminal, ending in three lines of .env.

Everything else MINUS needs is a value you can copy from a web page. An OAuth
refresh token is not: it only exists at the end of a round trip through a
browser, and it is the one credential that does not expire, so it is worth
getting once and storing rather than re-deriving. This command performs that
round trip and hands back the three lines to keep.

The redirect goes to a loopback server on a port picked at bind time. That is
the flow Google documents for "Desktop app" clients and the reason the client
type matters: a loopback address with *any* port is accepted for one, and no
port has to be registered in advance. PKCE is included because Google now
requires it for installed apps -- and because the client secret in a desktop
app is not secret in the first place, so the verifier is what actually ties the
code to this process.

Nothing here is imported by the assistant. It runs once, from a tty.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import logging
import secrets
import sys
import urllib.parse
import webbrowser

import httpx

from minus.config import load_settings
from minus.paths import project_root
from minus.services.env_file import update_env_file
from minus.services.google import AUTH_URL, SCOPE_PARAMETER, TOKEN_URL

logger = logging.getLogger(__name__)

# How long the loopback server waits for the browser before giving up. Long
# enough to create the OAuth client in another tab if you arrived unprepared.
LISTEN_TIMEOUT_SECONDS = 300.0

ENV_KEYS = {
    "client_id": "MINUS_GOOGLE_CLIENT_ID",
    "client_secret": "MINUS_GOOGLE_CLIENT_SECRET",
    "refresh_token": "MINUS_GOOGLE_REFRESH_TOKEN",
}

SETUP_HELP = """
To get a client ID and secret:

  1. https://console.cloud.google.com/projectcreate -- make a project (any name).
  2. APIs & Services > Library > enable BOTH "Google Tasks API" and
     "Google Calendar API". Missing either one fails only when a tool for it
     is first called, which is a long way from here.
  3. APIs & Services > OAuth consent screen > External. Fill in the app name and
     your own email. Under Audience, add your Google account as a Test user.
  4. APIs & Services > Credentials > Create credentials > OAuth client ID >
     Application type: Desktop app.
  5. Copy the client ID and client secret it shows you.

If MINUS is already connected and you are here to add calendar access, steps 1
and 2 are the only ones with anything new in them: enable the Calendar API,
then run this command again with the client ID and secret you already have. It
will re-approve both scopes and replace the refresh token.

Leaving the consent screen in Testing mode is fine for one user, with one
catch: Google expires refresh tokens issued by a Testing app after seven days.
Publishing the app (same screen, "Publish app") stops that. It needs no review
while you are the only user, since none of the scopes here are sensitive to an
app that only touches its own owner's data.
""".strip()


def run_google_auth(client_id: str = "", client_secret: str = "", *, write: bool | None = None):
    """Walk the consent flow and offer to store the result."""
    settings = load_settings()
    client_id = client_id or settings.google_client_id
    client_secret = client_secret or settings.google_client_secret

    if not (client_id and client_secret):
        print(SETUP_HELP)
        print()
        client_id = client_id or _ask("Client ID: ")
        client_secret = client_secret or _ask("Client secret: ")
    if not (client_id and client_secret):
        print("Nothing to do without a client ID and secret.", file=sys.stderr)
        return 1

    verifier = _random_string()
    challenge = _s256(verifier)
    state = _random_string()

    server = _CodeReceiver(("127.0.0.1", 0))
    server.timeout = LISTEN_TIMEOUT_SECONDS
    redirect_uri = f"http://127.0.0.1:{server.server_address[1]}/"

    url = f"{AUTH_URL}?" + urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            # Both scopes at once. They cannot be added to an existing grant
            # afterwards -- a token issued for tasks alone stays a tasks token
            # -- so connecting an account that predates the calendar tools
            # means coming back through here.
            "scope": SCOPE_PARAMETER,
            # Both are required for a *refresh* token to come back. Without
            # access_type=offline there is only an hour-long access token;
            # without prompt=consent Google silently omits the refresh token on
            # every grant after the first, which makes re-running this command
            # to recover from a lost token appear to succeed and return
            # nothing usable.
            "access_type": "offline",
            "prompt": "consent",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
    )

    print("\nOpen this in a browser and approve the request:\n")
    print(f"  {url}\n")
    try:
        webbrowser.open(url)
    except Exception:
        # A headless box has no browser to open, and that is fine: the URL is
        # printed above and can be pasted anywhere with one.
        logger.debug("Could not open a browser; the printed URL is the fallback")
    print(f"Waiting for the redirect on {redirect_uri} ...")

    code = server.wait_for_code(state)
    server.server_close()
    if code is None:
        print("No authorization code arrived.", file=sys.stderr)
        return 1

    tokens = _exchange(code, client_id, client_secret, redirect_uri, verifier)
    refresh_token = tokens.get("refresh_token", "")
    if not refresh_token:
        print(
            "Google returned no refresh token. Revoke this app's access at "
            "https://myaccount.google.com/permissions and run this again.",
            file=sys.stderr,
        )
        return 1

    values = {
        ENV_KEYS["client_id"]: client_id,
        ENV_KEYS["client_secret"]: client_secret,
        ENV_KEYS["refresh_token"]: refresh_token,
    }
    print("\nApproved, for both tasks and calendar. These three lines belong in .env:\n")
    for key, value in values.items():
        print(f"  {key}={value}")

    if write is None:
        write = sys.stdin.isatty() and _ask("\nWrite them to .env now? [y/N] ").lower() == "y"
    if write:
        env_path = project_root() / ".env"
        # Only these three keys, and only the lines holding them: every other
        # line in the file, comments included, is left exactly as it was.
        update_env_file(env_path, values, allowed=frozenset(values))
        print(f"Written to {env_path}. Restart MINUS to pick them up.")
    else:
        print("\nNot written. Paste them in yourself, then restart MINUS.")
    return 0


def _exchange(
    code: str, client_id: str, client_secret: str, redirect_uri: str, verifier: str
) -> dict:
    response = httpx.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": verifier,
        },
        timeout=30.0,
    )
    payload = response.json() if response.content else {}
    if response.status_code >= 400:
        detail = payload.get("error_description") or payload.get("error") or response.text[:200]
        print(f"Google refused the code exchange: {detail}", file=sys.stderr)
        return {}
    return payload


class _CodeReceiver(http.server.HTTPServer):
    """A one-shot loopback server that keeps whatever the redirect carried."""

    def __init__(self, address) -> None:
        super().__init__(address, _CallbackHandler)
        self.query: dict[str, list[str]] = {}

    def wait_for_code(self, expected_state: str) -> str | None:
        """Serve requests until the real redirect lands, or the timeout does.

        A loop rather than one `handle_request()`: a browser will ask for
        /favicon.ico, and answering that and stopping would strand the flow
        one request short of the code.
        """
        while not self.query:
            self.handle_request()
            if not self.query:
                continue

            if self.query.get("state", [""])[0] != expected_state:
                # Someone else's redirect, or a replay of an old one. Not this
                # process's grant, so not this process's code.
                print("Ignoring a redirect whose state did not match.", file=sys.stderr)
                self.query = {}
                continue

            error = self.query.get("error", [""])[0]
            if error:
                print(f"Google reported: {error}", file=sys.stderr)
                return None
            return self.query.get("code", [""])[0] or None
        return None


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # BaseHTTPRequestHandler's spelling, not ours
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if "code" not in query and "error" not in query:
            # A favicon or a stray probe. Answered so the browser stops asking,
            # and not treated as the redirect.
            self.send_error(404)
            return

        self.server.query = query  # type: ignore[attr-defined]
        body = (
            b"<html><body><h2>MINUS is connected.</h2><p>You can close this tab.</p></body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        """Silence the default stderr access log; this command prints its own."""


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def _random_string() -> str:
    return secrets.token_urlsafe(48)


def _s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
