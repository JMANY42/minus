"""qBittorrent's own network binding, and the pipeline gate that reads it.

A real HTTP server stands in for the Web UI: the point of these cases is that
`preferences()` parses an actual response and that the pipeline refuses before
it asks qBittorrent about any torrent.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import pytest

from minus.services.qbittorrent import QBittorrentClient
from minus.services.vpn import VpnError, require_torrent_tunnel
from minus.triage.pipeline import TriageError, TriagePipeline

HASH = "a" * 40


class FakeQbittorrent:
    """A Web UI with one configurable preference and a request log."""

    def __init__(self, interface: str = "") -> None:
        self.interface = interface
        self.paths: list[str] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> FakeQbittorrent:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def do_POST(self) -> None:
                owner.paths.append(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                if self.path == "/api/v2/auth/login":
                    self._send(200, b"Ok.")
                else:
                    self._send(404, b"")

            def do_GET(self) -> None:
                owner.paths.append(self.path)
                if self.path == "/api/v2/app/preferences":
                    body = json.dumps({"current_network_interface": owner.interface}).encode()
                    self._send(200, body)
                else:
                    self._send(404, b"")

            def _send(self, status: int, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        assert self._server is not None and self._thread is not None
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"


def test_matching_interface_passes():
    with (
        FakeQbittorrent("wg0") as fake,
        mock.patch("minus.services.vpn.link_is_up", return_value=True),
    ):
        client = QBittorrentClient(fake.url)
        try:
            require_torrent_tunnel(client, "wg0")
        finally:
            client.close()

    assert "/api/v2/app/preferences" in fake.paths


def test_a_matching_binding_to_a_missing_device_is_refused():
    """Bound to wg0, but wg0 is gone: intent without a route is not a tunnel."""
    with (
        FakeQbittorrent("wg0") as fake,
        mock.patch("minus.services.vpn.link_is_up", return_value=False),
    ):
        client = QBittorrentClient(fake.url)
        try:
            with pytest.raises(VpnError) as caught:
                require_torrent_tunnel(client, "wg0")
        finally:
            client.close()

    message = str(caught.value)
    assert "wg0" in message
    assert "down or does not exist" in message


def test_a_mismatched_interface_names_both_sides():
    with FakeQbittorrent("eth0") as fake:
        client = QBittorrentClient(fake.url)
        try:
            with pytest.raises(VpnError) as caught:
                require_torrent_tunnel(client, "wg0")
        finally:
            client.close()

    message = str(caught.value)
    assert "eth0" in message
    assert "wg0" in message


def test_an_empty_preference_is_a_leak_and_is_refused():
    with FakeQbittorrent("") as fake:
        client = QBittorrentClient(fake.url)
        try:
            with pytest.raises(VpnError) as caught:
                require_torrent_tunnel(client, "wg0")
        finally:
            client.close()

    message = str(caught.value)
    assert "(all interfaces)" in message
    assert "wg0" in message


def test_an_unset_expected_interface_is_refused_before_asking():
    # Nothing listens here: the point is that the empty setting fails closed
    # without reaching qBittorrent at all.
    client = QBittorrentClient("http://127.0.0.1:1")
    try:
        with pytest.raises(VpnError) as caught:
            require_torrent_tunnel(client, "")
    finally:
        client.close()

    assert "MINUS_QBITTORRENT_VPN_INTERFACE" in str(caught.value)


def test_the_pipeline_refuses_a_mismatched_binding_before_torrent_info(tmp_path):
    with FakeQbittorrent("eth0") as fake:
        client = QBittorrentClient(fake.url)
        pipeline = TriagePipeline(
            quarantine=tmp_path / "quarantine",
            library=tmp_path / "library",
            client=client,
            qbittorrent_vpn_interface="wg0",
            require_vpn=True,
        )
        try:
            with pytest.raises(TriageError) as caught:
                pipeline.download(HASH)
        finally:
            client.close()

    assert "eth0" in str(caught.value) and "wg0" in str(caught.value)
    assert "/api/v2/app/preferences" in fake.paths
    assert not any("/api/v2/torrents/info" in path for path in fake.paths)


def test_the_pipeline_refuses_when_no_interface_is_configured(tmp_path):
    pipeline = TriagePipeline(
        quarantine=tmp_path / "quarantine",
        library=tmp_path / "library",
        client=QBittorrentClient("http://127.0.0.1:1"),
        qbittorrent_vpn_interface="",
        vpn_interface="",
        require_vpn=True,
    )

    with pytest.raises(TriageError) as caught:
        pipeline.download(HASH)

    message = str(caught.value)
    assert "MINUS_QBITTORRENT_VPN_INTERFACE" in message
    assert "MINUS_VPN_INTERFACE" in message
    assert not (tmp_path / "quarantine" / HASH).exists()
