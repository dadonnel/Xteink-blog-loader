import json

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
