"""Durable edition and collected-article history.

The history is deliberately independent of EPUB files so cleanup never erases the
answer to "what have I already collected?".  Writes are atomic and guarded inside
the process; callers should keep transactions short.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import threading
import urllib.parse
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def normalize_article_url(value: str) -> str:
    """Conservatively normalize a URL without merging distinct articles."""
    try:
        parsed = urllib.parse.urlsplit(value.strip())
    except ValueError:
        return value.strip()
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return value.strip()
    host = parsed.hostname.lower() if parsed.hostname else ""
    port = parsed.port
    netloc = host
    if port and not ((parsed.scheme.lower() == "http" and port == 80) or (parsed.scheme.lower() == "https" and port == 443)):
        netloc = f"{host}:{port}"
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = [(k, v) for k, v in query if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}]
    return urllib.parse.urlunsplit((parsed.scheme.lower(), netloc, path, urllib.parse.urlencode(query), ""))


def article_key(article: dict[str, Any]) -> str:
    entry_id = str(article.get("entry_id") or article.get("id") or "").strip()
    source_url = str(article.get("feed_url") or "").strip()
    if entry_id:
        return f"entry:{source_url}|{entry_id}"
    return f"url:{normalize_article_url(str(article.get('url') or ''))}"


class HistoryStore:
    _locks: dict[str, threading.RLock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, path: Path | str):
        self.path = Path(path)
        key = str(self.path.resolve())
        with self._locks_guard:
            self._lock = self._locks.setdefault(key, threading.RLock())

    def load(self) -> dict[str, Any]:
        with self._lock:
            if not self.path.exists():
                return {"version": 1, "articles": {}, "editions": {}}
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return {"version": 1, "articles": {}, "editions": {}}
            data.setdefault("version", 1)
            data.setdefault("articles", {})
            data.setdefault("editions", {})
            return data

    def save(self, data: dict[str, Any]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent, delete=False, suffix=".tmp") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                temp_path = Path(handle.name)
            os.replace(temp_path, self.path)

    def collected_keys(self) -> set[str]:
        return set(self.load()["articles"])

    def record_edition(self, edition: dict[str, Any], articles: list[dict[str, Any]]) -> None:
        """Commit an edition and its articles only after the EPUB was built."""
        with self._lock:
            data = self.load()
            edition_id = str(edition["edition_id"])
            data["editions"][edition_id] = dict(edition)
            for article in articles:
                key = article_key(article)
                data["articles"][key] = {
                    "title": article.get("title") or "Untitled",
                    "url": article.get("url") or "",
                    "source": article.get("source") or "Unknown source",
                    "published": article.get("published") or None,
                    "first_collected_at": data["articles"].get(key, {}).get("first_collected_at") or edition.get("created_at") or utc_now(),
                    "last_edition_id": edition_id,
                }
            self.save(data)

    def update_delivery(self, edition_id: str, *, status: str, detail: str | None = None) -> None:
        with self._lock:
            data = self.load()
            edition = data["editions"].get(edition_id)
            if not edition:
                return
            edition["delivery_status"] = status
            edition["delivery_detail"] = detail
            edition["last_delivery_attempt_at"] = utc_now()
            if status == "uploaded":
                edition["delivered_at"] = utc_now()
            self.save(data)

    def latest_edition(self) -> dict[str, Any] | None:
        editions = list(self.load()["editions"].values())
        return max(editions, key=lambda item: item.get("created_at", ""), default=None)

