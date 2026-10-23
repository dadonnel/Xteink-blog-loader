"""Feed parsing and article extraction services."""

from __future__ import annotations

import datetime
import ssl

import feedparser
import requests
from bs4 import BeautifulSoup

from .config import (
    DAYS_BACK,
    MAX_ARTICLES_PER_FEED,
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
        if isinstance(entry, dict):
            has_date = "published_parsed" in entry or "updated_parsed" in entry
            parsed = entry.get("published_parsed") or entry.get("updated_parsed")
        else:
            has_date = hasattr(entry, "published_parsed") or hasattr(entry, "updated_parsed")
            parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
        if not has_date:
            return True, "No date found (defaulting to True)"
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
            if feed.get("paused") in {True, "true", "1", "yes"}:
                continue
            try:
                print(f"Fetching {feed['name']}...")
                if feed.get("feed_type") == "json":
                    response = requests.get(feed["url"], timeout=REQUEST_TIMEOUT_SECONDS, headers={"User-Agent": feedparser.USER_AGENT})
                    response.raise_for_status()
                    entries = []
                    for item in response.json().get("items", []):
                        published = item.get("date_published") or item.get("date_modified") or ""
                        parsed_date = None
                        if published:
                            try:
                                parsed_date = datetime.datetime.fromisoformat(published.replace("Z", "+00:00")).utctimetuple()
                            except ValueError:
                                pass
                        entries.append({"id": item.get("id", ""), "link": item.get("url") or item.get("external_url") or "", "title": item.get("title") or "Untitled", "published": published, "published_parsed": parsed_date})
                    parsed = type("ParsedFeed", (), {"entries": entries})()
                else:
                    parsed = feedparser.parse(feed["url"])
                if hasattr(parsed, "entries") and len(parsed.entries) > 0:
                    recent_count = 0
                    for entry in parsed.entries:
                        recent, _ = self.is_recent(entry)
                        if recent:
                            link = entry.get("link", "")
                            title = entry.get("title", link or "Untitled")
                            published = entry.get("published") or entry.get("updated") or ""
                            urls.append({
                                "title": title,
                                "url": link,
                                "entry_id": entry.get("id") or entry.get("guid") or "",
                                "feed_url": feed["url"],
                                "source": feed["name"],
                                "category": feed.get("category", "Other"),
                                "published": published,
                            })
                            recent_count += 1
                            if recent_count >= MAX_ARTICLES_PER_FEED:
                                break
                    print(f"  {recent_count} recent posts")
                else:
                    print(f"  Invalid RSS or no entries: {feed['url']}")
            except Exception as exc:
                print(f"  Feed error {feed['name']}: {exc}")

        from .history import article_key

        seen: set[str] = set()
        unique = [item for item in urls if article_key(item) not in seen and not seen.add(article_key(item))]
        return unique

    @staticmethod
    def clean_html_strict(soup: BeautifulSoup) -> str:
        target_tags = ["p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "ul", "ol", "li", "strong", "b", "em", "i", "br", "hr", "img"]
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
                elem.attrs = {k: v for k, v in elem.attrs.items() if k in ["src", "alt"]}
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
        for unwanted in soup(["script", "style", "nav", "footer", "header", "aside", "form", "button", "iframe"]):
            unwanted.decompose()

        clean_content = self.clean_html_strict(soup)

        if len(clean_content) < STRICT_MIN_CONTENT_LENGTH:
            print("    (Strict cleaning returned little text, using fallback...)")
            clean_content = self.clean_html_fallback(soup)

        if len(clean_content) < MIN_CONTENT_LENGTH:
            print(f"    ! Content too short ({len(clean_content)} chars). Skipping.")
            return None

        return clean_content
