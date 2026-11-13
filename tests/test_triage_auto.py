"""The whole torrent flow against a scripted client, with no network.

The client is a stand-in because qBittorrent's job here is only to report
progress and hand back a content path; the bytes, the staging, the manifest and
the verdict are the pipeline's own and are exercised for real.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from minus.errors import ToolExecutionError
from minus.tools.triage import TriageTools
from minus.triage import pipeline as pipeline_module
from minus.triage.pipeline import TriageError, TriagePipeline
from minus.triage.scoring import MALICIOUS, Verdict
from minus.triage.static import FileReport, StaticReport

HASH = "d" * 40
MAGNET = f"magnet:?xt=urn:btih:{HASH}&dn=Movie"


class ScriptedClient:
    """Reports a scripted progress sequence and the same completed content."""

    def __init__(self, content: Path, progress: tuple[float, ...] = (0.5, 1.0)) -> None:
        self.content = content
        self.progress = list(progress)
        self.items: list[dict] = []
        self.calls: list = []

    def torrents(self, **filters: str) -> list[dict]:
        self.calls.append("torrents")
        return [dict(item) for item in self.items]

    def add_torrent(
        self,
        urls: list[str],
        *,
        paused: bool = True,
        save_path: str | None = None,
    ) -> str:
        self.calls.append(("add_torrent", tuple(urls), paused, save_path))
        self.items.append({"hash": HASH, "name": "Movie", "progress": 0.0})
        return "Ok."

    def torrent(self, torrent_hash: str) -> dict:
        self.calls.append(("torrent", torrent_hash))
        if len(self.progress) > 1:
            current = self.progress.pop(0)
        else:
            current = self.progress[0] if self.progress else 1.0
        return {
            "hash": torrent_hash,
            "name": "Movie",
            "progress": current,
            "size": 5,
            "content_path": str(self.content),
            "save_path": str(self.content.parent),
        }


def _static(flags: list[str] | None = None, score: int = 0) -> StaticReport:
    return StaticReport(
        root="/quarantine",
        files=[
            FileReport(
                relative="Movie/file.txt",
                size=5,
                sha256="0" * 64,
                kind="text",
                entropy=1.0,
                flags=list(flags or []),
            )
        ],
        flags=list(flags or []),
        score=score,
    )


def _pipeline(tmp_path: Path, client: ScriptedClient, events: list, **kwargs) -> TriagePipeline:
    return TriagePipeline(
        quarantine=tmp_path / "quarantine",
        library=tmp_path / "library",
        client=client,
        require_vpn=False,
        poll_seconds=0.001,
        notify=lambda kind, payload: events.append((kind, payload)),
        **kwargs,
    )


def _content(tmp_path: Path) -> Path:
    content = tmp_path / "downloads" / "Movie"
    content.mkdir(parents=True)
    (content / "file.txt").write_bytes(b"hello")
    return content


def test_ingest_adds_waits_stages_and_scans_in_order(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline_module, "analyse", lambda root: _static())
    client = ScriptedClient(_content(tmp_path))
    events: list = []
    pipeline = _pipeline(tmp_path, client, events)

    result = pipeline.ingest(MAGNET)

    assert result["status"] == "needs_review"
    assert result["torrent_hash"] == HASH
    assert result["name"] == "Movie"
    assert result["files"] == 1
    assert result["alert"] is None
    assert result["error"] is None
    assert result["verdict"]["label"] == "needs_review"

    assert client.calls[0] == "torrents"
    add = client.calls[1]
    assert add[0] == "add_torrent"
    assert add[1] == (MAGNET,)
    assert add[2] is False
    assert add[3] == str(tmp_path / "quarantine" / "incoming")
    assert [call[0] for call in client.calls[2:]] == ["torrent", "torrent", "torrent"]

    assert [kind for kind, _ in events] == ["staged", "verdict"]
    assert events[0][1]["torrent_hash"] == HASH
    assert events[0][1]["files"] == 1
    assert events[1][1]["verdict"]["label"] == "needs_review"

    assert (tmp_path / "quarantine" / HASH / "manifest.json").is_file()
    assert not (tmp_path / "library").exists()


def test_a_malicious_scan_deletes_the_payload_and_emits_an_alert(tmp_path, monkeypatch):
    monkeypatch.setattr(
        pipeline_module,
        "analyse",
        lambda root: _static(flags=["windows-pe"], score=9),
    )
    monkeypatch.setattr(
        pipeline_module,
        "decide",
        lambda static, run, *, require_detonation=True, review=None: Verdict(
            MALICIOUS, 9, True, ("defender detection: Trojan:Win32/X",)
        ),
    )
    client = ScriptedClient(_content(tmp_path))
    events: list = []
    pipeline = _pipeline(tmp_path, client, events)

    result = pipeline.ingest(MAGNET)

    assert result["status"] == "malicious"
    assert result["verdict"]["label"] == MALICIOUS
    assert "ALERT: malicious torrent" in result["alert"]
    assert result["error"] is None

    assert [kind for kind, _ in events] == ["staged", "verdict", "malicious_deleted"]
    deleted = events[-1][1]
    assert deleted["deleted"] is True
    assert deleted["torrent_hash"] == HASH
    assert "ALERT" in deleted["alert"]
    assert Path(deleted["rejected_record"]).is_file()

    # The bytes are gone; the record is not.
    assert not (tmp_path / "quarantine" / HASH).exists()
    record = Path(deleted["rejected_record"]).read_text()
    assert "Trojan:Win32/X" in record


def test_a_timeout_names_the_progress_instead_of_hanging(tmp_path):
    client = ScriptedClient(_content(tmp_path), progress=(0.5,))
    pipeline = _pipeline(tmp_path, client, [])

    with pytest.raises(TriageError) as caught:
        pipeline.wait_for_completion(HASH, timeout=0.01, poll=0.001)

    assert "50%" in str(caught.value)


def test_ingest_returns_failure_instead_of_raising_on_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline_module, "analyse", lambda root: _static())
    client = ScriptedClient(_content(tmp_path), progress=(0.5,))
    pipeline = _pipeline(tmp_path, client, [], download_timeout=0.01)

    result = pipeline.ingest(MAGNET)

    assert result["status"] == "failed"
    assert "50%" in result["error"]


def test_find_new_torrent_reads_the_hash_out_of_a_magnet(tmp_path):
    client = ScriptedClient(_content(tmp_path))
    pipeline = _pipeline(tmp_path, client, [])

    assert pipeline.find_new_torrent(set(), MAGNET) == HASH
    assert client.calls == []


def test_find_new_torrent_polls_for_an_http_url(tmp_path):
    content = _content(tmp_path)
    client = ScriptedClient(content)
    client.items = []
    pipeline = _pipeline(tmp_path, client, [])

    def add_later():
        client.items.append({"hash": HASH, "name": "Movie"})

    # The first poll sees nothing, the second sees the torrent that appeared.
    original = client.torrents
    calls = {"count": 0}

    def torrents(**filters):
        calls["count"] += 1
        if calls["count"] == 2:
            add_later()
        return original(**filters)

    client.torrents = torrents  # type: ignore[method-assign]

    assert (
        pipeline.find_new_torrent(
            set(), "http://example.test/movie.torrent", timeout=0.1, poll=0.001
        )
        == HASH
    )


def test_an_observer_error_does_not_break_the_stage(tmp_path):
    pipeline = TriagePipeline(quarantine=tmp_path / "q", library=tmp_path / "l")

    def broken(kind, payload):
        raise RuntimeError("rendering failed")

    pipeline.notify = broken
    pipeline._notify("staged", {"torrent_hash": HASH})  # must not raise


def test_torrent_ingest_tool_refuses_paths_before_touching_the_client(tmp_path):
    client = ScriptedClient(_content(tmp_path))
    tool = TriageTools(_pipeline(tmp_path, client, []))

    with pytest.raises(ToolExecutionError) as caught:
        tool.torrent_ingest("/home/user/file.torrent")

    assert "magnet" in str(caught.value)
    assert client.calls == []


def test_torrent_progress_reports_only_the_fields_a_decision_needs(tmp_path):
    client = ScriptedClient(_content(tmp_path), progress=(1.0,))
    client.items = [{"hash": HASH, "name": "Movie", "progress": 1.0}]
    tool = TriageTools(_pipeline(tmp_path, client, []))

    assert tool.torrent_progress(HASH) == {
        "hash": HASH,
        "name": "Movie",
        "progress": 1.0,
        "state": None,
        "eta": None,
        "size": 5,
    }
