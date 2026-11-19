"""The model-facing triage tools, driven with stubs instead of a pipeline.

Each test pins one contract the schema-derived tool signature cannot express:
the default an omitted argument lands on, whether an exception becomes a
result or an error, and whether an argument reaches the pipeline unchanged.
The pipeline itself is covered by `test_triage_auto.py`; a stub keeps these
assertions about the wrapper rather than about quarantine layout.
"""

from __future__ import annotations

import pytest

from minus.errors import ToolExecutionError
from minus.services.qbittorrent import QBittorrentError, TorrentAlreadyAdded
from minus.tools.registry import ToolRegistry
from minus.tools.triage import TriageTools
from minus.triage.trace import JsonlTracer, TraceEvent

HASH = "a" * 40
OTHER_HASH = "b" * 40
MAGNET = f"magnet:?xt=urn:btih:{HASH}&dn=Movie"


class RecordingClient:
    """Records the add call and answers the way qBittorrent does."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[list[str], bool]] = []

    def add_torrent(self, urls: list[str], *, paused: bool = True) -> str:
        self.calls.append((urls, paused))
        if self.error is not None:
            raise self.error
        return "Ok."


class AddPipeline:
    def __init__(self, client: RecordingClient) -> None:
        self.client = client


class PromotePipeline:
    """Only `promote` is reached; everything else is a missing attribute."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def promote(
        self,
        torrent_hash: str,
        *,
        name: str = "",
        accept_review: bool = False,
        reason: str = "",
        keep: list[str] | None = None,
    ) -> dict:
        self.calls.append(
            {
                "torrent_hash": torrent_hash,
                "name": name,
                "accept_review": accept_review,
                "reason": reason,
                "keep": keep,
            }
        )
        return {"torrent_hash": torrent_hash, "status": "promoted"}


class TracePipeline:
    def __init__(self, path) -> None:
        self.trace_file = path


class StagePipeline:
    """Only the two media-aware stages are reached; the rest are unused."""

    def __init__(self) -> None:
        self.ingest_calls: list[dict] = []
        self.preview_calls: list[dict] = []
        self.vm_review_calls: list[dict] = []

    def ingest(self, url: str, *, expected_title: str = "", release: bool = False) -> dict:
        self.ingest_calls.append({"url": url, "expected_title": expected_title, "release": release})
        return {"torrent_hash": HASH, "status": "staged"}

    def vm_review(self, torrent_hash: str, *, keep=None) -> dict:
        self.vm_review_calls.append({"torrent_hash": torrent_hash, "keep": keep})
        return {
            "torrent_hash": torrent_hash,
            "name": "Movie",
            "reviewed_at": "2026-01-01T00:00:00+00:00",
            "selected": keep or [],
            "engine": "windows-vm",
            "detonated": True,
            "opened": 1,
            "defender_detections": [],
            "processes": [],
            "network_endpoints": [],
            "file_writes": [],
            "registry_writes": [],
            "notes": "opened",
        }

    def preview(self, torrent_hash: str, *, frames: int = 6, expected_title: str = "") -> dict:
        self.preview_calls.append(
            {
                "torrent_hash": torrent_hash,
                "frames": frames,
                "expected_title": expected_title,
            }
        )
        return {
            "torrent_hash": torrent_hash,
            "contact_sheet": f"/quarantine/{torrent_hash}/preview/contact_sheet.jpg",
        }


def test_torrent_add_defaults_to_unpaused():
    """A paused add would never finish: this client has no resume call."""
    client = RecordingClient()
    tools = TriageTools(AddPipeline(client))

    result = tools.torrent_add(MAGNET)

    assert client.calls == [([MAGNET], False)]
    assert result == {"status": "Ok.", "url": MAGNET, "paused": False}


def test_torrent_add_reports_an_already_present_torrent():
    """409 is an answer about the torrent, not a failure of the add."""
    client = RecordingClient(error=TorrentAlreadyAdded("qBittorrent already has this torrent"))
    tools = TriageTools(AddPipeline(client))

    result = tools.torrent_add(MAGNET, paused=True)

    assert result == {"status": "already_present", "url": MAGNET, "paused": True}
    assert client.calls == [([MAGNET], True)]


def test_torrent_add_still_wraps_other_client_errors():
    client = RecordingClient(error=QBittorrentError("qBittorrent refused the torrent"))
    tools = TriageTools(AddPipeline(client))

    with pytest.raises(ToolExecutionError):
        tools.torrent_add(MAGNET)


def test_triage_promote_forwards_accept_review_and_reason():
    pipeline = PromotePipeline()
    tools = TriageTools(pipeline)

    result = tools.triage_promote(HASH, accept_review=True, reason="checked")

    assert result["status"] == "promoted"
    assert pipeline.calls == [
        {
            "torrent_hash": HASH,
            "name": "",
            "accept_review": True,
            "reason": "checked",
            "keep": None,
        }
    ]


def test_triage_trace_filters_to_one_torrent(tmp_path):
    path = tmp_path / "trace.jsonl"
    tracer = JsonlTracer(path)
    tracer.emit(TraceEvent(stage="scan", event="inspect.start", torrent_hash=HASH))
    tracer.emit(TraceEvent(stage="scan", event="inspect.done", torrent_hash=OTHER_HASH))
    tracer.emit(TraceEvent(stage="verdict", event="verdict.done", torrent_hash=HASH))
    tools = TriageTools(TracePipeline(path))

    result = tools.triage_trace(HASH)

    assert result["count"] == 2
    assert result["torrent_hash"] == HASH
    assert result["trace_file"] == str(path)
    assert {event["torrent_hash"] for event in result["events"]} == {HASH}
    assert [event["event"] for event in result["events"]] == ["inspect.start", "verdict.done"]


def test_triage_trace_says_so_when_tracing_is_off(tmp_path):
    tools = TriageTools(TracePipeline(None))

    result = tools.triage_trace(HASH)

    assert result == {"events": [], "count": 0, "reason": "tracing is not configured"}


def test_torrent_ingest_forwards_the_expected_title():
    """The title is a review hint, so it has to reach ingest unaltered."""
    pipeline = StagePipeline()
    tools = TriageTools(pipeline)

    result = tools.torrent_ingest(MAGNET, expected_title="The Movie")

    assert result["status"] == "staged"
    assert pipeline.ingest_calls == [
        {"url": MAGNET, "expected_title": "The Movie", "release": False}
    ]


def test_torrent_ingest_forwards_release():
    pipeline = StagePipeline()

    TriageTools(pipeline).torrent_ingest(MAGNET, release=True)

    assert pipeline.ingest_calls == [{"url": MAGNET, "expected_title": "", "release": True}]


def test_torrent_ingest_defaults_to_no_expected_title():
    pipeline = StagePipeline()

    TriageTools(pipeline).torrent_ingest(MAGNET)

    assert pipeline.ingest_calls == [{"url": MAGNET, "expected_title": "", "release": False}]


def test_triage_preview_forwards_frames_and_title():
    pipeline = StagePipeline()
    tools = TriageTools(pipeline)

    result = tools.triage_preview(HASH, frames=3, expected_title="The Movie")

    assert pipeline.preview_calls == [
        {"torrent_hash": HASH, "frames": 3, "expected_title": "The Movie"}
    ]
    assert result["contact_sheet"].endswith("contact_sheet.jpg")


def test_triage_preview_defaults_to_six_frames():
    pipeline = StagePipeline()

    TriageTools(pipeline).triage_preview(HASH)

    assert pipeline.preview_calls == [{"torrent_hash": HASH, "frames": 6, "expected_title": ""}]


def test_register_derives_the_preview_and_ingest_schemas():
    """The schema comes from the signature; a missing argument is a bug here."""
    registry = ToolRegistry()
    TriageTools(StagePipeline()).register(registry)

    preview = registry.get("triage_preview")
    assert preview.category == "triage"
    params = preview.schema["function"]["parameters"]
    assert set(params["properties"]) == {"torrent_hash", "frames", "expected_title"}
    assert params["required"] == ["torrent_hash"]
    assert params["properties"]["frames"]["default"] == 6

    ingest = registry.get("torrent_ingest")
    params = ingest.schema["function"]["parameters"]
    assert set(params["properties"]) == {"url", "expected_title", "release"}
    assert params["required"] == ["url"]
    assert params["properties"]["expected_title"]["default"] == ""
    assert params["properties"]["release"]["default"] is False


def test_triage_vm_review_forwards_keep_and_summarizes_the_report():
    pipeline = StagePipeline()
    tools = TriageTools(pipeline)

    result = tools.triage_vm_review(HASH, keep="movie.mp4")

    assert pipeline.vm_review_calls == [{"torrent_hash": HASH, "keep": ["movie.mp4"]}]
    assert result["opened"] == 1
    assert result["selected"] == ["movie.mp4"]
    assert result["engine"] == "windows-vm"


def test_triage_promote_forwards_keep():
    pipeline = PromotePipeline()
    tools = TriageTools(pipeline)

    tools.triage_promote(HASH, accept_review=True, keep="movie.mp4")

    assert pipeline.calls[0]["keep"] == ["movie.mp4"]


class VerdictPipeline:
    def verdict(self, torrent_hash: str) -> dict:
        return {
            "torrent_hash": torrent_hash,
            "name": "Movie",
            "static": {"flags": [], "files": []},
            "sandbox": None,
            "notes": "",
        }

    def vm_review_evidence(self, torrent_hash: str, payload: dict) -> dict:
        return {"completed": True, "detonated": True, "opened": 1, "reviewed": True}


def test_triage_verdict_uses_the_standalone_review_evidence():
    result = TriageTools(VerdictPipeline()).triage_verdict(HASH)

    assert result["detonated"] is True
    assert result["reviewed"] is True
    assert result["opened"] == 1
