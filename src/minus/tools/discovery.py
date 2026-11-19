"""Torrent search as a model-facing tool.

This is the search half the harness already had, exposed on the tool surface so
the model can find something and then drive it through the quarantine pipeline.
Ranking is a pre-filter, never a trust boundary: a high score only decides what
is worth downloading, and the pipeline's verdict still gates promotion.
"""

from __future__ import annotations

import logging
from typing import Any

from minus.errors import ToolExecutionError
from minus.triage.discovery import ScoredCandidate, choose_best, discover

logger = logging.getLogger(__name__)

CATEGORY = "triage"


class DiscoveryTools:
    """Search across the configured torrent indexes."""

    def __init__(self, settings: Any) -> None:
        self.settings = settings

    def register(self, registry: Any) -> None:
        registry.tool(self.torrent_search, category=CATEGORY)

    def torrent_search(self, query: str, limit: int = 5) -> dict:
        """Search torrent indexes for candidates worth downloading.

        This only searches; it never adds or downloads anything. Feed a pick's
        `download_url` to `torrent_ingest` to run the quarantine pipeline.

        Args:
            query: What to look for, e.g. "debian 12 iso".
            limit: Maximum number of ranked candidates to return.
        """
        if not self.settings.discovery_enabled:
            raise ToolExecutionError(
                "Torrent discovery is disabled (MINUS_DISCOVERY_ENABLED is false)."
            )
        result = discover(self.settings, query, limit=limit)
        pick = choose_best(
            result,
            min_score=self.settings.discovery_min_score,
            low_score=self.settings.discovery_low_confidence_score,
        )
        return {
            "query": result.query,
            "kind": result.kind,
            "errors": list(result.errors),
            "candidates": [self._candidate(scored) for scored in result.candidates],
            "pick": self._pick(pick),
        }

    def _candidate(self, scored: ScoredCandidate) -> dict:
        item = scored.candidate
        return {
            "title": item.title,
            "provider": item.provider,
            "tracker": item.tracker,
            "size_bytes": item.size_bytes,
            "seeders": item.seeders,
            "infohash": item.infohash,
            "magnet": item.magnet,
            "url": item.url,
            "download_url": item.download_url,
            "score": scored.score,
            "eligible": scored.eligible,
            "reasons": list(scored.reasons),
            "reject_reason": scored.reject_reason,
        }

    def _pick(self, scored: ScoredCandidate | None) -> dict | None:
        if scored is None:
            return None
        entry = self._candidate(scored)
        # Below the normal threshold a pick is only ever the explicit
        # "download it and let the VM back-check it" floor, so the caller must
        # be able to see that this is a guess rather than a confident match.
        entry["low_confidence"] = scored.score < self.settings.discovery_min_score
        return entry
