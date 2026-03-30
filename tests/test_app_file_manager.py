import json
from http import HTTPStatus

import app


def test_build_file_manager_payload_marks_uploaded(monkeypatch, tmp_path):
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    epub_file = sync_dir / "one.epub"
    epub_file.write_text("epub", encoding="utf-8")

    monkeypatch.setattr(app, "UPLOAD_SYNC_DIR", sync_dir)
    monkeypatch.setattr(app, "UPLOAD_STATE_FILE", tmp_path / "state.json")

    state = app.UploadState(tmp_path / "state.json")
    pending = app.ensure_records_for_files(state, [epub_file])
    state.records[pending[0][0]]["uploaded_successfully"] = True
    state.records[pending[0][0]]["uploaded_at"] = "2026-01-01T00:00:00Z"
    state.save()

    payload, status = app.build_file_manager_payload()

    assert status == HTTPStatus.OK
    assert payload["total_files"] == 1
    assert payload["uploaded_files"] == 1
    assert payload["files"][0]["name"] == "one.epub"
    assert payload["files"][0]["uploaded"] is True


def test_build_delete_uploaded_epubs_payload_deletes_only_uploaded(monkeypatch, tmp_path):
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    uploaded = sync_dir / "uploaded.epub"
    pending = sync_dir / "pending.epub"
    uploaded.write_text("epub", encoding="utf-8")
    pending.write_text("epub", encoding="utf-8")

    monkeypatch.setattr(app, "UPLOAD_SYNC_DIR", sync_dir)
    monkeypatch.setattr(app, "UPLOAD_STATE_FILE", tmp_path / "state.json")

    state = app.UploadState(tmp_path / "state.json")
    pending_records = app.ensure_records_for_files(state, [uploaded, pending])
    state.records[pending_records[0][0]]["uploaded_successfully"] = True
    state.save()

    payload, status = app.build_delete_uploaded_epubs_payload()

    assert status == HTTPStatus.OK
    assert payload["deleted_count"] == 1
    assert payload["deleted_files"] == ["uploaded.epub"]
    assert not uploaded.exists()
    assert pending.exists()


def test_build_delete_file_payload_rejects_path_traversal(monkeypatch, tmp_path):
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()

    monkeypatch.setattr(app, "UPLOAD_SYNC_DIR", sync_dir)

    payload, status = app.build_delete_file_payload("../hack.epub")

    assert status == HTTPStatus.BAD_REQUEST
    assert payload["status"] == "error"


def test_build_upload_file_payload_updates_state(monkeypatch, tmp_path):
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    target = sync_dir / "one.epub"
    target.write_text("epub", encoding="utf-8")

    monkeypatch.setattr(app, "UPLOAD_SYNC_DIR", sync_dir)
    monkeypatch.setattr(app, "UPLOAD_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(app, "host_reachable", lambda *args: True)
    monkeypatch.setattr(app, "upload_file", lambda *args: (True, "ok"))

    payload, status = app.build_upload_file_payload("one.epub")

    assert status == HTTPStatus.OK
    assert payload["status"] == "ok"

    state = app.UploadState(tmp_path / "state.json")
    assert any(record.get("uploaded_successfully") for record in state.records.values())


def test_do_get_api_files_download_missing_returns_not_found(monkeypatch, tmp_path):
    handler = app.Handler.__new__(app.Handler)
    handler.path = "/api/files/download?filename=missing.epub"

    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    monkeypatch.setattr(app, "UPLOAD_SYNC_DIR", sync_dir)

    captured = {}

    def fake_send_bytes(payload: bytes, content_type: str, status: int = 200):
        captured["status"] = status
        captured["payload"] = json.loads(payload.decode("utf-8"))

    handler._send_bytes = fake_send_bytes

    app.Handler.do_GET(handler)

    assert captured["status"] == HTTPStatus.NOT_FOUND
    assert captured["payload"]["status"] == "error"


def test_do_post_api_files_delete_uploaded(monkeypatch):
    handler = app.Handler.__new__(app.Handler)
    handler.path = "/api/files/delete-uploaded"

    monkeypatch.setattr(
        app,
        "build_delete_uploaded_epubs_payload",
        lambda: ({"status": "ok", "deleted_count": 2}, HTTPStatus.OK),
    )

    captured = {}

    def fake_send_bytes(payload: bytes, content_type: str, status: int = 200):
        captured["status"] = status
        captured["payload"] = json.loads(payload.decode("utf-8"))

    handler._send_bytes = fake_send_bytes

    app.Handler.do_POST(handler)

    assert captured["status"] == HTTPStatus.OK
    assert captured["payload"]["deleted_count"] == 2
