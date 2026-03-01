"""Feed parsing and article extraction services."""

from __future__ import annotations

import datetime
import ssl

import feedparser
import requests
from bs4 import BeautifulSoup

from .config import (
    DAYS_BACK,
    MIN_CONTENT_LENGTH,
    REQUEST_TIMEOUT_SECONDS,
    STRICT_MIN_CONTENT_LENGTH,
)

# Fix for some SSL errors on older setups
if hasattr(ssl, "_create_unverified_context"):
    ssl._create_default_https_context = ssl._create_unverified_context

# Set User-Agent globally for feedparser
feedparser.USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
)


class FeedService:
    def __init__(self, days_back: int = DAYS_BACK):
        self.days_back = days_back

    def is_recent(self, entry) -> tuple[bool, str]:
        if not hasattr(entry, "published_parsed") and not hasattr(entry, "updated_parsed"):
            return True, "No date found (defaulting to True)"

        parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
        if not parsed:
            return True, "Date unparseable (defaulting to True)"

        try:
            entry_dt = datetime.datetime(*parsed[:6], tzinfo=datetime.timezone.utc)
            now = datetime.datetime.now(datetime.timezone.utc)
            delta = (now - entry_dt).days
            if delta <= self.days_back:
                return True, f"{delta} days ago"
            return False, f"{delta} days ago (Too old)"
        except Exception as exc:
            return True, f"Date error: {exc} (defaulting to True)"

    def fetch_weekly_urls(self, feeds: list[dict[str, str]]) -> list[dict[str, str]]:
        urls: list[dict[str, str]] = []
        for feed in feeds:
            try:
                print(f"Fetching {feed['name']}...")
                parsed = feedparser.parse(feed["url"])
                if hasattr(parsed, "entries") and len(parsed.entries) > 0:
                    recent_count = 0
                    for entry in parsed.entries:
                        recent, _ = self.is_recent(entry)
                        if recent:
                            link = entry.get("link", "")
                            title = entry.get("title", link or "Untitled")
                            urls.append({"title": title, "url": link, "source": feed["name"]})
                            recent_count += 1
                    print(f"  {recent_count} recent posts")
                else:
                    print(f"  Invalid RSS or no entries: {feed['url']}")
            except Exception as exc:
                print(f"  Feed error {feed['name']}: {exc}")

        seen: set[str] = set()
        unique = [item for item in urls if item["url"] not in seen and not seen.add(item["url"])]
        return unique

    @staticmethod
    def clean_html_strict(soup: BeautifulSoup) -> str:
        target_tags = ["p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "ul", "ol", "li"]
        parts: list[str] = []

        main_selectors = [
            "article",
            "main",
            ".post",
            ".entry-content",
            ".article-body",
            "[role=\"main\"]",
            ".content",
            "#content",
        ]
        content = None
        for selector in main_selectors:
            found = soup.select_one(selector)
            if found and len(found.get_text(strip=True)) > 200:
                content = found
                break
        if not content:
            content = soup

        for elem in content.find_all(target_tags):
            if elem.find_parent(target_tags):
                continue
            if hasattr(elem, "attrs"):
                elem.attrs = {k: v for k, v in elem.attrs.items() if k in ["href", "src", "alt"]}
            parts.append(str(elem))

        return "\n".join(parts)

    @staticmethod
    def clean_html_fallback(soup: BeautifulSoup) -> str:
        for tag in soup(["script", "style", "nav", "footer", "iframe"]):
            tag.decompose()
        return str(soup)

    def fetch_and_extract(self, url: str) -> str | None:
        try:
            headers = {"User-Agent": feedparser.USER_AGENT}
            resp = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS, headers=headers)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding
        except Exception as exc:
            print(f"  ! Network Fail: {exc}")
            return None

        soup = BeautifulSoup(resp.text, "html.parser")
        title = soup.title.string.strip() if soup.title and soup.title.string else "Untitled"

        for unwanted in soup(["script", "style", "nav", "footer", "header", "aside", "form", "button", "iframe"]):
            unwanted.decompose()

        clean_content = self.clean_html_strict(soup)

        if len(clean_content) < STRICT_MIN_CONTENT_LENGTH:
            print("    (Strict cleaning returned little text, using fallback...)")
            clean_content = self.clean_html_fallback(soup)

        if len(clean_content) < MIN_CONTENT_LENGTH:
            print(f"    ! Content too short ({len(clean_content)} chars). Skipping.")
            return None

        header = f"""
    <div style="margin-bottom: 2em; border-bottom: 1px solid #ccc; padding-bottom: 1em;">
        <h1 style="margin:0;">{title}</h1>
        <p style="font-size: 0.8em; color: #666;">
            Source: <a href="{url}">{url}</a>
        </p>
    </div>
    """
        return header + clean_content
