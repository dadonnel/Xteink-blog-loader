#!/usr/bin/env python3
import json
import os
import xml.etree.ElementTree as ET
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from feed_service import validate_feeds

BASE_DIR = Path(__file__).parent
SOURCES_FILE = os.environ.get("SOURCES_FILE", str(BASE_DIR / "feeds.opml"))
VALIDATION_TIMEOUT_SECONDS = int(os.environ.get("VALIDATION_TIMEOUT_SECONDS", "10"))
VALIDATION_MAX_WORKERS = int(os.environ.get("VALIDATION_MAX_WORKERS", "10"))


def load_sources(path: str = SOURCES_FILE):
    if not os.path.exists(path):
        return []

    feeds = []
    try:
        tree = ET.parse(path)
    except ET.ParseError:
        return []
    root = tree.getroot()

    for outline in root.findall('.//outline[@xmlUrl]'):
        name = outline.get("text") or outline.get("title") or "Untitled Feed"
        url = outline.get("xmlUrl")
        if url:
            feeds.append({"name": name, "url": url})

    return feeds


def build_validate_payload():
    feeds = load_sources()
    results = validate_feeds(
        feeds,
        timeout_s=VALIDATION_TIMEOUT_SECONDS,
        max_workers=VALIDATION_MAX_WORKERS,
    )
    return [
        {
            "feed": result.feed,
            "1 day": result.counts["1 day"],
            "7 days": result.counts["7 days"],
            "30 days": result.counts["30 days"],
            "status": result.status,
            "reason": result.reason,
        }
        for result in results
    ]


class Handler(BaseHTTPRequestHandler):
    def _send_bytes(self, payload: bytes, content_type: str, status: int = HTTPStatus.OK):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path != "/":
            self._send_bytes(b"Not found", "text/plain; charset=utf-8", HTTPStatus.NOT_FOUND)
            return

        html = (BASE_DIR / "templates" / "index.html").read_text(encoding="utf-8")
        self._send_bytes(html.encode("utf-8"), "text/html; charset=utf-8")

    def do_POST(self):
        if self.path != "/validate":
            self._send_bytes(b"Not found", "text/plain; charset=utf-8", HTTPStatus.NOT_FOUND)
            return

        payload = build_validate_payload()
        body = json.dumps(payload).encode("utf-8")
        self._send_bytes(body, "application/json; charset=utf-8")


def run(host: str = "0.0.0.0", port: int = 5000):
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Server running on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run()
