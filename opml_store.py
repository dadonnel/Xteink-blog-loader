from __future__ import annotations

import hashlib
import os
import tempfile
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List


class ValidationError(Exception):
    """Raised when feed input fails validation."""


@dataclass(frozen=True)
class Feed:
    feed_id: str
    name: str
    url: str


def _feed_id(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


class OPMLStore:
    def __init__(self, path: str | os.PathLike[str] = "feeds.opml") -> None:
        self.path = Path(path)

    def parse_feeds(self) -> Dict[str, List[Feed]]:
        tree = ET.parse(self.path)
        root = tree.getroot()
        body = root.find("body")
        if body is None:
            return {}

        grouped: Dict[str, List[Feed]] = {}
        for category_node in body.findall("outline"):
            category = (
                category_node.get("text")
                or category_node.get("title")
                or "Uncategorized"
            )
            grouped[category] = []
            for feed_node in category_node.findall("outline"):
                url = feed_node.get("xmlUrl", "").strip()
                if not url:
                    continue
                name = (
                    feed_node.get("text")
                    or feed_node.get("title")
                    or url
                ).strip()
                grouped[category].append(
                    Feed(feed_id=_feed_id(url), name=name, url=url)
                )
        return grouped

    def append_feed(self, *, name: str, url: str, category: str | None = None) -> None:
        name = name.strip()
        url = url.strip()
        category = (category or "Uncategorized").strip() or "Uncategorized"

        if not name or not url:
            raise ValidationError("Name and URL are required.")
        if not self._is_valid_url(url):
            raise ValidationError("Feed URL is malformed. Use http:// or https://")

        tree = ET.parse(self.path)
        root = tree.getroot()
        body = root.find("body")
        if body is None:
            body = ET.SubElement(root, "body")

        if self._url_exists(body, url):
            raise ValidationError("A feed with this URL already exists.")

        category_node = self._find_or_create_category(body, category)
        ET.SubElement(
            category_node,
            "outline",
            {
                "type": "rss",
                "text": name,
                "title": name,
                "xmlUrl": url,
            },
        )
        self._atomic_write(tree)

    def delete_feed(self, *, url: str | None = None, feed_id: str | None = None) -> bool:
        url = (url or "").strip()
        feed_id = (feed_id or "").strip()
        if not url and not feed_id:
            raise ValidationError("A feed URL or feed id is required for deletion.")

        tree = ET.parse(self.path)
        root = tree.getroot()
        body = root.find("body")
        if body is None:
            return False

        deleted = False
        for category_node in body.findall("outline"):
            for feed_node in list(category_node.findall("outline")):
                candidate = (feed_node.get("xmlUrl") or "").strip()
                if not candidate:
                    continue
                if (url and candidate == url) or (feed_id and _feed_id(candidate) == feed_id):
                    category_node.remove(feed_node)
                    deleted = True
                    break
            if deleted and len(category_node.findall("outline")) == 0:
                body.remove(category_node)
            if deleted:
                break

        if deleted:
            self._atomic_write(tree)
        return deleted

    @staticmethod
    def _is_valid_url(value: str) -> bool:
        parsed = urllib.parse.urlparse(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    @staticmethod
    def _url_exists(body: ET.Element, url: str) -> bool:
        for category_node in body.findall("outline"):
            for feed_node in category_node.findall("outline"):
                if (feed_node.get("xmlUrl") or "").strip() == url:
                    return True
        return False

    @staticmethod
    def _find_or_create_category(body: ET.Element, category: str) -> ET.Element:
        for category_node in body.findall("outline"):
            current_name = category_node.get("text") or category_node.get("title")
            if current_name == category:
                return category_node
        return ET.SubElement(body, "outline", {"text": category, "title": category})

    def _atomic_write(self, tree: ET.ElementTree) -> None:
        ET.indent(tree, space="    ")
        target_dir = self.path.parent
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=target_dir, delete=False, suffix=".tmp"
        ) as tmp:
            tmp_path = Path(tmp.name)
            tree.write(tmp, encoding="utf-8", xml_declaration=True)
        os.replace(tmp_path, self.path)
