"""Model-facing tools for the AirVPN tunnel.

Registered under the `vpn` category. The tools are deliberately thin: the
decision about whether a tunnel is safe is `services/vpn.py`'s routing
preflight, and `vpn_connect` checks it first so asking twice cannot spawn a
second goldcrest process against an already-healthy tunnel.
"""

from __future__ import annotations

import logging
from typing import Any

from minus.errors import ToolExecutionError
from minus.services import vpn as vpn_service
from minus.services.vpn import VpnError

logger = logging.getLogger(__name__)

CATEGORY = "vpn"


class VpnTools:
    """Status, connect and disconnect for the AirVPN tunnel."""

    def __init__(self, settings: Any) -> None:
        self.settings = settings

    def register(self, registry: Any) -> None:
        for tool in (self.vpn_status, self.vpn_connect, self.vpn_disconnect):
            registry.tool(tool, category=CATEGORY)

    def vpn_status(self) -> dict:
        """Report whether a VPN tunnel exists and whether traffic uses it.

        Read-only. `up` means the interface exists, `carrying` means the route
        to the internet leaves through it, and `detail` explains a failure.
        """
        try:
            snapshot = vpn_service.tunnel_status(self.settings)
        except VpnError as exc:
            raise ToolExecutionError(str(exc)) from exc
        return {
            "interface": snapshot.interface,
            "up": snapshot.link_up,
            "carrying": snapshot.carrying_traffic,
            "detail": snapshot.detail,
            "provider": self.settings.vpn_provider,
        }

    def vpn_connect(self) -> str:
        """Connect AirVPN and wait until the tunnel carries traffic.

        Does nothing when a healthy tunnel is already up. Returns a short
        sentence suitable for speaking.
        """
        try:
            current = vpn_service.tunnel_status(self.settings)
            if current.healthy:
                return f"VPN already up on {current.interface}."
            snapshot = vpn_service.connect(self.settings)
        except VpnError as exc:
            raise ToolExecutionError(str(exc)) from exc
        return f"VPN connected on {snapshot.interface}; traffic leaves through it."

    def vpn_disconnect(self) -> dict:
        """Disconnect AirVPN and report the client's exit and output."""
        try:
            return vpn_service.disconnect(self.settings)
        except VpnError as exc:
            raise ToolExecutionError(str(exc)) from exc
