#!/usr/bin/env python3
"""CLI entrypoint for the RSS -> EPUB workflow."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure local package imports work when running from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from rss_epub.config import OUTPUT_DIR as DEFAULT_OUTPUT_DIR
from rss_epub.config import SOURCES_FILE as DEFAULT_SOURCES_FILE
from rss_epub.config import DAYS_BACK as DEFAULT_DAYS_BACK
from rss_epub.epub_service import EpubService
from rss_epub.feed_service import FeedService
from rss_epub.opml_store import OpmlStore
from rss_epub.xteink_client import XteinkClient


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

    output_dir = Path(os.getenv("OUTPUT_DIR", str(DEFAULT_OUTPUT_DIR)))
    days_back = int(os.getenv("DAYS_BACK", str(DEFAULT_DAYS_BACK)))
    sources_file = _resolve_sources_file()

    store = OpmlStore(path=sources_file)
    feed_service = FeedService(days_back=days_back)
    epub_service = EpubService(output_dir=output_dir)
    xteink_client = XteinkClient()

    feeds = store.load_sources()
    if not feeds:
        print("No feeds found. Exiting.")
        return 1

    urls = feed_service.fetch_weekly_urls(feeds)
    if not urls:
        print("No recent URLs found. Exiting.")
        return 0

    articles: list[dict[str, str]] = []
    for item in urls:
        article = feed_service.fetch_and_extract(item["url"])
        if article:
            articles.append(article)

    if not articles:
        print("No article content extracted. Exiting.")
        return 0

    epub_path = epub_service.build_epub(articles)
    sync_path = xteink_client.sync_local_copy(epub_path)

    print(f"EPUB created: {epub_path}")
    print(f"Synced local copy: {sync_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
