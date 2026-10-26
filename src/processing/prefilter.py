"""Deterministic, no-LLM pre-scoring used to cap AI cost.

Horizon's default pipeline sends every fetched item to the model and only
filters afterwards, so the bill scales with everything the scrapers happened
to find. This module ranks items with a reproducible rule first, so the paid
analysis stage only ever sees the items that could plausibly matter.

The rule is intentionally simple and byte-for-byte reproducible:

    score = keyword_weight   * keyword_hits
          + source_weights[source]
          + freshness_weight * 0.5 ** (age_hours / half_life_hours)
          + engagement_weight * log10(1 + engagement)

No network access, no model call, no randomness.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Dict, List, Tuple

from ..models import ContentItem, PrefilterConfig

# Engagement counters that scrapers may attach, in metadata.
_ENGAGEMENT_KEYS = (
    "score",
    "points",
    "ups",
    "likes",
    "comments",
    "num_comments",
    "engagement",
    "stars",
)

# How much of the body text is scanned for keywords.
_CONTENT_SCAN_CHARS = 1000


def _engagement_of(item: ContentItem) -> float:
    """Sum the numeric engagement counters attached by the scrapers."""
    total = 0.0
    for key in _ENGAGEMENT_KEYS:
        value = item.metadata.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if value > 0:
            total += float(value)
    return total


def _keyword_hits(item: ContentItem, keywords: Dict[str, float]) -> float:
    """Sum the weights of keywords that appear in the title or body."""
    if not keywords:
        return 0.0
    body = (item.content or "")[:_CONTENT_SCAN_CHARS]
    haystack = f"{item.title}\n{body}".lower()
    return sum(
        float(weight)
        for keyword, weight in keywords.items()
        if keyword and keyword.lower() in haystack
    )


def _freshness(item: ContentItem, half_life_hours: float) -> float:
    """Exponential decay in (0, 1]; 1.0 means published just now."""
    published = item.published_at
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    age_hours = (datetime.now(timezone.utc) - published).total_seconds() / 3600.0
    age_hours = max(age_hours, 0.0)
    return 0.5 ** (age_hours / half_life_hours)


class RulePrefilter:
    """Ranks items by a deterministic rule and keeps the top slice."""

    def __init__(self, config: PrefilterConfig):
        self.config = config

    def score(self, item: ContentItem) -> float:
        """Return the rule score for one item."""
        cfg = self.config
        source_weight = float(cfg.source_weights.get(item.source_type.value, 1.0))
        return (
            cfg.keyword_weight * _keyword_hits(item, cfg.keywords)
            + source_weight
            + cfg.freshness_weight * _freshness(item, cfg.freshness_half_life_hours)
            + cfg.engagement_weight * math.log10(1.0 + _engagement_of(item))
        )

    def apply(
        self, items: List[ContentItem]
    ) -> Tuple[List[ContentItem], List[Tuple[ContentItem, float]]]:
        """Split items into the ones that continue and the ones dropped.

        Returns:
            (kept, dropped) where dropped carries each item's rule score.
        """
        scored = [(item, self.score(item)) for item in items]
        scored.sort(key=lambda pair: pair[1], reverse=True)

        kept: List[ContentItem] = []
        dropped: List[Tuple[ContentItem, float]] = []
        for item, score in scored:
            if score < self.config.min_score:
                dropped.append((item, score))
            elif len(kept) < self.config.max_items:
                kept.append(item)
            else:
                dropped.append((item, score))
        return kept, dropped
