"""Adding torrents: the client's one write call, and the tool in front of it.

The API answers "Ok." or "Fails." with HTTP 200 either way, so the fake below
serves both and records the raw form body -- the shape of that body (urlencoded,
newline-joined URLs) is part of the contract with qBittorrent.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest

from minus.errors import ToolExecutionError
from minus.services.qbittorrent import QBittorrentClient, QBittorrentError
from minus.tools.triage import TriageTools
from minus.triage.pipeline import TriagePipeline

ADD_PATH = "/api/v2/torrents/add"
LOGIN_PATH = "/api/v2/auth/login"
MAGNET = "magnet:?xt=urn:btih:" + "a" * 40
OTHER_MAGNET = "magnet:?xt=urn:btih:" + "b" * 40


class FakeQbittorrent:
    """A Web UI that logs in, accepts every add, and records what it was sent."""

    def __init__(self, add_response: str = "Ok.") -> None:
        self.add_response = add_response
        self.paths: list[str] = []
        self.bodies: list[str] = []
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
                raw = self.rfile.read(length)
                if self.path == LOGIN_PATH:
                    self._send("Ok.")
                elif self.path == ADD_PATH:
                    owner.bodies.append(raw.decode("utf-8"))
                    self._send(owner.add_response)
                else:
                    self._send("", status=404)

            def _send(self, body: str, status: int = 200) -> None:
                payload = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._server = server
        self._thread = thread
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


def test_add_posts_urlencoded_newline_joined_urls_paused():
    with FakeQbittorrent() as fake:
        client = QBittorrentClient(fake.url)
        try:
            assert client.add_torrent([MAGNET, OTHER_MAGNET]) == "Ok."
        finally:
            client.close()

    assert fake.paths == [LOGIN_PATH, ADD_PATH]
    assert len(fake.bodies) == 1
    form = parse_qs(fake.bodies[0])
    assert form["urls"] == [f"{MAGNET}\n{OTHER_MAGNET}"]
    assert form["paused"] == ["true"]
    # The raw body is urlencoded, with the newline escaped between the URLs.
    assert "urls=magnet%3A%3F" in fake.bodies[0]
    assert "%0Amagnet%3A%3F" in fake.bodies[0]
    assert "paused=true" in fake.bodies[0]


def test_add_accepts_qbittorrent_5s_json_summary():
    summary = (
        '{"added_torrent_ids":["' + MAGNET.split(":")[-1] + '"],'
        '"failure_count":0,"pending_count":0,"success_count":1}'
    )
    with FakeQbittorrent(summary) as fake:
        client = QBittorrentClient(fake.url)
        try:
            assert client.add_torrent([MAGNET]) == summary
        finally:
            client.close()


def test_add_accepts_a_pending_torrent_url():
    # A .torrent URL is fetched by qBittorrent after the add call returns.
    summary = '{"added_torrent_ids":[],"failure_count":0,"pending_count":1,"success_count":0}'
    with FakeQbittorrent(summary) as fake:
        client = QBittorrentClient(fake.url)
        try:
            assert client.add_torrent(["https://example.test/x.torrent"]) == summary
        finally:
            client.close()


def test_add_unpauses_when_asked():
    with FakeQbittorrent() as fake:
        client = QBittorrentClient(fake.url)
        try:
            client.add_torrent([MAGNET], paused=False)
        finally:
            client.close()

    assert "paused=false" in fake.bodies[0]


def test_a_refusal_raises_naming_the_body():
    with FakeQbittorrent("Fails.") as fake:
        client = QBittorrentClient(fake.url)
        try:
            with pytest.raises(QBittorrentError) as caught:
                client.add_torrent([MAGNET])
        finally:
            client.close()

    assert "Fails." in str(caught.value)


def test_the_tool_adds_a_magnet(tmp_path):
    with FakeQbittorrent() as fake:
        client = QBittorrentClient(fake.url)
        try:
            tool = TriageTools(TriagePipeline(quarantine=tmp_path, library=tmp_path, client=client))
            result = tool.torrent_add(MAGNET)
        finally:
            client.close()

    assert result == {"status": "Ok.", "url": MAGNET, "paused": True}
    assert ADD_PATH in fake.paths
    assert "paused=true" in fake.bodies[0]


def test_the_tool_refuses_paths_and_empty_strings_without_a_request(tmp_path):
    with FakeQbittorrent() as fake:
        client = QBittorrentClient(fake.url)
        try:
            tool = TriageTools(TriagePipeline(quarantine=tmp_path, library=tmp_path, client=client))
            for candidate in ("/home/user/Downloads/thing.torrent", "thing.torrent", ""):
                with pytest.raises(ToolExecutionError) as caught:
                    tool.torrent_add(candidate)
                assert "magnet" in str(caught.value)
        finally:
            client.close()

    # Validation happens before any network call, including the login.
    assert fake.paths == []
