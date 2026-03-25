import json
import urllib.parse
import xml.etree.ElementTree as ET
from http import HTTPStatus

import app
from feed_discovery import FeedResolution


def test_do_post_upload_epubs_alias(monkeypatch):
    handler = app.Handler.__new__(app.Handler)
    handler.path = "/upload-epubs"

    monkeypatch.setattr(
        app,
        "build_manual_upload_payload",
        lambda: ({"status": "ok", "uploaded_now": 1, "pending_after": 0}, 200),
    )

    captured = {}

    def fake_send_bytes(payload: bytes, content_type: str, status: int = 200):
        captured["payload"] = json.loads(payload.decode("utf-8"))
        captured["content_type"] = content_type
        captured["status"] = status

    handler._send_bytes = fake_send_bytes

    app.Handler.do_POST(handler)

    assert captured["status"] == 200
    assert captured["content_type"].startswith("application/json")
    assert captured["payload"]["status"] == "ok"


def test_render_feed_groups_handles_missing_sources_file(monkeypatch):
    monkeypatch.setattr(
        app.store,
        "parse_feeds",
        lambda: (_ for _ in ()).throw(FileNotFoundError("missing opml")),
    )

    assert app._render_feed_groups() == "<p>No feeds configured yet.</p>"


def test_do_post_feeds_handles_missing_sources_file(monkeypatch):
    handler = app.Handler.__new__(app.Handler)
    handler.path = "/feeds"
    handler._read_form_data = lambda: {
        "name": "Example",
        "url": "https://example.com/post/one",
        "category": "Tech",
    }

    monkeypatch.setattr(
        app,
        "resolve_feed",
        lambda *args, **kwargs: FeedResolution(
            name="Example",
            feed_url="https://example.com/feed.xml",
            category="Tech",
            feed_type="rss",
        ),
    )
    monkeypatch.setattr(
        app.store,
        "append_feed",
        lambda **_: (_ for _ in ()).throw(FileNotFoundError("missing opml")),
    )

    redirects = []
    handler._redirect = lambda location: redirects.append(location)

    app.Handler.do_POST(handler)

    assert len(redirects) == 1
    parsed = urllib.parse.urlparse(redirects[0])
    query = urllib.parse.parse_qs(parsed.query)
    assert query["error"] == ["missing opml"]


def test_do_post_feeds_handles_malformed_sources_file(monkeypatch):
    handler = app.Handler.__new__(app.Handler)
    handler.path = "/feeds"
    handler._read_form_data = lambda: {
        "name": "Example",
        "url": "https://example.com/post/one",
        "category": "Tech",
    }

    monkeypatch.setattr(
        app,
        "resolve_feed",
        lambda *args, **kwargs: FeedResolution(
            name="Example",
            feed_url="https://example.com/feed.xml",
            category="Tech",
            feed_type="rss",
        ),
    )
    monkeypatch.setattr(
        app.store,
        "append_feed",
        lambda **_: (_ for _ in ()).throw(ET.ParseError("malformed opml")),
    )

    redirects = []
    handler._redirect = lambda location: redirects.append(location)

    app.Handler.do_POST(handler)

    assert len(redirects) == 1
    parsed = urllib.parse.urlparse(redirects[0])
    query = urllib.parse.parse_qs(parsed.query)
    assert query["error"] == ["malformed opml"]


def test_do_post_feeds_autodiscovers_and_defaults_name_category(monkeypatch):
    handler = app.Handler.__new__(app.Handler)
    handler.path = "/feeds"
    handler._read_form_data = lambda: {
        "name": "",
        "url": "https://example.com/posts/one",
        "category": "",
    }

    monkeypatch.setattr(
        app.store,
        "parse_feeds",
        lambda: {"Engineering & Code": []},
    )

    monkeypatch.setattr(
        app,
        "resolve_feed",
        lambda *args, **kwargs: FeedResolution(
            name="Example Engineering",
            feed_url="https://example.com/feed.xml",
            category="Engineering & Code",
            feed_type="atom",
        ),
    )

    captured = {}

    def fake_append_feed(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(app.store, "append_feed", fake_append_feed)

    redirects = []
    handler._redirect = lambda location: redirects.append(location)

    app.Handler.do_POST(handler)

    assert captured == {
        "name": "Example Engineering",
        "url": "https://example.com/feed.xml",
        "category": "Engineering & Code",
        "feed_type": "atom",
    }
    parsed = urllib.parse.urlparse(redirects[0])
    query = urllib.parse.parse_qs(parsed.query)
    assert query["message"] == ["Feed added: Example Engineering (Engineering & Code)."]


def test_build_generate_epub_payload_uses_script_output_for_reason(monkeypatch):
    class FakeResult:
        returncode = 1
        stdout = "trace line 1\ntrace line 2"
        stderr = "fatal error"

    monkeypatch.setattr(app.subprocess, "run", lambda *args, **kwargs: FakeResult())

    payload, status = app.build_generate_epub_payload(3)

    assert status == HTTPStatus.INTERNAL_SERVER_ERROR
    assert payload["status"] == "error"
    assert payload["reason"] == "fatal error"


def test_do_post_api_send_latest_returns_job_id(monkeypatch):
    handler = app.Handler.__new__(app.Handler)
    handler.path = "/api/epubs/send-latest"

    captured = {}

    monkeypatch.setattr(app, "_start_background_job", lambda *_: "job-123")

    def fake_send_bytes(payload: bytes, content_type: str, status: int = 200):
        captured["payload"] = json.loads(payload.decode("utf-8"))
        captured["status"] = status

    handler._send_bytes = fake_send_bytes

    app.Handler.do_POST(handler)

    assert captured["status"] == HTTPStatus.ACCEPTED
    assert captured["payload"]["job_id"] == "job-123"


def test_do_post_api_generate_rejects_non_int_days_back():
    handler = app.Handler.__new__(app.Handler)
    handler.path = "/api/epubs/generate"
    handler._read_json_data = lambda: {"days_back": "bad"}

    captured = {}

    def fake_send_bytes(payload: bytes, content_type: str, status: int = 200):
        captured["payload"] = json.loads(payload.decode("utf-8"))
        captured["status"] = status

    handler._send_bytes = fake_send_bytes

    app.Handler.do_POST(handler)

    assert captured["status"] == HTTPStatus.BAD_REQUEST
    assert captured["payload"]["status"] == "error"
