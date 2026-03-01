import datetime
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from email.utils import parsedate_tz
from typing import Dict, List, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
)


@dataclass
class FeedValidationResult:
    feed: str
    status: str
    counts: Dict[str, int]
    reason: str | None = None


def _parse_date_struct(value: str | None):
    if not value:
        return None

    parsed = parsedate_tz(value)
    if parsed:
        return parsed

    cleaned = value.replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.utctimetuple()
    except ValueError:
        return None


def is_recent(entry: Dict[str, object], days_back: int) -> Tuple[bool, str]:
    if "published_parsed" not in entry and "updated_parsed" not in entry:
        return True, "No date found (defaulting to True)"

    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return True, "Date unparseable (defaulting to True)"

    try:
        entry_dt = datetime.datetime(*parsed[:6], tzinfo=datetime.timezone.utc)
        now = datetime.datetime.now(datetime.timezone.utc)
        delta = (now - entry_dt).days
        if delta <= days_back:
            return True, f"{delta} days ago"
        return False, f"{delta} days ago (Too old)"
    except Exception as exc:  # pragma: no cover
        return True, f"Date error: {exc} (defaulting to True)"


def _strip_ns(tag: str) -> str:
    return tag.split("}")[-1]


def _find_child_text(node: ET.Element, names: set[str]) -> str | None:
    for child in list(node):
        if _strip_ns(child.tag) in names and child.text:
            return child.text.strip()
    return None


def _extract_entries(xml_content: bytes) -> List[Dict[str, object]]:
    root = ET.fromstring(xml_content)
    root_name = _strip_ns(root.tag)
    entries: List[Dict[str, object]] = []

    if root_name == "rss":
        channel = next((child for child in root if _strip_ns(child.tag) == "channel"), None)
        if channel is None:
            return []
        for item in channel:
            if _strip_ns(item.tag) != "item":
                continue
            published = _find_child_text(item, {"pubDate", "published", "updated"})
            entries.append(
                {
                    "published_parsed": _parse_date_struct(published),
                    "updated_parsed": None,
                }
            )
    elif root_name == "feed":
        for entry in root:
            if _strip_ns(entry.tag) != "entry":
                continue
            published = _find_child_text(entry, {"published"})
            updated = _find_child_text(entry, {"updated"})
            entries.append(
                {
                    "published_parsed": _parse_date_struct(published),
                    "updated_parsed": _parse_date_struct(updated),
                }
            )

    return entries


def _fetch_feed(feed: Dict[str, str], timeout_s: int) -> FeedValidationResult:
    windows = {"1 day": 1, "7 days": 7, "30 days": 30}
    counts = {label: 0 for label in windows}
    request = Request(feed["url"], headers={"User-Agent": USER_AGENT})

    try:
        with urlopen(request, timeout=timeout_s) as response:
            payload = response.read()
    except TimeoutError:
        return FeedValidationResult(feed=feed["name"], status="error", counts=counts, reason="timeout")
    except HTTPError as exc:
        return FeedValidationResult(feed=feed["name"], status="error", counts=counts, reason=f"invalid feed: HTTP {exc.code}")
    except URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            return FeedValidationResult(feed=feed["name"], status="error", counts=counts, reason="timeout")
        return FeedValidationResult(feed=feed["name"], status="error", counts=counts, reason="invalid feed")
    except Exception:
        return FeedValidationResult(feed=feed["name"], status="error", counts=counts, reason="invalid feed")

    try:
        entries = _extract_entries(payload)
    except ET.ParseError:
        return FeedValidationResult(feed=feed["name"], status="error", counts=counts, reason="parse failure")

    if not entries:
        return FeedValidationResult(feed=feed["name"], status="error", counts=counts, reason="invalid feed")

    for entry in entries:
        for label, days in windows.items():
            recent, _ = is_recent(entry, days)
            if recent:
                counts[label] += 1

    return FeedValidationResult(feed=feed["name"], status="ok", counts=counts)


def validate_feeds(feeds: List[Dict[str, str]], timeout_s: int = 10, max_workers: int = 8) -> List[FeedValidationResult]:
    if not feeds:
        return []

    workers = min(max_workers, len(feeds))
    results: List[FeedValidationResult] = []

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_index = {
            executor.submit(_fetch_feed, feed, timeout_s): idx
            for idx, feed in enumerate(feeds)
        }
        indexed_results = []
        for future in as_completed(future_to_index):
            indexed_results.append((future_to_index[future], future.result()))

    indexed_results.sort(key=lambda item: item[0])
    results = [result for _, result in indexed_results]
    return results
