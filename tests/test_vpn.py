"""The VPN preflight, against the real kernel.

`lo` exists on every Linux box, so the link check is exercised for real; a name
that cannot exist exercises the refusal. No tunnels are created here.
"""

from __future__ import annotations

import pytest

from minus.config import Settings
from minus.services import vpn as vpn_service
from minus.services.qbittorrent import QBittorrentClient
from minus.services.vpn import (
    VpnError,
    discover_interface,
    link_is_up,
    require_tunnel,
    status,
    tunnel_status,
)
from minus.triage.pipeline import TriageError, TriagePipeline


def test_the_loopback_link_is_reported_up():
    assert link_is_up("lo") is True


def test_a_nonexistent_interface_is_down():
    snapshot = status("minus-no-such-interface")
    assert snapshot.link_up is False
    assert snapshot.healthy is False
    assert "does not exist" in snapshot.detail


def test_an_unset_interface_says_so():
    snapshot = status("")
    assert snapshot.healthy is False
    assert "no VPN interface configured" in snapshot.detail


def test_require_tunnel_refuses_an_interface_that_is_not_carrying_traffic():
    with pytest.raises(VpnError) as caught:
        require_tunnel("minus-no-such-interface")
    assert "VPN preflight failed" in str(caught.value)


def test_the_pipeline_refuses_to_fetch_without_the_tunnel(tmp_path):
    pipeline = TriagePipeline(
        quarantine=tmp_path / "quarantine",
        library=tmp_path / "library",
        client=QBittorrentClient("http://127.0.0.1:1"),
        vpn_interface="minus-no-such-interface",
        require_vpn=True,
    )

    with pytest.raises(TriageError) as caught:
        pipeline.download("a" * 40)

    assert "VPN preflight failed" in str(caught.value)
    assert not (tmp_path / "quarantine" / ("a" * 40)).exists()


def test_enforcement_with_no_interface_configured_fails_closed(tmp_path):
    pipeline = TriagePipeline(
        quarantine=tmp_path / "quarantine",
        library=tmp_path / "library",
        client=QBittorrentClient("http://127.0.0.1:1"),
        vpn_interface="",
        require_vpn=True,
    )

    with pytest.raises(TriageError) as caught:
        pipeline.download("a" * 40)

    assert "no VPN interface configured" in str(caught.value)


def test_discover_interface_accepts_tunnel_like_devices(monkeypatch):
    monkeypatch.setattr(vpn_service.shutil, "which", lambda name: "/usr/bin/ip")
    monkeypatch.setattr(
        vpn_service,
        "_run",
        lambda _argv: "1.1.1.1 dev tun0 src 10.8.0.2 uid 1000\n",
    )

    assert discover_interface() == "tun0"


@pytest.mark.parametrize("device", ["eth0", "wlan0", "enp3s0", "lo"])
def test_discover_interface_rejects_non_tunnel_devices(monkeypatch, device):
    monkeypatch.setattr(vpn_service.shutil, "which", lambda name: "/usr/bin/ip")
    monkeypatch.setattr(
        vpn_service, "_run", lambda _argv: f"1.1.1.1 dev {device} src 192.0.2.2\n"
    )

    assert discover_interface() == ""


def test_discover_interface_returns_empty_without_a_dev_clause(monkeypatch):
    monkeypatch.setattr(vpn_service.shutil, "which", lambda name: "/usr/bin/ip")
    monkeypatch.setattr(vpn_service, "_run", lambda _argv: "unreachable\n")

    assert discover_interface() == ""


def test_tunnel_status_uses_discovery_when_nothing_is_configured(monkeypatch):
    monkeypatch.setattr(vpn_service, "discover_interface", lambda: "tun0")
    monkeypatch.setattr(
        vpn_service,
        "status",
        lambda interface: vpn_service.TunnelStatus(interface, True, True, "ok"),
    )

    snapshot = tunnel_status(Settings(vpn_interface=""))

    assert snapshot.interface == "tun0"
    assert snapshot.healthy is True


def test_tunnel_status_explains_that_no_tunnel_was_found(monkeypatch):
    monkeypatch.setattr(vpn_service, "discover_interface", lambda: "")

    snapshot = tunnel_status(Settings(vpn_interface=""))

    assert snapshot.healthy is False
    assert "no VPN interface" in snapshot.detail
