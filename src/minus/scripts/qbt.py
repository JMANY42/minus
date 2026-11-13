"""Bring up qBittorrent and point it at quarantine, without a browser.

The Web UI's first-run flow is a web page: qBittorrent prints a temporary
password and expects someone to log in and choose a real one. That is a fine
experience for a desktop and a dead end for a harness, so this module does
three things a person would otherwise click through:

  * start `qbittorrent-nox` on a loopback WebUI with its own profile when
    nothing is listening, and wait for the port rather than the process;
  * replace the temporary password with one MINUS generated, storing it in
    `.env` so the next start can log in (the password is never printed);
  * write the settings the pipeline depends on -- save path, temp path,
    loopback binding, and the VPN interface -- through the API.

Everything here is a setup concern and is imported by the CLI, never by the
running assistant. Nothing in this module logs or returns a password.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import shlex
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

from minus import paths
from minus.config import Settings
from minus.services.env_file import update_env_file
from minus.services.qbittorrent import QBittorrentClient, QBittorrentError

logger = logging.getLogger(__name__)

# The first-run line qBittorrent writes to its log. The password is a token,
# so a non-space run is the whole shape it can have.
_TEMP_PASSWORD = re.compile(r"temporary password is provided for this session:\s*(\S+)")

START_TIMEOUT_SECONDS = 20.0
_PORT_POLL_SECONDS = 0.2

USERNAME_KEY = "MINUS_QBITTORRENT_USERNAME"
PASSWORD_KEY = "MINUS_QBITTORRENT_PASSWORD"


def base_url(settings: Settings) -> str:
    """The WebUI URL to use, configured or derived, always loopback-safe.

    A configured URL wins because it is the one the user already pointed
    MINUS at; the derived one can only ever be loopback, since a torrent
    client's WebUI on a public interface is remote code execution rather than
    convenience.
    """
    if settings.qbittorrent_url:
        return settings.qbittorrent_url.rstrip("/")
    return f"http://127.0.0.1:{settings.qbittorrent_webui_port}"


def _host_port(url: str) -> tuple[str, int]:
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return host, port


def port_is_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_port(
    host: str,
    port: int,
    *,
    timeout: float = START_TIMEOUT_SECONDS,
    poll: float = _PORT_POLL_SECONDS,
) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if port_is_open(host, port):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)


def temporary_password(log_path: Path) -> str:
    """The first-run password from the log, or "" when it is not there."""
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    matches = _TEMP_PASSWORD.findall(text)
    return matches[-1] if matches else ""


def _generate_password() -> str:
    # urlsafe base64: 24 bytes, 32 characters, no shell- or dotenv-hostile
    # characters, and far beyond what a loopback WebUI needs.
    return secrets.token_urlsafe(24)


def _profile_dir() -> Path:
    profile = paths.quarantine_dir() / "qbt-profile"
    profile.mkdir(parents=True, exist_ok=True)
    os.chmod(profile, 0o700)
    return profile


def _profile_log_path() -> Path:
    return paths.quarantine_dir() / "qbt-profile" / "qbittorrent.log"


def _start_process(settings: Settings, port: int, profile: Path) -> Path:
    """Start qBittorrent detached, returning the log file it writes."""
    log_path = profile / "qbittorrent.log"
    command = shlex.split(settings.qbittorrent_command) or [settings.qbittorrent_command]
    arguments = [
        *command,
        f"--webui-port={port}",
        f"--profile={profile}",
    ]
    with log_path.open("ab") as log:
        subprocess.Popen(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    logger.info("Started qBittorrent on port %d (log: %s)", port, log_path)
    return log_path


def _persist_credentials(username: str, password: str) -> None:
    env_path = paths.project_root() / ".env"
    update_env_file(
        env_path,
        {USERNAME_KEY: username, PASSWORD_KEY: password},
        allowed=frozenset({USERNAME_KEY, PASSWORD_KEY}),
    )


def _try_configured(settings: Settings, url: str) -> tuple[QBittorrentClient, str] | None:
    """Log in with `.env` credentials, or return None when they are refused.

    `None` also means "nothing configured": a missing password cannot be
    tried, and on a fresh profile it would not work even if it were set,
    because qBittorrent starts with `admin` and a temporary password until
    someone changes it.
    """
    if not settings.qbittorrent_password:
        return None
    username = settings.qbittorrent_username or "admin"
    client = QBittorrentClient(url, username, settings.qbittorrent_password)
    try:
        client.login()
    except QBittorrentError:
        client.close()
        return None
    return client, username


def _bootstrap(
    settings: Settings, url: str, log_path: Path
) -> tuple[QBittorrentClient, str] | None:
    """Replace qBittorrent's first-run password with the configured one.

    The temporary password in the log belongs to the *running* process, so it
    works both for a process just started here and for one an earlier run left
    running with the same profile. Returns None when the log has no temporary
    password to use.
    """
    bootstrap_password = temporary_password(log_path)
    if not bootstrap_password:
        return None

    username = settings.qbittorrent_username or "minus"
    configured = settings.qbittorrent_password
    generated = not configured
    password = configured or _generate_password()

    bootstrap = QBittorrentClient(url, "admin", bootstrap_password)
    try:
        bootstrap.login()
        bootstrap.set_preferences({"web_ui_username": username, "web_ui_password": password})
    except QBittorrentError as exc:
        raise QBittorrentError(
            "qBittorrent is running but neither the configured credentials nor "
            f"the temporary password in {log_path} were accepted. Set "
            "MINUS_QBITTORRENT_USERNAME and MINUS_QBITTORRENT_PASSWORD, or "
            "remove the profile directory and try again."
        ) from exc
    finally:
        bootstrap.close()

    # Only a password MINUS generated is written back: a configured one is
    # already in .env, and rewriting it would be an edit the user did not ask
    # for (and could mask a typo in their own file).
    if generated:
        _persist_credentials(username, password)

    client = QBittorrentClient(url, username, password)
    client.login()
    return client, username


def _spawn(settings: Settings, url: str) -> tuple[QBittorrentClient, str]:
    """Start qBittorrent, settle its credentials, and log in."""
    host, port = _host_port(url)
    profile = _profile_dir()
    log_path = _start_process(settings, port, profile)
    if not wait_for_port(host, port):
        raise QBittorrentError(
            f"qBittorrent did not open its WebUI on port {port} within "
            f"{START_TIMEOUT_SECONDS:.0f}s; see {log_path}"
        )

    configured = _try_configured(settings, url)
    if configured is not None:
        return configured

    bootstrapped = _bootstrap(settings, url, log_path)
    if bootstrapped is not None:
        return bootstrapped

    raise QBittorrentError(
        "qBittorrent started but MINUS could not establish a login: the "
        f"configured credentials were refused and no temporary password "
        f"appeared in {log_path}. Set MINUS_QBITTORRENT_PASSWORD or remove "
        "the profile directory and try again."
    )


def _ensure_running(settings: Settings) -> tuple[QBittorrentClient, bool, str]:
    url = base_url(settings)
    host, port = _host_port(url)
    if port_is_open(host, port):
        configured = _try_configured(settings, url)
        if configured is not None:
            client, username = configured
            return client, False, username

        # Already-running qBittorrent may still be on its first-run temporary
        # password: the profile log from that start is the only place to read
        # it. Never hand back an unauthenticated client and let the first real
        # call fail somewhere less obvious.
        bootstrapped = _bootstrap(settings, url, _profile_log_path())
        if bootstrapped is not None:
            client, username = bootstrapped
            return client, False, username

        raise QBittorrentError(
            f"qBittorrent is already running at {url}, but no credentials were "
            "accepted and no temporary password was found in the profile log. "
            "Set MINUS_QBITTORRENT_USERNAME and MINUS_QBITTORRENT_PASSWORD."
        )

    client, username = _spawn(settings, url)
    return client, True, username


def ensure_running(settings: Settings) -> QBittorrentClient:
    """A client, starting qBittorrent first when nothing answers."""
    client, _, _ = _ensure_running(settings)
    return client


def apply_preferences(client: QBittorrentClient, settings: Settings, interface: str) -> None:
    """Point qBittorrent at quarantine, loopback, and the VPN device.

    Refuses a named interface the kernel does not know: writing it would make
    qBittorrent fall back to "all interfaces" if the device is absent, which is
    exactly the leak the setting exists to prevent.
    """
    if interface:
        try:
            socket.if_nametoindex(interface)
        except OSError as exc:
            raise QBittorrentError(
                f"Network interface {interface!r} does not exist; refusing to "
                "bind qBittorrent to it."
            ) from exc

    incoming = Path(settings.qbittorrent_incoming_dir)
    temp = incoming / "tmp"
    for directory in (incoming, temp):
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)

    prefs: dict = {
        "save_path": str(incoming),
        "temp_path": str(temp),
        "temp_path_enabled": True,
        "web_ui_address": "127.0.0.1",
        "web_ui_port": settings.qbittorrent_webui_port,
        "web_ui_csrf_protection": True,
        "web_ui_clickjacking_protection": True,
        "web_ui_host_header_validation": True,
        "bypass_local_auth": False,
        "dht": True,
        "pex": True,
        "lsd": True,
    }
    if interface:
        prefs["current_network_interface"] = interface
    if settings.qbittorrent_listen_port > 0:
        prefs["listen_port"] = settings.qbittorrent_listen_port

    client.set_preferences(prefs)


def provision(settings: Settings) -> dict:
    """Ensure qBittorrent exists and is configured, without leaking secrets.

    The return value is safe to print or return from a tool: it names the URL,
    username, interface and incoming directory, never the password.
    """
    client, started, username = _ensure_running(settings)
    interface = settings.qbittorrent_vpn_interface or settings.vpn_interface

    try:
        apply_preferences(client, settings, interface)
    except QBittorrentError:
        client.close()
        raise

    return {
        "url": client.base_url,
        "username": username,
        "interface": interface,
        "incoming": str(settings.qbittorrent_incoming_dir),
        "started": started,
    }
