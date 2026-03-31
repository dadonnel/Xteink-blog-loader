#!/usr/bin/env python3
import datetime as dt
import html
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

from feed_discovery import FeedDiscoveryError, resolve_feed
from feed_service import validate_feeds
from morning_sync import (
    UploadState,
    ensure_records_for_files,
    host_reachable,
    list_epubs,
    try_upload_pending,
    upload_file,
)
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
UPLOAD_PATH = os.environ.get("MORNING_SYNC_UPLOAD_PATH", "/upload")
UPLOAD_FIELD_NAME = os.environ.get("MORNING_SYNC_UPLOAD_FIELD_NAME", "file")
UPLOAD_REACHABILITY_METHOD = os.environ.get("MORNING_SYNC_REACHABILITY_METHOD", "auto")
UPLOAD_TCP_PORT = int(os.environ.get("MORNING_SYNC_TCP_PORT", "80"))
UPLOAD_CONNECT_TIMEOUT = float(os.environ.get("MORNING_SYNC_CONNECT_TIMEOUT", "1.0"))
GENERATE_SCRIPT = Path(os.environ.get("GENERATE_EPUB_SCRIPT", str(BASE_DIR / "3dayblogs.py")))
GENERATE_TIMEOUT_SECONDS = int(os.environ.get("GENERATE_EPUB_TIMEOUT_SECONDS", "900"))

store = OPMLStore(SOURCES_FILE)
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


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


def build_validate_payload(auto_discover_invalid_feeds: bool = False):
    feeds = load_sources()
    results = validate_feeds(
        feeds,
        timeout_s=VALIDATION_TIMEOUT_SECONDS,
        max_workers=VALIDATION_MAX_WORKERS,
        auto_discover_invalid_feeds=auto_discover_invalid_feeds,
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

    try_upload_pending(state, UPLOAD_SYNC_DIR, UPLOAD_HOST, UPLOAD_PATH, UPLOAD_FIELD_NAME)
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




def infer_days_back_from_latest_epub(sync_dir: Path = UPLOAD_SYNC_DIR) -> tuple[int | None, str | None]:
    epub_files = list_epubs(sync_dir)
    if not epub_files:
        return None, f"No EPUB files found in {sync_dir}"

    latest_epub = max(epub_files, key=lambda path: path.stat().st_mtime)
    latest_generated_at = dt.datetime.fromtimestamp(latest_epub.stat().st_mtime, tz=dt.timezone.utc).date()
    today_utc = dt.datetime.now(tz=dt.timezone.utc).date()
    days_back = max(1, (today_utc - latest_generated_at).days)
    return days_back, latest_generated_at.isoformat()


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
        reason = "EPUB generation failed"
        if output:
            reason = output.splitlines()[-1][:240]
        return {
            "status": "error",
            "days_back": days_back,
            "reason": reason,
            "returncode": result.returncode,
            "output": output,
        }, HTTPStatus.INTERNAL_SERVER_ERROR

    return {
        "status": "ok",
        "days_back": days_back,
        "returncode": result.returncode,
        "output": output,
    }, HTTPStatus.OK


def build_upload_latest_epub_payload():
    state = UploadState(UPLOAD_STATE_FILE)
    epub_files = list_epubs(UPLOAD_SYNC_DIR)
    if not epub_files:
        return {
            "status": "error",
            "reason": f"No EPUB files found in {UPLOAD_SYNC_DIR}",
        }, HTTPStatus.NOT_FOUND

    latest_epub = max(epub_files, key=lambda path: path.stat().st_mtime)
    pending = ensure_records_for_files(state, [latest_epub])
    key = pending[0][0] if pending else None

    if not host_reachable(
        UPLOAD_HOST,
        UPLOAD_REACHABILITY_METHOD,
        UPLOAD_TCP_PORT,
        UPLOAD_CONNECT_TIMEOUT,
    ):
        return {
            "status": "unreachable",
            "host": UPLOAD_HOST,
            "filepath": str(latest_epub),
            "filename": latest_epub.name,
            "reason": "Device is unreachable",
        }, HTTPStatus.SERVICE_UNAVAILABLE

    ok, response_text = upload_file(latest_epub, UPLOAD_HOST, UPLOAD_PATH, UPLOAD_FIELD_NAME)
    if key:
        rec = state.records.get(key, {})
        rec["last_attempt_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        rec["attempt_count"] = int(rec.get("attempt_count", 0)) + 1
        rec["uploaded_successfully"] = bool(ok)
        rec["uploaded_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) if ok else None
        rec["error"] = None if ok else response_text
        state.records[key] = rec
        state.save()

    if not ok:
        return {
            "status": "error",
            "host": UPLOAD_HOST,
            "filepath": str(latest_epub),
            "filename": latest_epub.name,
            "reason": response_text or "unknown upload failure",
        }, HTTPStatus.BAD_GATEWAY

    return {
        "status": "ok",
        "host": UPLOAD_HOST,
        "filepath": str(latest_epub),
        "filename": latest_epub.name,
        "response": response_text,
    }, HTTPStatus.OK


def _find_record_for_file(state: UploadState, path: Path) -> tuple[str | None, dict]:
    resolved = str(path.resolve())
    matched = [
        (key, record)
        for key, record in state.records.items()
        if record.get("filepath") == resolved
    ]
    if not matched:
        return None, {}
    matched.sort(key=lambda item: item[1].get("created_at") or "")
    return matched[-1]


def build_file_manager_payload():
    state = UploadState(UPLOAD_STATE_FILE)
    epub_files = list_epubs(UPLOAD_SYNC_DIR)
    ensure_records_for_files(state, epub_files)
    state.save()

    files = []
    for epub_file in epub_files:
        key, record = _find_record_for_file(state, epub_file)
        stat = epub_file.stat()
        files.append(
            {
                "key": key,
                "name": epub_file.name,
                "filepath": str(epub_file.resolve()),
                "size_bytes": stat.st_size,
                "uploaded": bool(record.get("uploaded_successfully", False)),
                "uploaded_at": record.get("uploaded_at"),
                "attempt_count": int(record.get("attempt_count", 0)),
                "error": record.get("error"),
            }
        )

    uploaded_count = sum(1 for item in files if item["uploaded"])
    return {
        "status": "ok",
        "files": files,
        "total_files": len(files),
        "uploaded_files": uploaded_count,
        "pending_files": len(files) - uploaded_count,
    }, HTTPStatus.OK


def build_delete_uploaded_epubs_payload():
    state = UploadState(UPLOAD_STATE_FILE)
    epub_files = list_epubs(UPLOAD_SYNC_DIR)

    deleted_files: list[str] = []
    failed_files: list[dict[str, str]] = []
    for epub_file in epub_files:
        _, record = _find_record_for_file(state, epub_file)
        if not record.get("uploaded_successfully", False):
            continue
        try:
            epub_file.unlink()
            deleted_files.append(epub_file.name)
        except OSError as exc:
            failed_files.append({"name": epub_file.name, "error": str(exc)})

    if deleted_files:
        deleted_paths = {str((UPLOAD_SYNC_DIR / name).resolve()) for name in deleted_files}
        for key in list(state.records.keys()):
            if state.records[key].get("filepath") in deleted_paths:
                del state.records[key]
        state.save()

    status = "ok" if not failed_files else "partial"
    return {
        "status": status,
        "deleted_count": len(deleted_files),
        "deleted_files": deleted_files,
        "failed_count": len(failed_files),
        "failed_files": failed_files,
    }, HTTPStatus.OK


def build_delete_file_payload(filename: str):
    if not filename:
        return {"status": "error", "reason": "filename is required"}, HTTPStatus.BAD_REQUEST

    target = (UPLOAD_SYNC_DIR / filename).resolve()
    if target.parent != UPLOAD_SYNC_DIR.resolve() or target.suffix.lower() != ".epub":
        return {"status": "error", "reason": "invalid filename"}, HTTPStatus.BAD_REQUEST
    if not target.exists() or not target.is_file():
        return {"status": "error", "reason": "file not found"}, HTTPStatus.NOT_FOUND

    state = UploadState(UPLOAD_STATE_FILE)
    try:
        target.unlink()
    except OSError as exc:
        return {"status": "error", "reason": str(exc), "filename": filename}, HTTPStatus.BAD_REQUEST

    target_path = str(target)
    for key in list(state.records.keys()):
        if state.records[key].get("filepath") == target_path:
            del state.records[key]
    state.save()

    return {"status": "ok", "filename": filename}, HTTPStatus.OK


def build_upload_file_payload(filename: str):
    if not filename:
        return {"status": "error", "reason": "filename is required"}, HTTPStatus.BAD_REQUEST

    target = (UPLOAD_SYNC_DIR / filename).resolve()
    if target.parent != UPLOAD_SYNC_DIR.resolve() or target.suffix.lower() != ".epub":
        return {"status": "error", "reason": "invalid filename"}, HTTPStatus.BAD_REQUEST
    if not target.exists() or not target.is_file():
        return {"status": "error", "reason": "file not found"}, HTTPStatus.NOT_FOUND

    if not host_reachable(
        UPLOAD_HOST,
        UPLOAD_REACHABILITY_METHOD,
        UPLOAD_TCP_PORT,
        UPLOAD_CONNECT_TIMEOUT,
    ):
        return {
            "status": "unreachable",
            "host": UPLOAD_HOST,
            "filename": filename,
            "reason": "Device is unreachable",
        }, HTTPStatus.SERVICE_UNAVAILABLE

    state = UploadState(UPLOAD_STATE_FILE)
    pending = ensure_records_for_files(state, [target])
    key = pending[0][0] if pending else _find_record_for_file(state, target)[0]

    ok, response_text = upload_file(target, UPLOAD_HOST, UPLOAD_PATH, UPLOAD_FIELD_NAME)
    if key:
        rec = state.records.get(key, {})
        rec["last_attempt_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        rec["attempt_count"] = int(rec.get("attempt_count", 0)) + 1
        rec["uploaded_successfully"] = bool(ok)
        rec["uploaded_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) if ok else None
        rec["error"] = None if ok else response_text
        state.records[key] = rec
        state.save()

    if not ok:
        return {
            "status": "error",
            "host": UPLOAD_HOST,
            "filename": filename,
            "reason": response_text or "unknown upload failure",
        }, HTTPStatus.BAD_GATEWAY

    return {
        "status": "ok",
        "host": UPLOAD_HOST,
        "filename": filename,
        "response": response_text,
    }, HTTPStatus.OK


def _start_background_job(action: str, worker):
    job_id = uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {
            "job_id": job_id,
            "action": action,
            "status": "running",
            "created_at": time.time(),
            "updated_at": time.time(),
            "logs": [f"Started: {action}"],
            "result": None,
            "http_status": int(HTTPStatus.OK),
        }

    def _run():
        try:
            payload, status = worker()
            with JOBS_LOCK:
                JOBS[job_id]["status"] = "succeeded" if int(status) < 400 else "failed"
                JOBS[job_id]["result"] = payload
                JOBS[job_id]["http_status"] = int(status)
                JOBS[job_id]["updated_at"] = time.time()
                JOBS[job_id]["logs"].append(f"Finished with HTTP {int(status)}")
        except Exception as exc:  # noqa: BLE001
            with JOBS_LOCK:
                JOBS[job_id]["status"] = "failed"
                JOBS[job_id]["result"] = {"status": "error", "reason": str(exc)}
                JOBS[job_id]["http_status"] = int(HTTPStatus.INTERNAL_SERVER_ERROR)
                JOBS[job_id]["updated_at"] = time.time()
                JOBS[job_id]["logs"].append(f"Error: {exc}")

    threading.Thread(target=_run, daemon=True).start()
    return job_id


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


def render_index_html(
    message: str = "", error: str = "", form_data: dict | None = None, active_page: str = "dashboard"
) -> str:
    form_data = form_data or {}
    message_html = f"<p class='ok-msg'>{html.escape(message)}</p>" if message else ""
    error_html = f"<p class='error-msg'>{html.escape(error)}</p>" if error else ""

    dashboard_display = "block" if active_page == "dashboard" else "none"
    epubs_display = "block" if active_page == "epubs" else "none"
    feeds_display = "block" if active_page == "feeds" else "none"
    files_display = "block" if active_page == "files" else "none"

    return f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"UTF-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />
  <title>RSS EPUB Control Panel</title>
  <style>
    body {{ font-family: Inter, Arial, sans-serif; margin: 24px; background: #f4f7fb; color: #1c2430; }}
    .card {{ background: #fff; border: 1px solid #dce3ef; border-radius: 12px; padding: 1rem 1.2rem; margin-top: 1rem; box-shadow: 0 4px 12px rgba(0,0,0,0.04); }}
    nav a {{ margin-right: 1rem; color: #1b4fa8; text-decoration: none; font-weight: 600; }}
    button {{ padding: 8px 14px; font-size: 14px; border-radius: 8px; border: 1px solid #1b4fa8; background: #1b4fa8; color: #fff; }}
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
    pre {{ background: #0b1220; color: #e7edf8; border-radius: 8px; padding: .75rem; min-height: 90px; max-height: 260px; overflow: auto; }}
  </style>
</head>
<body>
  <h1>RSS EPUB Control Panel</h1>
  <nav>
    <a href=\"/dashboard\">Dashboard</a>
    <a href=\"/epubs\">EPUB Operations</a>
    <a href=\"/files\">File Manager</a>
    <a href=\"/feeds\">Feed Manager</a>
  </nav>

  {message_html}
  {error_html}

  <section id=\"dashboard\" class=\"card\" style=\"display: {dashboard_display};\">
    <h2>Feed Validation</h2>
    <label style=\"display:block;margin-bottom:.5rem;\">
      <input id=\"autoDiscoverInvalidFeeds\" type=\"checkbox\" />
      Try automated feed finder for invalid feed URLs
    </label>
    <button id=\"validateBtn\">Validate feeds</button>
    <h2>Upload Pending EPUBs</h2>
    <button id=\"uploadBtn\">Upload pending EPUBs</button>
    <h3>Feed Validation Results</h3>
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
      <tbody id=\"resultsBody\"></tbody>
    </table>
  </section>

  <section id=\"epubs\" class=\"card\" style=\"display: {epubs_display};\">
    <h2>Generate New EPUB</h2>
    <form id=\"generateForm\">
      <label for=\"days_back\">Days back:</label>
      <input id=\"days_back\" type=\"number\" name=\"days_back\" min=\"1\" step=\"1\" value=\"{html.escape(form_data.get('days_back', '3'))}\" required />
      <label style=\"display:block;margin-top:.6rem;\">
        <input id=\"backfillToLastGenerated\" type=\"checkbox\" />
        Auto-calculate days back from the most recently generated EPUB
      </label>
      <button type=\"submit\">Generate new EPUB</button>
    </form>
    <h2>Send Latest Generated EPUB</h2>
    <button id=\"sendLatestBtn\">Send latest EPUB to device</button>
  </section>

  <section id=\"files\" class=\"card\" style=\"display: {files_display};\">
    <h2>File Management</h2>
    <button id=\"refreshFilesBtn\">Refresh file list</button>
    <button id=\"deleteUploadedBtn\">Delete uploaded files</button>
    <table>
      <thead>
        <tr>
          <th>File</th>
          <th>Size</th>
          <th>Uploaded</th>
          <th>Attempts</th>
          <th>Error</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody id=\"filesBody\"></tbody>
    </table>
  </section>

  <section class=\"card\">
    <h2>Progress & Error Feedback</h2>
    <p id=\"message\"></p>
    <pre id=\"jobLog\">No active jobs.</pre>
  </section>

  <section id=\"feeds\" class=\"card\" style=\"display: {feeds_display};\">
    <h2>Feed Manager</h2>
    <h3>Add Feed</h3>
    <form method=\"post\" action=\"/feeds\">
      <input name=\"name\" placeholder=\"Feed Name (optional)\" value=\"{html.escape(form_data.get('name', ''))}\" />
      <input name=\"url\" placeholder=\"https://example.com/post/slug\" value=\"{html.escape(form_data.get('url', ''))}\" required />
      <input name=\"category\" placeholder=\"Category (optional override)\" value=\"{html.escape(form_data.get('category', ''))}\" />
      <button type=\"submit\">Add Feed</button>
    </form>

    <h3>Current Feeds</h3>
    {_render_feed_groups()}
  </section>

  <script>
    const validateButton = document.getElementById('validateBtn');
    const autoDiscoverInvalidFeedsCheckbox = document.getElementById('autoDiscoverInvalidFeeds');
    const tableBody = document.getElementById('resultsBody');
    const uploadButton = document.getElementById('uploadBtn');
    const sendLatestButton = document.getElementById('sendLatestBtn');
    const generateForm = document.getElementById('generateForm');
    const refreshFilesBtn = document.getElementById('refreshFilesBtn');
    const deleteUploadedBtn = document.getElementById('deleteUploadedBtn');
    const filesBody = document.getElementById('filesBody');
    const message = document.getElementById('message');
    const jobLog = document.getElementById('jobLog');
    let activeJobId = null;

    function renderRows(results) {{
      if (!tableBody) return;
      tableBody.innerHTML = '';
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
        tableBody.appendChild(tr);
      }}
    }}

    function setButtonsDisabled(disabled) {{
      if (validateButton) validateButton.disabled = disabled;
      if (uploadButton) uploadButton.disabled = disabled;
      if (sendLatestButton) sendLatestButton.disabled = disabled;
      if (refreshFilesBtn) refreshFilesBtn.disabled = disabled;
      if (deleteUploadedBtn) deleteUploadedBtn.disabled = disabled;
      const submitBtn = generateForm ? generateForm.querySelector('button[type="submit"]') : null;
      if (submitBtn) submitBtn.disabled = disabled;
    }}

    function formatBytes(bytes) {{
      const size = Number(bytes || 0);
      if (size < 1024) return `${{size}} B`;
      if (size < 1024 * 1024) return `${{(size / 1024).toFixed(1)}} KB`;
      return `${{(size / (1024 * 1024)).toFixed(1)}} MB`;
    }}

    function renderFilesTable(files) {{
      if (!filesBody) return;
      filesBody.innerHTML = '';
      if (!files.length) {{
        const tr = document.createElement('tr');
        tr.innerHTML = '<td colspan=\"6\">No EPUB files found in output folder.</td>';
        filesBody.appendChild(tr);
        return;
      }}

      for (const file of files) {{
        const tr = document.createElement('tr');
        const uploadedLabel = file.uploaded ? `Yes${{file.uploaded_at ? ` (${{file.uploaded_at}})` : ''}}` : 'No';
        tr.innerHTML = `
          <td>${{file.name}}</td>
          <td>${{formatBytes(file.size_bytes)}}</td>
          <td class=\"${{file.uploaded ? 'ok' : ''}}\">${{uploadedLabel}}</td>
          <td>${{file.attempt_count || 0}}</td>
          <td>${{file.error || ''}}</td>
          <td>
            <button type=\"button\" data-action=\"download\" data-name=\"${{file.name}}\">Download</button>
            <button type=\"button\" data-action=\"send\" data-name=\"${{file.name}}\">Send</button>
            <button type=\"button\" data-action=\"delete\" data-name=\"${{file.name}}\">Delete</button>
          </td>
        `;
        filesBody.appendChild(tr);
      }}
    }}

    async function loadFiles() {{
      const response = await fetch('/api/files');
      const payload = await response.json();
      renderFilesTable(payload.files || []);
      return payload;
    }}

    async function pollJob(jobId, onSuccess) {{
      activeJobId = jobId;
      setButtonsDisabled(true);
      while (activeJobId === jobId) {{
        const response = await fetch(`/api/jobs/${{jobId}}`);
        const payload = await response.json();
        jobLog.textContent = (payload.logs || []).join('\\n');
        if (payload.status === 'succeeded' || payload.status === 'failed') {{
          const result = payload.result || {{}};
          if (Array.isArray(result)) renderRows(result);
          if (payload.status === 'succeeded') {{
            if (onSuccess) onSuccess(result);
          }} else {{
            const reason = result.reason || result.output || 'Unknown error';
            message.textContent = `Failed: ${{reason}}`;
          }}
          setButtonsDisabled(false);
          activeJobId = null;
          return;
        }}
        await new Promise((resolve) => setTimeout(resolve, 800));
      }}
    }}

    if (validateButton) {{
      validateButton.addEventListener('click', async () => {{
        message.textContent = 'Validating feeds...';
        try {{
          const autoDiscoverInvalidFeeds = !!(autoDiscoverInvalidFeedsCheckbox && autoDiscoverInvalidFeedsCheckbox.checked);
          const response = await fetch('/api/validate', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ auto_discover_invalid_feeds: autoDiscoverInvalidFeeds }}),
          }});
          const result = await response.json();
          await pollJob(result.job_id, (payload) => {{
            message.textContent = `Validated ${{payload.length || 0}} feed(s).`;
          }});
        }} catch (error) {{
          message.textContent = `Validation failed: ${{error.message}}`;
        }}
      }});
    }}

    if (uploadButton) {{
      uploadButton.addEventListener('click', async () => {{
        message.textContent = 'Uploading pending EPUBs...';
        try {{
          const response = await fetch('/api/upload-pending', {{ method: 'POST' }});
          const result = await response.json();
          await pollJob(result.job_id, (payload) => {{
            if (payload.status === 'partial') {{
              const firstFailedItem = payload.failed_items && payload.failed_items.length ? payload.failed_items[0] : null;
              const firstError = firstFailedItem && firstFailedItem.error ? firstFailedItem.error : 'unknown upload failure';
              message.textContent = `Upload attempted: ${{payload.uploaded_now}} uploaded, ${{payload.failed_now}} failed, ${{payload.pending_after}} pending. First error: ${{firstError}}`;
            }} else {{
              message.textContent = `Upload complete: ${{payload.uploaded_now || 0}} uploaded.`;
            }}
          }});
        }} catch (error) {{
          message.textContent = `Upload failed: ${{error.message}}`;
        }}
      }});
    }}

    if (generateForm) {{
      generateForm.addEventListener('submit', async (event) => {{
        event.preventDefault();
        const daysBack = parseInt(document.getElementById('days_back').value, 10);
        const backfillToLastGenerated = !!document.getElementById('backfillToLastGenerated')?.checked;
        message.textContent = backfillToLastGenerated
          ? 'Generating EPUB from the date of the most recent EPUB...'
          : `Generating EPUB for last ${{daysBack}} day(s)...`;
        try {{
          const response = await fetch('/api/epubs/generate', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{
              days_back: daysBack,
              backfill_to_last_generated: backfillToLastGenerated,
            }}),
          }});
          const result = await response.json();
          if (!response.ok) {{
            message.textContent = `Generation failed: ${{result.reason || result.error || 'unknown'}}`;
            return;
          }}
          if (!result.job_id) {{
            message.textContent = 'Generation failed: missing job id from server response.';
            return;
          }}
          await pollJob(result.job_id, (payload) => {{
            if (payload.status === 'ok') {{
              if (payload.inferred_from_last_generated) {{
                message.textContent = `Generation complete for ${{payload.days_back}} day(s) (from last EPUB date ${{payload.latest_generated_date}}).`;
              }} else {{
                message.textContent = `Generation complete for ${{payload.days_back}} day(s).`;
              }}
            }} else {{
              message.textContent = `Generation failed: ${{payload.reason || 'unknown'}}`;
            }}
          }});
        }} catch (error) {{
          message.textContent = `Generation failed: ${{error.message}}`;
        }}
      }});
    }}

    if (sendLatestButton) {{
      sendLatestButton.addEventListener('click', async () => {{
        message.textContent = 'Sending latest generated EPUB...';
        try {{
          const response = await fetch('/api/epubs/send-latest', {{ method: 'POST' }});
          const result = await response.json();
          await pollJob(result.job_id, (payload) => {{
            if (payload.status === 'ok') {{
              message.textContent = `Sent ${{payload.filename}} to ${{payload.host}}`;
            }} else {{
              message.textContent = `Send failed: ${{payload.reason || 'unknown error'}}`;
            }}
          }});
        }} catch (error) {{
          message.textContent = `Send failed: ${{error.message}}`;
        }}
      }});
    }}

    if (refreshFilesBtn) {{
      refreshFilesBtn.addEventListener('click', async () => {{
        try {{
          const payload = await loadFiles();
          message.textContent = `Loaded ${{payload.total_files || 0}} file(s): ${{payload.uploaded_files || 0}} uploaded, ${{payload.pending_files || 0}} pending.`;
        }} catch (error) {{
          message.textContent = `Failed to load files: ${{error.message}}`;
        }}
      }});
    }}

    if (deleteUploadedBtn) {{
      deleteUploadedBtn.addEventListener('click', async () => {{
        try {{
          const response = await fetch('/api/files/delete-uploaded', {{ method: 'POST' }});
          const payload = await response.json();
          await loadFiles();
          message.textContent = `Deleted ${{payload.deleted_count || 0}} uploaded file(s).`;
        }} catch (error) {{
          message.textContent = `Failed to delete uploaded files: ${{error.message}}`;
        }}
      }});
    }}

    if (filesBody) {{
      filesBody.addEventListener('click', async (event) => {{
        const target = event.target;
        if (!target || target.tagName !== 'BUTTON') return;
        const action = target.getAttribute('data-action');
        const filename = target.getAttribute('data-name');
        if (!filename || !action) return;
        if (action === 'download') {{
          window.location.href = `/api/files/download?filename=${{encodeURIComponent(filename)}}`;
          return;
        }}
        try {{
          if (action === 'send') {{
            const response = await fetch('/api/files/send', {{
              method: 'POST',
              headers: {{ 'Content-Type': 'application/json' }},
              body: JSON.stringify({{ filename }}),
            }});
            const payload = await response.json();
            message.textContent = payload.status === 'ok'
              ? `Sent ${{filename}} to ${{payload.host}}.`
              : `Send failed for ${{filename}}: ${{payload.reason || 'unknown error'}}`;
          }}
          if (action === 'delete') {{
            const response = await fetch('/api/files/delete', {{
              method: 'POST',
              headers: {{ 'Content-Type': 'application/json' }},
              body: JSON.stringify({{ filename }}),
            }});
            const payload = await response.json();
            message.textContent = payload.status === 'ok'
              ? `Deleted ${{filename}}.`
              : `Delete failed for ${{filename}}: ${{payload.reason || 'unknown error'}}`;
          }}
          await loadFiles();
        }} catch (error) {{
          message.textContent = `File action failed: ${{error.message}}`;
        }}
      }});
    }}

    if (window.location.pathname === '/files') {{
      loadFiles().catch((error) => {{
        message.textContent = `Failed to load files: ${{error.message}}`;
      }});
    }}
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

    def _send_file(self, path: Path, download_name: str):
        payload = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/epub+zip")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.end_headers()
        self.wfile.write(payload)

    def _read_form_data(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        parsed = urllib.parse.parse_qs(raw)
        return {key: values[0] for key, values in parsed.items() if values}

    def _read_json_data(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def _redirect(self, location: str):
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/api/files":
            payload, status = build_file_manager_payload()
            self._send_bytes(
                json.dumps(payload).encode("utf-8"),
                "application/json; charset=utf-8",
                status,
            )
            return

        if parsed.path == "/api/files/download":
            query = urllib.parse.parse_qs(parsed.query)
            filename = query.get("filename", [""])[0]
            target = (UPLOAD_SYNC_DIR / filename).resolve() if filename else None
            if (
                not target
                or target.parent != UPLOAD_SYNC_DIR.resolve()
                or target.suffix.lower() != ".epub"
                or not target.exists()
                or not target.is_file()
            ):
                self._send_bytes(
                    json.dumps({"status": "error", "reason": "file not found"}).encode("utf-8"),
                    "application/json; charset=utf-8",
                    HTTPStatus.NOT_FOUND,
                )
                return
            self._send_file(target, target.name)
            return

        if parsed.path.startswith("/api/jobs/"):
            job_id = parsed.path.rsplit("/", 1)[-1]
            with JOBS_LOCK:
                payload = JOBS.get(job_id)
            if payload is None:
                self._send_bytes(
                    json.dumps({"status": "error", "reason": "Job not found"}).encode("utf-8"),
                    "application/json; charset=utf-8",
                    HTTPStatus.NOT_FOUND,
                )
                return
            self._send_bytes(
                json.dumps(payload).encode("utf-8"),
                "application/json; charset=utf-8",
                HTTPStatus.OK,
            )
            return

        if parsed.path == "/":
            self._redirect("/dashboard")
            return

        if parsed.path not in {"/dashboard", "/epubs", "/feeds", "/files"}:
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
            active_page=parsed.path.lstrip("/"),
        )
        self._send_bytes(html_text.encode("utf-8"), "text/html; charset=utf-8")

    def do_POST(self):
        if self.path == "/api/validate":
            payload = self._read_json_data()
            auto_discover_invalid_feeds = bool(payload.get("auto_discover_invalid_feeds"))
            job_id = _start_background_job(
                "validate",
                lambda: (build_validate_payload(auto_discover_invalid_feeds), HTTPStatus.OK),
            )
            self._send_bytes(
                json.dumps({"status": "accepted", "job_id": job_id}).encode("utf-8"),
                "application/json; charset=utf-8",
                HTTPStatus.ACCEPTED,
            )
            return

        if self.path == "/api/upload-pending":
            job_id = _start_background_job("upload-pending", build_manual_upload_payload)
            self._send_bytes(
                json.dumps({"status": "accepted", "job_id": job_id}).encode("utf-8"),
                "application/json; charset=utf-8",
                HTTPStatus.ACCEPTED,
            )
            return

        if self.path == "/api/epubs/send-latest":
            job_id = _start_background_job("send-latest-epub", build_upload_latest_epub_payload)
            self._send_bytes(
                json.dumps({"status": "accepted", "job_id": job_id}).encode("utf-8"),
                "application/json; charset=utf-8",
                HTTPStatus.ACCEPTED,
            )
            return

        if self.path == "/api/epubs/generate":
            payload = self._read_json_data()
            backfill_to_last_generated = bool(payload.get("backfill_to_last_generated"))
            if backfill_to_last_generated:
                inferred_days_back, latest_generated_date = infer_days_back_from_latest_epub()
                if inferred_days_back is None:
                    self._send_bytes(
                        json.dumps({"status": "error", "reason": latest_generated_date}).encode("utf-8"),
                        "application/json; charset=utf-8",
                        HTTPStatus.BAD_REQUEST,
                    )
                    return
                days_back = inferred_days_back
            else:
                latest_generated_date = None
                try:
                    days_back = int(payload.get("days_back", 3))
                except (TypeError, ValueError):
                    self._send_bytes(
                        json.dumps({"status": "error", "reason": "days_back must be an integer"}).encode(
                            "utf-8"
                        ),
                        "application/json; charset=utf-8",
                        HTTPStatus.BAD_REQUEST,
                    )
                    return

            def _generate_epub_job():
                result_payload, status = build_generate_epub_payload(days_back)
                if status == HTTPStatus.OK and backfill_to_last_generated:
                    result_payload["inferred_from_last_generated"] = True
                    result_payload["latest_generated_date"] = latest_generated_date
                return result_payload, status

            job_id = _start_background_job("generate-epub", _generate_epub_job)
            self._send_bytes(
                json.dumps({"status": "accepted", "job_id": job_id}).encode("utf-8"),
                "application/json; charset=utf-8",
                HTTPStatus.ACCEPTED,
            )
            return

        if self.path == "/api/files/delete-uploaded":
            payload, status = build_delete_uploaded_epubs_payload()
            self._send_bytes(
                json.dumps(payload).encode("utf-8"),
                "application/json; charset=utf-8",
                status,
            )
            return

        if self.path == "/api/files/delete":
            payload = self._read_json_data()
            response, status = build_delete_file_payload(str(payload.get("filename", "")))
            self._send_bytes(
                json.dumps(response).encode("utf-8"),
                "application/json; charset=utf-8",
                status,
            )
            return

        if self.path == "/api/files/send":
            payload = self._read_json_data()
            response, status = build_upload_file_payload(str(payload.get("filename", "")))
            self._send_bytes(
                json.dumps(response).encode("utf-8"),
                "application/json; charset=utf-8",
                status,
            )
            return

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

            if not url.strip():
                query = urllib.parse.urlencode(
                    {
                        "error": "URL is required.",
                        "name": name,
                        "url": url,
                        "category": category,
                    }
                )
                self._redirect(f"/feeds?{query}")
                return

            existing_categories: list[str] = []
            try:
                existing_categories = list(store.parse_feeds().keys())
            except (FileNotFoundError, ET.ParseError):
                existing_categories = []

            try:
                resolved = resolve_feed(url, existing_categories)
                final_name = name.strip() or resolved.name
                final_category = category.strip() or resolved.category
                store.append_feed(
                    name=final_name,
                    url=resolved.feed_url,
                    category=final_category,
                    feed_type=resolved.feed_type,
                )
            except (FeedDiscoveryError, ValidationError, FileNotFoundError, ET.ParseError) as exc:
                query = urllib.parse.urlencode(
                    {
                        "error": str(exc),
                        "name": name,
                        "url": url,
                        "category": category,
                    }
                )
                self._redirect(f"/feeds?{query}")
                return

            query = urllib.parse.urlencode(
                {
                    "message": f"Feed added: {final_name} ({final_category}).",
                }
            )
            self._redirect(f"/feeds?{query}")
            return

        if self.path == "/feeds/delete":
            form = self._read_form_data()
            url = form.get("url", "")
            feed_id = form.get("feed_id", "")

            try:
                deleted = store.delete_feed(url=url, feed_id=feed_id)
            except ValidationError as exc:
                query = urllib.parse.urlencode({"error": str(exc)})
                self._redirect(f"/feeds?{query}")
                return

            if not deleted:
                query = urllib.parse.urlencode({"error": "Feed not found."})
                self._redirect(f"/feeds?{query}")
                return

            query = urllib.parse.urlencode({"message": "Feed removed successfully."})
            self._redirect(f"/feeds?{query}")
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
                self._redirect(f"/epubs?{query}")
                return

            payload, status = build_generate_epub_payload(days_back)
            if status == HTTPStatus.OK:
                msg = f"Generated EPUB for last {days_back} day(s)."
                query = urllib.parse.urlencode({"message": msg, "days_back": str(days_back)})
            else:
                reason = payload.get("reason", "Failed to generate EPUB")
                query = urllib.parse.urlencode({"error": reason, "days_back": str(days_back)})
            self._redirect(f"/epubs?{query}")
            return

        self._send_bytes(b"Not found", "text/plain; charset=utf-8", HTTPStatus.NOT_FOUND)


def run(host: str = "0.0.0.0", port: int = 5001):
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Server running on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run()
