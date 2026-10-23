import datetime as dt
import json
from http import HTTPStatus

import app
import morning_sync
from rss_epub.feed_service import FeedService
from rss_epub.history import HistoryStore


def test_reader_shell_uses_new_navigation_and_inline_progress(monkeypatch):
    monkeypatch.setattr(app, "_render_feed_groups", lambda: "<p>No sources.</p>")
    rendered = app.render_reader_html(active_page="today")

    assert "Xteink Reader" in rendered
    assert 'aria-current="page" href="/today"' in rendered
    assert ">Today<" in rendered
    assert ">Editions<" in rendered
    assert ">Sources<" in rendered
    assert "Progress &amp; Error Feedback" not in rendered
    assert 'id="latestEdition"' in rendered


def test_legacy_dashboard_redirects_to_today():
    handler = app.Handler.__new__(app.Handler)
    handler.path = "/dashboard"
    redirects = []
    handler._redirect = redirects.append

    app.Handler.do_GET(handler)

    assert redirects == ["/today"]


def test_prepare_and_send_no_content_never_calls_upload(monkeypatch):
    handler = app.Handler.__new__(app.Handler)
    handler.path = "/api/prepare-and-send"
    handler._read_json_data = lambda: {"days_back": 3, "preset": "quick", "include_previous": True}
    captured = {}

    def start(_action, worker):
        captured["result"], captured["status"] = worker()
        return "job-no-content"

    monkeypatch.setattr(app, "_start_background_job", start)
    monkeypatch.setattr(app, "_set_job_stage", lambda *args: None)
    monkeypatch.setattr(
        app,
        "build_generate_epub_payload",
        lambda days, **options: ({"status": "no_new_articles", "reason": "You are caught up", "options": options}, HTTPStatus.OK),
    )
    monkeypatch.setattr(app, "build_upload_edition_payload", lambda *_: (_ for _ in ()).throw(AssertionError("upload must not run")))
    response = {}
    handler._send_bytes = lambda payload, _content_type, status=200: response.update(payload=json.loads(payload), status=status)

    app.Handler.do_POST(handler)

    assert response["status"] == HTTPStatus.ACCEPTED
    assert captured["result"]["status"] == "no_new_articles"
    assert captured["result"]["options"] == {"include_previous": True, "preset": "quick"}


def test_json_feed_date_filter_uses_parsed_dictionary_date(monkeypatch):
    class FixedDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 23, tzinfo=tz)

    monkeypatch.setattr("rss_epub.feed_service.datetime.datetime", FixedDateTime)
    service = FeedService(days_back=3)
    old = {"published_parsed": dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc).utctimetuple()}
    recent = {"published_parsed": dt.datetime(2026, 10, 22, tzinfo=dt.timezone.utc).utctimetuple()}

    assert service.is_recent(old)[0] is False
    assert service.is_recent(recent)[0] is True


def test_saved_device_address_is_used_for_manual_upload(monkeypatch, tmp_path):
    sync = tmp_path / "sync"; sync.mkdir(); (sync / "one.epub").write_bytes(b"book")
    monkeypatch.setattr(app, "UPLOAD_SYNC_DIR", sync)
    monkeypatch.setattr(app, "UPLOAD_STATE_FILE", tmp_path / "upload.json")
    monkeypatch.setattr(app.settings_store, "effective", lambda: {"device_address": "reader.local"})
    checked = []
    monkeypatch.setattr(app, "host_reachable", lambda host, *args: (checked.append(host), False)[1])

    payload, status = app.build_manual_upload_payload()

    assert status == HTTPStatus.SERVICE_UNAVAILABLE
    assert checked == ["reader.local"]
    assert payload["host"] == "reader.local"


def test_scheduler_upload_updates_matching_edition_history(monkeypatch, tmp_path):
    sync = tmp_path / "sync"; sync.mkdir(); book = sync / "edition.epub"; book.write_bytes(b"book")
    history_path = tmp_path / "history.json"
    HistoryStore(history_path).save({"version": 1, "articles": {}, "editions": {"ed-1": {"edition_id": "ed-1", "sync_file": str(book.resolve()), "delivery_status": "queued"}}})
    monkeypatch.setattr(morning_sync, "HISTORY_FILE", history_path)
    monkeypatch.setattr(morning_sync, "upload_file", lambda *args: (True, "accepted"))
    state = morning_sync.UploadState(tmp_path / "upload.json")

    morning_sync.try_upload_pending(state, sync, "reader.local", "/upload", "file")

    edition = HistoryStore(history_path).load()["editions"]["ed-1"]
    assert edition["delivery_status"] == "uploaded"
    assert edition["delivered_at"]


def test_outcome_copy_keeps_offline_and_no_content_truthful():
    script = (app.BASE_DIR / "static" / "app.js").read_text(encoding="utf-8")
    assert "You’re caught up" in script
    assert "no edition was created" in script
    assert "Edition ready; reader offline" in script
    assert "retained for retry" in script
    assert "The prepared edition was accepted by the reader" in script
