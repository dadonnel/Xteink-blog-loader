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
    make_record_key,
    try_upload_pending,
    upload_file,
)
from opml_store import OPMLStore, ValidationError

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR / "src"))

from rss_epub.config import HISTORY_FILE as DEFAULT_HISTORY_FILE, JOBS_FILE as DEFAULT_JOBS_FILE
from rss_epub.history import HistoryStore
from rss_epub.settings import SettingsStore
SOURCES_FILE = os.environ.get("SOURCES_FILE", str(BASE_DIR / "feeds.opml"))
VALIDATION_TIMEOUT_SECONDS = int(os.environ.get("VALIDATION_TIMEOUT_SECONDS", "10"))
VALIDATION_MAX_WORKERS = int(os.environ.get("VALIDATION_MAX_WORKERS", "10"))
UPLOAD_HOST = os.environ.get("MORNING_SYNC_HOST", "192.168.1.211")
UPLOAD_SYNC_DIR = Path(
    os.environ.get("MORNING_SYNC_SYNC_DIR", str(Path(os.environ.get("OUTPUT_DIR", "storage/downloads/rss_epub/output_epubs")) / "xteink_sync"))
)
UPLOAD_STATE_FILE = Path(
    os.environ.get("MORNING_SYNC_STATE_FILE", "storage/downloads/rss_epub/upload_state.json")
)
UPLOAD_PATH = os.environ.get("MORNING_SYNC_UPLOAD_PATH", "/upload")
UPLOAD_FIELD_NAME = os.environ.get("MORNING_SYNC_UPLOAD_FIELD_NAME", "file")
UPLOAD_REACHABILITY_METHOD = os.environ.get("MORNING_SYNC_REACHABILITY_METHOD", "auto")
UPLOAD_TCP_PORT = int(os.environ.get("MORNING_SYNC_TCP_PORT", "80"))
UPLOAD_CONNECT_TIMEOUT = float(os.environ.get("MORNING_SYNC_CONNECT_TIMEOUT", "1.0"))
HISTORY_FILE = Path(os.environ.get("EDITION_HISTORY_FILE", str(DEFAULT_HISTORY_FILE)))
JOBS_STATE_FILE = Path(os.environ.get("JOBS_STATE_FILE", str(DEFAULT_JOBS_FILE)))
GENERATE_SCRIPT = Path(os.environ.get("GENERATE_EPUB_SCRIPT", str(BASE_DIR / "3dayblogs.py")))
GENERATE_TIMEOUT_SECONDS = int(os.environ.get("GENERATE_EPUB_TIMEOUT_SECONDS", "900"))

store = OPMLStore(SOURCES_FILE)
settings_store = SettingsStore()
LAST_DEVICE_CHECK: dict = {"status": "not_checked", "checked_at": None}
def _load_jobs() -> dict[str, dict]:
    if not JOBS_STATE_FILE.exists():
        return {}
    try:
        loaded = json.loads(JOBS_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    for job in loaded.values():
        if job.get("status") == "running":
            job["status"] = "interrupted"
            job["stage"] = "interrupted"
            job["logs"] = list(job.get("logs") or []) + ["Server restarted before this job completed."]
    return loaded


JOBS: dict[str, dict] = _load_jobs()
JOBS_LOCK = threading.Lock()


def _save_jobs_locked() -> None:
    JOBS_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = JOBS_STATE_FILE.with_suffix(JOBS_STATE_FILE.suffix + ".tmp")
    temp.write_text(json.dumps(JOBS, indent=2, sort_keys=True), encoding="utf-8")
    temp.replace(JOBS_STATE_FILE)


def _set_job_stage(action: str, stage: str, log: str) -> None:
    with JOBS_LOCK:
        for job in JOBS.values():
            if job.get("action") == action and job.get("status") == "running":
                job["stage"] = stage
                job["updated_at"] = time.time()
                job.setdefault("logs", []).append(log)
                _save_jobs_locked()
                return


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
        if (outline.get("paused") or "false").lower() == "true":
            continue
        name = outline.get("text") or outline.get("title") or "Untitled Feed"
        url = outline.get("xmlUrl")
        if url:
            feeds.append({"name": name, "url": url, "feed_type": outline.get("type") or "rss"})

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


def build_preflight_payload() -> dict:
    checks = []
    for label, path in (("subscriptions", Path(SOURCES_FILE)), ("edition output", UPLOAD_SYNC_DIR), ("state", HISTORY_FILE.parent)):
        target = path if label == "subscriptions" else path
        exists = target.exists()
        writable_parent = os.access(target if target.is_dir() else target.parent, os.W_OK) if (target if target.is_dir() else target.parent).exists() else os.access((target if target.is_dir() else target.parent).parent, os.W_OK)
        checks.append({"name": label, "ok": bool(exists or writable_parent), "detail": str(target)})
    try:
        feed_count = sum(len(items) for items in store.parse_feeds().values())
    except (FileNotFoundError, ET.ParseError):
        feed_count = 0
    checks.append({"name": "active subscriptions", "ok": feed_count > 0, "detail": f"{feed_count} configured; zero is supported but cannot create an edition"})
    settings = settings_store.effective()
    checks.append({"name": "device address", "ok": bool(settings.get("device_address")), "detail": settings.get("device_address") or "not configured"})
    return {"status": "ok" if all(item["ok"] for item in checks if item["name"] != "active subscriptions") else "attention", "checks": checks}


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


def build_generate_epub_payload(days_back: int, *, include_previous: bool = False, preset: str = "full"):
    if days_back < 1:
        return {
            "status": "error",
            "reason": "days_back must be 1 or greater",
        }, HTTPStatus.BAD_REQUEST

    env = os.environ.copy()
    env["DAYS_BACK"] = str(days_back)
    env["INCLUDE_PREVIOUS_ARTICLES"] = "true" if include_previous else "false"
    env["EDITION_PRESET"] = preset if preset in {"quick", "commute", "full"} else "full"

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

    structured = None
    for line in reversed(result.stdout.splitlines()):
        if line.startswith("XTEINK_RESULT_JSON="):
            try:
                structured = json.loads(line.split("=", 1)[1])
            except json.JSONDecodeError:
                structured = None
            break

    if result.returncode != 0 or not structured:
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

    structured.update({"days_back": days_back, "returncode": result.returncode, "output": output})
    # A clean process exit is not proof that a book exists.
    if structured.get("status") not in {"created", "created_with_warnings", "no_new_articles", "no_extractable_articles"}:
        structured.setdefault("reason", "Generation failed")
        return structured, HTTPStatus.INTERNAL_SERVER_ERROR
    return structured, HTTPStatus.OK


def build_upload_edition_payload(edition_id: str):
    """Upload exactly the edition named by a generation result."""
    history = HistoryStore(HISTORY_FILE)
    edition = history.load()["editions"].get(edition_id)
    if not edition:
        return {"status": "error", "reason": "Edition not found or expired.", "edition_id": edition_id}, HTTPStatus.NOT_FOUND
    target = Path(edition.get("sync_file") or edition.get("output_file") or "")
    if not target.exists() or not target.is_file():
        return {"status": "error", "reason": "The local copy for this edition is missing.", "edition_id": edition_id}, HTTPStatus.NOT_FOUND
    upload_state = UploadState(UPLOAD_STATE_FILE)
    pending = ensure_records_for_files(upload_state, [target])
    upload_key = pending[0][0] if pending else _find_record_for_file(upload_state, target)[0]
    host = settings_store.effective()["device_address"]
    if not host_reachable(host, UPLOAD_REACHABILITY_METHOD, UPLOAD_TCP_PORT, UPLOAD_CONNECT_TIMEOUT):
        history.update_delivery(edition_id, status="queued", detail="Device unreachable")
        upload_state.save()
        return {
            "status": "queued", "edition_id": edition_id, "filename": target.name,
            "host": host, "reason": "Device is offline. This edition remains queued for retry.",
        }, HTTPStatus.SERVICE_UNAVAILABLE
    ok, response_text = upload_file(target, host, UPLOAD_PATH, UPLOAD_FIELD_NAME)
    if upload_key:
        record = upload_state.records[upload_key]
        record["last_attempt_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        record["attempt_count"] = int(record.get("attempt_count", 0)) + 1
        record["uploaded_successfully"] = bool(ok)
        record["uploaded_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) if ok else None
        record["error"] = None if ok else response_text
        record["edition_id"] = edition_id
        upload_state.save()
    history.update_delivery(edition_id, status="uploaded" if ok else "failed", detail=response_text or None)
    if not ok:
        return {"status": "error", "edition_id": edition_id, "filename": target.name, "reason": response_text or "Upload failed"}, HTTPStatus.BAD_GATEWAY
    return {
        "status": "uploaded", "edition_id": edition_id, "filename": target.name,
        "host": host, "response": response_text,
        "delivery_claim": "The device accepted the upload request; on-screen library visibility was not verified.",
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
    try:
        current_key = make_record_key(path)
    except OSError:
        return None, {}
    if current_key in state.records:
        return current_key, state.records[current_key]
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
    newest = max(epub_files, key=lambda path: path.stat().st_mtime, default=None)
    trash_dir = UPLOAD_SYNC_DIR / ".trash"
    for epub_file in epub_files:
        if newest is not None and epub_file == newest:
            continue
        _, record = _find_record_for_file(state, epub_file)
        if not record.get("uploaded_successfully", False):
            continue
        try:
            trash_dir.mkdir(parents=True, exist_ok=True)
            destination = trash_dir / epub_file.name
            if destination.exists():
                destination = trash_dir / f"{int(time.time())}-{epub_file.name}"
            epub_file.replace(destination)
            deleted_files.append(epub_file.name)
        except OSError as exc:
            failed_files.append({"name": epub_file.name, "error": str(exc)})

    state.save()

    status = "ok" if not failed_files else "partial"
    return {
        "status": status,
        "deleted_count": len(deleted_files),
        "deleted_files": deleted_files,
        "failed_count": len(failed_files),
        "failed_files": failed_files,
        "preserved_newest": newest.name if newest else None,
        "recoverable": True,
        "trash_directory": str(trash_dir),
    }, HTTPStatus.OK


def build_cleanup_preview_payload():
    state = UploadState(UPLOAD_STATE_FILE)
    files = list_epubs(UPLOAD_SYNC_DIR)
    ensure_records_for_files(state, files)
    newest = max(files, key=lambda path: path.stat().st_mtime, default=None)
    eligible = []
    for path in files:
        _, record = _find_record_for_file(state, path)
        if path != newest and record.get("uploaded_successfully", False):
            eligible.append(path.name)
    return {"status": "ok", "files": eligible, "preserved_newest": newest.name if newest else None, "action": "move_to_trash"}, HTTPStatus.OK


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
        trash_dir = UPLOAD_SYNC_DIR / ".trash"
        trash_dir.mkdir(parents=True, exist_ok=True)
        destination = trash_dir / target.name
        if destination.exists():
            destination = trash_dir / f"{int(time.time())}-{target.name}"
        target.replace(destination)
    except OSError as exc:
        return {"status": "error", "reason": str(exc), "filename": filename}, HTTPStatus.BAD_REQUEST

    state.save()

    return {"status": "ok", "filename": filename, "recoverable": True, "trash_name": destination.name}, HTTPStatus.OK


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
    with JOBS_LOCK:
        for existing in JOBS.values():
            if existing.get("action") == action and existing.get("status") == "running":
                return existing["job_id"]
        job_id = uuid4().hex
        JOBS[job_id] = {
            "job_id": job_id,
            "action": action,
            "status": "running",
            "stage": "starting",
            "created_at": time.time(),
            "updated_at": time.time(),
            "logs": [f"Started: {action}"],
            "result": None,
            "http_status": int(HTTPStatus.OK),
        }
        _save_jobs_locked()

    def _run():
        try:
            payload, status = worker()
            with JOBS_LOCK:
                JOBS[job_id]["status"] = "succeeded" if int(status) < 400 else "failed"
                JOBS[job_id]["stage"] = "complete" if int(status) < 400 else "failed"
                JOBS[job_id]["result"] = payload
                JOBS[job_id]["http_status"] = int(status)
                JOBS[job_id]["updated_at"] = time.time()
                JOBS[job_id]["logs"].append(f"Finished with HTTP {int(status)}")
                _save_jobs_locked()
        except Exception as exc:  # noqa: BLE001
            with JOBS_LOCK:
                JOBS[job_id]["status"] = "failed"
                JOBS[job_id]["stage"] = "failed"
                JOBS[job_id]["result"] = {"status": "error", "reason": str(exc)}
                JOBS[job_id]["http_status"] = int(HTTPStatus.INTERNAL_SERVER_ERROR)
                JOBS[job_id]["updated_at"] = time.time()
                JOBS[job_id]["logs"].append(f"Error: {exc}")
                _save_jobs_locked()

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
            state_label = "Paused" if feed.paused else "Active"
            parts.append(
                "<li><div class='feed-row'>"
                f"<strong>{html.escape(feed.name)}</strong> "
                f"<span class='url'>{html.escape(feed.url)}</span>"
                f"<span>{state_label}</span>"
                "<form method='post' action='/feeds/pause' style='display:inline'>"
                f"<input type='hidden' name='feed_id' value='{html.escape(feed.feed_id)}'/>"
                f"<input type='hidden' name='paused' value='{'false' if feed.paused else 'true'}'/>"
                f"<button type='submit'>{'Resume' if feed.paused else 'Pause'}</button></form>"
                "<form method='post' action='/feeds/delete' style='display:inline'>"
                f"<input type='hidden' name='feed_id' value='{html.escape(feed.feed_id)}'/>"
                "<button class='delete-feed' type='submit'>Remove subscription</button>"
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
    settings_display = "block" if active_page == "settings" else "none"
    nav_class = lambda page: "active" if active_page == page else ""

    return f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"UTF-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />
  <title>RSS EPUB Control Panel</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ font-family: Inter, Arial, sans-serif; margin: 24px; background: #f4f7fb; color: #1c2430; line-height: 1.45; }}
    main {{ max-width: 1080px; margin: auto; }}
    .card {{ background: #fff; border: 1px solid #dce3ef; border-radius: 12px; padding: 1rem 1.2rem; margin-top: 1rem; box-shadow: 0 4px 12px rgba(0,0,0,0.04); }}
    nav {{ display:flex; gap:.35rem; overflow-x:auto; padding:.25rem 0; }}
    nav a {{ padding:.7rem .8rem; color: #1b4fa8; text-decoration: none; font-weight: 600; border-radius:8px; white-space:nowrap; }}
    nav a.active {{ color:#fff; background:#1b4fa8; }}
    button, input, select {{ min-height: 44px; font: inherit; }}
    button {{ padding: 8px 14px; border-radius: 8px; border: 1px solid #1b4fa8; background: #1b4fa8; color: #fff; cursor:pointer; }}
    button:focus-visible, input:focus-visible, select:focus-visible, a:focus-visible {{ outline:3px solid #f59e0b; outline-offset:2px; }}
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
    .readiness {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:.75rem; }}
    .metric {{ padding:.8rem; background:#f7f9fc; border-radius:8px; }}
    .metric strong {{ display:block; font-size:1.1rem; }}
    .primary {{ font-size:1.05rem; padding:.85rem 1.1rem; }}
    .edition-actions {{ display:flex; gap:.5rem; flex-wrap:wrap; margin-top:.75rem; }}
    @media (max-width: 640px) {{
      body {{ margin:12px; }} .card {{ padding:.85rem; }}
      table, thead, tbody, tr, th, td {{ display:block; }} thead {{ position:absolute; left:-9999px; }}
      tr {{ border:1px solid #dce3ef; border-radius:10px; padding:.65rem; margin:.65rem 0; }}
      td {{ border:0; padding:.3rem 0; overflow-wrap:anywhere; }}
      td::before {{ content:attr(data-label); font-weight:700; display:inline-block; min-width:7rem; }}
    }}
  </style>
</head>
<body><main>
  <h1>RSS EPUB Control Panel</h1>
  <nav>
    <a class=\"{nav_class('dashboard')}\" href=\"/dashboard\">Dashboard</a>
    <a class=\"{nav_class('epubs')}\" href=\"/epubs\">Prepare</a>
    <a class=\"{nav_class('files')}\" href=\"/files\">Local copies</a>
    <a class=\"{nav_class('feeds')}\" href=\"/feeds\">Subscriptions</a>
    <a class=\"{nav_class('settings')}\" href=\"/settings\">Settings</a>
  </nav>

  {message_html}
  {error_html}

  <section id=\"dashboard\" class=\"card\" style=\"display: {dashboard_display};\">
    <h2>Reading readiness</h2>
    <div class=\"readiness\" id=\"readiness\"><div class=\"metric\">Latest edition<strong>Loading…</strong></div></div>
    <p><button class=\"primary\" id=\"prepareSendBtn\">Prepare &amp; send new reading</button></p>
    <div id=\"latestActions\" class=\"edition-actions\"></div>
    <details><summary>Feed health and advanced actions</summary>
    <h3>Feed Validation</h3>
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
    </details>
  </section>

  <section id=\"epubs\" class=\"card\" style=\"display: {epubs_display};\">
    <h2>Generate New EPUB</h2>
    <form id=\"generateForm\">
      <label for=\"days_back\">Days back:</label>
      <input id=\"days_back\" type=\"number\" name=\"days_back\" min=\"1\" step=\"1\" value=\"{html.escape(form_data.get('days_back', '3'))}\" required />
      <label for=\"editionPreset\">Edition size</label>
      <select id=\"editionPreset\"><option value=\"quick\">Quick read · up to 6 articles / 9,000 words</option><option value=\"commute\">Commute · up to 15 / 25,000 words</option><option value=\"full\" selected>Full edition · up to 40 / 75,000 words</option></select>
      <label style=\"display:block;margin-top:.6rem;\"><input id=\"includePrevious\" type=\"checkbox\" /> Include previously collected articles</label>
      <label style=\"display:block;margin-top:.6rem;\">
        <input id=\"backfillToLastGenerated\" type=\"checkbox\" />
        Auto-calculate days back from the most recently generated EPUB
      </label>
      <button type=\"submit\">Generate new EPUB</button>
    </form>
    <div id=\"generatedEdition\" class=\"edition-actions\"></div>
  </section>

  <section id=\"files\" class=\"card\" style=\"display: {files_display};\">
    <h2>File Management</h2>
    <button id=\"refreshFilesBtn\">Refresh file list</button>
    <button id=\"deleteUploadedBtn\">Move delivered local copies to trash</button>
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
    <p id=\"message\" role=\"status\" aria-live=\"polite\"></p>
    <details><summary>Details</summary><pre id=\"jobLog\">No active jobs.</pre></details>
  </section>

  <section id=\"feeds\" class=\"card\" style=\"display: {feeds_display};\">
    <h2>Feed Manager</h2>
    <h3>Add Feed</h3>
    <form method=\"post\" action=\"/feeds\">
      <label>Feed name (optional)<input name=\"name\" value=\"{html.escape(form_data.get('name', ''))}\" /></label>
      <label>Website or feed URL<input name=\"url\" type=\"url\" value=\"{html.escape(form_data.get('url', ''))}\" required /></label>
      <label>Category (optional)<input name=\"category\" value=\"{html.escape(form_data.get('category', ''))}\" /></label>
      <button type=\"submit\">Add Feed</button>
    </form>

    <h3>Current Feeds</h3>
    {_render_feed_groups()}
  </section>

  <section id=\"settings\" class=\"card\" style=\"display: {settings_display};\">
    <h2>Device and schedule</h2>
    <form id=\"settingsForm\">
      <label for=\"deviceAddress\">Device address</label><input id=\"deviceAddress\" name=\"device_address\" required />
      <button type=\"button\" id=\"testConnectionBtn\">Test connection</button><br/>
      <label for=\"generationTime\">Morning generation time</label><input id=\"generationTime\" name=\"generation_time\" type=\"time\" required />
      <label for=\"deliveryEnd\">Delivery retry window ends</label><input id=\"deliveryEnd\" name=\"delivery_window_end\" type=\"time\" required />
      <label for=\"timezone\">Timezone</label><input id=\"timezone\" name=\"timezone\" required />
      <button type=\"submit\">Save settings</button>
    </form>
    <p id=\"settingsOverrides\"></p>
    <p>Connection success checks only network reachability. It does not upload a file or verify delivery. If unreachable, confirm the address, Wi-Fi network, and the reader's transfer mode.</p>
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
    const prepareSendButton = document.getElementById('prepareSendBtn');
    const readiness = document.getElementById('readiness');
    const latestActions = document.getElementById('latestActions');
    const generatedEdition = document.getElementById('generatedEdition');
    const settingsForm = document.getElementById('settingsForm');
    const testConnectionBtn = document.getElementById('testConnectionBtn');
    let activeJobId = null;

    function appendCell(row, label, value, className = '') {{
      const cell = document.createElement('td');
      cell.dataset.label = label;
      cell.textContent = value == null ? '' : String(value);
      if (className) cell.className = className;
      row.appendChild(cell);
      return cell;
    }}

    function renderRows(results) {{
      if (!tableBody) return;
      tableBody.innerHTML = '';
      for (const row of results) {{
        const tr = document.createElement('tr');
        const statusText = row.status === 'ok' ? 'ok' : `error: ${{row.reason || 'unknown'}}`;
        appendCell(tr, 'Feed', row.feed);
        appendCell(tr, '1 day', row['1 day']); appendCell(tr, '7 days', row['7 days']);
        appendCell(tr, '30 days', row['30 days']); appendCell(tr, 'Status', statusText, row.status === 'ok' ? 'ok' : 'error');
        tableBody.appendChild(tr);
      }}
    }}

    function setButtonsDisabled(disabled) {{
      if (validateButton) validateButton.disabled = disabled;
      if (uploadButton) uploadButton.disabled = disabled;
      if (sendLatestButton) sendLatestButton.disabled = disabled;
      if (refreshFilesBtn) refreshFilesBtn.disabled = disabled;
      if (deleteUploadedBtn) deleteUploadedBtn.disabled = disabled;
      if (prepareSendButton) prepareSendButton.disabled = disabled;
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
        const cell = appendCell(tr, '', 'No local editions yet. Prepare new reading to create one.'); cell.colSpan = 6;
        filesBody.appendChild(tr);
        return;
      }}

      for (const file of files) {{
        const tr = document.createElement('tr');
        const uploadedLabel = file.uploaded ? `Yes${{file.uploaded_at ? ` (${{file.uploaded_at}})` : ''}}` : 'No';
        appendCell(tr, 'File', file.name); appendCell(tr, 'Size', formatBytes(file.size_bytes));
        appendCell(tr, 'Uploaded', uploadedLabel, file.uploaded ? 'ok' : '');
        appendCell(tr, 'Attempts', file.attempt_count || 0); appendCell(tr, 'Error', file.error || '');
        const actions = appendCell(tr, 'Actions', '');
        for (const [action, label] of [['download', 'Download'], ['send', 'Send'], ['delete', 'Delete local copy']]) {{
          const button = document.createElement('button'); button.type = 'button'; button.dataset.action = action;
          button.dataset.name = file.name; button.textContent = label; actions.appendChild(button);
        }}
        filesBody.appendChild(tr);
      }}
    }}

    async function loadFiles() {{
      const response = await fetch('/api/files');
      const payload = await response.json();
      renderFilesTable(payload.files || []);
      return payload;
    }}

    function renderEditionActions(container, edition) {{
      if (!container) return;
      container.replaceChildren();
      if (!edition || !edition.edition_id) return;
      const send = document.createElement('button'); send.type = 'button'; send.textContent = 'Send this edition';
      send.addEventListener('click', async () => {{
        message.textContent = `Sending ${{edition.filename || 'this edition'}}…`;
        const response = await fetch('/api/editions/send', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{edition_id:edition.edition_id}})}});
        const accepted = await response.json();
        if (!response.ok) {{ message.textContent = accepted.reason || 'Could not start delivery.'; return; }}
        await pollJob(accepted.job_id, (delivery) => {{ message.textContent = delivery.status === 'queued' ? delivery.reason : `Upload accepted for ${{delivery.filename}}. Check the reader library to confirm it appears.`; }});
      }});
      const download = document.createElement('a'); download.href = `/api/editions/download?edition_id=${{encodeURIComponent(edition.edition_id)}}`; download.textContent = 'Download'; download.style.padding = '.65rem';
      container.append(send, download);
    }}

    async function loadDashboard() {{
      if (!readiness) return;
      const response = await fetch('/api/dashboard'); const payload = await response.json(); readiness.replaceChildren();
      const latest = payload.latest_edition;
      const metrics = latest ? [['Latest edition', latest.created_at || 'Unknown time'], ['Reading', `${{latest.article_count || 0}} articles · ${{latest.estimated_reading_minutes || 0}} min`], ['Delivery', latest.delivery_status || 'pending'], ['Pending', String((payload.pending_editions || []).length)]] : [['Latest edition', 'None yet'], ['Next action', 'Prepare new reading']];
      for (const [label, value] of metrics) {{ const item=document.createElement('div'); item.className='metric'; item.append(document.createTextNode(label)); const strong=document.createElement('strong'); strong.textContent=value; item.append(strong); readiness.append(item); }}
      renderEditionActions(latestActions, latest);
      const active = (payload.active_jobs || [])[0]; if (active) pollJob(active.job_id, () => loadDashboard());
    }}

    async function loadSettings() {{
      if (!settingsForm) return;
      const response = await fetch('/api/settings'); const values = await response.json();
      settingsForm.elements.device_address.value = values.device_address || '';
      settingsForm.elements.generation_time.value = values.generation_time || '06:00';
      settingsForm.elements.delivery_window_end.value = values.delivery_window_end || '07:30';
      settingsForm.elements.timezone.value = values.timezone || '';
      const overrides = Object.entries(values.overridden_by_environment || {{}}).map(([key, env]) => `${{key}} is overridden by ${{env}}`);
      document.getElementById('settingsOverrides').textContent = overrides.length ? overrides.join('. ') : 'No environment-variable overrides are active.';
    }}

    async function pollJob(jobId, onSuccess) {{
      activeJobId = jobId;
      sessionStorage.setItem('activeJobId', jobId);
      setButtonsDisabled(true);
      let pollErrors = 0;
      while (activeJobId === jobId) {{
        let response, payload;
        try {{ response = await fetch(`/api/jobs/${{jobId}}`); payload = await response.json(); }}
        catch (error) {{
          pollErrors += 1; message.textContent = `Connection lost while checking progress (attempt ${{pollErrors}}). Retrying…`;
          if (pollErrors >= 5) {{ setButtonsDisabled(false); sessionStorage.removeItem('activeJobId'); activeJobId=null; message.textContent='Progress could not be recovered. The operation may still be running; refresh to check.'; return; }}
          await new Promise((resolve) => setTimeout(resolve, 1500)); continue;
        }}
        if (!response.ok) {{ setButtonsDisabled(false); sessionStorage.removeItem('activeJobId'); activeJobId=null; message.textContent = payload.reason || 'This job expired or no longer exists.'; return; }}
        jobLog.textContent = (payload.logs || []).join('\\n');
        message.textContent = payload.stage === 'starting' ? 'Starting…' : `Working: ${{payload.stage || payload.action || 'in progress'}}`;
        if (['succeeded', 'failed', 'interrupted'].includes(payload.status)) {{
          const result = payload.result || {{}};
          if (Array.isArray(result)) renderRows(result);
          if (payload.status === 'succeeded') {{
            if (onSuccess) onSuccess(result);
          }} else {{
            const reason = result.reason || result.output || 'Unknown error';
            message.textContent = `Failed: ${{reason}}`;
          }}
          setButtonsDisabled(false);
          sessionStorage.removeItem('activeJobId');
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
        const includePrevious = !!document.getElementById('includePrevious')?.checked;
        const preset = document.getElementById('editionPreset')?.value || 'full';
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
              include_previous: includePrevious,
              preset,
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
            if (payload.status === 'created' || payload.status === 'created_with_warnings') {{
              message.textContent = `Edition ready: ${{payload.article_count}} article(s), about ${{payload.estimated_reading_minutes}} minutes.${{payload.warnings?.length ? ` ${{payload.warnings.length}} warning(s).` : ''}}`;
              renderEditionActions(generatedEdition, payload);
            }} else {{
              message.textContent = payload.reason || 'No edition was created.';
              if (generatedEdition) generatedEdition.replaceChildren();
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

    if (prepareSendButton) {{
      prepareSendButton.addEventListener('click', async () => {{
        message.textContent = 'Fetching feeds…';
        try {{
          const response = await fetch('/api/prepare-and-send', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{days_back:3}})}});
          const accepted = await response.json();
          if (!response.ok) {{ message.textContent = accepted.reason || 'Could not start.'; return; }}
          await pollJob(accepted.job_id, (payload) => {{
            const edition = payload.generation;
            if (edition?.edition_id) renderEditionActions(latestActions, edition);
            message.textContent = payload.status === 'queued' ? 'Edition prepared. Reader is offline, so delivery is queued without rebuilding.' : `Edition prepared and upload accepted: ${{edition?.article_count || 0}} articles.`;
            loadDashboard();
          }});
        }} catch (error) {{ message.textContent = `Prepare failed: ${{error.message}}`; }}
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
          const previewResponse = await fetch('/api/files/cleanup-preview'); const preview = await previewResponse.json();
          if (!(preview.files || []).length) {{ message.textContent = `Nothing eligible for cleanup. Newest local copy preserved: ${{preview.preserved_newest || 'none'}}.`; return; }}
          if (!window.confirm(`Move these local copies to recoverable trash?\\n\\n${{preview.files.join('\\n')}}\\n\\nNewest preserved: ${{preview.preserved_newest}}`)) return;
          const response = await fetch('/api/files/delete-uploaded', {{ method: 'POST' }});
          const payload = await response.json();
          await loadFiles();
          message.textContent = `Moved ${{payload.deleted_count || 0}} delivered local copy/copies to trash.${{payload.failed_count ? ` ${{payload.failed_count}} failed.` : ''}}`;
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
    if (window.location.pathname === '/dashboard') {{ loadDashboard().catch((error) => {{ message.textContent = `Could not load reading status: ${{error.message}}`; }}); }}
    if (settingsForm) {{
      loadSettings().catch((error) => {{ message.textContent = `Could not load settings: ${{error.message}}`; }});
      settingsForm.addEventListener('submit', async (event) => {{ event.preventDefault(); const values=Object.fromEntries(new FormData(settingsForm)); const response=await fetch('/api/settings', {{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(values)}}); const payload=await response.json(); message.textContent=response.ok?'Settings saved.':(payload.reason||'Settings were not saved.'); if(response.ok) loadSettings(); }});
    }}
    if (testConnectionBtn) {{ testConnectionBtn.addEventListener('click', async () => {{ message.textContent='Testing network connection…'; const response=await fetch('/api/device/test',{{method:'POST'}}); const payload=await response.json(); message.textContent=payload.reachable?`Network connection to ${{payload.host}} succeeded. No file was uploaded.`:`Could not reach ${{payload.host}}. Check the address, network, and transfer mode.`; }}); }}
    for (const form of document.querySelectorAll('form[action="/feeds/delete"]')) {{ form.addEventListener('submit', (event) => {{ if (!window.confirm('Remove this subscription? You can add it again later.')) event.preventDefault(); }}); }}
    const rememberedJob = sessionStorage.getItem('activeJobId');
    if (rememberedJob && !activeJobId) pollJob(rememberedJob, () => {{ loadDashboard(); }});
  </script>
</main></body>
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

        if parsed.path == "/api/dashboard":
            history = HistoryStore(HISTORY_FILE).load()
            editions = sorted(history["editions"].values(), key=lambda item: item.get("created_at", ""), reverse=True)
            with JOBS_LOCK:
                active_jobs = [dict(job) for job in JOBS.values() if job.get("status") == "running"]
            latest = editions[0] if editions else None
            pending = [item for item in editions if item.get("delivery_status") in {"pending", "queued", "failed"}]
            effective = settings_store.effective()
            payload = {"status": "ok", "latest_edition": latest, "pending_editions": pending, "active_jobs": active_jobs, "device_check": LAST_DEVICE_CHECK, "effective_schedule": {"generation_time": effective["generation_time"], "delivery_window_end": effective["delivery_window_end"], "timezone": effective["timezone"]}}
            self._send_bytes(json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")
            return

        if parsed.path == "/api/settings":
            self._send_bytes(json.dumps(settings_store.effective()).encode("utf-8"), "application/json; charset=utf-8")
            return

        if parsed.path == "/api/preflight":
            self._send_bytes(json.dumps(build_preflight_payload()).encode("utf-8"), "application/json; charset=utf-8")
            return

        if parsed.path == "/api/editions/download":
            edition_id = urllib.parse.parse_qs(parsed.query).get("edition_id", [""])[0]
            edition = HistoryStore(HISTORY_FILE).load()["editions"].get(edition_id)
            target = Path((edition or {}).get("sync_file") or (edition or {}).get("output_file") or "")
            if not edition or not target.exists() or not target.is_file():
                self._send_bytes(json.dumps({"status": "error", "reason": "edition file not found"}).encode("utf-8"), "application/json; charset=utf-8", HTTPStatus.NOT_FOUND)
                return
            self._send_file(target, target.name)
            return

        if parsed.path == "/api/files":
            payload, status = build_file_manager_payload()
            self._send_bytes(
                json.dumps(payload).encode("utf-8"),
                "application/json; charset=utf-8",
                status,
            )
            return

        if parsed.path == "/api/files/cleanup-preview":
            payload, status = build_cleanup_preview_payload()
            self._send_bytes(json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8", status)
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

        if parsed.path not in {"/dashboard", "/epubs", "/feeds", "/files", "/settings"}:
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
        if self.path == "/api/settings":
            try:
                payload = settings_store.update(self._read_json_data())
            except ValueError as exc:
                self._send_bytes(json.dumps({"status": "error", "reason": str(exc)}).encode("utf-8"), "application/json; charset=utf-8", HTTPStatus.BAD_REQUEST)
                return
            self._send_bytes(json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")
            return

        if self.path == "/api/device/test":
            global LAST_DEVICE_CHECK
            effective = settings_store.effective(); host = effective["device_address"]
            reachable = host_reachable(host, UPLOAD_REACHABILITY_METHOD, UPLOAD_TCP_PORT, UPLOAD_CONNECT_TIMEOUT)
            LAST_DEVICE_CHECK = {"status": "reachable" if reachable else "unreachable", "reachable": reachable, "host": host, "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "claim": "Network reachability only; no upload attempted."}
            self._send_bytes(json.dumps(LAST_DEVICE_CHECK).encode("utf-8"), "application/json; charset=utf-8", HTTPStatus.OK if reachable else HTTPStatus.SERVICE_UNAVAILABLE)
            return
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

        if self.path == "/api/editions/send":
            payload = self._read_json_data()
            edition_id = str(payload.get("edition_id") or "")
            if not edition_id:
                self._send_bytes(json.dumps({"status": "error", "reason": "edition_id is required"}).encode("utf-8"), "application/json; charset=utf-8", HTTPStatus.BAD_REQUEST)
                return
            job_id = _start_background_job(f"send-edition:{edition_id}", lambda: build_upload_edition_payload(edition_id))
            self._send_bytes(json.dumps({"status": "accepted", "job_id": job_id, "edition_id": edition_id}).encode("utf-8"), "application/json; charset=utf-8", HTTPStatus.ACCEPTED)
            return

        if self.path == "/api/prepare-and-send":
            payload = self._read_json_data()
            try:
                days_back = int(payload.get("days_back", 3))
            except (TypeError, ValueError):
                self._send_bytes(json.dumps({"status": "error", "reason": "days_back must be an integer"}).encode("utf-8"), "application/json; charset=utf-8", HTTPStatus.BAD_REQUEST)
                return

            def _prepare_and_send_job():
                _set_job_stage("prepare-and-send", "fetching_and_extracting", "Fetching feeds and extracting new articles")
                generated, status = build_generate_epub_payload(days_back)
                if status != HTTPStatus.OK or generated.get("status") not in {"created", "created_with_warnings"}:
                    return generated, status
                _set_job_stage("prepare-and-send", "sending", f"Sending edition {generated['edition_id']}")
                delivered, delivery_status = build_upload_edition_payload(generated["edition_id"])
                return {"status": "complete" if delivery_status < 400 else "queued", "generation": generated, "delivery": delivered, "edition_id": generated["edition_id"]}, (HTTPStatus.OK if delivery_status in {HTTPStatus.OK, HTTPStatus.SERVICE_UNAVAILABLE} else delivery_status)

            job_id = _start_background_job("prepare-and-send", _prepare_and_send_job)
            self._send_bytes(json.dumps({"status": "accepted", "job_id": job_id}).encode("utf-8"), "application/json; charset=utf-8", HTTPStatus.ACCEPTED)
            return

        if self.path == "/api/epubs/generate":
            payload = self._read_json_data()
            include_previous = bool(payload.get("include_previous"))
            preset = str(payload.get("preset") or "full")
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
                _set_job_stage("generate-epub", "fetching_and_building", "Fetching feeds, extracting articles, and building EPUB")
                if include_previous or preset != "full":
                    result_payload, status = build_generate_epub_payload(days_back, include_previous=include_previous, preset=preset)
                else:
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

        if self.path == "/feeds/pause":
            form = self._read_form_data(); feed_id = form.get("feed_id", ""); paused = form.get("paused", "false").lower() == "true"
            try:
                feed = store.update_feed(feed_id, paused=paused)
            except (ValidationError, FileNotFoundError, ET.ParseError) as exc:
                self._redirect(f"/feeds?{urllib.parse.urlencode({'error': str(exc)})}")
                return
            verb = "paused" if paused else "resumed"
            self._redirect(f"/feeds?{urllib.parse.urlencode({'message': f'{feed.name} {verb}.'})}")
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
