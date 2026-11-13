"""Which listening sockets are reachable beyond loopback.

`ss` is the kernel's own answer, so nothing is probed from outside and no
socket is opened. The only question here is whether a listener is bound
somewhere other than loopback, because that is the set a bind address or a
firewall has to deal with.

The parser tolerates output with or without a header: `ss -H` omits it and
versions differ, and a header row simply fails the LISTEN check.
"""

from __future__ import annotations

import subprocess

_SS = ("ss", "-ltn")
_TIMEOUT_SECONDS = 5


def _is_loopback(address: str) -> bool:
    # 127.0.0.0/8 is the whole IPv4 loopback range, not just 127.0.0.1.
    return address == "::1" or address.startswith("127.")


def _split_address(field: str) -> tuple[str, int] | None:
    """Split `address:port`, including bracketed IPv6, at the last colon."""
    address, separator, port_text = field.rpartition(":")
    if not separator or not port_text.isdigit():
        return None
    if address.startswith("[") and address.endswith("]"):
        address = address[1:-1]
    return address, int(port_text)


def parse_ss(output: str) -> list[dict]:
    """Every LISTEN row in `ss -ltn` as address, port and public flag."""
    found: list[dict] = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 4 or fields[0] != "LISTEN":
            continue
        parsed = _split_address(fields[3])
        if parsed is None:
            continue
        address, port = parsed
        found.append(
            {"address": address, "port": port, "public": not _is_loopback(address)}
        )
    return sorted(found, key=lambda entry: (entry["port"], entry["address"]))


def listeners() -> list[dict]:
    """The current listening sockets, or [] when `ss` cannot answer."""
    try:
        completed = subprocess.run(
            _SS,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if completed.returncode != 0:
        return []
    return parse_ss(completed.stdout)


def public_listeners() -> list[dict]:
    """Only the listeners that something off this machine could reach."""
    return [entry for entry in listeners() if entry["public"]]
