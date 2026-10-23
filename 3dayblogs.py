#!/usr/bin/env python3
"""CLI entrypoint for the RSS -> EPUB workflow."""

from __future__ import annotations

import os
import sys
import importlib.util
import json
import datetime as dt
from pathlib import Path
from uuid import uuid4

# Ensure local package imports work when running from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from rss_epub.config import OUTPUT_DIR as DEFAULT_OUTPUT_DIR
from rss_epub.config import SOURCES_FILE as DEFAULT_SOURCES_FILE
from rss_epub.config import DAYS_BACK as DEFAULT_DAYS_BACK
from rss_epub.config import MAX_ARTICLES, MAX_TOTAL_WORDS
from rss_epub.config import HISTORY_FILE as DEFAULT_HISTORY_FILE
from rss_epub.history import HistoryStore, article_key, utc_now
from rss_epub.selection import PRESETS, balanced_select

RESULT_PREFIX = "XTEINK_RESULT_JSON="


def _emit(result: dict) -> None:
    """Machine-readable final line; preceding output remains useful CLI detail."""
    print(RESULT_PREFIX + json.dumps(result, sort_keys=True))


def _resolve_sources_file() -> Path:
    configured = Path(os.getenv("SOURCES_FILE", str(DEFAULT_SOURCES_FILE)))
    if configured.exists():
        return configured

    local_fallback = Path("feeds.opml")
    if local_fallback.exists():
        return local_fallback

    return configured


def main() -> int:
    print("--- GenAI Weekly Generator ---")

    if importlib.util.find_spec("ebooklib") is None:
        print("Missing dependency: ebooklib")
        print("Install with: pip install ebooklib")
        _emit({"status": "failed", "reason": "Missing dependency: ebooklib"})
        return 1

    from rss_epub.epub_service import EpubService
    from rss_epub.feed_service import FeedService
    from rss_epub.opml_store import OpmlStore
    from rss_epub.xteink_client import XteinkClient

    output_dir = Path(os.getenv("OUTPUT_DIR", str(DEFAULT_OUTPUT_DIR)))
    days_back = int(os.getenv("DAYS_BACK", str(DEFAULT_DAYS_BACK)))
    sources_file = _resolve_sources_file()
    history = HistoryStore(Path(os.getenv("EDITION_HISTORY_FILE", str(DEFAULT_HISTORY_FILE))))
    include_previous = os.getenv("INCLUDE_PREVIOUS_ARTICLES", "false").lower() in {"1", "true", "yes"}
    preset = PRESETS.get(os.getenv("EDITION_PRESET", "full"), PRESETS["full"])

    store = OpmlStore(path=sources_file)
    feed_service = FeedService(days_back=days_back)
    epub_service = EpubService(output_dir=output_dir)
    xteink_client = XteinkClient()

    feeds = store.load_sources()
    if not feeds:
        result = {"status": "no_new_articles", "reason": "No active subscriptions are configured.", "warnings": []}
        print(result["reason"])
        _emit(result)
        return 0

    urls = feed_service.fetch_weekly_urls(feeds)
    if not urls:
        result = {"status": "no_new_articles", "reason": "No recent articles were found.", "warnings": []}
        print(result["reason"])
        _emit(result)
        return 0

    collected = history.collected_keys()
    duplicates = [] if include_previous else [item for item in urls if article_key(item) in collected]
    candidates = urls if include_previous else [item for item in urls if article_key(item) not in collected]
    if not candidates:
        result = {
            "status": "no_new_articles",
            "reason": "All recent articles were included in an earlier edition.",
            "warnings": [],
            "omitted": [{"title": item.get("title"), "reason": "duplicate"} for item in duplicates],
        }
        print(result["reason"])
        _emit(result)
        return 0

    articles: list[dict[str, str]] = []
    total_words = 0
    extraction_failures = []
    warnings = []
    for item in candidates:
        content = feed_service.fetch_and_extract(item["url"])
        if content:
            words = len(content.split())
            articles.append(
                {
                    "title": item.get("title") or item["url"],
                    "content": content,
                    "source": item.get("source", "Unknown source"),
                    "category": item.get("category", "Other"),
                    "published": item.get("published", ""),
                    "url": item["url"],
                    "entry_id": item.get("entry_id", ""),
                    "feed_url": item.get("feed_url", ""),
                    "word_count": words,
                }
            )
            total_words += words
            if not item.get("published"):
                warnings.append(f"{item.get('title', 'Untitled')} has no publication date")
        else:
            extraction_failures.append({"title": item.get("title") or item["url"], "url": item["url"], "reason": "extraction_failure"})

    if not articles:
        result = {
            "status": "no_extractable_articles",
            "reason": "Articles were found, but none could be extracted.",
            "warnings": warnings,
            "omitted": extraction_failures,
        }
        print(result["reason"])
        _emit(result)
        return 0

    selected, budget_omitted = balanced_select(
        articles,
        int(os.getenv("MAX_ARTICLES", str(preset["max_articles"]))),
        int(os.getenv("MAX_TOTAL_WORDS", str(preset["max_words"]))),
    )
    if not selected:
        result = {"status": "no_extractable_articles", "reason": "Extracted articles exceeded the selected edition budget.", "warnings": warnings, "omitted": budget_omitted}
        _emit(result)
        return 0

    edition_id = f"{dt.datetime.now().astimezone().strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}"
    epub_path = epub_service.build_epub(selected, edition_id=edition_id)
    sync_path = xteink_client.sync_local_copy(epub_path)

    article_count = len(selected)
    reading_minutes = max(1, round(sum(int(item.get("word_count") or 0) for item in selected) / 225))
    omitted = extraction_failures + [
        {"title": item.get("title"), "url": item.get("url"), "reason": item.get("omitted_reason", "budget_limit")}
        for item in budget_omitted
    ] + [{"title": item.get("title"), "url": item.get("url"), "reason": "duplicate"} for item in duplicates]
    if omitted:
        warnings.append(f"{len(omitted)} article(s) were skipped; see details.")
    result = {
        "status": "created_with_warnings" if warnings or omitted else "created",
        "edition_id": edition_id,
        "output_file": str(epub_path.resolve()),
        "sync_file": str(sync_path.resolve()),
        "filename": sync_path.name,
        "article_count": article_count,
        "estimated_reading_minutes": reading_minutes,
        "created_at": utc_now(),
        "warnings": warnings,
        "omitted": omitted,
    }
    history.record_edition({**result, "delivery_status": "pending"}, selected)

    print(f"EPUB created: {epub_path}")
    print(f"Synced local copy: {sync_path}")
    _emit(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
