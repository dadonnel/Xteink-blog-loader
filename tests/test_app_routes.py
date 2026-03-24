import json
import urllib.parse
import xml.etree.ElementTree as ET

import app


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
        "url": "https://example.com/feed.xml",
        "category": "Tech",
    }

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
    assert parsed.fragment == "feeds"
    assert query["error"] == ["missing opml"]


def test_do_post_feeds_handles_malformed_sources_file(monkeypatch):
    handler = app.Handler.__new__(app.Handler)
    handler.path = "/feeds"
    handler._read_form_data = lambda: {
        "name": "Example",
        "url": "https://example.com/feed.xml",
        "category": "Tech",
    }

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
    assert parsed.fragment == "feeds"
    assert query["error"] == ["malformed opml"]
