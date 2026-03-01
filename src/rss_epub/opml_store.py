"""Utilities for reading/writing OPML feed source files."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from .config import SOURCES_FILE


class OpmlStore:
    """Persist and retrieve RSS feed definitions from OPML."""

    def __init__(self, path: Path = SOURCES_FILE):
        self.path = Path(path)

    def load_sources(self) -> list[dict[str, str]]:
        """Parses an OPML file to extract feed URLs."""
        if not self.path.exists():
            print(f"ERROR: {self.path} not found. Make sure {self.path.name} is in the folder.")
            return []

        feeds: list[dict[str, str]] = []
        try:
            tree = ET.parse(self.path)
            root = tree.getroot()

            for outline in root.findall(".//outline[@xmlUrl]"):
                name = outline.get("text") or outline.get("title") or "Untitled Feed"
                url = outline.get("xmlUrl")
                if url:
                    feeds.append({"name": name, "url": url})

            print(f"Loaded {len(feeds)} feeds from OPML.")
        except Exception as exc:
            print(f"ERROR parsing OPML file: {exc}")
            return []

        return feeds

    def write_sources(self, feeds: list[dict[str, str]]) -> None:
        """Writes feed definitions back to an OPML file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)

        opml = ET.Element("opml", version="2.0")
        body = ET.SubElement(opml, "body")
        for feed in feeds:
            ET.SubElement(
                body,
                "outline",
                text=feed.get("name", "Untitled Feed"),
                title=feed.get("name", "Untitled Feed"),
                type="rss",
                xmlUrl=feed.get("url", ""),
            )

        ET.ElementTree(opml).write(self.path, encoding="utf-8", xml_declaration=True)
