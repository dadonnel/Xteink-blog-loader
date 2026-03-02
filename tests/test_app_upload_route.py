from pathlib import Path

import app


def test_build_manual_upload_payload_returns_unreachable(monkeypatch, tmp_path):
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    (sync_dir / "one.epub").write_text("epub", encoding="utf-8")

    monkeypatch.setattr(app, "UPLOAD_SYNC_DIR", sync_dir)
    monkeypatch.setattr(app, "UPLOAD_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(app, "host_reachable", lambda *args: False)

    payload, status = app.build_manual_upload_payload()

    assert status == 503
    assert payload["status"] == "unreachable"
    assert payload["pending_before"] == 1
    assert payload["uploaded_now"] == 0


def test_build_manual_upload_payload_uploads_when_reachable(monkeypatch, tmp_path):
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    (sync_dir / "one.epub").write_text("epub", encoding="utf-8")

    monkeypatch.setattr(app, "UPLOAD_SYNC_DIR", sync_dir)
    monkeypatch.setattr(app, "UPLOAD_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(app, "host_reachable", lambda *args: True)

    def fake_try_upload_pending(state, sync_dir: Path, host: str, cmd_template: str):
        for record in state.records.values():
            record["uploaded_successfully"] = True

    monkeypatch.setattr(app, "try_upload_pending", fake_try_upload_pending)

    payload, status = app.build_manual_upload_payload()

    assert status == 200
    assert payload["status"] == "ok"
    assert payload["pending_before"] == 1
    assert payload["pending_after"] == 0
    assert payload["uploaded_now"] == 1


def test_build_manual_upload_payload_reports_partial_when_upload_fails(monkeypatch, tmp_path):
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    file_path = sync_dir / "one.epub"
    file_path.write_text("epub", encoding="utf-8")

    monkeypatch.setattr(app, "UPLOAD_SYNC_DIR", sync_dir)
    monkeypatch.setattr(app, "UPLOAD_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(app, "host_reachable", lambda *args: True)

    def fake_try_upload_pending(state, sync_dir: Path, host: str, cmd_template: str):
        for record in state.records.values():
            record["uploaded_successfully"] = False
            record["error"] = "permission denied"

    monkeypatch.setattr(app, "try_upload_pending", fake_try_upload_pending)

    payload, status = app.build_manual_upload_payload()

    assert status == 200
    assert payload["status"] == "partial"
    assert payload["pending_before"] == 1
    assert payload["pending_after"] == 1
    assert payload["uploaded_now"] == 0
    assert payload["failed_now"] == 1
    assert payload["failed_items"][0]["filepath"].endswith("one.epub")
    assert payload["failed_items"][0]["error"] == "permission denied"
