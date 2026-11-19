"""Read-only security posture for a MINUS checkout.

`minus doctor` answers one question -- what has this machine actually been set
up to do, and where is that configuration unsafe? -- without changing anything:
no writes, no network calls, no subprocess that modifies state, and never a
secret value. A key is reported by _where it came from_ ("environment",
"dotenv", "missing"), because the useful fact is whether it is present at all,
not what it is; printing it would leak it into shells, logs and screenshots.

The dangerous list is deliberately fail-closed prose rather than a score. Each
entry names a concrete condition and its consequence, so the reader can decide,
and so the wording in a bug report is the same as the wording on screen.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
from pathlib import Path
from urllib.parse import urlparse

from minus import paths
from minus.config import Settings
from minus.services import exposure
from minus.services.vpn import tunnel_status
from minus.triage.windows_vm import runner_from_settings

# The two names a provider key can arrive under. The first is a direct
# provider, the second OpenRouter; either one satisfies the provider tier.
_KEY_ENV_VARS = ("MINUS_LLM_API_KEY", "OPENROUTER_API_KEY")

# DryRunRunner has no `unavailable_reason`, and inventing one would imply it
# might become available. It is always available and always executes nothing.
_DRY_RUN_REASON = "dry run: nothing is executed"

_TRUSTED_SOCKET_DIR_MODE = "0700"


def _mode(path: Path) -> str | None:
    """The permission bits as an octal string, or None when they cannot be read."""
    try:
        return f"{stat.S_IMODE(path.stat().st_mode):04o}"
    except OSError:
        return None


def _nearest_existing(path: Path) -> Path | None:
    """`path` or the closest ancestor that exists, for sizes on a missing dir."""
    candidate = path
    while not candidate.exists():
        if candidate.parent == candidate:
            return None
        candidate = candidate.parent
    return candidate


def _free_bytes(path: Path) -> int | None:
    holder = path if path.exists() else _nearest_existing(path)
    if holder is None:
        return None
    try:
        return shutil.disk_usage(holder).free
    except OSError:
        return None


def _without_userinfo(url: str) -> str:
    """Drop `user:password@` from a URL, keeping everything else.

    A base URL is safe to show and useful to show -- it is what the assistant
    will actually call -- but a URL may carry credentials, and those are not.
    Only the netloc's userinfo is removed so the path and scheme still identify
    the endpoint.
    """
    parsed = urlparse(url)
    if "@" not in parsed.netloc:
        return url
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return parsed._replace(netloc=host).geturl()


def _host_and_port(url: str) -> str:
    """`host:port` from a URL, and nothing else it may contain."""
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return ""
    if not parsed.hostname:
        return ""
    return f"{parsed.hostname}:{port}" if port else parsed.hostname


def _key_source(settings: Settings) -> str:
    if any(os.environ.get(name) for name in _KEY_ENV_VARS):
        return "environment"
    if settings.llm_api_key or settings.openrouter_api_key:
        return "dotenv"
    return "missing"


def collect_posture(settings: Settings, *, socket_path: Path | None = None) -> dict:
    """Snapshot the security-relevant configuration. Reads only."""
    provider_url = settings.llm_base_url or settings.openrouter_base_url
    api_key = _key_source(settings)

    qbittorrent_interface = getattr(settings, "qbittorrent_vpn_interface", "")
    tunnel = tunnel_status(settings)

    sandbox_configured = bool(settings.windows_base_image)
    if sandbox_configured:
        runner = runner_from_settings(settings)
        sandbox_reason = runner.unavailable_reason()
        sandbox_available = runner.available()
    else:
        # The dry run is available in the sense that the pipeline can call it;
        # what it cannot do is observe anything.
        sandbox_available = True
        sandbox_reason = _DRY_RUN_REASON

    triage_enabled = bool(settings.qbittorrent_url)

    embeddings_available = importlib.util.find_spec("sentence_transformers") is not None
    memory_db = paths.semantic_memory_db()

    socket = socket_path or paths.control_socket()
    socket_parent = socket.parent
    socket_parent_mode = _mode(socket_parent)

    quarantine = _storage(settings.triage_quarantine_dir)
    library = _storage(settings.triage_library_dir)

    # `public_listeners` is the query the report acts on; the total is only
    # context for how many sockets were inspected, so it is counted here.
    public_sockets = exposure.public_listeners()

    posture: dict = {
        "provider": {
            "base_url": _without_userinfo(provider_url),
            "api_key": api_key,
            "chat_model": settings.chat_model,
            "deep_model": settings.deep_model,
            "fact_extraction_model": settings.fact_extraction_model,
        },
        "vpn": {
            "provider": settings.vpn_provider,
            "airvpn_configured": bool(settings.airvpn_username and settings.airvpn_password),
            "autoconnect": settings.vpn_autoconnect,
            "interface": settings.vpn_interface,
            "qbittorrent_interface": qbittorrent_interface,
            "require_for_triage": settings.require_vpn_for_triage,
            "host_route": {
                "interface": tunnel.interface,
                "link_up": tunnel.link_up,
                "carrying_traffic": tunnel.carrying_traffic,
                "detail": tunnel.detail,
            },
        },
        "sandbox": {
            "configured": sandbox_configured,
            "available": sandbox_available,
            "reason": sandbox_reason,
        },
        "triage": {
            "enabled": triage_enabled,
            "host": _host_and_port(settings.qbittorrent_url),
            "require_detonation": settings.triage_require_detonation,
            "model_review": settings.triage_model_review,
            "delete_malicious": settings.triage_delete_malicious,
            "media_review": settings.triage_media_review,
            "media_frames": settings.triage_media_frames,
        },
        "plugins": {
            "enabled": settings.plugins_enabled,
            "lua_installed": _lua_available(),
        },
        "memory": {
            "embeddings": embeddings_available,
            "db": str(memory_db),
            "db_exists": memory_db.exists(),
        },
        "control_socket": {
            "path": str(socket),
            "exists": socket.exists(),
            "mode": _mode(socket),
            "parent_mode": socket_parent_mode,
        },
        "storage": {"quarantine": quarantine, "library": library},
        "listeners": {
            "public": public_sockets,
            "total": len(exposure.listeners()),
        },
        "dangerous": [],
    }

    posture["dangerous"] = _dangerous(
        settings,
        posture,
        qbittorrent_interface=qbittorrent_interface,
        triage_enabled=triage_enabled,
        sandbox_available=sandbox_available,
        sandbox_reason=sandbox_reason,
        embeddings_available=embeddings_available,
        socket_parent=socket_parent,
        socket_parent_mode=socket_parent_mode,
        api_key=api_key,
    )
    return posture


def _lua_available() -> bool:
    # Imported here rather than at module scope: importing minus.plugins pulls
    # the tool registry, which is seconds of work for a probe that is usually
    # False (lupa is an optional extra).
    from minus.plugins import lua_available

    return lua_available()


def _storage(path: Path) -> dict:
    exists = path.exists()
    return {
        "path": str(path),
        "exists": exists,
        "mode": _mode(path) if exists else None,
        "free_bytes": _free_bytes(path),
    }


def _dangerous(
    settings: Settings,
    posture: dict,
    *,
    qbittorrent_interface: str,
    triage_enabled: bool,
    sandbox_available: bool,
    sandbox_reason: str,
    embeddings_available: bool,
    socket_parent: Path,
    socket_parent_mode: str | None,
    api_key: str,
) -> list[str]:
    """Every condition worth acting on, as a complete sentence each."""
    findings: list[str] = []

    if not settings.require_vpn_for_triage:
        findings.append(
            "VPN enforcement is off: torrent fetches are not gated on the tunnel, so a "
            "leak would go unnoticed."
        )
    elif not settings.vpn_interface and not qbittorrent_interface:
        findings.append(
            "VPN enforcement is on but neither interface is configured, so every triage "
            "preflight fails closed and nothing can be fetched."
        )

    if triage_enabled and not qbittorrent_interface:
        findings.append(
            "qBittorrent is configured without a pinned VPN interface, so its own traffic "
            "can leave outside the tunnel even when the host route is correct."
        )

    if triage_enabled and not sandbox_available:
        findings.append(
            f"Triage is enabled but the sandbox is unavailable ({sandbox_reason}), so "
            "downloaded content cannot be detonated."
        )

    if triage_enabled and not settings.triage_model_review:
        findings.append(
            "Triage is enabled but the model review is off: code-shaped files are judged "
            "only by static rules and a detonation, with no agent reading them first."
        )

    if settings.vpn_autoconnect and not (settings.airvpn_username and settings.airvpn_password):
        findings.append(
            "VPN autoconnect is on but the AirVPN username or password is empty, so a "
            "tunnel cannot be brought up before a fetch."
        )

    if settings.plugins_enabled:
        findings.append(
            "Lua plugins are enabled: plugin files run as in-process Lua code with no "
            "instruction or memory sandbox and the host's full privileges."
        )

    if not embeddings_available:
        findings.append(
            "Semantic memory is unavailable: sentence-transformers is not installed, so "
            "facts cannot be embedded, stored or searched."
        )

    if socket_parent_mode is not None and socket_parent_mode != _TRUSTED_SOCKET_DIR_MODE:
        findings.append(
            f"The control socket directory {socket_parent} is mode {socket_parent_mode}, "
            f"not {_TRUSTED_SOCKET_DIR_MODE}: another local user may be able to send "
            "MINUS commands."
        )

    for label in ("quarantine", "library"):
        entry = posture["storage"][label]
        mode = entry["mode"]
        if entry["exists"] and mode is not None and int(mode, 8) & 0o077:
            findings.append(
                f"The {label} directory {entry['path']} is mode {mode} with group or other "
                "permission bits, so staged content is readable or writable by other users."
            )

    if api_key == "missing":
        findings.append(
            "No provider API key is set: every model call will fail with an authentication error."
        )

    public_sockets = posture["listeners"]["public"]
    if public_sockets:
        # Deduplicate because a dual-stack listener can appear twice (v4 and v6)
        # on the same port, and the reader needs the port once.
        ports = sorted({entry["port"] for entry in public_sockets})
        joined = ", ".join(str(port) for port in ports)
        findings.append(
            f"{len(public_sockets)} public listening socket(s) reachable beyond loopback: "
            f"ports {joined} — bind them to 127.0.0.1 or firewall them."
        )

    return findings


def _flatten(value: dict, prefix: str = ""):
    for key, item in value.items():
        name = f"{prefix}.{key}" if prefix else key
        if isinstance(item, dict):
            yield from _flatten(item, name)
        else:
            yield name, item


def _render(value: object, name: str) -> str:
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if name.endswith("free_bytes") and isinstance(value, int):
        # Bytes are unreadable; the JSON form keeps the exact number for anything
        # that needs it.
        return f"{value / 1024**3:.1f} GiB"
    return str(value)


def format_posture(posture: dict) -> str:
    """Aligned, grouped lines for a terminal."""
    sections: list[tuple[str, list[tuple[str, str]]]] = []
    for key, value in posture.items():
        if key in ("dangerous", "listeners"):
            continue
        if isinstance(value, dict):
            rows = [(name, _render(item, name)) for name, item in _flatten(value)]
        else:
            rows = [(key, _render(value, key))]
        sections.append((key, rows))

    width = max(
        (len(name) for _, rows in sections for name, _ in rows),
        default=0,
    )

    lines: list[str] = []
    for key, rows in sections:
        if lines:
            lines.append("")
        lines.append(f"{key}:")
        for name, text in rows:
            lines.append(f"  {name:<{width}}  {text}")

    listeners = posture.get("listeners") or {}
    public_sockets = listeners.get("public") or []
    lines.append("")
    lines.append("listeners:")
    if public_sockets:
        for entry in public_sockets:
            lines.append(f"  public  {entry['address']}:{entry['port']}")
    else:
        lines.append("  public  none public")
    lines.append(f"  total   {listeners.get('total', 0)}")

    lines.append("")
    lines.append("dangerous:")
    findings = posture.get("dangerous") or []
    if findings:
        lines.extend(f"  - {sentence}" for sentence in findings)
    else:
        lines.append("  none")
    return "\n".join(lines)


def posture_json(posture: dict) -> str:
    return json.dumps(posture, indent=2, sort_keys=True)
