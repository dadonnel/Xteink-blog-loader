"""Shared, deterministic article selection for preview and generation."""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any


PRESETS = {
    "quick": {"label": "Quick read", "max_articles": 6, "max_words": 9_000},
    "commute": {"label": "Commute", "max_articles": 15, "max_words": 25_000},
    "full": {"label": "Full edition", "max_articles": 40, "max_words": 75_000},
}


def balanced_select(candidates: list[dict[str, Any]], max_articles: int, max_words: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Round-robin sources so OPML order cannot consume the whole budget."""
    queues: dict[str, deque] = defaultdict(deque)
    source_order: list[str] = []
    for item in candidates:
        source = str(item.get("source") or "Unknown source")
        if source not in queues:
            source_order.append(source)
        queues[source].append(item)

    selected: list[dict[str, Any]] = []
    omitted: list[dict[str, Any]] = []
    total_words = 0
    while any(queues.values()):
        made_progress = False
        for source in source_order:
            if not queues[source]:
                continue
            item = queues[source].popleft()
            words = int(item.get("word_count") or 0)
            if len(selected) >= max_articles or total_words + words > max_words:
                omitted.append({**item, "omitted_reason": "budget_limit"})
                continue
            selected.append(item)
            total_words += words
            made_progress = True
        if not made_progress and len(selected) >= max_articles:
            break
    for source in source_order:
        while queues[source]:
            omitted.append({**queues[source].popleft(), "omitted_reason": "budget_limit"})
    return selected, omitted
