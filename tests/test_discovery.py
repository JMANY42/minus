"""Ranking and provider parsing, entirely offline.

The providers are fed literal JSON, so the test asserts the mapping and the
heuristic rather than whether an index happens to be reachable today.
"""

from __future__ import annotations

import httpx
import pytest

from minus.config import Settings
from minus.triage import discovery
from minus.triage.discovery import (
    GB,
    MB,
    DiscoveryResult,
    ScoredCandidate,
    TorrentCandidate,
    choose_best,
    discover,
    infer_kind,
    rank_candidates,
)


class FakeResponse:
    def __init__(self, payload: object, status: int = 200) -> None:
        self.payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code < 400:
            return
        request = httpx.Request("GET", "https://example.test")
        response = httpx.Response(self.status_code, request=request)
        raise httpx.HTTPStatusError("boom", request=request, response=response)

    def json(self) -> object:
        return self.payload

    @property
    def text(self) -> str:
        return self.payload if isinstance(self.payload, str) else ""


class FakeClient:
    def __init__(
        self,
        *,
        knaben: object | None = None,
        bitsearch: object | None = None,
        apibay: object | None = None,
        nyaa: object | None = None,
        jackett: object | None = None,
        archive: object | None = None,
        metadata: dict[str, object] | None = None,
        fail: tuple[str, ...] = (),
    ) -> None:
        self.knaben = knaben if knaben is not None else {"hits": []}
        self.bitsearch = bitsearch if bitsearch is not None else {"results": []}
        self.apibay = apibay if apibay is not None else []
        self.nyaa = nyaa if nyaa is not None else ""
        self.jackett = jackett if jackett is not None else ""
        self.archive = archive if archive is not None else {"response": {"docs": []}}
        self.metadata = metadata or {}
        self.fail = set(fail)
        self.closed = False
        self.calls: list[tuple] = []

    def post(self, url: str, json: object = None, **kwargs: object) -> FakeResponse:
        self.calls.append(("post", url, json))
        if "knaben" in self.fail:
            raise httpx.ConnectError("knaben down")
        return FakeResponse(self.knaben)

    def get(
        self,
        url: str,
        params: object = None,
        headers: object = None,
        **kwargs: object,
    ) -> FakeResponse:
        self.calls.append(("get", url, params, headers))
        if url.startswith(discovery.BITSEARCH_ENDPOINT):
            if "bitsearch" in self.fail:
                raise httpx.ConnectError("bitsearch down")
            return FakeResponse(self.bitsearch)
        if url.startswith(discovery.APIBAY_ENDPOINT):
            if "apibay" in self.fail:
                raise httpx.ConnectError("apibay down")
            return FakeResponse(self.apibay)
        if url.startswith(discovery.NYAA_ENDPOINT):
            if "nyaa" in self.fail:
                raise httpx.ConnectError("nyaa down")
            return FakeResponse(self.nyaa)
        if "/api/v2.0/indexers/" in url:
            if "jackett" in self.fail:
                raise httpx.ConnectError("jackett down")
            return FakeResponse(self.jackett)
        if url.startswith(discovery.ARCHIVE_SEARCH_ENDPOINT):
            return FakeResponse(self.archive)
        if url.startswith("https://archive.org/metadata/"):
            identifier = url.rsplit("/", 1)[-1]
            payload = self.metadata.get(identifier)
            if payload is None:
                raise httpx.ConnectError(f"no metadata for {identifier}")
            return FakeResponse(payload)
        raise AssertionError(f"unexpected URL {url}")

    def close(self) -> None:
        self.closed = True


def candidate(**overrides: object) -> TorrentCandidate:
    values: dict[str, object] = {
        "title": "The Great Gatsby (2013) 1080p BluRay x264-GRP",
        "provider": "knaben",
        "tracker": "1337x",
        "category": "Movies / HD",
        "size_bytes": int(2.2 * GB),
        "seeders": 80,
        "leechers": 10,
        "infohash": "a" * 40,
        "magnet": "magnet:?xt=urn:btih:" + "a" * 40,
        "verified": True,
        "virus_score": 0.02,
        "sources": ("knaben", "bitsearch"),
    }
    values.update(overrides)
    return TorrentCandidate(**values)  # type: ignore[arg-type]


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "discovery_providers": "knaben,bitsearch",
        "discovery_fallback_providers": "",
        "discovery_timeout": 1.0,
        "triage_max_bytes": 5 * GB,
        "prowlarr_url": "",
        "prowlarr_api_key": "",
        "jackett_url": "",
        "jackett_api_key": "",
        "jackett_indexers": "",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_infer_kind_uses_request_words() -> None:
    assert infer_kind("the great gatsby movie") == "movie"
    assert infer_kind("the great gatsby 1080p") == "movie"
    assert infer_kind("the great gatsby audiobook") == "audio"
    assert infer_kind("the great gatsby epub") == "book"
    assert infer_kind("debian iso") == "software"
    assert infer_kind("the great gatsby") == "any"


def test_ranking_prefers_seeded_verified_1080p_and_rejects_bad_rows() -> None:
    good = candidate()
    cam = candidate(
        title="The Great Gatsby 2013 HDCAM",
        infohash="b" * 40,
        seeders=500,
        sources=("knaben",),
    )
    huge = candidate(
        title="The Great Gatsby (2013) 2160p REMUX",
        infohash="c" * 40,
        size_bytes=30 * GB,
    )
    book = candidate(
        title="The Great Gatsby epub",
        category="Books / EBooks",
        infohash="d" * 40,
        size_bytes=2 * MB,
    )
    worse = candidate(
        title="The Great Gatsby (1974) 720p BRRip",
        infohash="e" * 40,
        seeders=4,
        sources=("knaben",),
        verified=False,
    )

    ranked = rank_candidates(
        "the great gatsby movie",
        [cam, huge, book, worse, good],
        kind="movie",
        preferred_quality="1080p",
        max_bytes=5 * GB,
    )

    assert ranked[0].candidate is good
    assert "1080p" in ranked[0].reasons
    assert "verified" in ranked[0].reasons

    by_hash = {scored.candidate.infohash: scored for scored in ranked}
    assert not by_hash["b" * 40].eligible
    assert "CAM" in by_hash["b" * 40].reject_reason
    assert not by_hash["c" * 40].eligible
    assert "MINUS_TRIAGE_MAX_BYTES" in by_hash["c" * 40].reject_reason
    assert not by_hash["d" * 40].eligible
    assert "not movie" in by_hash["d" * 40].reject_reason
    assert by_hash["e" * 40].score < ranked[0].score


def test_ranking_rejects_an_executable_dressed_as_a_film() -> None:
    bad = candidate(
        title="The Great Gatsby (2013) 1080p BluRay.exe",
        infohash="f" * 40,
    )
    scored = rank_candidates("the great gatsby", [bad], kind="movie")
    assert not scored[0].eligible
    assert "executable" in scored[0].reject_reason


def test_choose_best_needs_the_threshold_but_keeps_archive_fallback() -> None:
    low = ScoredCandidate(candidate(seeders=3), 40.0, eligible=True)
    result = DiscoveryResult("q", "movie", (low,))
    assert choose_best(result, min_score=55.0) is None
    assert choose_best(result, min_score=0.0) is low

    archive = ScoredCandidate(
        candidate(
            title="The Great Gatsby",
            provider="internetarchive",
            tracker="Internet Archive",
            category="movies",
            infohash="",
            magnet="",
            url="https://archive.org/download/item/item_archive.torrent",
            seeders=0,
            sources=("internetarchive",),
        ),
        42.0,
        eligible=True,
    )
    fallback = DiscoveryResult("q", "movie", (archive,), used_fallback=True)
    assert choose_best(fallback, min_score=55.0) is archive


def test_discover_merges_same_infohash_and_survives_a_provider_failure() -> None:
    shared = "a" * 40
    knaben = {
        "hits": [
            {
                "title": "The Great Gatsby (2013) 1080p BluRay x264-GRP",
                "tracker": "YTS",
                "category": "Movies / HD",
                "bytes": int(2.2 * GB),
                "seeders": 80,
                "peers": 4,
                "virusDetection": 0.01,
                "hash": shared.upper(),
                "magnetUrl": f"magnet:?xt=urn:btih:{shared}",
                "date": "2026-01-01",
            }
        ]
    }
    bitsearch = {
        "results": [
            {
                "title": "The Great Gatsby (2013) 1080p BluRay x264-GRP",
                "category": 2,
                "size": int(2.2 * GB),
                "seeders": 90,
                "leechers": 7,
                "verified": True,
                "infohash": shared.upper(),
            }
        ]
    }
    client = FakeClient(knaben=knaben, bitsearch=bitsearch, fail=("bitsearch",))

    result = discover(
        settings(),
        "the great gatsby",
        client=client,
        providers=["knaben", "bitsearch"],
        fallback_providers=[],
    )

    assert any("bitsearch:" in error for error in result.errors)
    assert len(result.candidates) == 1
    merged = result.candidates[0].candidate
    assert merged.source_count == 1  # Bitsearch failed, so no consensus
    assert merged.seeders == 80
    assert client.closed is False


def test_discover_merges_the_same_hash_from_two_providers() -> None:
    shared = "a" * 40
    client = FakeClient(
        knaben={
            "hits": [
                {
                    "title": "The Great Gatsby (2013) 1080p BluRay",
                    "tracker": "1337x",
                    "category": "Movies / HD",
                    "bytes": int(2.2 * GB),
                    "seeders": 80,
                    "hash": shared,
                    "magnetUrl": f"magnet:?xt=urn:btih:{shared}",
                    "virusDetection": 0.01,
                }
            ]
        },
        bitsearch={
            "results": [
                {
                    "title": "The Great Gatsby (2013) 1080p BluRay",
                    "size": int(2.2 * GB),
                    "seeders": 95,
                    "verified": True,
                    "infohash": shared,
                }
            ]
        },
    )

    result = discover(
        settings(),
        "the great gatsby",
        client=client,
        providers=["knaben", "bitsearch"],
        fallback_providers=[],
    )

    merged = result.candidates[0].candidate
    assert merged.source_count == 2
    assert merged.seeders == 95
    assert merged.verified is True
    assert merged.virus_score == pytest.approx(0.01)


def test_prowlarr_results_map_to_the_same_candidate_shape() -> None:
    class ProwlarrClient:
        def get(self, url, params=None, headers=None, **kwargs):
            assert url == "http://prowlarr:9696/api/v1/search"
            assert headers == {"X-Api-Key": "secret"}
            return FakeResponse(
                [
                    {
                        "title": "The Great Gatsby (2013) 1080p BluRay",
                        "size": 2 * GB,
                        "seeders": 40,
                        "leechers": 3,
                        "infoHash": "a" * 40,
                        "magnetUrl": f"magnet:?xt=urn:btih:{'a' * 40}",
                        "indexer": "1337x",
                        "categories": [{"id": 2000, "name": "Movies"}],
                        "publishDate": "2026-01-01",
                    }
                ]
            )

    found = discovery.search_prowlarr(
        settings(prowlarr_url="http://prowlarr:9696/", prowlarr_api_key="secret"),
        "the great gatsby",
        client=ProwlarrClient(),
        limit=5,
    )

    assert len(found) == 1
    item = found[0]
    assert item.tracker == "1337x"
    assert item.category == "Movies"
    assert item.magnet.startswith("magnet:?xt=urn:btih:")
    assert item.sources == ("prowlarr:1337x",)


def test_apibay_maps_pirate_bay_rows() -> None:
    row = {
        "id": "1",
        "name": "The Great Gatsby (2013) 1080p BrRip x264 - YIFY",
        "info_hash": "A" * 40,
        "seeders": "80",
        "leechers": "4",
        "size": "2191939752",
        "status": "vip",
        "category": "207",
    }
    found = discovery.search_apibay("the great gatsby", client=FakeClient(apibay=[row]), limit=5)

    assert len(found) == 1
    item = found[0]
    assert item.infohash == "a" * 40
    assert item.tracker == "The Pirate Bay"
    assert item.category == "Video"
    assert item.seeders == 80
    assert item.verified is True
    assert item.magnet.startswith("magnet:?xt=urn:btih:")


def test_nyaa_rss_maps_size_hash_and_trusted() -> None:
    rss = f"""<rss xmlns:nyaa="{discovery.NYAA_NAMESPACE}">
      <channel><item>
        <title>Frieren 1080p</title>
        <link>https://nyaa.si/download/1.torrent</link>
        <pubDate>Fri, 30 Oct 2026 15:32:00 -0000</pubDate>
        <nyaa:seeders>6</nyaa:seeders>
        <nyaa:leechers>1</nyaa:leechers>
        <nyaa:infoHash>{"b" * 40}</nyaa:infoHash>
        <nyaa:category>Anime - English-translated</nyaa:category>
        <nyaa:size>7.6 GiB</nyaa:size>
        <nyaa:trusted>Yes</nyaa:trusted>
      </item></channel>
    </rss>"""
    found = discovery.search_nyaa("frieren", client=FakeClient(nyaa=rss), limit=5)

    assert len(found) == 1
    item = found[0]
    assert item.infohash == "b" * 40
    assert item.size_bytes > 7 * GB
    assert item.seeders == 6
    assert item.verified is True
    assert item.category.startswith("Anime")
    assert item.magnet.startswith("magnet:?xt=urn:btih:")


def test_jackett_torznab_maps_attributes() -> None:
    torznab = f"""<rss xmlns:torznab="{discovery.TORZNAB_NAMESPACE}">
      <channel><item>
        <title>The Great Gatsby 2013 1080p</title>
        <link>http://jackett:9117/dl/1</link>
        <size>2.2 GB</size>
        <torznab:attr name="seeders" value="80"/>
        <torznab:attr name="peers" value="5"/>
        <torznab:attr name="infohash" value="{"c" * 40}"/>
        <torznab:attr name="category" value="Movies"/>
        <torznab:attr name="magneturl" value="magnet:?xt=urn:btih:{"c" * 40}"/>
      </item></channel>
    </rss>"""
    configured = settings(
        jackett_url="http://jackett:9117",
        jackett_api_key="secret",
        jackett_indexers="1337x",
    )
    found = discovery.search_jackett(
        configured,
        "the great gatsby",
        client=FakeClient(jackett=torznab),
        limit=5,
    )

    assert len(found) == 1
    item = found[0]
    assert item.tracker == "1337x"
    assert item.seeders == 80
    assert item.infohash == "c" * 40
    assert item.magnet.startswith("magnet:?xt=urn:btih:")
    assert item.sources == ("jackett:1337x",)
    assert "jackett" in discovery._configured_providers(configured)


def test_discover_falls_back_to_the_archive_when_primary_is_empty() -> None:
    client = FakeClient(
        archive={
            "response": {
                "docs": [
                    {
                        "identifier": "gatsby-1926",
                        "title": "The Great Gatsby",
                        "year": 1926,
                        "item_size": GB,
                    }
                ]
            }
        },
        metadata={"gatsby-1926": {"files": [{"name": "gatsby-1926_archive.torrent"}]}},
    )

    result = discover(
        settings(),
        "the great gatsby",
        client=client,
        providers=["knaben", "bitsearch"],
        fallback_providers=["internetarchive"],
    )

    assert result.used_fallback is True
    archive = result.candidates[0]
    assert archive.candidate.provider == "internetarchive"
    assert archive.candidate.url.endswith("gatsby-1926_archive.torrent")
    assert choose_best(result, min_score=55.0) is archive
