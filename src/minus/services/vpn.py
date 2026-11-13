"""VPN preflight and AirVPN connection management.

The preflight half is an assertion about *this host's* routing rather than a
"is the VPN connected" API: clients disagree about how they report state, and
the question that matters is whether the route to the internet leaves through
the expected interface. If it does not, a leak has already started.

The connection half drives AirVPN's own suite -- bluetit is the daemon and
goldcrest is the CLI that talks to it -- rather than writing an OpenVPN
configuration. AirVPN owns server selection, the network lock and the WireGuard
keys, so a second implementation here would be a copy that drifts. What this
module owns is the part AirVPN does not: deciding when a tunnel counts as
carrying traffic, and keeping the password out of every error string.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Any

from minus.services.qbittorrent import QBittorrentClient

logger = logging.getLogger(__name__)

PROBE_ADDRESS = "1.1.1.1"
_TIMEOUT = 5.0

# Interfaces a VPN client may create. `discover_interface` returns only one of
# these so a broken tunnel that leaves traffic on eth0 is reported as "no
# tunnel", not as a healthy one on the wrong device.
_TUNNEL_PREFIXES = ("tun", "wg", "airvpn", "ppp")

# bluetit's D-Bus errors when the caller is neither in the `airvpn` group nor a
# policy-cleared user. Only these justify the passwordless root retry; every
# other non-zero exit is a real failure and must be surfaced.
_POLICY_MARKERS = (
    "d-bus policy",
    "dbus policy",
    "name refused",
    "access denied",
    "accessdenied",
    "not allowed",
    "not permitted",
    "permission denied",
)

_OUTPUT_TAIL = 600


class VpnError(Exception):
    """The tunnel is not in the state the caller requires."""


@dataclass(frozen=True)
class TunnelStatus:
    interface: str
    link_up: bool
    carrying_traffic: bool
    detail: str

    @property
    def healthy(self) -> bool:
        return self.link_up and self.carrying_traffic


def _run(arguments: list[str]) -> str:
    completed = subprocess.run(
        arguments, capture_output=True, text=True, timeout=_TIMEOUT, check=False
    )
    return completed.stdout if completed.returncode == 0 else ""


def link_is_up(interface: str) -> bool:
    """Whether the interface exists and the kernel reports it UP."""
    if not interface:
        return False
    output = _run(["ip", "-o", "link", "show", interface])
    if not output:
        return False
    return "UP" in output.split("<", 1)[-1].split(">", 1)[0].split(",")


def route_uses(interface: str, address: str = PROBE_ADDRESS) -> bool:
    """Whether the route to `address` leaves through `interface`."""
    if not interface:
        return False
    output = _run(["ip", "route", "get", address])
    return f"dev {interface}" in output


def status(interface: str) -> TunnelStatus:
    """One snapshot, with a reason a person can act on."""
    if not interface:
        return TunnelStatus(interface, False, False, "no VPN interface configured")
    if shutil.which("ip") is None:
        return TunnelStatus(interface, False, False, "`ip` is not installed")
    up = link_is_up(interface)
    carried = up and route_uses(interface)
    if not up:
        detail = f"{interface} is down or does not exist"
    elif not carried:
        detail = f"traffic to {PROBE_ADDRESS} does not leave through {interface}"
    else:
        detail = f"traffic leaves through {interface}"
    return TunnelStatus(interface, up, carried, detail)


def require_torrent_tunnel(client: QBittorrentClient, expected_interface: str) -> None:
    """Raise unless qBittorrent itself is bound to the expected interface.

    Torrent traffic leaves qBittorrent's process, not MINUS's, so checking
    this host's route cannot prove the download is tunneled. The one assertion
    available from outside is qBittorrent's own Network Interface setting, and
    only an exact match passes: the "all interfaces" empty value is a leak.
    """
    if not expected_interface:
        raise VpnError(
            "no qBittorrent VPN interface configured (set MINUS_QBITTORRENT_VPN_INTERFACE)"
        )

    preferences = client.preferences()
    observed = preferences.get("current_network_interface") or ""
    if observed == expected_interface:
        return

    raise VpnError(
        f"qBittorrent is bound to {observed or '(all interfaces)'}, not "
        f"{expected_interface!r}. Set qBittorrent's Network Interface to the VPN "
        "device, or ensure the download user's traffic is policy-routed through it."
    )


def require_tunnel(interface: str) -> TunnelStatus:
    """Raise unless the tunnel exists, is up, and carries the default route."""
    snapshot = status(interface)
    if not snapshot.healthy:
        raise VpnError(
            f"VPN preflight failed: {snapshot.detail}. Bring the tunnel up, then retry; "
            "MINUS will not fetch torrents over a route it cannot vouch for."
        )
    return snapshot


# ---- AirVPN connection management ------------------------------------------


def discover_interface() -> str:
    """Return the tunnel-like device the default route actually leaves through.

    `ip route get` reports the interface the kernel chose for a probe address.
    Only tunnel-shaped names are accepted: a machine with no VPN up usually
    still has a default route, and returning `eth0` would turn "no tunnel" into
    a false positive that the preflight then has to catch anyway.
    """
    if shutil.which("ip") is None:
        return ""
    output = _run(["ip", "route", "get", PROBE_ADDRESS])
    match = re.search(r"\bdev\s+(\S+)", output)
    if match is None:
        return ""
    interface = match.group(1)
    return interface if interface.startswith(_TUNNEL_PREFIXES) else ""


def tunnel_status(settings: Any) -> TunnelStatus:
    """A preflight snapshot for the configured or discovered tunnel."""
    interface = settings.vpn_interface or discover_interface()
    if not interface:
        return TunnelStatus(
            "",
            False,
            False,
            "no VPN interface configured and no tunnel-like default route discovered",
        )
    return status(interface)


def _resolve_program(name: str) -> str:
    """Find a bare command, including hand-installed goldcrest."""
    resolved = shutil.which(name)
    if resolved is None and name == "goldcrest":
        candidate = "/usr/local/bin/goldcrest"
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            resolved = candidate
    return resolved or name


def _resolve_command(settings: Any) -> list[str]:
    """Turn `vpn_client_command` into an argv prefix.

    `shutil.which` only searches PATH, and goldcrest installed by hand often
    lands in /usr/local/bin when the login session's PATH has not caught up.
    Checking that one path explicitly is the difference between "VPN refused to
    connect" and "command not found" on a fresh Arch install.
    """
    raw = (settings.vpn_client_command or "goldcrest").strip()
    parts = shlex.split(raw) or ["goldcrest"]
    return [_resolve_program(parts[0]), *parts[1:]]


def _tail(text: str, limit: int = _OUTPUT_TAIL) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else "..." + text[-limit:]


def _redact(text: str, settings: Any) -> str:
    """Never let a password reach a log line, an exception or the model."""
    password = getattr(settings, "airvpn_password", "") or ""
    return text.replace(password, "***") if password else text


def _output_of(completed: subprocess.CompletedProcess[str]) -> str:
    return (completed.stdout or "") + (completed.stderr or "")


def _policy_refusal(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _POLICY_MARKERS)


def _run_client(
    argv: list[str], timeout: float, settings: Any | None = None
) -> subprocess.CompletedProcess[str]:
    """Run one goldcrest/bluetit command, with the one permitted retry.

    On Arch a freshly added `airvpn` group is not in the current login session,
    so a direct call can fail with a D-Bus policy error even though the user is
    entitled to it. The retry is `sudo -n <client>`: root is allowed by
    bluetit's D-Bus policy, and the shipped sudoers drop-in permits exactly the
    resolved client path. `-n` guarantees sudo never prompts, because a harness
    that blocks on a password prompt has already failed the user. Everything
    else is returned unchanged so the caller decides what a non-zero exit
    means.
    """
    if argv and os.sep not in argv[0]:
        argv = [_resolve_program(argv[0]), *argv[1:]]
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise VpnError(
            f"{os.path.basename(argv[0])} did not finish within {int(timeout)}s"
        ) from exc
    except OSError as exc:
        raise VpnError(f"cannot run {argv[0]}: {exc}") from exc

    allowed = True if settings is None else bool(getattr(settings, "vpn_use_sudo_fallback", True))
    if not allowed or completed.returncode == 0:
        return completed
    if not _policy_refusal(_output_of(completed)):
        return completed
    if shutil.which("sudo") is None:
        return completed
    try:
        return subprocess.run(
            ["sudo", "-n", *argv],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return completed


def _connect_argv(settings: Any) -> list[str]:
    argv = [*_resolve_command(settings), "--air-connect"]
    argv += [
        "--air-user",
        settings.airvpn_username,
        "--air-password",
        settings.airvpn_password,
    ]
    # A server name is more specific than a country; when both are set the
    # server wins rather than sending two contradictory selectors.
    if settings.airvpn_server:
        argv += ["--air-server", settings.airvpn_server]
    elif settings.airvpn_country:
        argv += ["--air-country", settings.airvpn_country]
    argv += [
        "--air-vpn-type",
        settings.airvpn_vpn_type or "wireguard",
        "--network-lock",
        "on" if settings.airvpn_network_lock else "off",
        "--timeout",
        str(int(settings.vpn_connect_timeout)),
    ]
    return argv


def connect(settings: Any) -> TunnelStatus:
    """Bring an AirVPN tunnel up and refuse to report success until it carries traffic."""
    if not settings.airvpn_username or not settings.airvpn_password:
        raise VpnError(
            "AirVPN credentials are empty; set MINUS_AIRVPN_USERNAME and "
            "MINUS_AIRVPN_PASSWORD before connecting."
        )

    argv = _connect_argv(settings)
    timeout = float(settings.vpn_connect_timeout) + 30.0
    completed = _run_client(argv, timeout, settings)
    output = _output_of(completed)
    # goldcrest can exit 0 after "AirVPN login failed", so the exit code alone
    # would leave the caller polling for two minutes for a tunnel that was
    # never going to exist. The credential check is the useful failure.
    login_error = re.search(
        r"login failed|authentication failed|invalid user|incorrect password",
        output,
        re.I,
    )
    if login_error:
        detail = _redact(_tail(output), settings)
        raise VpnError(f"AirVPN login failed: {detail or 'the client rejected the credentials'}")
    if completed.returncode != 0:
        detail = _redact(_tail(output), settings)
        raise VpnError(
            f"AirVPN connect failed (exit {completed.returncode}): {detail or 'no output'}"
        )

    deadline = time.monotonic() + max(0.0, float(settings.vpn_connect_timeout))
    last = tunnel_status(settings)
    while True:
        if last.healthy:
            return last
        if time.monotonic() >= deadline:
            raise VpnError(
                f"AirVPN reported connected but the tunnel never came up within "
                f"{int(settings.vpn_connect_timeout)}s: {last.detail}"
            )
        time.sleep(1)
        last = tunnel_status(settings)


def bind_downloads(settings: Any, snapshot: TunnelStatus) -> dict:
    """Make a just-connected tunnel the path qBittorrent must use.

    The preflight refuses a fetch when qBittorrent is bound to anything other
    than the named device, so a successful connect has to carry that name the
    rest of the way: persist it for the next start, then apply it to the
    running client. A binding failure is reported rather than raised -- the
    tunnel is up, and the preflight will still refuse the fetch until the
    client agrees.
    """
    changes: dict[str, str] = {}
    if not settings.vpn_interface:
        settings.vpn_interface = snapshot.interface
        changes["MINUS_VPN_INTERFACE"] = snapshot.interface
    if not settings.qbittorrent_vpn_interface:
        settings.qbittorrent_vpn_interface = snapshot.interface
        changes["MINUS_QBITTORRENT_VPN_INTERFACE"] = snapshot.interface

    if changes:
        from minus.paths import project_root
        from minus.services.env_file import update_env_file

        try:
            update_env_file(project_root() / ".env", changes, allowed=frozenset(changes))
        except OSError as exc:
            logger.warning("Could not persist VPN interface to .env: %s", exc)

    if not getattr(settings, "qbittorrent_url", ""):
        return {
            "bound": False,
            "interface": snapshot.interface,
            "reason": "qBittorrent is not configured",
        }

    try:
        from minus.scripts.qbt import apply_preferences, ensure_running

        client = ensure_running(settings)
        try:
            apply_preferences(client, settings, snapshot.interface)
        finally:
            client.close()
    except Exception as exc:
        logger.warning("Could not bind qBittorrent to %s: %s", snapshot.interface, exc)
        return {"bound": False, "interface": snapshot.interface, "error": str(exc)}

    return {"bound": True, "interface": snapshot.interface}


def disconnect(settings: Any) -> dict:
    """Ask AirVPN to tear the tunnel down; report both the exit and the output."""
    argv = [*_resolve_command(settings), "--disconnect"]
    completed = _run_client(argv, float(settings.vpn_connect_timeout) + 30.0, settings)
    return {
        "disconnected": completed.returncode == 0,
        "output": _redact(_tail(_output_of(completed)), settings),
    }


def client_is_connected(settings: Any) -> bool:
    """Whether bluetit's status says a tunnel is currently established.

    This answers a different question from `tunnel_status`: a client can claim
    a live connection while the host route still leaks. Callers that gate
    downloads use the routing snapshot; this one is for display and for not
    spawning a second connect.
    """
    argv = [*_resolve_command(settings), "--bluetit-status"]
    try:
        completed = _run_client(argv, _TIMEOUT, settings)
    except VpnError:
        return False
    if completed.returncode != 0:
        return False
    output = _output_of(completed).lower()
    if "not connected" in output:
        return False
    return "connected" in output
