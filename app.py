#!/usr/bin/env python3
import html
import json
import os
import subprocess
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from feed_service import validate_feeds
from morning_sync import UploadState, ensure_records_for_files, host_reachable, try_upload_pending
from opml_store import OPMLStore, ValidationError

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
GENERATE_SCRIPT = Path(os.environ.get("GENERATE_EPUB_SCRIPT", str(BASE_DIR / "3dayblogs.py")))
GENERATE_TIMEOUT_SECONDS = int(os.environ.get("GENERATE_EPUB_TIMEOUT_SECONDS", "900"))

store = OPMLStore(SOURCES_FILE)


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
    pending_items = ensure_records_for_files(state, list(UPLOAD_SYNC_DIR.glob("*.epub")))
    pending_before = len(pending_items)
    pending_keys = [key for key, _ in pending_items]

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
            "failed_now": 0,
            "failed_items": [],
        }, HTTPStatus.SERVICE_UNAVAILABLE

    try_upload_pending(state, UPLOAD_SYNC_DIR, UPLOAD_HOST, UPLOAD_CMD_TEMPLATE)
    state.save()

    pending_after = len(
        [r for r in state.records.values() if not r.get("uploaded_successfully", False)]
    )
    uploaded_now = sum(
        1 for key in pending_keys if state.records.get(key, {}).get("uploaded_successfully", False)
    )
    failed_items = []
    for key in pending_keys:
        rec = state.records.get(key, {})
        if not rec.get("uploaded_successfully", False):
            failed_items.append(
                {
                    "filepath": rec.get("filepath"),
                    "error": rec.get("error") or "unknown upload failure",
                }
            )

    return {
        "status": "ok" if not failed_items else "partial",
        "host": UPLOAD_HOST,
        "pending_before": pending_before,
        "pending_after": pending_after,
        "uploaded_now": uploaded_now,
        "failed_now": len(failed_items),
        "failed_items": failed_items,
    }, HTTPStatus.OK


def build_generate_epub_payload(days_back: int):
    if days_back < 1:
        return {
            "status": "error",
            "reason": "days_back must be 1 or greater",
        }, HTTPStatus.BAD_REQUEST

    env = os.environ.copy()
    env["DAYS_BACK"] = str(days_back)

    try:
        result = subprocess.run(
            [sys.executable, str(GENERATE_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=GENERATE_TIMEOUT_SECONDS,
            cwd=BASE_DIR,
            env=env,
            check=False,
        )
    except FileNotFoundError:
        return {
            "status": "error",
            "reason": f"generate script not found: {GENERATE_SCRIPT}",
        }, HTTPStatus.INTERNAL_SERVER_ERROR
    except subprocess.TimeoutExpired:
        return {
            "status": "error",
            "reason": f"Generation timed out after {GENERATE_TIMEOUT_SECONDS} seconds",
        }, HTTPStatus.REQUEST_TIMEOUT

    output = "\n".join(
        line for line in [result.stdout.strip(), result.stderr.strip()] if line
    ).strip()

    if result.returncode != 0:
        return {
            "status": "error",
            "days_back": days_back,
            "reason": "EPUB generation failed",
            "returncode": result.returncode,
            "output": output,
        }, HTTPStatus.INTERNAL_SERVER_ERROR

    return {
        "status": "ok",
        "days_back": days_back,
        "returncode": result.returncode,
        "output": output,
    }, HTTPStatus.OK


def _render_feed_groups() -> str:
    try:
        feeds = store.parse_feeds()
    except FileNotFoundError:
        return "<p>No feeds configured yet.</p>"
    except ET.ParseError:
        return "<p class='error'>Unable to parse feeds.opml.</p>"

    if not feeds:
        return "<p>No feeds configured yet.</p>"

    parts = []
    for category, feed_items in feeds.items():
        parts.append(f"<h3>{html.escape(category)}</h3><ul>")
        for feed in feed_items:
            parts.append(
                "<li><div class='feed-row'>"
                f"<strong>{html.escape(feed.name)}</strong> "
                f"<span class='url'>{html.escape(feed.url)}</span>"
                "<form method='post' action='/feeds/delete' style='display:inline'>"
                f"<input type='hidden' name='feed_id' value='{html.escape(feed.feed_id)}'/>"
                "<button type='submit'>Delete</button>"
                "</form></div></li>"
            )
        parts.append("</ul>")
    return "".join(parts)


def render_index_html(message: str = "", error: str = "", form_data: dict | None = None) -> str:
    form_data = form_data or {}
    message_html = f"<p class='ok-msg'>{html.escape(message)}</p>" if message else ""
    error_html = f"<p class='error-msg'>{html.escape(error)}</p>" if error else ""

    return f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"UTF-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />
  <title>RSS EPUB Control Panel</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 24px; }}
    nav a {{ margin-right: 1rem; }}
    button {{ padding: 8px 14px; font-size: 14px; }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 16px; }}
    th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
    th {{ background: #f5f5f5; }}
    .ok {{ color: #08660f; font-weight: 600; }}
    .error {{ color: #b00020; font-weight: 600; }}
    .error-msg {{ background: #ffe5e5; border: 1px solid #b00; color: #600; padding: .75rem; margin-bottom: 1rem; }}
    .ok-msg {{ background: #e9ffe9; border: 1px solid #2a7; color: #153; padding: .75rem; margin-bottom: 1rem; }}
    form {{ margin: 1rem 0; }}
    input {{ margin-right: .5rem; padding: .3rem .4rem; }}
    ul {{ padding-left: 1.2rem; }}
    li {{ margin-bottom: .35rem; }}
    .feed-row {{ display: flex; align-items: center; gap: .5rem; flex-wrap: wrap; }}
    .url {{ color: #555; font-size: .9rem; }}
    section {{ margin-top: 2rem; }}
  </style>
</head>
<body>
  <h1>RSS EPUB Control Panel</h1>
  <nav>
    <a href="#validation">Feed validation</a>
    <a href="#upload">EPUB upload</a>
    <a href="#generate">Generate EPUB</a>
    <a href="#feeds">Feed manager</a>
  </nav>

  {message_html}
  {error_html}

  <section id="validation">
    <h2>Feed Validation</h2>
    <button id="validateBtn">Validate feeds</button>
  </section>

  <section id="upload">
    <h2>Upload Pending EPUBs</h2>
    <button id="uploadBtn">Upload pending EPUBs</button>
  </section>

  <section id="generate">
    <h2>Generate New EPUB</h2>
    <form method="post" action="/generate-epub">
      <label for="days_back">Days back:</label>
      <input id="days_back" type="number" name="days_back" min="1" step="1" value="{html.escape(form_data.get('days_back', '3'))}" required />
      <button type="submit">Generate new EPUB</button>
    </form>
  </section>

  <p id="message"></p>

  <table>
    <thead>
      <tr>
        <th>Feed</th>
        <th>1 day</th>
        <th>7 days</th>
        <th>30 days</th>
        <th>Status</th>
      </tr>
    </thead>
    <tbody id="resultsBody"></tbody>
  </table>

  <section id="feeds">
    <h2>Feed Manager</h2>
    <h3>Add Feed</h3>
    <form method="post" action="/feeds">
      <input name="name" placeholder="Feed Name" value="{html.escape(form_data.get('name', ''))}" required />
      <input name="url" placeholder="https://example.com/feed.xml" value="{html.escape(form_data.get('url', ''))}" required />
      <input name="category" placeholder="Category (optional)" value="{html.escape(form_data.get('category', ''))}" />
      <button type="submit">Add Feed</button>
    </form>

    <h3>Current Feeds</h3>
    {_render_feed_groups()}
  </section>

  <script>
    const validateButton = document.getElementById('validateBtn');
    const body = document.getElementById('resultsBody');
    const uploadButton = document.getElementById('uploadBtn');
    const message = document.getElementById('message');

    function renderRows(results) {{
      body.innerHTML = '';
      for (const row of results) {{
        const tr = document.createElement('tr');
        const statusText = row.status === 'ok' ? 'ok' : `error: ${{row.reason || 'unknown'}}`;
        tr.innerHTML = `
          <td>${{row.feed}}</td>
          <td>${{row['1 day']}}</td>
          <td>${{row['7 days']}}</td>
          <td>${{row['30 days']}}</td>
          <td class="${{row.status}}">${{statusText}}</td>
        `;
        body.appendChild(tr);
      }}
    }}

    validateButton.addEventListener('click', async () => {{
      validateButton.disabled = true;
      message.textContent = 'Validating feeds...';
      try {{
        const response = await fetch('/validate', {{ method: 'POST' }});
        const result = await response.json();
        renderRows(result);
        message.textContent = `Validated ${{result.length}} feed(s).`;
      }} catch (error) {{
        message.textContent = `Validation failed: ${{error.message}}`;
      }} finally {{
        validateButton.disabled = false;
      }}
    }});

    uploadButton.addEventListener('click', async () => {{
      uploadButton.disabled = true;
      message.textContent = 'Uploading pending EPUBs...';
      try {{
        const response = await fetch('/upload-pending', {{ method: 'POST' }});
        const result = await response.json();
        if (response.ok && result.status === 'partial') {{
          const firstError = result.failed_items?.[0]?.error || 'unknown upload failure';
          message.textContent = `Upload attempted: ${{result.uploaded_now}} uploaded, ${{result.failed_now}} failed, ${{result.pending_after}} still pending. First error: ${{firstError}}`;
        }} else if (response.ok) {{
          message.textContent = `Upload complete: ${{result.uploaded_now}} file(s) uploaded, ${{result.pending_after}} still pending.`;
        }} else {{
          message.textContent = `Upload skipped: device ${{result.host}} unreachable (${{result.pending_before}} pending).`;
        }}
      }} catch (error) {{
        message.textContent = `Upload failed: ${{error.message}}`;
      }} finally {{
        uploadButton.disabled = false;
      }}
    }});
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send_bytes(self, payload: bytes, content_type: str, status: int = HTTPStatus.OK):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_form_data(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        parsed = urllib.parse.parse_qs(raw)
        return {key: values[0] for key, values in parsed.items() if values}

    def _redirect(self, location: str):
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/":
            self._send_bytes(b"Not found", "text/plain; charset=utf-8", HTTPStatus.NOT_FOUND)
            return

        query = urllib.parse.parse_qs(parsed.query)
        form_data = {
            "name": query.get("name", [""])[0],
            "url": query.get("url", [""])[0],
            "category": query.get("category", [""])[0],
            "days_back": query.get("days_back", ["3"])[0],
        }
        html_text = render_index_html(
            message=query.get("message", [""])[0],
            error=query.get("error", [""])[0],
            form_data=form_data,
        )
        self._send_bytes(html_text.encode("utf-8"), "text/html; charset=utf-8")

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

        if self.path == "/feeds":
            form = self._read_form_data()
            name = form.get("name", "")
            url = form.get("url", "")
            category = form.get("category", "")

            if not name.strip() or not url.strip():
                query = urllib.parse.urlencode(
                    {
                        "error": "Name and URL are required.",
                        "name": name,
                        "url": url,
                        "category": category,
                    }
                )
                self._redirect(f"/?{query}#feeds")
                return

            try:
                store.append_feed(name=name, url=url, category=category)
            except ValidationError as exc:
                query = urllib.parse.urlencode(
                    {
                        "error": str(exc),
                        "name": name,
                        "url": url,
                        "category": category,
                    }
                )
                self._redirect(f"/?{query}#feeds")
                return

            query = urllib.parse.urlencode({"message": "Feed added successfully."})
            self._redirect(f"/?{query}#feeds")
            return

        if self.path == "/feeds/delete":
            form = self._read_form_data()
            url = form.get("url", "")
            feed_id = form.get("feed_id", "")

            try:
                deleted = store.delete_feed(url=url, feed_id=feed_id)
            except ValidationError as exc:
                query = urllib.parse.urlencode({"error": str(exc)})
                self._redirect(f"/?{query}#feeds")
                return

            if not deleted:
                query = urllib.parse.urlencode({"error": "Feed not found."})
                self._redirect(f"/?{query}#feeds")
                return

            query = urllib.parse.urlencode({"message": "Feed removed successfully."})
            self._redirect(f"/?{query}#feeds")
            return

        if self.path == "/generate-epub":
            form = self._read_form_data()
            raw_days_back = form.get("days_back", "3")
            try:
                days_back = int(raw_days_back)
            except ValueError:
                query = urllib.parse.urlencode(
                    {"error": "Days back must be an integer.", "days_back": raw_days_back}
                )
                self._redirect(f"/?{query}#generate")
                return

            payload, status = build_generate_epub_payload(days_back)
            if status == HTTPStatus.OK:
                msg = f"Generated EPUB for last {days_back} day(s)."
                query = urllib.parse.urlencode({"message": msg, "days_back": str(days_back)})
            else:
                reason = payload.get("reason", "Failed to generate EPUB")
                query = urllib.parse.urlencode({"error": reason, "days_back": str(days_back)})
            self._redirect(f"/?{query}#generate")
            return

        self._send_bytes(b"Not found", "text/plain; charset=utf-8", HTTPStatus.NOT_FOUND)


def run(host: str = "0.0.0.0", port: int = 5001):
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Server running on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run()
