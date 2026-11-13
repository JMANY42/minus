"""Provisioning qBittorrent from outside a browser, over a fake WebUI.

The client methods and the preference write are the contract here: the API
takes one form field (`json`) holding compact JSON, and every network call must
stay on the loopback URL. The fake server records the raw bodies so both are
observable without a running torrent client.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest

from minus.config import Settings
from minus.scripts import qbt
from minus.scripts.qbt import (
    apply_preferences,
    base_url,
    provision,
    temporary_password,
)
from minus.services.qbittorrent import QBittorrentClient, QBittorrentError

LOGIN_PATH = "/api/v2/auth/login"
PREFS_PATH = "/api/v2/app/setPreferences"
VERSION_PATH = "/api/v2/app/version"
TRANSFER_PATH = "/api/v2/transfer/info"
ADD_PATH = "/api/v2/torrents/add"
DELETE_PATH = "/api/v2/torrents/delete"


class FakeWebUi:
    """A WebUI that logs in, records every request, and serves fixed answers."""

    def __init__(
        self, accepted: set[tuple[str, str]] | None = None, *, login_status: int = 200
    ) -> None:
        # None means "accept every login", which is enough for the plain client
        # tests; a set makes the fake behave like a first-run qBittorrent.
        self.accepted = accepted
        # qBittorrent 5.3 answers 204 with no body on a successful login, while
        # older releases answer 200 "Ok.".
        self.login_status = login_status
        self.requests: list[tuple[str, str, bytes]] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> FakeWebUi:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def _record(self, body: bytes = b"") -> None:
                owner.requests.append((self.command, self.path, body))

            def do_GET(self) -> None:
                self._record()
                if self.path == VERSION_PATH:
                    self._send("v5.0.0")
                elif self.path == TRANSFER_PATH:
                    self._send(json.dumps({"connection_status": "connected"}))
                else:
                    self._send("", status=404)

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                self._record(raw)
                if self.path == LOGIN_PATH:
                    if owner.accepted is None:
                        self._send("" if owner.login_status == 204 else "Ok.",
                                   status=owner.login_status)
                        return
                    form = parse_qs(raw.decode("utf-8"))
                    pair = (
                        form.get("username", [""])[0],
                        form.get("password", [""])[0],
                    )
                    permitted = pair in owner.accepted
                    self._send(
                        ("" if owner.login_status == 204 else "Ok.") if permitted else "Fails.",
                        status=owner.login_status if permitted else 200,
                    )
                elif self.path == ADD_PATH:
                    self._send("Ok.")
                elif self.path in (PREFS_PATH, DELETE_PATH):
                    if self.path == PREFS_PATH and owner.accepted is not None:
                        prefs = json.loads(parse_qs(raw.decode("utf-8"))["json"][0])
                        if "web_ui_username" in prefs:
                            owner.accepted.add((prefs["web_ui_username"], prefs["web_ui_password"]))
                    self._send("")
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

    @property
    def paths(self) -> list[str]:
        return [path for _, path, _ in self.requests]

    def body_for(self, path: str) -> list[str]:
        return [raw.decode("utf-8") for _, seen, raw in self.requests if seen == path]


def test_set_preferences_posts_compact_json_on_the_loopback_url():
    prefs = {
        "web_ui_address": "127.0.0.1",
        "web_ui_port": 8080,
        "bypass_local_auth": False,
    }
    with FakeWebUi() as fake:
        client = QBittorrentClient(fake.url)
        try:
            client.set_preferences(prefs)
        finally:
            client.close()

    assert fake.paths == [LOGIN_PATH, PREFS_PATH]
    form = parse_qs(fake.body_for(PREFS_PATH)[0])
    assert json.loads(form["json"][0]) == prefs
    # Compact: no spaces from the default separators.
    assert " " not in form["json"][0]
    assert fake.url.startswith("http://127.0.0.1:")


def test_version_and_transfer_info_are_read_back():
    with FakeWebUi() as fake:
        client = QBittorrentClient(fake.url)
        try:
            assert client.version() == "v5.0.0"
            assert client.transfer_info()["connection_status"] == "connected"
        finally:
            client.close()

    assert fake.paths == [LOGIN_PATH, VERSION_PATH, TRANSFER_PATH]


def test_add_torrent_sends_savepath_only_when_given(tmp_path):
    with FakeWebUi() as fake:
        client = QBittorrentClient(fake.url)
        try:
            client.add_torrent(["magnet:?xt=urn:btih:" + "a" * 40], paused=False)
            client.add_torrent(
                ["magnet:?xt=urn:btih:" + "b" * 40],
                paused=True,
                save_path=str(tmp_path / "incoming"),
            )
        finally:
            client.close()

    first, second = (parse_qs(body) for body in fake.body_for(ADD_PATH))
    assert first["paused"] == ["false"]
    assert "savepath" not in first
    assert second["paused"] == ["true"]
    assert second["savepath"] == [str(tmp_path / "incoming")]


def test_delete_torrent_posts_the_hash_and_the_file_flag():
    with FakeWebUi() as fake:
        client = QBittorrentClient(fake.url)
        try:
            client.delete_torrent("a" * 40, delete_files=False)
        finally:
            client.close()

    form = parse_qs(fake.body_for(DELETE_PATH)[0])
    assert form["hashes"] == ["a" * 40]
    assert form["deleteFiles"] == ["false"]


def test_apply_preferences_writes_the_quarantine_and_loopback_settings(tmp_path):
    incoming = tmp_path / "incoming"
    settings = Settings(
        qbittorrent_incoming_dir=incoming,
        qbittorrent_webui_port=8090,
        qbittorrent_listen_port=51413,
    )

    with FakeWebUi() as fake:
        client = QBittorrentClient(fake.url)
        try:
            apply_preferences(client, settings, "lo")
        finally:
            client.close()

    prefs = json.loads(parse_qs(fake.body_for(PREFS_PATH)[0])["json"][0])
    assert prefs["save_path"] == str(incoming)
    assert prefs["temp_path"] == str(incoming / "tmp")
    assert prefs["temp_path_enabled"] is True
    assert prefs["current_network_interface"] == "lo"
    assert prefs["web_ui_address"] == "127.0.0.1"
    assert prefs["web_ui_port"] == 8090
    assert prefs["web_ui_csrf_protection"] is True
    assert prefs["web_ui_clickjacking_protection"] is True
    assert prefs["web_ui_host_header_validation"] is True
    assert prefs["bypass_local_auth"] is False
    assert prefs["listen_port"] == 51413
    assert prefs["dht"] is True and prefs["pex"] is True and prefs["lsd"] is True
    assert "max_active_downloads" not in prefs
    assert (incoming.stat().st_mode & 0o777) == 0o700
    assert ((incoming / "tmp").stat().st_mode & 0o777) == 0o700


def test_an_empty_interface_is_left_out(tmp_path):
    settings = Settings(qbittorrent_incoming_dir=tmp_path / "incoming")
    with FakeWebUi() as fake:
        client = QBittorrentClient(fake.url)
        try:
            apply_preferences(client, settings, "")
        finally:
            client.close()

    prefs = json.loads(parse_qs(fake.body_for(PREFS_PATH)[0])["json"][0])
    assert "current_network_interface" not in prefs


def test_a_named_interface_the_kernel_does_not_know_is_refused(tmp_path):
    settings = Settings(qbittorrent_incoming_dir=tmp_path / "incoming")
    with FakeWebUi() as fake:
        client = QBittorrentClient(fake.url)
        try:
            with pytest.raises(QBittorrentError) as caught:
                apply_preferences(client, settings, "minus-does-not-exist0")
        finally:
            client.close()

    assert "does not exist" in str(caught.value)
    # The refusal happens before any request, including the login.
    assert fake.requests == []


def test_base_url_derives_loopback_from_the_webui_port():
    settings = Settings(qbittorrent_url="", qbittorrent_webui_port=8123)
    assert base_url(settings) == "http://127.0.0.1:8123"


def test_provision_uses_an_answerable_port_without_spawning(tmp_path):
    settings = Settings(
        qbittorrent_url="",
        qbittorrent_username="minus",
        qbittorrent_password="secret",
        qbittorrent_incoming_dir=tmp_path / "incoming",
        qbittorrent_vpn_interface="",
        vpn_interface="",
    )
    with FakeWebUi() as fake:
        settings = settings.model_copy(update={"qbittorrent_url": fake.url})
        result = provision(settings)

        assert result["url"] == fake.url
        assert result["started"] is False
        assert result["username"] == "minus"
        assert result["interface"] == ""
        assert result["incoming"] == str(tmp_path / "incoming")
        assert "password" not in result

    prefs = json.loads(parse_qs(fake.body_for(PREFS_PATH)[0])["json"][0])
    assert prefs["save_path"] == str(tmp_path / "incoming")


def test_temporary_password_is_read_from_the_log(tmp_path):
    log = tmp_path / "qbittorrent.log"
    log.write_text(
        "The WebUI administrator password was not set. A temporary password is "
        "provided for this session: AbC123xyz\n"
    )
    assert temporary_password(log) == "AbC123xyz"
    assert temporary_password(tmp_path / "missing.log") == ""


def test_spawn_bootstraps_when_the_configured_password_is_not_known(tmp_path, monkeypatch):
    """A fresh profile is admin + temporary password, not the `.env` pair."""
    monkeypatch.setattr("minus.paths.quarantine_dir", lambda: tmp_path)
    log = tmp_path / "qbittorrent.log"
    log.write_text(
        "The WebUI administrator password was not set. A temporary password is "
        "provided for this session: first-run\n"
    )
    persisted: list[tuple[str, str]] = []

    with FakeWebUi(accepted={("admin", "first-run")}) as fake:
        settings = Settings(
            qbittorrent_url=fake.url,
            qbittorrent_username="minus",
            qbittorrent_password="secret",
            qbittorrent_incoming_dir=tmp_path / "incoming",
        )
        monkeypatch.setattr(qbt, "_start_process", lambda *args, **kwargs: log)
        monkeypatch.setattr(qbt, "wait_for_port", lambda *args, **kwargs: True)
        monkeypatch.setattr(
            qbt,
            "_persist_credentials",
            lambda username, password: persisted.append((username, password)),
        )

        client, username = qbt._spawn(settings, fake.url)
        client.close()

    assert username == "minus"
    # A configured password is already in .env; only a generated one is written.
    assert persisted == []
    prefs = json.loads(parse_qs(fake.body_for(PREFS_PATH)[0])["json"][0])
    assert prefs == {"web_ui_username": "minus", "web_ui_password": "secret"}


def test_login_accepts_qbittorrent_5s_empty_204(tmp_path):
    # A real 5.3 install answers 204 with no body; treating that as a refusal
    # made provisioning fail against a Web UI that curl could log into.
    with FakeWebUi(login_status=204) as fake:
        client = QBittorrentClient(fake.url, "admin", "temp")
        try:
            client.login()
        finally:
            client.close()

    with FakeWebUi({("admin", "right")}, login_status=204) as fake:
        client = QBittorrentClient(fake.url, "admin", "wrong")
        try:
            with pytest.raises(QBittorrentError):
                client.login()
        finally:
            client.close()
