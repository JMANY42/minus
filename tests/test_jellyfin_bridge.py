"""The bridge's HTTP contract and the index.html patch, driven for real.

Every test binds a Bridge to port 0 and talks to it over real HTTP: the point
of the module is the wire format the jellyfin-web script depends on, so
asserting on handler internals would miss the thing under test. Discovery and
the pipeline are fakes, so no test touches a tracker or qBittorrent.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import stat
import threading
import time
from pathlib import Path

import pytest

from minus.config import Settings
from minus.jellyfin import Bridge, JellyfinError, install, remove, status

INFOHASH = "a" * 40
OTHER_HASH = "b" * 40
MAGNET = f"magnet:?xt=urn:btih:{INFOHASH}&dn=debian"


def candidate(**overrides) -> dict:
    item = {
        "title": "debian-12.5.0-amd64-netinst.iso",
        "provider": "knaben",
        "tracker": "knaben",
        "size_bytes": 700 * 1024**2,
        "seeders": 120,
        "infohash": INFOHASH,
        "magnet": MAGNET,
        "url": "",
        "download_url": MAGNET,
        "score": 80.0,
        "eligible": True,
        "reasons": ["title match", "seeders"],
        "reject_reason": None,
    }
    item.update(overrides)
    return item


class FakeDiscovery:
    """Stands in for DiscoveryTools: same envelope, no network."""

    def __init__(self, *candidates: dict, pick: dict | None = None) -> None:
        self.candidates = list(candidates)
        self.pick = pick
        self.calls: list[tuple[str, int]] = []

    def torrent_search(self, query: str, limit: int = 5) -> dict:
        self.calls.append((query, limit))
        return {
            "query": query,
            "kind": "software",
            "errors": ["nyaa: timeout"],
            "candidates": [dict(item) for item in self.candidates],
            "pick": self.pick,
        }


class FakePipeline:
    """Records the harness sequence; blocks on `gate` when a test needs it."""

    def __init__(self, result_status: str = "clean", gate: threading.Event | None = None):
        self.result_status = result_status
        self.gate = gate
        self.ingested: list[dict] = []
        self.promoted: list[tuple[str, str]] = []
        self.notify = None

    def ingest(
        self,
        url: str,
        *,
        detonate: bool = True,
        wait: bool = True,
        expected_title: str = "",
        release: bool = False,
        name: str = "",
    ) -> dict:
        self.ingested.append(
            {
                "url": url,
                "detonate": detonate,
                "release": release,
                "expected_title": expected_title,
            }
        )
        if self.notify is not None:
            self.notify("staged", {"torrent_hash": INFOHASH, "name": "debian"})
            self.notify("verdict", {"torrent_hash": INFOHASH, "verdict": {"label": "clean"}})
        if self.gate is not None:
            self.gate.wait(5)
        return {"status": self.result_status, "torrent_hash": INFOHASH, "name": "debian"}

    def promote(self, torrent_hash: str, *, name: str = "", **kwargs) -> dict:
        self.promoted.append((torrent_hash, name))
        return {"moved_to": f"/tmp/library/{name}"}


@pytest.fixture
def run_bridge():
    bridges: list[Bridge] = []

    def make(discovery=None, pipeline=None, *, settings: dict | None = None, token=None):
        values = {
            "vpn_autoconnect": False,
            "qbittorrent_url": "",
            "discovery_min_score": 45.0,
            "discovery_auto_promote": True,
        }
        values.update(settings or {})
        bridge = Bridge(
            Settings(**values),
            "127.0.0.1",
            0,
            discovery=discovery if discovery is not None else FakeDiscovery(),
            pipeline_factory=lambda: pipeline,
            token=token,
        )
        bridge.start()
        bridges.append(bridge)
        return bridge

    yield make
    for bridge in bridges:
        bridge.stop()


def request(bridge, method: str, path: str, *, body=None, token=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", bridge.port, timeout=5)
    hdrs = dict(headers or {})
    if token is not None:
        hdrs["X-Minus-Token"] = token
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    try:
        conn.request(method, path, body=data, headers=hdrs)
        response = conn.getresponse()
        payload = response.read()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, payload
    finally:
        conn.close()


def get_job(bridge, job_id: str, token: str) -> dict:
    code, _, body = request(bridge, "GET", f"/api/job/{job_id}", token=token)
    assert code == 200
    return json.loads(body)


def poll_until(bridge, job_id: str, token: str, predicate, deadline: float = 5.0) -> dict:
    end = time.monotonic() + deadline
    last: dict | None = None
    while time.monotonic() < end:
        last = get_job(bridge, job_id, token)
        if predicate(last):
            return last
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} never matched; last={last}")


def grab(bridge, *, query: str = "debian 12", infohash: str = INFOHASH, **extra):
    body = {"query": query, "infohash": infohash, **extra}
    return request(bridge, "POST", "/api/grab", body=body, token=bridge.token)


def test_health_needs_no_token(run_bridge) -> None:
    bridge = run_bridge()
    code, _, body = request(bridge, "GET", "/health")
    assert code == 200
    assert json.loads(body) == {"ok": True}


def test_search_returns_the_contract_envelope(run_bridge) -> None:
    scored = candidate()
    discovery = FakeDiscovery(scored, pick={**scored, "low_confidence": False})
    bridge = run_bridge(discovery)

    code, headers, body = request(
        bridge, "GET", "/api/search?q=debian+12&limit=8", token=bridge.token
    )

    assert code == 200
    assert headers["content-type"] == "application/json"
    payload = json.loads(body)
    assert payload["query"] == "debian 12"
    assert payload["kind"] == "software"
    assert payload["errors"] == ["nyaa: timeout"]
    assert payload["threshold"] == 45.0
    assert payload["candidates"][0]["infohash"] == INFOHASH
    assert payload["candidates"][0]["download_url"] == MAGNET
    assert payload["candidates"][0]["score"] == 80.0
    assert payload["pick"]["low_confidence"] is False
    assert discovery.calls == [("debian 12", 8)]


def test_search_without_text_is_400(run_bridge) -> None:
    bridge = run_bridge()
    code, _, body = request(bridge, "GET", "/api/search?q=", token=bridge.token)
    assert code == 400
    assert "q" in json.loads(body)["error"]


@pytest.mark.parametrize("supplied", [None, "not-the-token"])
def test_missing_or_wrong_token_is_401(run_bridge, supplied) -> None:
    bridge = run_bridge()
    code, _, body = request(bridge, "GET", "/api/search?q=debian", token=supplied)
    assert code == 401
    assert json.loads(body)["error"]
    code, _, _ = request(bridge, "POST", "/api/grab", body={}, token=supplied)
    assert code == 401


def test_grab_with_unknown_infohash_is_409(run_bridge) -> None:
    pipeline = FakePipeline()
    bridge = run_bridge(FakeDiscovery(candidate()), pipeline)

    code, _, body = grab(bridge, infohash=OTHER_HASH)

    assert code == 409
    assert OTHER_HASH in json.loads(body)["error"]
    assert pipeline.ingested == []


def test_grab_uses_the_re_search_not_the_request(run_bridge) -> None:
    pipeline = FakePipeline()
    discovery = FakeDiscovery(candidate())
    bridge = run_bridge(discovery, pipeline)

    # A body carrying its own download_url must not reach the pipeline: the
    # URL comes from the authoritative search for the infohash.
    code, _, body = grab(bridge, download_url="http://evil.example/x.torrent")
    assert code == 202
    job = poll_until(
        bridge,
        json.loads(body)["job"],
        bridge.token,
        lambda item: item["state"] in ("done", "error"),
    )

    assert job["state"] == "done"
    assert pipeline.ingested == [
        {
            "url": MAGNET,
            "detonate": True,
            "release": True,
            "expected_title": "debian 12",
        }
    ]
    assert pipeline.promoted == [(INFOHASH, "debian-12.5.0-amd64-netinst.iso")]
    assert job["result"]["status"] == "clean"
    assert job["result"]["torrent_hash"] == INFOHASH
    assert job["result"]["moved_to"] == "/tmp/library/debian-12.5.0-amd64-netinst.iso"
    # The grab re-searches at the wider limit, never at the UI's display limit.
    assert discovery.calls == [("debian 12", 20)]


def test_low_confidence_candidate_is_never_promoted(run_bridge) -> None:
    pipeline = FakePipeline()
    bridge = run_bridge(FakeDiscovery(candidate(score=10.0)), pipeline)

    code, _, body = grab(bridge)
    assert code == 202
    job = poll_until(
        bridge,
        json.loads(body)["job"],
        bridge.token,
        lambda item: item["state"] in ("done", "error"),
    )

    assert job["state"] == "done"
    assert pipeline.promoted == []
    assert "moved_to" not in job["result"]
    assert job["result"]["status"] == "clean"


def test_needs_review_is_not_promoted(run_bridge) -> None:
    pipeline = FakePipeline(result_status="needs_review")
    bridge = run_bridge(FakeDiscovery(candidate()), pipeline)

    code, _, body = grab(bridge)
    assert code == 202
    job = poll_until(
        bridge,
        json.loads(body)["job"],
        bridge.token,
        lambda item: item["state"] in ("done", "error"),
    )

    assert job["state"] == "done"
    assert pipeline.promoted == []
    assert job["result"]["status"] == "needs_review"


def test_pipeline_milestones_move_the_job_stage(run_bridge) -> None:
    gate = threading.Event()
    pipeline = FakePipeline(gate=gate)
    bridge = run_bridge(FakeDiscovery(candidate()), pipeline)

    code, _, body = grab(bridge)
    assert code == 202
    job_id = json.loads(body)["job"]

    seen = poll_until(bridge, job_id, bridge.token, lambda item: item["stage"] == "verdict")
    assert seen["state"] == "running"
    assert seen["detail"] == "clean"

    gate.set()
    done = poll_until(bridge, job_id, bridge.token, lambda item: item["state"] == "done")
    assert done["stage"] == "done"


def test_missing_pipeline_is_503(run_bridge) -> None:
    discovery = FakeDiscovery(candidate())
    bridge = run_bridge(discovery, None)

    code, _, body = grab(bridge)

    assert code == 503
    assert "QBITTORRENT_URL" in json.loads(body)["error"]
    assert discovery.calls == []


def test_unknown_job_is_404(run_bridge) -> None:
    bridge = run_bridge()
    code, _, body = request(bridge, "GET", "/api/job/nope", token=bridge.token)
    assert code == 404
    assert json.loads(body)["error"]


def test_third_grab_is_429_while_two_are_in_flight(run_bridge) -> None:
    gate = threading.Event()
    pipeline = FakePipeline(gate=gate)
    bridge = run_bridge(FakeDiscovery(candidate()), pipeline)

    assert [grab(bridge)[0] for _ in range(2)] == [202, 202]
    code, _, body = grab(bridge)
    assert code == 429
    assert "full" in json.loads(body)["error"]
    gate.set()


def test_minus_js_publishes_a_token_the_api_accepts(run_bridge) -> None:
    bridge = run_bridge()

    code, headers, body = request(bridge, "GET", "/minus.js")

    assert code == 200
    assert headers["content-type"] == "application/javascript"
    assert headers["cache-control"] == "no-store"
    text = body.decode("utf-8")
    assert text.startswith("window.__MINUS_JELLYFIN__ = {base: ")
    assert f"127.0.0.1:{bridge.port}" in text
    match = re.search(r'token: "([^"]+)"', text)
    assert match is not None
    published = match.group(1)
    assert published == bridge.token
    # The published token is a working credential, and the only way to get one.
    code, _, _ = request(bridge, "GET", "/api/search?q=debian", token=published)
    assert code == 200


def test_cors_echoes_loopback_origins_only(run_bridge) -> None:
    bridge = run_bridge()

    code, headers, _ = request(
        bridge,
        "OPTIONS",
        "/api/search",
        headers={
            "Origin": "http://127.0.0.1:8096",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert code == 204
    assert headers["access-control-allow-origin"] == "http://127.0.0.1:8096"
    assert headers["access-control-allow-methods"] == "GET, POST, OPTIONS"
    assert headers["access-control-allow-headers"] == "content-type, x-minus-token"

    _, headers, _ = request(bridge, "GET", "/health", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in headers


# ---- index.html patching -------------------------------------------------

ORIGINAL_INDEX = (
    "<!DOCTYPE html>\n"
    "<html>\n"
    "<head>\n"
    "<title>Jellyfin</title>\n"
    '<script defer src="main.jellyfin.bundle.js"></script>\n'
    "</head>\n"
    '<body><div id="reactRoot"></div></body>\n'
    "</html>\n"
)


def web_with_index(tmp_path: Path) -> tuple[Path, Path]:
    web = tmp_path / "web"
    web.mkdir()
    index = web / "index.html"
    index.write_text(ORIGINAL_INDEX, encoding="utf-8")
    return web, index


def test_install_is_idempotent_with_one_backup(tmp_path) -> None:
    web, index = web_with_index(tmp_path)
    assert status(web) is False

    assert install(web) is True
    assert install(web) is False

    text = index.read_text(encoding="utf-8")
    assert text.count("data-minus-jellyfin") == 1
    assert text.count("/minus.js") == 1
    assert text.index("/minus.js") < text.index("</head>")
    assert status(web) is True
    assert (web / "index.html.minus-backup").read_text(encoding="utf-8") == ORIGINAL_INDEX


def test_remove_restores_the_backup(tmp_path) -> None:
    web, index = web_with_index(tmp_path)
    install(web)

    assert remove(web) is True
    assert index.read_text(encoding="utf-8") == ORIGINAL_INDEX
    assert not (web / "index.html.minus-backup").exists()
    assert status(web) is False
    # Nothing left to undo is not an error.
    assert remove(web) is False


def test_remove_strips_the_tag_without_a_backup(tmp_path) -> None:
    web, index = web_with_index(tmp_path)
    install(web)
    (web / "index.html.minus-backup").unlink()

    assert remove(web) is True
    assert "data-minus-jellyfin" not in index.read_text(encoding="utf-8")


def test_status_of_a_missing_index_is_false(tmp_path) -> None:
    assert status(tmp_path) is False


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write a read-only directory")
def test_install_without_write_permission_names_sudo(tmp_path) -> None:
    web, _ = web_with_index(tmp_path)
    web.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        with pytest.raises(JellyfinError) as excinfo:
            install(web)
    finally:
        web.chmod(stat.S_IRWXU)

    assert "sudo" in str(excinfo.value)


def test_permission_error_is_not_swallowed(tmp_path, monkeypatch) -> None:
    """The same message holds when the OS refuses the write, whatever the uid."""
    web, _ = web_with_index(tmp_path)

    def refuse(self, *args, **kwargs):
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(Path, "write_text", refuse)

    with pytest.raises(JellyfinError) as excinfo:
        install(web)

    assert "sudo" in str(excinfo.value)
    assert "install" in str(excinfo.value)
