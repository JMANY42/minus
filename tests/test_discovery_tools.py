"""The search tool's payload, its disabled switch, and its registration."""

from __future__ import annotations

import pytest

import minus.tools.discovery as discovery_tools
from minus.config import Settings
from minus.errors import ToolExecutionError
from minus.tools.discovery import CATEGORY, DiscoveryTools
from minus.triage.discovery import DiscoveryResult, ScoredCandidate, TorrentCandidate

INFOHASH = "a" * 40
MAGNET = f"magnet:?xt=urn:btih:{INFOHASH}&dn=debian"


def _settings(**overrides) -> Settings:
    values = {
        "discovery_enabled": True,
        "discovery_min_score": 45.0,
        "discovery_low_confidence_score": 25.0,
    }
    values.update(overrides)
    return Settings(**values)


def _scored(score: float = 80.0, eligible: bool = True) -> ScoredCandidate:
    candidate = TorrentCandidate(
        title="debian-12.5.0-amd64-netinst.iso",
        provider="knaben",
        tracker="knaben",
        category="Software",
        size_bytes=700 * 1024**2,
        seeders=120,
        infohash=INFOHASH,
        magnet=MAGNET,
    )
    return ScoredCandidate(
        candidate=candidate,
        score=score,
        reasons=("title match", "seeders"),
        eligible=eligible,
        reject_reason="" if eligible else "too few seeders",
    )


def test_provider_errors_and_candidate_fields_surface(monkeypatch):
    scored = _scored()
    monkeypatch.setattr(
        discovery_tools,
        "discover",
        lambda settings, query, *, limit=5: DiscoveryResult(
            query=query,
            kind="software",
            candidates=(scored,),
            errors=("knaben: timeout", "nyaa: 503"),
        ),
    )
    monkeypatch.setattr(discovery_tools, "choose_best", lambda result, **kwargs: scored)

    payload = DiscoveryTools(_settings()).torrent_search("debian 12")

    assert payload["query"] == "debian 12"
    assert payload["kind"] == "software"
    assert payload["errors"] == ["knaben: timeout", "nyaa: 503"]
    candidate = payload["candidates"][0]
    assert candidate["title"] == scored.candidate.title
    assert candidate["provider"] == "knaben"
    assert candidate["tracker"] == "knaben"
    assert candidate["size_bytes"] == scored.candidate.size_bytes
    assert candidate["seeders"] == 120
    assert candidate["infohash"] == INFOHASH
    assert candidate["magnet"] == MAGNET
    assert candidate["download_url"] == MAGNET
    assert candidate["score"] == 80.0
    assert candidate["eligible"] is True
    assert candidate["reasons"] == ["title match", "seeders"]
    assert candidate["reject_reason"] == ""
    assert payload["pick"]["download_url"] == MAGNET


def test_pick_is_low_confidence_below_min_score(monkeypatch):
    scored = _scored(score=30.0)
    monkeypatch.setattr(
        discovery_tools,
        "discover",
        lambda settings, query, *, limit=5: DiscoveryResult(
            query=query, kind="software", candidates=(scored,)
        ),
    )
    monkeypatch.setattr(discovery_tools, "choose_best", lambda result, **kwargs: scored)

    payload = DiscoveryTools(_settings(discovery_min_score=45.0)).torrent_search("debian 12")

    assert payload["pick"]["low_confidence"] is True
    assert payload["pick"]["score"] == 30.0


def test_pick_is_confident_at_or_above_min_score(monkeypatch):
    scored = _scored(score=60.0)
    monkeypatch.setattr(
        discovery_tools,
        "discover",
        lambda settings, query, *, limit=5: DiscoveryResult(
            query=query, kind="software", candidates=(scored,)
        ),
    )
    monkeypatch.setattr(discovery_tools, "choose_best", lambda result, **kwargs: scored)

    payload = DiscoveryTools(_settings(discovery_min_score=45.0)).torrent_search("debian 12")

    assert payload["pick"]["low_confidence"] is False


def test_no_pick_is_none(monkeypatch):
    monkeypatch.setattr(
        discovery_tools,
        "discover",
        lambda settings, query, *, limit=5: DiscoveryResult(query=query, kind="any"),
    )
    monkeypatch.setattr(discovery_tools, "choose_best", lambda result, **kwargs: None)

    payload = DiscoveryTools(_settings()).torrent_search("nothing at all")

    assert payload["pick"] is None
    assert payload["candidates"] == []


def test_disabled_discovery_raises(monkeypatch):
    def explode(*args, **kwargs):  # pragma: no cover - a call here is the failure
        raise AssertionError("discovery must not run when disabled")

    monkeypatch.setattr(discovery_tools, "discover", explode)

    with pytest.raises(ToolExecutionError):
        DiscoveryTools(_settings(discovery_enabled=False)).torrent_search("debian 12")


class _Registry:
    def __init__(self) -> None:
        self.tools = []

    def tool(self, tool, *, category=None):
        self.tools.append((tool, category))
        return tool


def test_register_puts_the_tool_under_triage():
    registry = _Registry()
    tools = DiscoveryTools(_settings())

    tools.register(registry)

    assert [(tool.__name__, category) for tool, category in registry.tools] == [
        ("torrent_search", CATEGORY)
    ]
    assert registry.tools[0][0].__self__ is tools


def test_build_triage_tools_includes_discovery(monkeypatch):
    from minus import assembly
    from minus.tools.triage import TriageTools

    monkeypatch.setattr(assembly, "build_triage_pipeline", lambda settings: object())

    built = assembly.build_triage_tools(_settings())

    assert isinstance(built[0], TriageTools)
    assert isinstance(built[1], DiscoveryTools)
