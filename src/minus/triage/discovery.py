"""Find a torrent worth handing to the quarantine pipeline.

A query is not a download. This module turns one into a ranked shortlist from
cheap meta-search APIs, then stops: the harness picks the best candidate and
sends its magnet/URL through the same add -> wait -> quarantine -> scan path a
typed magnet uses.

The ranking is deliberately a *pre-filter*, not a trust boundary. It rejects
the obvious failures (CAM rips, executables dressed as films, dead swarms,
files larger than the staging cap) and prefers seeded 1080p releases with
cross-source agreement, low virus signal and a verified flag. It cannot prove
anything is safe; only `triage/scoring.py` can label a file clean, and only
after a detonation.

The default providers are the reachable public APIs:

  * Knaben is a meta-index with a JSON API that already includes 1337x and
    The Pirate Bay results, so MINUS does not have to defeat Cloudflare itself.
  * Bitsearch is a second, independent index. A matching infohash on both is
    evidence of a real release, not of safe content.
  * The Pirate Bay's `apibay.org` JSON endpoint and Nyaa's RSS feed add two
    more independent sources without HTML scraping.
  * Internet Archive is only a fallback: it is legal and verifiable, but its
    catalog is not a general movie index.
  * Prowlarr and Jackett are used when configured. They own the per-site
    definitions, including 1337x and YourBittorrent, so every site they support
    becomes searchable without a scraper in this module.

Direct scraping of 1337x and YourBittorrent is not here on purpose: both answer
plain HTTP with a Cloudflare interstitial, and Demonoid is account-based and has
no live public API. `jackett_indexers` can target any configured Jackett
definition. Deprecated sites (RARBG, old Demonoid clones, dead meta-searches)
are not added. A provider that cannot be fetched is a bug, not a feature.
"""

from __future__ import annotations

import logging
import math
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, quote_plus

import httpx

logger = logging.getLogger(__name__)

KNABEN_ENDPOINT = "https://api.knaben.org/v1"
BITSEARCH_ENDPOINT = "https://bitsearch.eu/api/v1/search"
APIBAY_ENDPOINT = "https://apibay.org/q.php"
NYAA_ENDPOINT = "https://nyaa.si/"
JACKETT_SEARCH_ENDPOINT = "{base}/api/v2.0/indexers/{indexer}/results/torznab/api"
TORZNAB_NAMESPACE = "http://torznab.com/schemas/2015/feed"
NYAA_NAMESPACE = "https://nyaa.si/xmlns/nyaa"
ARCHIVE_SEARCH_ENDPOINT = "https://archive.org/advancedsearch.php"
ARCHIVE_METADATA_ENDPOINT = "https://archive.org/metadata/{identifier}"
ARCHIVE_DOWNLOAD_ENDPOINT = "https://archive.org/download/{identifier}/{name}"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140 Safari/537.36"
)

MB = 1024**2
GB = 1024**3

_HASH = re.compile(r"\A[0-9a-fA-F]{40}\Z")
_MAGNET_HASH = re.compile(r"xt=urn:btih:([0-9a-fA-F]{40})", re.IGNORECASE)
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_UNSAFE_EXT = re.compile(r"\.(?:exe|msi|bat|cmd|ps1|vbs|js|scr|lnk|dll|jar)\b", re.IGNORECASE)
_UNSAFE_WORDS = re.compile(
    r"\b(?:crack|keygen|activator|passworded|password[ ._-]?protected)\b", re.IGNORECASE
)
_CAM = re.compile(
    r"\b(?:cam|hdcam|ts|hdts|telesync|telecine|screener|scr)\b",
    re.IGNORECASE,
)

# Words that describe the request rather than the title. Keeping them out of
# the coverage calculation stops "the great gatsby 1080p" from demanding that
# every release put "1080p" in its name.
_STOPWORDS = {
    "a",
    "an",
    "and",
    "bluray",
    "blu",
    "ray",
    "download",
    "dl",
    "dvd",
    "film",
    "for",
    "hd",
    "in",
    "movie",
    "of",
    "on",
    "the",
    "to",
    "torrent",
    "uhd",
    "web",
    "webrip",
    "with",
    "x264",
    "x265",
    "hevc",
    "aac",
    "ac3",
    "dts",
    "hdr",
    "4k",
    "2160p",
    "1080p",
    "720p",
    "480p",
}

_MOVIE_HINTS = {
    "movie",
    "film",
    "cinema",
    "1080p",
    "720p",
    "2160p",
    "4k",
    "uhd",
    "bluray",
    "webrip",
    "x264",
    "x265",
    "hevc",
}
_AUDIO_HINTS = {"album", "music", "mp3", "flac", "ost", "soundtrack", "audiobook"}
_BOOK_HINTS = {"book", "ebook", "epub", "pdf", "mobi", "azw3", "novel"}
_SOFTWARE_HINTS = {"linux", "ubuntu", "debian", "fedora", "iso", "distro", "windows", "software"}


@dataclass(frozen=True)
class TorrentCandidate:
    """One normalized search result, before ranking."""

    title: str
    provider: str
    tracker: str = ""
    category: str = ""
    size_bytes: int = 0
    seeders: int = 0
    leechers: int = 0
    infohash: str = ""
    magnet: str = ""
    url: str = ""
    verified: bool = False
    virus_score: float | None = None
    published: str = ""
    sources: tuple[str, ...] = ()

    @property
    def download_url(self) -> str:
        return self.magnet or self.url

    @property
    def source_count(self) -> int:
        return len(self.sources or (self.provider,))


@dataclass(frozen=True)
class ScoredCandidate:
    """A candidate plus the reasons its score came out the way it did."""

    candidate: TorrentCandidate
    score: float
    reasons: tuple[str, ...] = ()
    eligible: bool = True
    reject_reason: str = ""

    def describe(self) -> str:
        item = self.candidate
        return (
            f"{item.title} · {self.score:.0f}/100 · {item.seeders} seeds · "
            f"{human_size(item.size_bytes)} · {item.tracker or item.provider}"
        )


@dataclass(frozen=True)
class DiscoveryResult:
    """The ranked shortlist, plus per-provider failures worth surfacing."""

    query: str
    kind: str
    candidates: tuple[ScoredCandidate, ...] = ()
    errors: tuple[str, ...] = ()
    used_fallback: bool = False

    def summary_lines(self, limit: int = 3) -> list[str]:
        lines = [f"search · {self.query} · {self.kind} · {len(self.candidates)} candidate(s)"]
        for scored in self.candidates[:limit]:
            state = "" if scored.eligible else f" · rejected: {scored.reject_reason}"
            lines.append(f"  {scored.describe()}{state}")
        if self.errors:
            lines.append("  providers: " + "; ".join(self.errors[:3]))
        if self.used_fallback:
            lines.append("  fallback: internet archive")
        return lines


# ---- Small parsing helpers -------------------------------------------------


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_size(text: Any) -> int:
    """Turn Nyaa/Torznab size text such as "7.6 GiB" into bytes."""
    raw = str(text or "").strip().replace(",", "")
    match = re.match(r"([0-9]+(?:\.[0-9]+)?)\s*([KMGTPE]?i?B)?", raw, re.IGNORECASE)
    if not match:
        return 0
    number = float(match.group(1))
    unit = (match.group(2) or "B").upper()
    factors = {
        "B": 1,
        "KB": 1000,
        "KIB": 1024,
        "MB": 1000**2,
        "MIB": 1024**2,
        "GB": 1000**3,
        "GIB": 1024**3,
        "TB": 1000**4,
        "TIB": 1024**4,
        "PB": 1000**5,
        "PIB": 1024**5,
    }
    return int(number * factors.get(unit, 1))


def _tpb_category(value: Any) -> str:
    code = str(value or "").strip()
    if code.startswith("1"):
        return "Audio"
    if code.startswith("2"):
        return "Video"
    if code.startswith("3"):
        return "Software"
    if code.startswith("4"):
        return "Games"
    if code.startswith("5"):
        return "Video"
    return "Other"


def _xml_text(element: Any) -> str:
    return (element.text or "").strip() if element is not None else ""


def _normalise(text: Any) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(text or "").casefold()))


def _words(text: Any) -> list[str]:
    return re.findall(r"[a-z0-9]+", str(text or "").casefold())


def _query_tokens(query: str) -> set[str]:
    return {word for word in _words(query) if word not in _STOPWORDS}


def _year(text: str) -> int | None:
    match = _YEAR.search(text or "")
    return int(match.group(0)) if match else None


def _query_year(query: str) -> int | None:
    return _year(query)


def _hash_of(value: Any) -> str:
    text = str(value or "").strip()
    if _HASH.match(text):
        return text.lower()
    match = _MAGNET_HASH.search(text)
    return match.group(1).lower() if match else ""


def _magnet_for(infohash: str, title: str) -> str:
    if not infohash:
        return ""
    return f"magnet:?xt=urn:btih:{infohash}&dn={quote_plus(title, safe='')}"


def human_size(size: int) -> str:
    value = float(size or 0)
    if value <= 0:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def infer_kind(query: str) -> str:
    words = set(_words(query))
    if words & _MOVIE_HINTS:
        return "movie"
    if words & _AUDIO_HINTS:
        return "audio"
    if words & _BOOK_HINTS:
        return "book"
    if words & _SOFTWARE_HINTS:
        return "software"
    return "any"


def _kind_from_category(category: str) -> str:
    text = _normalise(category)
    if not text:
        return ""
    if "audiobook" in text or "audio" in text or "music" in text:
        return "audio"
    if "book" in text or "ebook" in text or "text" in text:
        return "book"
    if "movie" in text or "video" in text or "tv" in text or "anime" in text:
        return "movie"
    if "software" in text or "app" in text or "game" in text:
        return "software"
    return ""


def _compatible(kind: str, candidate_kind: str) -> bool:
    if kind == "any" or not candidate_kind:
        return True
    if kind == candidate_kind:
        return True
    return {kind, candidate_kind} <= {"movie", "video"}


def _resolution(title: str) -> int:
    text = _normalise(title)
    if "2160p" in text:
        return 2160
    if "1080p" in text:
        return 1080
    if "720p" in text:
        return 720
    if "480p" in text:
        return 480
    if "4k" in text or "uhd" in text:
        return 2160
    return 0


def _preferred_resolution(value: str) -> int:
    match = re.search(r"(\d{3,4})", value or "")
    return int(match.group(1)) if match else 0


# ---- Providers -------------------------------------------------------------


def search_knaben(query: str, *, client: Any, limit: int = 25) -> list[TorrentCandidate]:
    payload = {
        "search_type": "100%",
        "search_field": "title",
        "query": query,
        "order_by": "seeders",
        "order_direction": "desc",
        "size": max(1, min(300, limit)),
        "hide_unsafe": True,
        "hide_xxx": True,
    }
    response = client.post(KNABEN_ENDPOINT, json=payload)
    response.raise_for_status()
    data = response.json()
    hits = data.get("hits") if isinstance(data, dict) else None
    results: list[TorrentCandidate] = []
    for hit in hits or []:
        if not isinstance(hit, dict):
            continue
        title = str(hit.get("title") or "").strip()
        infohash = _hash_of(hit.get("hash")) or _hash_of(hit.get("magnetUrl"))
        magnet = str(hit.get("magnetUrl") or "").strip()
        if not magnet and infohash:
            magnet = _magnet_for(infohash, title)
        url = str(hit.get("link") or hit.get("details") or "").strip()
        if not magnet and not url:
            continue
        results.append(
            TorrentCandidate(
                title=title,
                provider="knaben",
                tracker=str(hit.get("tracker") or ""),
                category=str(hit.get("category") or ""),
                size_bytes=_as_int(hit.get("bytes")),
                seeders=_as_int(hit.get("seeders")),
                leechers=_as_int(hit.get("peers")),
                infohash=infohash,
                magnet=magnet,
                url=url,
                virus_score=_as_float(hit.get("virusDetection")),
                published=str(hit.get("date") or ""),
                sources=("knaben",),
            )
        )
    return results


def search_bitsearch(query: str, *, client: Any, limit: int = 25) -> list[TorrentCandidate]:
    response = client.get(BITSEARCH_ENDPOINT, params={"q": query})
    response.raise_for_status()
    data = response.json()
    rows = data.get("results") if isinstance(data, dict) else data
    results: list[TorrentCandidate] = []
    for row in (rows or [])[: max(1, limit)]:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        infohash = _hash_of(row.get("infohash"))
        if not infohash:
            continue
        results.append(
            TorrentCandidate(
                title=title,
                provider="bitsearch",
                tracker="bitsearch",
                category=str(row.get("category") or ""),
                size_bytes=_as_int(row.get("size")),
                seeders=_as_int(row.get("seeders")),
                leechers=_as_int(row.get("leechers")),
                infohash=infohash,
                magnet=_magnet_for(infohash, title),
                verified=bool(row.get("verified")),
                sources=("bitsearch",),
            )
        )
    return results


def search_apibay(query: str, *, client: Any, limit: int = 25) -> list[TorrentCandidate]:
    """The Pirate Bay's public JSON endpoint. No API key, no HTML parsing."""
    response = client.get(APIBAY_ENDPOINT, params={"q": query})
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list):
        return []
    results: list[TorrentCandidate] = []
    for row in rows[: max(1, limit)]:
        if not isinstance(row, dict):
            continue
        title = str(row.get("name") or "").strip()
        infohash = _hash_of(row.get("info_hash"))
        if not title or not infohash or str(row.get("id") or "") == "0":
            continue
        results.append(
            TorrentCandidate(
                title=title,
                provider="apibay",
                tracker="The Pirate Bay",
                category=_tpb_category(row.get("category")),
                size_bytes=_as_int(row.get("size")),
                seeders=_as_int(row.get("seeders")),
                leechers=_as_int(row.get("leechers")),
                infohash=infohash,
                magnet=_magnet_for(infohash, title),
                verified=str(row.get("status") or "").lower() in ("vip", "trusted"),
                published=str(row.get("added") or ""),
                sources=("apibay",),
            )
        )
    return results


def search_nyaa(query: str, *, client: Any, limit: int = 25) -> list[TorrentCandidate]:
    """Nyaa's RSS feed: public, stable, and already carries seeders/hash/size."""
    response = client.get(
        NYAA_ENDPOINT,
        params={"page": "rss", "q": query, "c": "0_0", "f": "0"},
    )
    response.raise_for_status()
    root = ET.fromstring(response.text)
    results: list[TorrentCandidate] = []
    ns = {"nyaa": NYAA_NAMESPACE}
    for item in root.findall(".//item")[: max(1, limit)]:
        title = _xml_text(item.find("title"))
        link = _xml_text(item.find("link"))
        infohash = _hash_of(_xml_text(item.find("nyaa:infoHash", ns)))
        if not title:
            continue
        category = _xml_text(item.find("nyaa:category", ns))
        magnet = _magnet_for(infohash, title)
        url = "" if magnet else link
        if not magnet and not url:
            continue
        results.append(
            TorrentCandidate(
                title=title,
                provider="nyaa",
                tracker="Nyaa",
                category=category,
                size_bytes=_parse_size(_xml_text(item.find("nyaa:size", ns))),
                seeders=_as_int(_xml_text(item.find("nyaa:seeders", ns))),
                leechers=_as_int(_xml_text(item.find("nyaa:leechers", ns))),
                infohash=infohash,
                magnet=magnet,
                url=url,
                verified=_xml_text(item.find("nyaa:trusted", ns)).lower() == "yes",
                published=_xml_text(item.find("pubDate")),
                sources=("nyaa",),
            )
        )
    return results


def search_jackett(
    settings: Any, query: str, *, client: Any, limit: int = 25
) -> list[TorrentCandidate]:
    """Search every configured Jackett indexer through its Torznab endpoint.

    Jackett owns the per-site definitions (including 1337x and YourBittorrent).
    MINUS only speaks Torznab, so adding a site is a Jackett configuration
    change, not a new scraper.
    """
    base = str(getattr(settings, "jackett_url", "") or "").rstrip("/")
    key = str(getattr(settings, "jackett_api_key", "") or "")
    if not base or not key:
        return []
    indexers = _names(getattr(settings, "jackett_indexers", ""), "all")
    results: list[TorrentCandidate] = []
    for indexer in indexers:
        response = client.get(
            JACKETT_SEARCH_ENDPOINT.format(base=base, indexer=indexer),
            params={"apikey": key, "t": "search", "q": query},
        )
        response.raise_for_status()
        root = ET.fromstring(response.text)
        for item in root.findall(".//item")[: max(1, limit)]:
            title = _xml_text(item.find("title"))
            link = _xml_text(item.find("link"))
            guid = _xml_text(item.find("guid"))
            attrs = {
                str(attr.get("name") or ""): str(attr.get("value") or "")
                for attr in item.findall(f"{{{TORZNAB_NAMESPACE}}}attr")
            }
            infohash = _hash_of(attrs.get("infohash") or attrs.get("magneturl") or link or guid)
            magnet = attrs.get("magneturl") or _magnet_for(infohash, title)
            url = "" if magnet else (link or guid)
            if not title or (not magnet and not url):
                continue
            results.append(
                TorrentCandidate(
                    title=title,
                    provider="jackett",
                    tracker=indexer,
                    category=attrs.get("category", ""),
                    size_bytes=_parse_size(_xml_text(item.find("size"))),
                    seeders=_as_int(attrs.get("seeders")),
                    leechers=_as_int(attrs.get("peers")),
                    infohash=infohash,
                    magnet=magnet,
                    url=url,
                    published=_xml_text(item.find("pubDate")),
                    sources=(f"jackett:{indexer}",),
                )
            )
    return results


def search_prowlarr(
    settings: Any, query: str, *, client: Any, limit: int = 25
) -> list[TorrentCandidate]:
    base = str(getattr(settings, "prowlarr_url", "") or "").rstrip("/")
    key = str(getattr(settings, "prowlarr_api_key", "") or "")
    if not base or not key:
        return []
    response = client.get(
        f"{base}/api/v1/search",
        params={"query": query, "indexerIds": -2, "type": "search"},
        headers={"X-Api-Key": key},
    )
    response.raise_for_status()
    data = response.json()
    rows = data.get("results") if isinstance(data, dict) else data
    results: list[TorrentCandidate] = []
    for row in (rows or [])[: max(1, limit)]:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        infohash = _hash_of(row.get("infoHash"))
        magnet = str(row.get("magnetUrl") or "").strip()
        if not magnet and infohash:
            magnet = _magnet_for(infohash, title)
        download = str(row.get("downloadUrl") or "").strip()
        if download.startswith("/"):
            download = base + download
        if not magnet and not download:
            continue
        categories = row.get("categories") or []
        category = ", ".join(
            str(item.get("name") or item.get("id") or "")
            for item in categories
            if isinstance(item, dict)
        )
        tracker = str(row.get("indexer") or "Prowlarr")
        results.append(
            TorrentCandidate(
                title=title,
                provider="prowlarr",
                tracker=tracker,
                category=category,
                size_bytes=_as_int(row.get("size")),
                seeders=_as_int(row.get("seeders")),
                leechers=_as_int(row.get("leechers")),
                infohash=infohash,
                magnet=magnet,
                url=download,
                published=str(row.get("publishDate") or ""),
                sources=(f"prowlarr:{tracker}",),
            )
        )
    return results


def _archive_mediatype(kind: str) -> str:
    return {
        "movie": "movies",
        "book": "texts",
        "audio": "audio",
        "software": "software",
    }.get(kind, "")


def search_internetarchive(
    query: str, *, client: Any, kind: str = "any", limit: int = 5
) -> list[TorrentCandidate]:
    escaped = query.replace("\\", "\\\\").replace('"', '\\"')
    archive_query = f'title:("{escaped}")'
    mediatype = _archive_mediatype(kind)
    if mediatype:
        archive_query += f" AND mediatype:({mediatype})"
    params: list[tuple[str, str]] = [
        ("q", archive_query),
        ("fl[]", "identifier"),
        ("fl[]", "title"),
        ("fl[]", "year"),
        ("fl[]", "item_size"),
        ("rows", str(max(1, min(10, limit)))),
        ("output", "json"),
    ]
    response = client.get(ARCHIVE_SEARCH_ENDPOINT, params=params)
    response.raise_for_status()
    data = response.json()
    docs = ((data.get("response") or {}).get("docs") or []) if isinstance(data, dict) else []
    results: list[TorrentCandidate] = []
    for doc in docs[: max(1, limit)]:
        if not isinstance(doc, dict):
            continue
        identifier = str(doc.get("identifier") or "").strip()
        if not identifier:
            continue
        try:
            meta_response = client.get(
                ARCHIVE_METADATA_ENDPOINT.format(identifier=quote(identifier, safe=""))
            )
            meta_response.raise_for_status()
            meta = meta_response.json()
        except Exception as exc:  # one bad item must not hide the rest
            logger.debug("Archive metadata failed for %s: %s", identifier, exc)
            continue
        files = meta.get("files") if isinstance(meta, dict) else None
        torrent_name = ""
        for file in files or []:
            if not isinstance(file, dict):
                continue
            name = str(file.get("name") or "")
            if name.endswith(".torrent"):
                torrent_name = name
                break
        if not torrent_name:
            continue
        title = str(doc.get("title") or identifier)
        torrent_url = ARCHIVE_DOWNLOAD_ENDPOINT.format(
            identifier=quote(identifier, safe=""),
            name=quote(torrent_name, safe=""),
        )
        results.append(
            TorrentCandidate(
                title=title,
                provider="internetarchive",
                tracker="Internet Archive",
                category=mediatype or "archive",
                size_bytes=_as_int(doc.get("item_size")),
                infohash="",
                url=torrent_url,
                verified=True,
                published=str(doc.get("year") or ""),
                sources=("internetarchive",),
            )
        )
    return results


# ---- Ranking --------------------------------------------------------------


def _reject(
    candidate: TorrentCandidate, score: float, reason: str, reasons: list[str] | None = None
) -> ScoredCandidate:
    merged = list(reasons or [])
    merged.append(reason)
    return ScoredCandidate(
        candidate=candidate,
        score=round(max(0.0, min(100.0, score)), 1),
        reasons=tuple(merged[:8]),
        eligible=False,
        reject_reason=reason,
    )


def _size_penalty(kind: str, resolution: int, size: int) -> tuple[int, str]:
    if size <= 0:
        return 8, "size unknown"
    if kind == "movie":
        if size < 100 * MB:
            return 30, "too small for a movie"
        if resolution >= 2160:
            low, high = 2 * GB, 60 * GB
        elif resolution >= 1080:
            low, high = 500 * MB, 15 * GB
        elif resolution >= 720:
            low, high = 200 * MB, 5 * GB
        else:
            low, high = 100 * MB, 20 * GB
    elif kind == "book":
        low, high = 10 * 1024, 500 * MB
    elif kind == "audio":
        low, high = 5 * MB, 3 * GB
    elif kind == "software":
        low, high = 10 * MB, 20 * GB
    else:
        low, high = MB, 20 * GB

    if size < low:
        return (20 if size < low / 2 else 6), "small for its category"
    if size > high:
        return (20 if size > high * 2 else 6), "large for its category"
    return 0, ""


def _score(
    candidate: TorrentCandidate,
    *,
    query_tokens: set[str],
    query_text: str,
    query_year: int | None,
    kind: str,
    preferred_quality: str,
    max_bytes: int,
) -> ScoredCandidate:
    title = candidate.title
    title_tokens = set(_words(title))
    coverage = len(query_tokens & title_tokens) / len(query_tokens) if query_tokens else 0.0
    score = coverage * 20.0
    reasons: list[str] = []
    if coverage < 0.55:
        return _reject(candidate, score, f"title match {coverage:.0%}")

    normalized = _normalise(title)
    if query_text and query_text in normalized:
        score += 6
        reasons.append("exact title")

    if _UNSAFE_EXT.search(title) or _UNSAFE_WORDS.search(title):
        return _reject(candidate, 0, "executable or crack-style title")
    if kind != "any":
        candidate_kind = _kind_from_category(candidate.category)
        if candidate_kind and not _compatible(kind, candidate_kind):
            return _reject(candidate, score, f"category {candidate.category!r} is not {kind}")
    candidate_year = _year(title)
    if query_year and candidate_year and candidate_year != query_year:
        return _reject(candidate, score, f"year {candidate_year} does not match {query_year}")
    if _CAM.search(title):
        return _reject(candidate, 0, "CAM/TS/screener release")
    # Internet Archive torrents are web-seeded and often report no swarm at
    # all; the fallback is deliberately allowed through, and still scanned.
    sources = candidate.sources or (candidate.provider,)
    if candidate.seeders < 2 and "internetarchive" not in sources:
        return _reject(candidate, score, f"{candidate.seeders} seeders")
    if max_bytes and candidate.size_bytes > max_bytes:
        return _reject(candidate, score, "larger than MINUS_TRIAGE_MAX_BYTES")

    resolution = _resolution(title)
    score += {2160: 12, 1080: 12, 720: 8, 480: 4}.get(resolution, 0)
    if resolution:
        reasons.append(f"{resolution}p")
    preferred = _preferred_resolution(preferred_quality)
    if preferred and resolution == preferred:
        score += 3
        reasons.append(f"preferred {preferred}p")

    if "remux" in normalized:
        score += 3
        reasons.append("remux")
    elif "bluray" in normalized or "blu ray" in normalized:
        score += 5
        reasons.append("bluray")
    elif "web dl" in normalized or "webdl" in normalized:
        score += 5
        reasons.append("web-dl")
    elif "webrip" in normalized:
        score += 3
        reasons.append("webrip")
    elif "brrip" in normalized or "bdrip" in normalized:
        score += 2
        reasons.append("brrip")
    if any(codec in normalized for codec in ("x265", "hevc", "x264")):
        score += 1

    # Demand is the best available proxy for "what the requester wants" when
    # the query names no year: the 2013 Gatsby has twice the swarm of the 1974
    # version, and a seed count should be able to outweigh a better source tag.
    score += min(18.0, math.log1p(max(candidate.seeders, 0)) * 4.0)
    reasons.append(f"{candidate.seeders} seeders")

    if candidate.verified:
        score += 6
        reasons.append("verified")
    if candidate.virus_score is not None:
        if candidate.virus_score <= 0.25:
            score += 3
            reasons.append("low virus signal")
        elif candidate.virus_score <= 0.5:
            score -= 2
            reasons.append("virus signal")
        else:
            score -= 12
            reasons.append("high virus signal")

    if candidate.source_count >= 3:
        score += 12
        reasons.append(f"{candidate.source_count} sources")
    elif candidate.source_count == 2:
        score += 6
        reasons.append("2 sources")

    tracker = candidate.tracker.casefold()
    if "internet archive" in tracker:
        score += 15
        reasons.append("legal archive")
    elif "1337x" in tracker:
        score += 2
    elif tracker:
        score += 1

    if candidate_year:
        if candidate_year >= 2010:
            score += 4
            reasons.append(f"{candidate_year}")
        elif candidate_year >= 2000:
            score += 2
            reasons.append(f"{candidate_year}")
        elif candidate_year >= 1990:
            score += 1

    penalty, size_reason = _size_penalty(kind, resolution, candidate.size_bytes)
    if penalty:
        score -= penalty
        reasons.append(size_reason)

    return ScoredCandidate(
        candidate=candidate,
        score=round(max(0.0, min(100.0, score)), 1),
        reasons=tuple(reasons[:8]),
    )


def rank_candidates(
    query: str,
    candidates: Iterable[TorrentCandidate],
    *,
    kind: str | None = None,
    preferred_quality: str = "1080p",
    max_bytes: int = 0,
) -> list[ScoredCandidate]:
    """Score and order candidates; ineligible rows sort after eligible ones."""
    resolved_kind = kind or infer_kind(query)
    tokens = _query_tokens(query)
    text = " ".join(tokens)
    year = _query_year(query)
    scored = [
        _score(
            candidate,
            query_tokens=tokens,
            query_text=text,
            query_year=year,
            kind=resolved_kind,
            preferred_quality=preferred_quality,
            max_bytes=max_bytes,
        )
        for candidate in candidates
    ]
    scored.sort(
        key=lambda item: (
            not item.eligible,
            -item.score,
            -item.candidate.seeders,
            item.candidate.title.casefold(),
        )
    )
    return scored


def choose_best(result: DiscoveryResult, *, min_score: float = 55.0) -> ScoredCandidate | None:
    """The first eligible candidate that clears the score, or None.

    A verified Internet Archive item is the one exception: it has no swarm to
    score, so it is accepted as the fallback when no tracker result clears the
    threshold. It still goes through the same quarantine and verdict rules.
    """
    archive: ScoredCandidate | None = None
    for scored in result.candidates:
        if not scored.eligible:
            continue
        if scored.score >= min_score:
            return scored
        if "internetarchive" in scored.candidate.sources and archive is None:
            archive = scored
    return archive


# ---- Orchestration --------------------------------------------------------


def _names(value: Any, default: str) -> list[str]:
    raw = str(value if value is not None else default)
    return [name.strip().lower() for name in raw.split(",") if name.strip()]


def _configured_providers(settings: Any) -> list[str]:
    names = _names(
        getattr(settings, "discovery_providers", ""),
        "knaben,bitsearch,apibay,nyaa",
    )
    if "auto" in names:
        names = [name for name in names if name != "auto"] + [
            "knaben",
            "bitsearch",
            "apibay",
            "nyaa",
        ]
    if (
        getattr(settings, "prowlarr_url", "")
        and getattr(settings, "prowlarr_api_key", "")
        and "prowlarr" not in names
    ):
        names.insert(0, "prowlarr")
    if (
        getattr(settings, "jackett_url", "")
        and getattr(settings, "jackett_api_key", "")
        and "jackett" not in names
    ):
        names.insert(0, "jackett")
    return names


def _collect(
    names: Iterable[str],
    settings: Any,
    query: str,
    *,
    kind: str,
    client: Any,
    limit: int,
) -> tuple[list[TorrentCandidate], list[str]]:
    found: list[TorrentCandidate] = []
    errors: list[str] = []
    for name in names:
        name = name.strip().lower()
        if not name:
            continue
        try:
            if name == "knaben":
                found.extend(search_knaben(query, client=client, limit=limit))
            elif name == "bitsearch":
                found.extend(search_bitsearch(query, client=client, limit=limit))
            elif name == "apibay":
                found.extend(search_apibay(query, client=client, limit=limit))
            elif name == "nyaa":
                found.extend(search_nyaa(query, client=client, limit=limit))
            elif name == "jackett":
                if not (
                    getattr(settings, "jackett_url", "")
                    and getattr(settings, "jackett_api_key", "")
                ):
                    errors.append("jackett: not configured")
                    continue
                found.extend(search_jackett(settings, query, client=client, limit=limit))
            elif name == "prowlarr":
                if not (
                    getattr(settings, "prowlarr_url", "")
                    and getattr(settings, "prowlarr_api_key", "")
                ):
                    errors.append("prowlarr: not configured")
                    continue
                found.extend(search_prowlarr(settings, query, client=client, limit=limit))
            elif name == "internetarchive":
                found.extend(
                    search_internetarchive(query, client=client, kind=kind, limit=min(5, limit))
                )
            else:
                errors.append(f"{name}: unknown provider")
        except Exception as exc:  # a provider failure is one missing source
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            logger.debug("Discovery provider %s failed", name, exc_info=True)
    return found, errors


def _merge(candidates: Iterable[TorrentCandidate]) -> list[TorrentCandidate]:
    """Deduplicate by infohash (or title+size), merging the trust signals."""
    merged: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        key = (
            candidate.infohash.lower()
            if candidate.infohash
            else f"{_normalise(candidate.title)}|{candidate.size_bytes}"
        )
        current = merged.get(key)
        if current is None:
            merged[key] = {
                "title": candidate.title,
                "provider": candidate.provider,
                "tracker": candidate.tracker,
                "category": candidate.category,
                "size_bytes": candidate.size_bytes,
                "seeders": candidate.seeders,
                "leechers": candidate.leechers,
                "infohash": candidate.infohash.lower(),
                "magnet": candidate.magnet,
                "url": candidate.url,
                "verified": candidate.verified,
                "virus_score": candidate.virus_score,
                "published": candidate.published,
                "sources": set(candidate.sources or (candidate.provider,)),
            }
            continue
        current["seeders"] = max(current["seeders"], candidate.seeders)
        current["leechers"] = max(current["leechers"], candidate.leechers)
        current["size_bytes"] = max(current["size_bytes"], candidate.size_bytes)
        current["verified"] = bool(current["verified"] or candidate.verified)
        if candidate.virus_score is not None and (
            current["virus_score"] is None or candidate.virus_score < current["virus_score"]
        ):
            current["virus_score"] = candidate.virus_score
        if not current["magnet"] and candidate.magnet:
            current["magnet"] = candidate.magnet
        if not current["url"] and candidate.url:
            current["url"] = candidate.url
        if not current["tracker"] and candidate.tracker:
            current["tracker"] = candidate.tracker
        if not current["category"] and candidate.category:
            current["category"] = candidate.category
        current["sources"].update(candidate.sources or (candidate.provider,))

    return [
        TorrentCandidate(
            title=item["title"],
            provider=item["provider"],
            tracker=item["tracker"],
            category=item["category"],
            size_bytes=item["size_bytes"],
            seeders=item["seeders"],
            leechers=item["leechers"],
            infohash=item["infohash"],
            magnet=item["magnet"],
            url=item["url"],
            verified=item["verified"],
            virus_score=item["virus_score"],
            published=item["published"],
            sources=tuple(sorted(item["sources"])),
        )
        for item in merged.values()
    ]


def discover(
    settings: Any,
    query: str,
    *,
    limit: int = 5,
    client: Any | None = None,
    providers: Iterable[str] | None = None,
    fallback_providers: Iterable[str] | None = None,
) -> DiscoveryResult:
    """Query the configured providers, merge by infohash, and rank once.

    Provider failures are collected rather than raised: one index being down
    must not make the others useless. A fallback provider is only consulted
    when the primary set produced nothing eligible, so the legal archive does
    not pad every shortlist.
    """
    cleaned = " ".join(str(query or "").split())
    if not cleaned:
        return DiscoveryResult("", "any", errors=("empty query",))
    kind = infer_kind(cleaned)
    timeout = float(getattr(settings, "discovery_timeout", 12.0) or 12.0)
    provider_limit = max(limit, _as_int(getattr(settings, "discovery_limit", 20), 20))
    max_bytes = _as_int(getattr(settings, "triage_max_bytes", 0))
    preferred = str(getattr(settings, "discovery_preferred_quality", "1080p") or "")

    owns_client = client is None
    if client is None:
        client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
    try:
        primary = list(providers) if providers is not None else _configured_providers(settings)
        fallbacks = (
            list(fallback_providers)
            if fallback_providers is not None
            else _names(getattr(settings, "discovery_fallback_providers", ""), "")
        )

        found, errors = _collect(
            primary, settings, cleaned, kind=kind, client=client, limit=provider_limit
        )
        ranked = rank_candidates(
            cleaned,
            _merge(found),
            kind=kind,
            preferred_quality=preferred,
            max_bytes=max_bytes,
        )
        used_fallback = False
        if not any(scored.eligible for scored in ranked) and fallbacks:
            used_fallback = True
            more, more_errors = _collect(
                fallbacks, settings, cleaned, kind=kind, client=client, limit=limit
            )
            errors.extend(more_errors)
            ranked = rank_candidates(
                cleaned,
                _merge([*found, *more]),
                kind=kind,
                preferred_quality=preferred,
                max_bytes=max_bytes,
            )

        return DiscoveryResult(
            query=cleaned,
            kind=kind,
            candidates=tuple(ranked[: max(1, limit)]),
            errors=tuple(errors),
            used_fallback=used_fallback,
        )
    finally:
        if owns_client:
            client.close()
