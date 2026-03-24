from __future__ import annotations

import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Iterable
from urllib.request import Request, urlopen

USER_AGENT = "RSS-EPUB-Feed-Discovery/1.0"

FEED_MIME_TYPES = {
    "application/rss+xml": "rss",
    "application/atom+xml": "atom",
    "application/feed+json": "json",
    "application/json": "json",
    "text/xml": "rss",
    "application/xml": "rss",
}


class FeedDiscoveryError(Exception):
    """Raised when a URL cannot be resolved to a feed."""


@dataclass(frozen=True)
class FeedResolution:
    name: str
    feed_url: str
    category: str
    feed_type: str


class _FeedLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.feed_links: list[tuple[str, str]] = []
        self.page_title: str = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        if tag.lower() == "title":
            self._in_title = True
        if tag.lower() != "link":
            return
        rel_tokens = {part.strip().lower() for part in attr_map.get("rel", "").split()}
        link_type = attr_map.get("type", "").split(";", 1)[0].strip().lower()
        href = attr_map.get("href", "").strip()
        if "alternate" in rel_tokens and link_type in FEED_MIME_TYPES and href:
            self.feed_links.append((href, FEED_MIME_TYPES[link_type]))

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title and data.strip() and not self.page_title:
            self.page_title = data.strip()


def resolve_feed(url: str, existing_categories: Iterable[str] = ()) -> FeedResolution:
    candidate = url.strip()
    parsed = urllib.parse.urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise FeedDiscoveryError("Feed URL is malformed. Use http:// or https://")

    fetched = _fetch(candidate)
    as_feed = _parse_as_feed(fetched["body"], fetched["content_type"])
    if as_feed:
        name = _preferred_feed_name(as_feed.get("title"), fetched["final_url"])
        category = _classify_category(name, fetched["final_url"], existing_categories)
        return FeedResolution(name=name, feed_url=fetched["final_url"], category=category, feed_type=as_feed["feed_type"])

    parser = _FeedLinkParser()
    parser.feed(fetched["body"])

    feed_candidates: list[tuple[str, str | None]] = [
        (urllib.parse.urljoin(fetched["final_url"], href), feed_type)
        for href, feed_type in parser.feed_links
    ]
    feed_candidates.extend(_fallback_feed_candidates(fetched["final_url"]))

    visited: set[str] = set()
    for feed_url, hinted_type in feed_candidates:
        if feed_url in visited:
            continue
        visited.add(feed_url)
        try:
            feed_fetched = _fetch(feed_url)
        except FeedDiscoveryError:
            continue
        parsed_feed = _parse_as_feed(feed_fetched["body"], feed_fetched["content_type"])
        if parsed_feed:
            feed_type = parsed_feed["feed_type"] or hinted_type or "rss"
            name = _preferred_feed_name(parsed_feed.get("title") or parser.page_title, feed_fetched["final_url"])
            category = _classify_category(name, fetched["final_url"], existing_categories)
            return FeedResolution(name=name, feed_url=feed_fetched["final_url"], category=category, feed_type=feed_type)

    raise FeedDiscoveryError("Unable to automatically discover a feed from that URL.")


def _fetch(url: str, timeout_s: int = 10) -> dict[str, str]:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=timeout_s) as response:
            final_url = response.geturl()
            content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            body = response.read(1024 * 1024).decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        raise FeedDiscoveryError(f"Unable to fetch URL: {exc}") from exc

    return {
        "final_url": final_url,
        "content_type": content_type,
        "body": body,
    }


def _parse_as_feed(body: str, content_type: str) -> dict[str, str] | None:
    body_trimmed = body.lstrip()

    if content_type in {"application/feed+json", "application/json"} or body_trimmed.startswith("{"):
        try:
            parsed = json.loads(body_trimmed)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict) and ("jsonfeed.org/version" in str(parsed.get("version", "")) or "items" in parsed):
            return {
                "feed_type": "json",
                "title": str(parsed.get("title") or "").strip(),
            }

    if not body_trimmed.startswith("<"):
        return None

    try:
        root = ET.fromstring(body_trimmed)
    except ET.ParseError:
        return None

    root_name = _local_name(root.tag).lower()
    if root_name == "rss" or root_name == "rdf":
        title = root.findtext("./channel/title", default="").strip()
        return {"feed_type": "rss", "title": title}
    if root_name == "feed":
        title = root.findtext("./{*}title", default="").strip()
        return {"feed_type": "atom", "title": title}
    return None


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def _fallback_feed_candidates(url: str) -> list[tuple[str, str | None]]:
    parsed = urllib.parse.urlparse(url)
    root = f"{parsed.scheme}://{parsed.netloc}/"
    paths = [
        "feed",
        "feed.xml",
        "rss",
        "rss.xml",
        "atom.xml",
        "index.xml",
    ]
    return [(urllib.parse.urljoin(root, p), None) for p in paths]


def _preferred_feed_name(candidate_title: str | None, url: str) -> str:
    if candidate_title and candidate_title.strip():
        return candidate_title.strip()
    domain = urllib.parse.urlparse(url).netloc
    return re.sub(r"^www\.", "", domain).strip() or "Untitled Feed"


def _classify_category(name: str, url: str, existing_categories: Iterable[str]) -> str:
    categories = [c.strip() for c in existing_categories if c and c.strip()]
    if not categories:
        return _new_category_from_url(url)

    text_tokens = set(_tokenize(f"{name} {url}"))
    best: tuple[int, str] | None = None
    for category in categories:
        category_tokens = set(_tokenize(category))
        score = len(text_tokens & category_tokens)

        # Domain/topic hinting against known category semantics.
        if any(k in text_tokens for k in {"ai", "llm", "research", "openai", "anthropic"}) and "ai" in category.lower():
            score += 2
        if any(k in text_tokens for k in {"code", "python", "engineering", "dev", "software"}) and any(k in category.lower() for k in {"engineering", "code"}):
            score += 2
        if any(k in text_tokens for k in {"city", "urban", "civic"}) and "civic" in category.lower():
            score += 2

        if best is None or score > best[0]:
            best = (score, category)

    if best and best[0] > 0:
        return best[1]
    return _new_category_from_url(url)


def _new_category_from_url(url: str) -> str:
    domain = urllib.parse.urlparse(url).netloc.lower()
    domain = re.sub(r"^www\.", "", domain)
    label = domain.split(".")[0].replace("-", " ").replace("_", " ").strip().title()
    return label or "Uncategorized"


def _tokenize(value: str) -> list[str]:
    return [part for part in re.split(r"[^a-z0-9]+", value.lower()) if part]
