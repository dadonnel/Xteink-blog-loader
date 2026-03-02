#!/usr/bin/env python3
import json
import os
import xml.etree.ElementTree as ET
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from feed_service import validate_feeds
from morning_sync import UploadState, ensure_records_for_files, host_reachable, try_upload_pending

BASE_DIR = Path(__file__).parent
SOURCES_FILE = os.environ.get("SOURCES_FILE", str(BASE_DIR / "feeds.opml"))
VALIDATION_TIMEOUT_SECONDS = int(os.environ.get("VALIDATION_TIMEOUT_SECONDS", "10"))
VALIDATION_MAX_WORKERS = int(os.environ.get("VALIDATION_MAX_WORKERS", "10"))
UPLOAD_HOST = os.environ.get("MORNING_SYNC_HOST", "192.168.1.211")
UPLOAD_SYNC_DIR = Path(
    os.environ.get("MORNING_SYNC_SYNC_DIR", "storage/downloads/rss_epub/output_epubs/xteink_sync")
)
UPLOAD_STATE_FILE = Path(
    os.environ.get("MORNING_SYNC_STATE_FILE", "storage/downloads/rss_epub/upload_state.json")
)
UPLOAD_CMD_TEMPLATE = os.environ.get(
    "MORNING_SYNC_UPLOAD_CMD", 'scp "{file}" "root@{host}:/mnt/onboard/"'
)
UPLOAD_REACHABILITY_METHOD = os.environ.get("MORNING_SYNC_REACHABILITY_METHOD", "auto")
UPLOAD_TCP_PORT = int(os.environ.get("MORNING_SYNC_TCP_PORT", "22"))
UPLOAD_CONNECT_TIMEOUT = float(os.environ.get("MORNING_SYNC_CONNECT_TIMEOUT", "1.0"))


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


def build_manual_upload_payload():
    state = UploadState(UPLOAD_STATE_FILE)
    pending_before = len(ensure_records_for_files(state, list(UPLOAD_SYNC_DIR.glob("*.epub"))))

    if not host_reachable(
        UPLOAD_HOST,
        UPLOAD_REACHABILITY_METHOD,
        UPLOAD_TCP_PORT,
        UPLOAD_CONNECT_TIMEOUT,
    ):
        return {
            "status": "unreachable",
            "host": UPLOAD_HOST,
            "pending_before": pending_before,
            "pending_after": pending_before,
            "uploaded_now": 0,
        }, HTTPStatus.SERVICE_UNAVAILABLE

    try_upload_pending(state, UPLOAD_SYNC_DIR, UPLOAD_HOST, UPLOAD_CMD_TEMPLATE)
    state.save()

    pending_after = len(
        [r for r in state.records.values() if not r.get("uploaded_successfully", False)]
    )
    uploaded_now = max(0, pending_before - pending_after)

    return {
        "status": "ok",
        "host": UPLOAD_HOST,
        "pending_before": pending_before,
        "pending_after": pending_after,
        "uploaded_now": uploaded_now,
    }, HTTPStatus.OK


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
        if self.path == "/validate":
            payload = build_validate_payload()
            body = json.dumps(payload).encode("utf-8")
            self._send_bytes(body, "application/json; charset=utf-8")
            return

        if self.path in {"/upload-pending", "/upload-epubs"}:
            payload, status = build_manual_upload_payload()
            body = json.dumps(payload).encode("utf-8")
            self._send_bytes(body, "application/json; charset=utf-8", status)
            return

        else:
            self._send_bytes(b"Not found", "text/plain; charset=utf-8", HTTPStatus.NOT_FOUND)
            return


def run(host: str = "0.0.0.0", port: int = 5001):
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Server running on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run()
