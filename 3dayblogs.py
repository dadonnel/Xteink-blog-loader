#!/usr/bin/env python3
"""CLI wrapper for RSS -> EPUB workflow."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from rss_epub.config import OUTPUT_DIR
from rss_epub.epub_service import EpubService
from rss_epub.feed_service import FeedService
from rss_epub.opml_store import OpmlStore
from rss_epub.xteink_client import XteinkClient


def main() -> None:
    print("--- GenAI Weekly Generator ---")

    feeds = OpmlStore().load_sources()
    if not feeds:
        print("No feeds loaded.")
        return

    feed_service = FeedService()
    urls = feed_service.fetch_weekly_urls(feeds)
    print(f"\nTotal unique URLs found: {len(urls)}")
    if not urls:
        print("No recent articles found in any feed.")
        return

    articles: list[dict[str, str]] = []
    for meta in urls:
        print(f"Processing: {meta['title'][:40]}...")
        extracted = feed_service.fetch_and_extract(meta["url"])
        if extracted:
            articles.append(
                {
                    "title": meta["title"],
                    "url": meta["url"],
                    "source": meta["source"],
                    "content": extracted,
                }
            )
            print("  OK")
        else:
            print("  Failed")

    if not articles:
        print("\nERROR: Found URLs but failed to extract content from all of them.")
        return

    print(f"\nBuilding EPUB with {len(articles)} articles...")
    out_path = EpubService().build_epub(articles)
    print(f"Success! EPUB saved to: {out_path}")

    synced_path = XteinkClient().sync_local_copy(out_path)
    print(f"Synced to: {synced_path}")
    print(f"Output directory: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
